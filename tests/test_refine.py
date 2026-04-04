#!/usr/bin/env python3
"""Tests pour le module refiner v2 — parcours récursif + matching sous-dossiers."""

import os
import shutil
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from lib.logger import setup_logger

setup_logger(verbose=False)

from lib.refiner import (
    _normalize_dirname,
    load_refinement_rules,
    match_keywords,
    match_subdirs,
    save_refine_report,
    scan_and_refine,
)

# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

def _create_tree(base: str, structure: dict) -> None:
    """
    Crée une arborescence à partir d'un dict.
    Clé = nom de dossier/fichier, Valeur = dict (sous-dossier) ou None (fichier).

    Exemple :
        {'SCIENCES': {'MATHS': {'algebra.pdf': None}, 'physics.pdf': None}}
    """
    for name, content in structure.items():
        path = os.path.join(base, name)
        if content is None:
            # C'est un fichier
            with open(path, 'w') as f:
                f.write('dummy pdf content')
        else:
            # C'est un dossier
            os.makedirs(path, exist_ok=True)
            _create_tree(path, content)


# ═══════════════════════════════════════════════════════════════════
# Tests _normalize_dirname
# ═══════════════════════════════════════════════════════════════════

class TestNormalizeDirname(unittest.TestCase):
    """Tests pour la normalisation des noms de dossiers."""

    def test_simple(self):
        result = _normalize_dirname('Deep-Learning')
        self.assertIn('deep-learning', result)
        self.assertIn('deep learning', result)
        self.assertIn('deeplearning', result)

    def test_numeric_prefix_stripped(self):
        result = _normalize_dirname('05-IA-ML')
        self.assertIn('ia-ml', result)
        self.assertIn('ia ml', result)

    def test_no_hyphens(self):
        result = _normalize_dirname('NLP')
        self.assertEqual(result, ['nlp'])

    def test_single_hyphen(self):
        result = _normalize_dirname('Machine-Learning')
        self.assertIn('machine-learning', result)
        self.assertIn('machine learning', result)
        self.assertIn('machinelearning', result)


# ═══════════════════════════════════════════════════════════════════
# Tests match_subdirs
# ═══════════════════════════════════════════════════════════════════

class TestMatchSubdirs(unittest.TestCase):
    """Tests pour le matching implicite par nom de sous-dossier."""

    def test_match_with_hyphens(self):
        result = match_subdirs(
            'Deep Learning with Python.pdf',
            ['Deep-Learning', 'NLP', 'Vision-par-Ordinateur'])
        self.assertEqual(result, 'Deep-Learning')

    def test_match_simple(self):
        result = match_subdirs('NLP Fundamentals.pdf',
                               ['Deep-Learning', 'NLP', 'Vision'])
        self.assertEqual(result, 'NLP')

    def test_no_match(self):
        result = match_subdirs('Algorithms.pdf',
                               ['Deep-Learning', 'NLP'])
        self.assertIsNone(result)

    def test_skip_hidden_folders(self):
        result = match_subdirs('.hidden stuff.pdf',
                               ['.git', '_A-TRIER', 'NLP'])
        self.assertIsNone(result)

    def test_skip_underscore_folders(self):
        """Les dossiers commençant par _ sont ignorés."""
        result = match_subdirs('A-TRIER document.pdf',
                               ['_A-TRIER', 'NLP'])
        self.assertIsNone(result)

    def test_short_variants_ignored(self):
        """Variantes ≤2 chars ne matchent pas (évite faux positifs)."""
        result = match_subdirs('AI is great.pdf', ['AI'])
        # 'ai' a 2 chars → ignoré
        self.assertIsNone(result)

    def test_numeric_prefix_stripped(self):
        """'05-IA-ML' → 'ia-ml' matche 'IA-ML Handbook.pdf'."""
        result = match_subdirs('IA-ML Handbook.pdf',
                               ['05-IA-ML', 'Deep-Learning'])
        self.assertEqual(result, '05-IA-ML')


