#!/usr/bin/env python3
"""
Test d'intégration — Pipeline complet classify → copie → refine.

Teste le pipeline de bout en bout avec de vrais fichiers PDF dans
tests/fixtures/pdfs/ et un profil de test minimal. Seul l'appel
LLM Vision est mocké — tout le reste (classification, copie, refine)
tourne en réel.

Les PDFs de test doivent être déposés manuellement dans tests/fixtures/pdfs/.
Si le dossier est vide, les tests sont skippés automatiquement.
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

import yaml

from lib.logger import get_logger, setup_logger

setup_logger(verbose=False)

from commands.helpers import (
    check_inbox_safety,
    execute_classify,
    process_single_file,
)
from lib.classifier import classify_by_theme, load_keyword_classifier

log = get_logger()

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), 'fixtures')
FIXTURES_PDFS = os.path.join(FIXTURES_DIR, 'pdfs')
FIXTURES_PROFILE = os.path.join(FIXTURES_DIR, 'profile')


def _get_test_pdfs():
    # type: () -> list[str]
    """Retourne la liste des PDFs dans fixtures/pdfs/."""
    if not os.path.isdir(FIXTURES_PDFS):
        return []
    return [
        os.path.join(FIXTURES_PDFS, f)
        for f in sorted(os.listdir(FIXTURES_PDFS))
        if f.lower().endswith('.pdf')
    ]


def _load_fixture_yaml(filename):
    # type: (str) -> dict
    """Charge un fichier YAML depuis fixtures/profile/."""
    path = os.path.join(FIXTURES_PROFILE, filename)
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


class FakeProfile:
    """Profil de test construit à partir des fixtures YAML.

    Reproduit l'interface de lib.profile.Profile sans passer par
    le constructeur qui dépend de get_project_root().
    """

    def __init__(self, inbox: str, target: str) -> None:
        profile_data = _load_fixture_yaml('profile.yaml')
        tree_data = _load_fixture_yaml('tree.yaml')
        mapping_data = _load_fixture_yaml('theme_mapping.yaml')
        refinement_data = _load_fixture_yaml('refinement.yaml')

        self.name = profile_data.get('name', 'test')
        self.description = profile_data.get('description', '')
        self.inbox = inbox
        self.target = target
        self.fallback = profile_data.get('fallback', '_A-TRIER')

        llm = profile_data.get('llm', {})
        self.llm_provider = llm.get('provider', 'siliconflow')
        self.llm_model = llm.get('model', 'test-model')
        self.llm_endpoint = llm.get('endpoint', 'http://localhost/v1')

        self.defaults = profile_data.get('defaults', {})
        self.tree = tree_data.get('folders', [])
        self.theme_mapping = mapping_data if isinstance(mapping_data, dict) else {}
        self.refinement_rules = refinement_data.get('rules', [])
        self.profile_dir = FIXTURES_PROFILE


def _mock_vision_response(theme: str, title: str = '', author: str = '', confidence: float = 0.9) -> dict:
    """Construit une réponse LLM Vision simulée."""
    return {
        'title': title or 'Test Title',
        'author': author or 'Test Author',
        'theme': theme,
        'language': 'en',
        'confidence': confidence,
    }


@unittest.skipIf(not _get_test_pdfs(), "Pas de PDFs dans tests/fixtures/pdfs/")
class TestIntegrationClassify(unittest.TestCase):
    """Test d'intégration : classification par theme_mapping + keywords."""

    def setUp(self):
        """Crée l'arborescence temporaire (inbox + target)."""
        self.tmpdir = tempfile.mkdtemp(prefix='klodo-integ-')
        self.inbox = os.path.join(self.tmpdir, 'INBOX')
        self.target = os.path.join(self.tmpdir, 'BIBLIO')
        os.makedirs(self.inbox)
        os.makedirs(self.target)

        self.profile = FakeProfile(self.inbox, self.target)

        # Créer l'arborescence cible
        for folder in self.profile.tree:
            os.makedirs(os.path.join(self.target, folder), exist_ok=True)

        # Copier les PDFs de test dans l'inbox
        self.test_pdfs = []
        for pdf_path in _get_test_pdfs():
            dest = os.path.join(self.inbox, os.path.basename(pdf_path))
            shutil.copy2(pdf_path, dest)
            self.test_pdfs.append(dest)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_classify_by_theme_mapping(self):
        """Les fichiers sont classifiés via le theme_mapping."""
        theme_mapping = self.profile.theme_mapping

        # Vérifier que le mapping fonctionne sur des thèmes connus
        result = classify_by_theme('machine learning', theme_mapping)
        self.assertEqual(result, '02-INFORMATIQUE/IA-ML')

        result = classify_by_theme('physics', theme_mapping)
        self.assertEqual(result, '01-SCIENCES/PHYSIQUE')

        result = classify_by_theme('unknown garbage', theme_mapping)
        self.assertIsNone(result)

    @patch('commands.helpers.analyze_cover')
    def test_process_single_file_with_mock_vision(self, mock_vision):
        """process_single_file classe un PDF quand la vision retourne un thème connu."""
        if not self.test_pdfs:
            self.skipTest("Pas de PDFs de test")

        pdf_path = self.test_pdfs[0]

        # Simuler une réponse LLM Vision avec un thème reconnu
        mock_vision.return_value = _mock_vision_response(
            theme='machine learning',
            title='Introduction to ML',
            author='Test Author',
        )

        classifier = load_keyword_classifier(
            os.path.join(FIXTURES_PROFILE, 'categories.yaml'))

        result = process_single_file(
            pdf_path,
            api_key='fake-key',
            endpoint='http://fake/v1',
            model='fake-model',
            theme_mapping=self.profile.theme_mapping,
            classifier=classifier,
        )

        self.assertEqual(result['status'], 'classifié')
        self.assertEqual(result['destination'], '02-INFORMATIQUE/IA-ML')
        self.assertEqual(result['titre_detecte'], 'Introduction to ML')

    @patch('commands.helpers.analyze_cover')
    def test_process_single_file_unknown_theme(self, mock_vision):
        """Un thème inconnu sans fallback → non_classifié."""
        if not self.test_pdfs:
            self.skipTest("Pas de PDFs de test")

        pdf_path = self.test_pdfs[0]

        mock_vision.return_value = _mock_vision_response(
            theme='underwater basket weaving',
            title='Strange Book',
        )

        result = process_single_file(
            pdf_path,
            api_key='fake-key',
            endpoint='http://fake/v1',
            model='fake-model',
            theme_mapping=self.profile.theme_mapping,
        )

        self.assertEqual(result['status'], 'non_classifié')

    @patch('commands.helpers.analyze_cover')
    def test_full_classify_then_copy(self, mock_vision):
        """Pipeline complet : classify tous les PDFs puis copie vers target."""
        if not self.test_pdfs:
            self.skipTest("Pas de PDFs de test")

        # Simuler la vision pour chaque fichier
        mock_vision.return_value = _mock_vision_response(
            theme='mathematics',
            title='Algebra Book',
        )

        classifier = load_keyword_classifier(
            os.path.join(FIXTURES_PROFILE, 'categories.yaml'))

        # Classifier tous les fichiers
        results = []
        for pdf_path in self.test_pdfs:
            r = process_single_file(
                pdf_path,
                api_key='fake-key',
                endpoint='http://fake/v1',
                model='fake-model',
                theme_mapping=self.profile.theme_mapping,
                classifier=classifier,
            )
            results.append(r)

        # Tous doivent être classifiés
        classified = [r for r in results if r['status'] == 'classifié']
        self.assertEqual(len(classified), len(self.test_pdfs))

        # Copier vers la cible
        execute_classify(results, self.target, fallback='_A-TRIER')

        # Vérifier que les fichiers sont arrivés
        dest_dir = os.path.join(self.target, '01-SCIENCES/MATHEMATIQUES')
        copied_files = os.listdir(dest_dir)
        self.assertEqual(len(copied_files), len(self.test_pdfs))

        # Vérifier que l'inbox est vidée (source supprimée après copie)
        remaining = [f for f in os.listdir(self.inbox) if f.endswith('.pdf')]
        self.assertEqual(len(remaining), 0)


