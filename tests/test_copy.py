#!/usr/bin/env python3
"""Test 4 : _safe_remove_source + _copy_files — copie, suppression, anti-collision."""

import os
import sys
import shutil
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from lib.logger import setup_logger
setup_logger(verbose=False)

from biblio import _safe_remove_source, _copy_files


class TestSafeRemoveSource(unittest.TestCase):
    """Tests unitaires pour _safe_remove_source."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _create_file(self, name, content='hello' * 100):
        # type: (str, str) -> str
        """Crée un fichier temporaire et retourne son chemin."""
        path = os.path.join(self.tmp, name)
        with open(path, 'w') as f:
            f.write(content)
        return path

    def test_verified_copy_removes_source(self):
        """Copie OK (même taille) → source supprimée."""
        src = self._create_file('src.pdf')
        dest = self._create_file('dest.pdf')  # même contenu = même taille
        result = _safe_remove_source(src, dest)
        self.assertTrue(result)
        self.assertFalse(os.path.exists(src))
        self.assertTrue(os.path.exists(dest))

    def test_same_file_never_deleted(self):
        """src == dest (même chemin) → jamais supprimé."""
        same = self._create_file('same.pdf')
        result = _safe_remove_source(same, same)
        self.assertFalse(result)
        self.assertTrue(os.path.exists(same))

    def test_different_sizes_no_delete(self):
        """Tailles différentes → pas de suppression."""
        src = self._create_file('src.pdf', 'short')
        dest = self._create_file('dest.pdf', 'much longer content here')
        result = _safe_remove_source(src, dest)
        self.assertFalse(result)
        self.assertTrue(os.path.exists(src))

    def test_dest_missing_no_delete(self):
        """Destination absente → pas de suppression."""
        src = self._create_file('src.pdf')
        result = _safe_remove_source(src, os.path.join(self.tmp, 'nope.pdf'))
        self.assertFalse(result)
        self.assertTrue(os.path.exists(src))

    def test_src_missing_no_delete(self):
        """Source absente → pas de suppression."""
        dest = self._create_file('dest.pdf')
        result = _safe_remove_source(os.path.join(self.tmp, 'nope.pdf'), dest)
        self.assertFalse(result)

    def test_symlink_same_file_no_delete(self):
        """Symlink vers le même fichier → pas de suppression."""
        real = self._create_file('real.pdf')
        link = os.path.join(self.tmp, 'link.pdf')
        os.symlink(real, link)
        result = _safe_remove_source(link, real)
        self.assertFalse(result)
        self.assertTrue(os.path.exists(real))


class TestCopyFiles(unittest.TestCase):
    """Tests unitaires pour _copy_files."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.inbox = os.path.join(self.tmp, 'inbox')
        self.target = os.path.join(self.tmp, 'biblio')
        os.makedirs(self.inbox)
        os.makedirs(self.target)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _create_inbox_file(self, name, content='PDF content'):
        # type: (str, str) -> str
        """Crée un fichier dans l'inbox."""
        path = os.path.join(self.inbox, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(content * 50)
        return path

    def test_classified_copy_and_remove(self):
        """Fichiers classifiés → copiés dans le bon sous-dossier + source supprimée."""
        path = self._create_inbox_file('algo.pdf')
        results = [{
            'chemin': path, 'fichier': 'algo.pdf',
            'destination': '02-INFORMATIQUE/ALGO',
            'renommage': False, 'nouveau_nom': '',
        }]
        done, cleaned = _copy_files(results, self.target, None, False, 'test')
        self.assertEqual(done, 1)
        self.assertEqual(cleaned, 1)
        self.assertTrue(os.path.exists(
            os.path.join(self.target, '02-INFORMATIQUE/ALGO/algo.pdf')))
        self.assertFalse(os.path.exists(path))

    def test_rename_during_copy(self):
        """classify_only=False + renommage → fichier renommé à destination."""
        path = self._create_inbox_file('old_name.pdf')
        results = [{
            'chemin': path, 'fichier': 'old_name.pdf',
            'destination': '01-SCIENCES/PHYSIQUE',
            'renommage': True, 'nouveau_nom': 'Physique Quantique - Feynman.pdf',
        }]
        done, cleaned = _copy_files(results, self.target, None, False, 'test')
        self.assertEqual(done, 1)
        self.assertTrue(os.path.exists(os.path.join(
            self.target, '01-SCIENCES/PHYSIQUE/Physique Quantique - Feynman.pdf')))

    def test_classify_only_no_rename(self):
        """classify_only=True → garde le nom original même si renommage=True."""
        path = self._create_inbox_file('keep_name.pdf')
        results = [{
            'chemin': path, 'fichier': 'keep_name.pdf',
            'destination': '04-SHS/PHILOSOPHIE',
            'renommage': True, 'nouveau_nom': 'Nouveau Nom.pdf',
        }]
        done, cleaned = _copy_files(results, self.target, None, True, 'test')
        self.assertEqual(done, 1)
        self.assertTrue(os.path.exists(os.path.join(
            self.target, '04-SHS/PHILOSOPHIE/keep_name.pdf')))
        self.assertFalse(os.path.exists(os.path.join(
            self.target, '04-SHS/PHILOSOPHIE/Nouveau Nom.pdf')))

    def test_fallback_copy(self):
        """Non-classifiés → copiés vers le dossier fallback."""
        path = self._create_inbox_file('unknown.pdf')
        results = [{
            'chemin': path, 'fichier': 'unknown.pdf',
            'destination': '', 'renommage': False, 'nouveau_nom': '',
        }]
        done, cleaned = _copy_files(results, self.target, '_A-TRIER', False, 'test')
        self.assertEqual(done, 1)
        self.assertTrue(os.path.exists(
            os.path.join(self.target, '_A-TRIER/unknown.pdf')))

    def test_anti_collision_different_size(self):
        """Fichier existant avec même nom mais taille différente → suffixe (2)."""
        # Créer un fichier existant dans la destination
        dest_dir = os.path.join(self.target, '02-INFORMATIQUE/IA-ML')
        os.makedirs(dest_dir)
        with open(os.path.join(dest_dir, 'ml.pdf'), 'w') as f:
            f.write('existing content')

        path = self._create_inbox_file('ml.pdf', 'DIFFERENT')
        results = [{
            'chemin': path, 'fichier': 'ml.pdf',
            'destination': '02-INFORMATIQUE/IA-ML',
            'renommage': False, 'nouveau_nom': '',
        }]
        done, cleaned = _copy_files(results, self.target, None, False, 'test')
        self.assertEqual(done, 1)
        self.assertTrue(os.path.exists(
            os.path.join(dest_dir, 'ml (2).pdf')))

    def test_anti_collision_same_size_skip(self):
        """Fichier existant avec même nom et même taille → skip (déjà présent)."""
        content = 'identical content' * 50
        dest_dir = os.path.join(self.target, '01-SCIENCES')
        os.makedirs(dest_dir)
        with open(os.path.join(dest_dir, 'dup.pdf'), 'w') as f:
            f.write(content)

        path = self._create_inbox_file('dup.pdf', 'identical content')
        results = [{
            'chemin': path, 'fichier': 'dup.pdf',
            'destination': '01-SCIENCES',
            'renommage': False, 'nouveau_nom': '',
        }]
        done, cleaned = _copy_files(results, self.target, None, False, 'test')
        self.assertEqual(done, 0, "Ne devrait pas compter comme copié (déjà présent)")

    def test_missing_source_skip(self):
        """Source inexistante → skip gracieux, pas d'erreur."""
        results = [{
            'chemin': '/nonexistent/path.pdf', 'fichier': 'path.pdf',
            'destination': '01-SCIENCES',
            'renommage': False, 'nouveau_nom': '',
        }]
        done, cleaned = _copy_files(results, self.target, None, False, 'test')
        self.assertEqual(done, 0)
        self.assertEqual(cleaned, 0)

    def test_multiple_files(self):
        """Copie de plusieurs fichiers en une seule passe."""
        paths = []
        results = []
        for i in range(5):
            p = self._create_inbox_file('book{}.pdf'.format(i))
            paths.append(p)
            results.append({
                'chemin': p, 'fichier': 'book{}.pdf'.format(i),
                'destination': '08-LOISIRS/LITTERATURE',
                'renommage': False, 'nouveau_nom': '',
            })
        done, cleaned = _copy_files(results, self.target, None, False, 'test')
        self.assertEqual(done, 5)
        self.assertEqual(cleaned, 5)
        for p in paths:
            self.assertFalse(os.path.exists(p), "Source devrait être supprimée")


if __name__ == '__main__':
    unittest.main()