# ═══════════════════════════════════════════════════════════════════
# Tests scan_and_refine — parcours récursif
# ═══════════════════════════════════════════════════════════════════

class TestScanAndRefineRecursive(unittest.TestCase):
    """Tests du parcours récursif et de la logique de raffinement."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    # ── Leaf folder skip ──

    def test_leaf_folder_skip(self):
        """Fichiers dans un dossier feuille → ignorés (pas dans les résultats)."""
        _create_tree(self.tmp, {
            'LEAF': {
                'book.pdf': None,
            }
        })
        results = scan_and_refine(self.tmp, [], execute=False)
        self.assertEqual(len(results), 0)

    # ── Non-leaf detection ──

    def test_non_leaf_detects_pdf(self):
        """PDF dans un dossier non-feuille → apparaît comme non_classé."""
        _create_tree(self.tmp, {
            'PARENT': {
                'Sub1': {'leaf.pdf': None},
                'orphan.pdf': None,
            }
        })
        results = scan_and_refine(self.tmp, [], execute=False)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['fichier'], 'orphan.pdf')
        self.assertEqual(results[0]['status'], 'non_classé')

    # ── YAML rule matching ──

    def test_yaml_rule_match(self):
        """Règle YAML matche un fichier → statut à_déplacer."""
        _create_tree(self.tmp, {
            'SCIENCES': {
                'MATHS': {
                    '01-Algebre': {'existing.pdf': None},
                    'Linear-Algebra-Hoffman.pdf': None,
                }
            }
        })
        rules = load_refinement_rules([{
            'parent': 'SCIENCES/MATHS',
            'target': '01-Algebre',
            'keywords': ['algebra', 'algèbre'],
        }])
        results = scan_and_refine(self.tmp, rules, execute=False)

        matched = [r for r in results if r['status'] == 'à_déplacer']
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]['fichier'], 'Linear-Algebra-Hoffman.pdf')
        self.assertEqual(matched[0]['source_match'], 'règle_yaml')
        self.assertIn('01-Algebre', matched[0]['destination'])

    # ── Subdirectory name matching ──

    def test_subdir_name_match(self):
        """Nom de sous-dossier matche un fichier (sans règle YAML)."""
        _create_tree(self.tmp, {
            'INFO': {
                'Deep-Learning': {'existing.pdf': None},
                'NLP': {'existing.pdf': None},
                'Deep Learning with Python - Chollet.pdf': None,
            }
        })
        results = scan_and_refine(self.tmp, [], execute=False)

        matched = [r for r in results if r['status'] == 'à_déplacer']
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]['fichier'],
                         'Deep Learning with Python - Chollet.pdf')
        self.assertEqual(matched[0]['source_match'], 'nom_dossier')
        self.assertIn('Deep-Learning', matched[0]['destination'])

    # ── YAML rule priority over subdir name ──

    def test_yaml_priority_over_subdir(self):
        """Règle YAML est prioritaire sur matching par nom de dossier."""
        _create_tree(self.tmp, {
            'INFO': {
                'Neural-Networks': {'existing.pdf': None},
                'Deep-Learning': {'existing.pdf': None},
                'Neural Network Methods.pdf': None,
            }
        })
        # Règle YAML qui redirige "neural" vers Deep-Learning
        rules = load_refinement_rules([{
            'parent': 'INFO',
            'target': 'Deep-Learning',
            'keywords': ['neural'],
        }])
        results = scan_and_refine(self.tmp, rules, execute=False)

        matched = [r for r in results if r['status'] == 'à_déplacer']
        self.assertEqual(len(matched), 1)
        # La règle YAML envoie vers Deep-Learning, pas Neural-Networks
        self.assertIn('Deep-Learning', matched[0]['destination'])
        self.assertEqual(matched[0]['source_match'], 'règle_yaml')

    # ── Non classé ──

    def test_non_classe_no_match(self):
        """Fichier sans aucun match → non_classé."""
        _create_tree(self.tmp, {
            'PARENT': {
                'SubA': {'leaf.pdf': None},
                'mystery-document.pdf': None,
            }
        })
        results = scan_and_refine(self.tmp, [], execute=False)
        nc = [r for r in results if r['status'] == 'non_classé']
        self.assertEqual(len(nc), 1)
        self.assertEqual(nc[0]['fichier'], 'mystery-document.pdf')
        self.assertEqual(nc[0]['mot_cle'], '')
        self.assertEqual(nc[0]['source_match'], '')

    # ── Execute mode ──

    def test_execute_moves_file(self):
        """En mode execute, le fichier est physiquement déplacé."""
        _create_tree(self.tmp, {
            'PARENT': {
                'Deep-Learning': {},
                'Deep Learning Book.pdf': None,
            }
        })
        results = scan_and_refine(self.tmp, [], execute=True)

        moved = [r for r in results if r['status'] == 'déplacé']
        self.assertEqual(len(moved), 1)

        # Fichier déplacé physiquement
        old_path = os.path.join(self.tmp, 'PARENT', 'Deep Learning Book.pdf')
        new_path = os.path.join(self.tmp, 'PARENT', 'Deep-Learning',
                                'Deep Learning Book.pdf')
        self.assertFalse(os.path.exists(old_path))
        self.assertTrue(os.path.exists(new_path))

    # ── Already present ──

    def test_deja_present(self):
        """Fichier déjà dans la cible → statut déjà_présent."""
        _create_tree(self.tmp, {
            'PARENT': {
                'Deep-Learning': {'Deep Learning Book.pdf': None},
                'Deep Learning Book.pdf': None,
            }
        })
        results = scan_and_refine(self.tmp, [], execute=False)

        present = [r for r in results if r['status'] == 'déjà_présent']
        self.assertEqual(len(present), 1)

    # ── Multi-level recursion ──

    def test_multi_level_recursion(self):
        """Parcours récursif sur 3 niveaux — détecte les fichiers à chaque niveau."""
        _create_tree(self.tmp, {
            'L1': {
                'L2': {
                    'L3-Leaf': {'ok.pdf': None},
                    'orphan-level2.pdf': None,
                },
                'orphan-level1.pdf': None,
            }
        })
        results = scan_and_refine(self.tmp, [], execute=False)

        fichiers = {r['fichier'] for r in results}
        # Level 1 : L1 a L2 → non-feuille → orphan-level1.pdf détecté
        self.assertIn('orphan-level1.pdf', fichiers)
        # Level 2 : L2 a L3-Leaf → non-feuille → orphan-level2.pdf détecté
        self.assertIn('orphan-level2.pdf', fichiers)
        # Level 3 : L3-Leaf n'a pas de sous-dossier → feuille → ok.pdf ignoré
        self.assertNotIn('ok.pdf', fichiers)

    # ── Non-PDF files ignored ──

    def test_non_pdf_ignored(self):
        """Les fichiers non-PDF sont ignorés."""
        _create_tree(self.tmp, {
            'PARENT': {
                'Sub': {'leaf.pdf': None},
                'readme.txt': None,
                'image.png': None,
            }
        })
        results = scan_and_refine(self.tmp, [], execute=False)
        self.assertEqual(len(results), 0)

    # ── Empty non-leaf (no PDFs) ──

    def test_non_leaf_no_pdfs(self):
        """Dossier non-feuille sans PDF → aucun résultat."""
        _create_tree(self.tmp, {
            'PARENT': {
                'Sub': {'leaf.pdf': None},
                'readme.txt': None,
            }
        })
        results = scan_and_refine(self.tmp, [], execute=False)
        self.assertEqual(len(results), 0)


# ═══════════════════════════════════════════════════════════════════
# Tests save_refine_report
# ═══════════════════════════════════════════════════════════════════

class TestSaveRefineReport(unittest.TestCase):
    """Tests pour la sauvegarde du rapport CSV."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_report_includes_source_match(self):
        """Le rapport CSV contient la colonne source_match."""
        results = [{
            'fichier': 'test.pdf',
            'source': 'A/test.pdf',
            'destination': 'A/B/test.pdf',
            'mot_cle': 'neural',
            'source_match': 'règle_yaml',
            'status': 'à_déplacer',
        }]
        path = save_refine_report(results, self.tmp)
        self.assertTrue(os.path.exists(path))

        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
        self.assertIn('source_match', content)
        self.assertIn('règle_yaml', content)

    def test_report_non_classe(self):
        """Les non_classé apparaissent dans le rapport."""
        results = [{
            'fichier': 'mystery.pdf',
            'source': 'A/mystery.pdf',
            'destination': '',
            'mot_cle': '',
            'source_match': '',
            'status': 'non_classé',
        }]
        path = save_refine_report(results, self.tmp)

        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
        self.assertIn('non_classé', content)