@unittest.skipIf(not _get_test_pdfs(), "Pas de PDFs dans tests/fixtures/pdfs/")
class TestIntegrationCopy(unittest.TestCase):
    """Test d'intégration : copie de fichiers (classifiés + fallback)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='klodo-integ-copy-')
        self.target = os.path.join(self.tmpdir, 'BIBLIO')
        os.makedirs(os.path.join(self.target, '01-SCIENCES/MATHEMATIQUES'), exist_ok=True)
        os.makedirs(os.path.join(self.target, '_A-TRIER'), exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_copy_classified_and_fallback(self):
        """Les classifiés vont dans leur dossier, les non-classifiés dans le fallback."""
        inbox = os.path.join(self.tmpdir, 'INBOX')
        os.makedirs(inbox)

        # Copier des PDFs dans l'inbox
        pdf_paths = _get_test_pdfs()
        for pdf_path in pdf_paths:
            shutil.copy2(pdf_path, inbox)

        files_in_inbox = [f for f in os.listdir(inbox) if f.endswith('.pdf')]

        if len(files_in_inbox) < 2:
            self.skipTest("Au moins 2 PDFs nécessaires pour ce test")

        # Simuler des résultats : le premier classifié, le reste non
        results = []
        first = True
        for fname in files_in_inbox:
            r = {
                'fichier': fname,
                'chemin': os.path.join(inbox, fname),
                'status': 'classifié' if first else 'non_classifié',
                'destination': '01-SCIENCES/MATHEMATIQUES' if first else '',
            }
            results.append(r)
            first = False

        execute_classify(results, self.target, fallback='_A-TRIER')

        # Vérifier le classifié
        math_files = os.listdir(os.path.join(self.target, '01-SCIENCES/MATHEMATIQUES'))
        self.assertEqual(len(math_files), 1)

        # Vérifier le fallback
        fallback_files = os.listdir(os.path.join(self.target, '_A-TRIER'))
        self.assertEqual(len(fallback_files), len(files_in_inbox) - 1)


@unittest.skipIf(not _get_test_pdfs(), "Pas de PDFs dans tests/fixtures/pdfs/")
class TestIntegrationSafety(unittest.TestCase):
    """Test d'intégration : vérifications de sécurité."""

    def test_inbox_different_from_target(self):
        """check_inbox_safety passe quand inbox != target."""
        tmpdir = tempfile.mkdtemp(prefix='klodo-integ-safe-')
        try:
            inbox = os.path.join(tmpdir, 'INBOX')
            target = os.path.join(tmpdir, 'BIBLIO')
            os.makedirs(inbox)
            os.makedirs(target)
            os.makedirs(os.path.join(target, '_A-TRIER'))

            # Ne doit pas lever d'exception
            check_inbox_safety(inbox, target, '_A-TRIER')
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