# ═══════════════════════════════════════════════════════════════════
# Tests load_refinement_rules (existants, vérification rétrocompat)
# ═══════════════════════════════════════════════════════════════════

class TestLoadRefinementRules(unittest.TestCase):

    def test_basic_loading(self):
        rules = load_refinement_rules([
            {'parent': 'A/B', 'target': 'C', 'keywords': ['x']},
            {'parent': 'A', 'target': 'D', 'keywords': ['y']},
        ])
        self.assertEqual(len(rules), 2)
        # Sorted longest first
        self.assertEqual(rules[0][0], 'A/B')

    def test_empty_rules(self):
        rules = load_refinement_rules([])
        self.assertEqual(len(rules), 0)

    def test_missing_fields_skipped(self):
        rules = load_refinement_rules([
            {'parent': 'A', 'target': '', 'keywords': ['x']},
            {'parent': '', 'target': 'B', 'keywords': ['y']},
            {'parent': 'C', 'target': 'D', 'keywords': []},
        ])
        self.assertEqual(len(rules), 0)


# ═══════════════════════════════════════════════════════════════════
# Tests match_keywords (existants, vérification rétrocompat)
# ═══════════════════════════════════════════════════════════════════

class TestMatchKeywords(unittest.TestCase):

    def test_basic_match(self):
        result = match_keywords('Neural Networks.pdf', ['neural', 'quantum'])
        self.assertEqual(result, 'neural')

    def test_no_match(self):
        result = match_keywords('Algorithms.pdf', ['neural', 'quantum'])
        self.assertIsNone(result)

    def test_case_insensitive(self):
        result = match_keywords('QUANTUM Physics.pdf', ['quantum'])
        self.assertEqual(result, 'quantum')


# ═══════════════════════════════════════════════════════════════════
# Tests LLM fallback (Phase 2)
# ═══════════════════════════════════════════════════════════════════

class TestScanAndRefineLLM(unittest.TestCase):
    """Tests du fallback LLM dans scan_and_refine."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_llm_called_for_non_classe(self):
        """LLM callback est appelé pour les fichiers sans match keyword."""
        _create_tree(self.tmp, {
            'PARENT': {
                'Physics': {'existing.pdf': None},
                'Chemistry': {'existing.pdf': None},
                'Quantum Mechanics Explained.pdf': None,
            }
        })
        # LLM renvoie "Physics" pour ce fichier
        def mock_llm(filename, current_folder, subdirs, pdf_path=None):
            if 'quantum' in filename.lower():
                return ('Physics', 'llm')
            return (None, '')

        mock_llm.stats = {'calls': 0, 'successes': 0, 'failures': 0}

        results = scan_and_refine(self.tmp, [], execute=False,
                                  llm_callback=mock_llm)

        matched = [r for r in results if r['status'] == 'à_déplacer']
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]['source_match'], 'llm')
        self.assertIn('Physics', matched[0]['destination'])

    def test_llm_not_called_when_keyword_matches(self):
        """LLM n'est PAS appelé si le matching keyword a déjà trouvé."""
        _create_tree(self.tmp, {
            'PARENT': {
                'Physics': {'existing.pdf': None},
                'Physics Introduction.pdf': None,
            }
        })
        call_count = [0]

        def mock_llm(filename, current_folder, subdirs, pdf_path=None):
            call_count[0] += 1
            return ('Physics', 'llm')

        mock_llm.stats = {'calls': 0, 'successes': 0, 'failures': 0}

        results = scan_and_refine(self.tmp, [], execute=False,
                                  llm_callback=mock_llm)

        # Le matching par nom de dossier a déjà trouvé "physics"
        matched = [r for r in results if r['status'] == 'à_déplacer']
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]['source_match'], 'nom_dossier')
        # LLM ne devrait PAS avoir été appelé
        self.assertEqual(call_count[0], 0)

    def test_llm_returns_none_stays_non_classe(self):
        """Si LLM retourne None → fichier reste non_classé."""
        _create_tree(self.tmp, {
            'PARENT': {
                'SubA': {'existing.pdf': None},
                'mystery-document.pdf': None,
            }
        })

        def mock_llm(filename, current_folder, subdirs, pdf_path=None):
            return (None, '')  # LLM ne sait pas

        mock_llm.stats = {'calls': 0, 'successes': 0, 'failures': 0}

        results = scan_and_refine(self.tmp, [], execute=False,
                                  llm_callback=mock_llm)

        nc = [r for r in results if r['status'] == 'non_classé']
        self.assertEqual(len(nc), 1)

    def test_no_llm_callback_no_call(self):
        """Sans llm_callback, pas d'appel LLM (phase 1 behavior)."""
        _create_tree(self.tmp, {
            'PARENT': {
                'SubA': {'existing.pdf': None},
                'mystery.pdf': None,
            }
        })
        results = scan_and_refine(self.tmp, [], execute=False,
                                  llm_callback=None)

        nc = [r for r in results if r['status'] == 'non_classé']
        self.assertEqual(len(nc), 1)

    def test_llm_execute_moves_file(self):
        """LLM match + execute → fichier physiquement déplacé."""
        _create_tree(self.tmp, {
            'PARENT': {
                'Target-Folder': {},
                'some-random-book.pdf': None,
            }
        })

        def mock_llm(filename, current_folder, subdirs, pdf_path=None):
            return ('Target-Folder', 'llm')

        mock_llm.stats = {'calls': 0, 'successes': 0, 'failures': 0}

        results = scan_and_refine(self.tmp, [], execute=True,
                                  llm_callback=mock_llm)

        moved = [r for r in results if r['status'] == 'déplacé']
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0]['source_match'], 'llm')

        # Vérifier le déplacement physique
        old_path = os.path.join(self.tmp, 'PARENT', 'some-random-book.pdf')
        new_path = os.path.join(self.tmp, 'PARENT', 'Target-Folder',
                                'some-random-book.pdf')
        self.assertFalse(os.path.exists(old_path))
        self.assertTrue(os.path.exists(new_path))

    def test_yaml_priority_over_llm(self):
        """Règle YAML est prioritaire sur LLM."""
        _create_tree(self.tmp, {
            'INFO': {
                'AI': {'existing.pdf': None},
                'Web': {'existing.pdf': None},
                'neural-networks-intro.pdf': None,
            }
        })
        rules = load_refinement_rules([{
            'parent': 'INFO',
            'target': 'AI',
            'keywords': ['neural'],
        }])

        call_count = [0]

        def mock_llm(filename, current_folder, subdirs, pdf_path=None):
            call_count[0] += 1
            return ('Web', 'llm')  # LLM dirait Web

        mock_llm.stats = {'calls': 0, 'successes': 0, 'failures': 0}

        results = scan_and_refine(self.tmp, rules, execute=False,
                                  llm_callback=mock_llm)

        matched = [r for r in results if r['status'] == 'à_déplacer']
        self.assertEqual(len(matched), 1)
        # YAML gagne → AI, pas Web
        self.assertIn('AI', matched[0]['destination'])
        self.assertEqual(matched[0]['source_match'], 'règle_yaml')
        # LLM pas appelé
        self.assertEqual(call_count[0], 0)


# ═══════════════════════════════════════════════════════════════════
# Tests _parse_llm_json
# ═══════════════════════════════════════════════════════════════════

from lib.refiner import _parse_llm_json


class TestParseLlmJson(unittest.TestCase):

    def test_clean_json(self):
        result = _parse_llm_json('{"folder": "Physics", "confidence": 0.9}')
        self.assertEqual(result['folder'], 'Physics')

    def test_markdown_block(self):
        content = '```json\n{"folder": "Physics", "confidence": 0.9}\n```'
        result = _parse_llm_json(content)
        self.assertEqual(result['folder'], 'Physics')

    def test_text_around_json(self):
        content = 'Here is my answer: {"folder": "NLP", "confidence": 0.8} done.'
        result = _parse_llm_json(content)
        self.assertEqual(result['folder'], 'NLP')

    def test_invalid_json(self):
        result = _parse_llm_json('not json at all')
        self.assertIsNone(result)

    def test_empty_string(self):
        result = _parse_llm_json('')
        self.assertIsNone(result)


# ═══════════════════════════════════════════════════════════════════
# Tests parser refine --llm
# ═══════════════════════════════════════════════════════════════════

class TestRefineParser(unittest.TestCase):
    """Tests des options CLI refine."""

    def setUp(self):
        sys.path.insert(0, PROJECT_ROOT)
        from klodo import _build_parser
        self.parser = _build_parser()

    def test_refine_llm_flag(self):
        """refine --llm active le flag."""
        args = self.parser.parse_args(['refine', '--llm'])
        self.assertTrue(args.llm)

    def test_refine_no_llm_default(self):
        """refine sans --llm → llm=False."""
        args = self.parser.parse_args(['refine'])
        self.assertFalse(args.llm)

    def test_refine_max(self):
        """refine --llm --max 10."""
        args = self.parser.parse_args(['refine', '--llm', '--max', '10'])
        self.assertEqual(args.max, 10)

    def test_refine_max_default(self):
        """refine sans --max → max=0."""
        args = self.parser.parse_args(['refine'])
        self.assertEqual(args.max, 0)

    def test_refine_workers(self):
        """refine --workers 10."""
        args = self.parser.parse_args(['refine', '--workers', '10'])
        self.assertEqual(args.workers, 10)

    def test_refine_workers_short(self):
        """refine -w 5."""
        args = self.parser.parse_args(['refine', '-w', '5'])
        self.assertEqual(args.workers, 5)

    def test_refine_workers_default(self):
        """refine sans --workers → workers=1."""
        args = self.parser.parse_args(['refine'])
        self.assertEqual(args.workers, 1)


# ═══════════════════════════════════════════════════════════════════
# Tests parallélisme LLM (workers)
# ═══════════════════════════════════════════════════════════════════

class TestScanAndRefineWorkers(unittest.TestCase):
    """Tests du parallélisme LLM dans scan_and_refine."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_workers_parallel_all_resolved(self):
        """Avec workers=4, tous les fichiers sont traités en parallèle."""
        _create_tree(self.tmp, {
            'PARENT': {
                'SubA': {'existing.pdf': None},
                'SubB': {'existing.pdf': None},
                'file1.pdf': None,
                'file2.pdf': None,
                'file3.pdf': None,
                'file4.pdf': None,
            }
        })
        import threading
        seen_threads = set()

        def mock_llm(filename, current_folder, subdirs, pdf_path=None):
            seen_threads.add(threading.current_thread().name)
            return ('SubA', 'llm')

        mock_llm.stats = {'calls': 0, 'successes': 0, 'failures': 0}

        results = scan_and_refine(self.tmp, [], execute=False,
                                  llm_callback=mock_llm, workers=4)

        matched = [r for r in results if r['source_match'] == 'llm']
        self.assertEqual(len(matched), 4)

    def test_workers_1_sequential(self):
        """Avec workers=1, le traitement est séquentiel."""
        _create_tree(self.tmp, {
            'PARENT': {
                'SubA': {'existing.pdf': None},
                'file1.pdf': None,
                'file2.pdf': None,
            }
        })

        call_order = []

        def mock_llm(filename, current_folder, subdirs, pdf_path=None):
            call_order.append(filename)
            return ('SubA', 'llm')

        mock_llm.stats = {'calls': 0, 'successes': 0, 'failures': 0}

        results = scan_and_refine(self.tmp, [], execute=False,
                                  llm_callback=mock_llm, workers=1)

        matched = [r for r in results if r['source_match'] == 'llm']
        self.assertEqual(len(matched), 2)
        self.assertEqual(len(call_order), 2)

    def test_workers_no_llm_no_thread(self):
        """Sans llm_callback, pas de passe 2 même avec workers > 1."""
        _create_tree(self.tmp, {
            'PARENT': {
                'SubA': {'existing.pdf': None},
                'mystery.pdf': None,
            }
        })
        results = scan_and_refine(self.tmp, [], execute=False,
                                  llm_callback=None, workers=4)

        nc = [r for r in results if r['status'] == 'non_classé']
        self.assertEqual(len(nc), 1)

    def test_workers_llm_exception_handled(self):
        """Exception dans un thread LLM → non_classé, pas de crash."""
        _create_tree(self.tmp, {
            'PARENT': {
                'SubA': {'existing.pdf': None},
                'crash-me.pdf': None,
            }
        })

        def mock_llm(filename, current_folder, subdirs, pdf_path=None):
            raise RuntimeError("boom")

        mock_llm.stats = {'calls': 0, 'successes': 0, 'failures': 0}

        results = scan_and_refine(self.tmp, [], execute=False,
                                  llm_callback=mock_llm, workers=2)

        nc = [r for r in results if r['status'] == 'non_classé']
        self.assertEqual(len(nc), 1)

    def test_keyword_match_not_sent_to_llm(self):
        """Fichiers déjà matchés par keyword ne passent PAS par le LLM."""
        _create_tree(self.tmp, {
            'PARENT': {
                'Deep-Learning': {'existing.pdf': None},
                'Deep Learning Book.pdf': None,
                'unknown-stuff.pdf': None,
            }
        })
        llm_calls = []

        def mock_llm(filename, current_folder, subdirs, pdf_path=None):
            llm_calls.append(filename)
            return ('Deep-Learning', 'llm')

        mock_llm.stats = {'calls': 0, 'successes': 0, 'failures': 0}

        scan_and_refine(self.tmp, [], execute=False,
                        llm_callback=mock_llm, workers=2)

        # Deep Learning Book.pdf matched by subdir name → no LLM
        # unknown-stuff.pdf → LLM called
        self.assertEqual(len(llm_calls), 1)
        self.assertEqual(llm_calls[0], 'unknown-stuff.pdf')


# ═══════════════════════════════════════════════════════════════════
# Tests vision escalation
# ═══════════════════════════════════════════════════════════════════

class TestScanAndRefineVision(unittest.TestCase):
    """Tests de l'escalade vision dans le callback LLM."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_vision_source_tag(self):
        """Un callback retournant llm_vision → source_match = llm_vision."""
        _create_tree(self.tmp, {
            'PARENT': {
                'Physics': {'existing.pdf': None},
                'Chemistry': {'existing.pdf': None},
                '978-3-030-12345.pdf': None,
            }
        })

        def mock_vision_llm(filename, current_folder, subdirs, pdf_path=None):
            # Simule : texte échoue, vision réussit
            return ('Physics', 'llm_vision')

        mock_vision_llm.stats = {'calls': 0, 'successes': 0, 'failures': 0}

        results = scan_and_refine(self.tmp, [], execute=False,
                                  llm_callback=mock_vision_llm)

        matched = [r for r in results if r['status'] == 'à_déplacer']
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]['source_match'], 'llm_vision')
        self.assertIn('Physics', matched[0]['destination'])

    def test_vision_execute_moves_file(self):
        """Vision match + execute → fichier physiquement déplacé."""
        _create_tree(self.tmp, {
            'PARENT': {
                'Target': {},
                'unknown-isbn.pdf': None,
            }
        })

        def mock_vision_llm(filename, current_folder, subdirs, pdf_path=None):
            return ('Target', 'llm_vision')

        mock_vision_llm.stats = {'calls': 0, 'successes': 0, 'failures': 0}

        results = scan_and_refine(self.tmp, [], execute=True,
                                  llm_callback=mock_vision_llm)

        moved = [r for r in results if r['status'] == 'déplacé']
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0]['source_match'], 'llm_vision')

        old_path = os.path.join(self.tmp, 'PARENT', 'unknown-isbn.pdf')
        new_path = os.path.join(self.tmp, 'PARENT', 'Target', 'unknown-isbn.pdf')
        self.assertFalse(os.path.exists(old_path))
        self.assertTrue(os.path.exists(new_path))

    def test_pdf_path_passed_to_callback(self):
        """Le callback reçoit bien le pdf_path complet."""
        _create_tree(self.tmp, {
            'PARENT': {
                'SubA': {'existing.pdf': None},
                'test-doc.pdf': None,
            }
        })
        received_paths = []

        def mock_llm(filename, current_folder, subdirs, pdf_path=None):
            received_paths.append(pdf_path)
            return ('SubA', 'llm')

        mock_llm.stats = {'calls': 0, 'successes': 0, 'failures': 0}

        scan_and_refine(self.tmp, [], execute=False, llm_callback=mock_llm)

        self.assertEqual(len(received_paths), 1)
        expected = os.path.join(self.tmp, 'PARENT', 'test-doc.pdf')
        self.assertEqual(received_paths[0], expected)

    def test_mixed_llm_and_vision_results(self):
        """Mix de résultats texte et vision dans le même scan."""
        _create_tree(self.tmp, {
            'PARENT': {
                'Physics': {'existing.pdf': None},
                'Chemistry': {'existing.pdf': None},
                'quantum-mechanics.pdf': None,      # texte → Physics
                '978-unknown-isbn.pdf': None,        # vision → Chemistry
            }
        })

        def mock_mixed(filename, current_folder, subdirs, pdf_path=None):
            if 'quantum' in filename.lower():
                return ('Physics', 'llm')
            elif 'isbn' in filename.lower():
                return ('Chemistry', 'llm_vision')
            return (None, '')

        mock_mixed.stats = {'calls': 0, 'successes': 0, 'failures': 0}

        results = scan_and_refine(self.tmp, [], execute=False,
                                  llm_callback=mock_mixed)

        llm_text = [r for r in results if r['source_match'] == 'llm']
        llm_vision = [r for r in results if r['source_match'] == 'llm_vision']
        self.assertEqual(len(llm_text), 1)
        self.assertEqual(len(llm_vision), 1)
        self.assertIn('Physics', llm_text[0]['destination'])
        self.assertIn('Chemistry', llm_vision[0]['destination'])


class TestRefineVisionParser(unittest.TestCase):
    """Tests du parser --vision."""

    def _parse(self, args_list):
        from klodo import _build_parser
        parser = _build_parser()
        return parser.parse_args(args_list)

    def test_vision_flag(self):
        """--vision est reconnu."""
        args = self._parse(['refine', '/tmp', '--llm', '--vision'])
        self.assertTrue(args.vision)

    def test_no_vision_default(self):
        """Sans --vision, la valeur est False."""
        args = self._parse(['refine', '/tmp', '--llm'])
        self.assertFalse(args.vision)

    def test_vision_with_workers(self):
        """--vision combiné avec --workers."""
        args = self._parse(['refine', '/tmp', '--llm', '--vision',
                            '--workers', '5'])
        self.assertTrue(args.vision)
        self.assertEqual(args.workers, 5)


if __name__ == '__main__':
    unittest.main()
