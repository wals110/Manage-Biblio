#!/usr/bin/env python3
"""Test 6 : Rename --llm — fallback LLM Vision dans le pipeline de renommage."""

import os
import shutil
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from lib.logger import setup_logger

setup_logger(verbose=False)

from klodo import _build_parser
from lib.renamer import compute_new_name


class TestRenameLLMParser(unittest.TestCase):
    """Vérifie que --llm et --force sont acceptés par le parser."""

    def setUp(self):
        self.parser = _build_parser()

    def test_llm_flag(self):
        """rename --llm active le flag."""
        args = self.parser.parse_args(['rename', '/tmp', '--llm'])
        self.assertTrue(args.llm)

    def test_no_llm_default(self):
        """rename sans --llm → llm=False."""
        args = self.parser.parse_args(['rename', '/tmp'])
        self.assertFalse(args.llm)

    def test_force_flag(self):
        """rename --force active le flag."""
        args = self.parser.parse_args(['rename', '/tmp', '--force'])
        self.assertTrue(args.force)

    def test_no_force_default(self):
        """rename sans --force → force=False."""
        args = self.parser.parse_args(['rename', '/tmp'])
        self.assertFalse(args.force)

    def test_force_with_llm(self):
        """rename --llm --force → les deux flags activés."""
        args = self.parser.parse_args(['rename', '/tmp', '--llm', '--force'])
        self.assertTrue(args.llm)
        self.assertTrue(args.force)

    def test_max_flag(self):
        """rename --max 10 → max=10."""
        args = self.parser.parse_args(['rename', '/tmp', '--max', '10'])
        self.assertEqual(args.max, 10)

    def test_pages_flag(self):
        """rename --pages 3 → pages=3."""
        args = self.parser.parse_args(['rename', '/tmp', '--pages', '3'])
        self.assertEqual(args.pages, 3)

    def test_pages_default(self):
        """rename sans --pages → pages=1."""
        args = self.parser.parse_args(['rename', '/tmp'])
        self.assertEqual(args.pages, 1)

    def test_force_pages_llm_combined(self):
        """rename --llm --force --pages 2 → les trois options combinées."""
        args = self.parser.parse_args(['rename', '/tmp', '--llm', '--force', '--pages', '2'])
        self.assertTrue(args.llm)
        self.assertTrue(args.force)
        self.assertEqual(args.pages, 2)


class TestComputeNewNameLLM(unittest.TestCase):
    """Tests de compute_new_name avec llm_callback."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fake_pdf = os.path.join(self.tmp, '4829473829.pdf')
        with open(self.fake_pdf, 'w') as f:
            f.write('fake pdf content')

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_llm_success_extraire_llm(self):
        """LLM retourne titre+auteur → EXTRAIRE_LLM."""
        def callback(path):
            return {'title': 'Deep Learning Foundations', 'author': 'Ian Goodfellow'}

        name, action, source = compute_new_name(
            self.fake_pdf, '4829473829.pdf',
            isbn_cache={}, enable_online=False, enable_pdf=False,
            llm_callback=callback)
        self.assertEqual(action, 'EXTRAIRE_LLM')
        self.assertEqual(source, 'llm_vision')
        self.assertIn('Deep Learning', name)
        self.assertIn('Goodfellow', name)

    def test_llm_title_only(self):
        """LLM retourne titre sans auteur → EXTRAIRE_LLM quand même."""
        def callback(path):
            return {'title': 'Introduction to Algorithms', 'author': ''}

        name, action, source = compute_new_name(
            self.fake_pdf, '4829473829.pdf',
            isbn_cache={}, enable_online=False, enable_pdf=False,
            llm_callback=callback)
        self.assertEqual(action, 'EXTRAIRE_LLM')
        self.assertIn('Algorithms', name)

    def test_llm_error_api(self):
        """LLM retourne erreur API → fallback ECHEC."""
        def callback(path):
            return {'error': 'api'}

        _, action, _ = compute_new_name(
            self.fake_pdf, '4829473829.pdf',
            isbn_cache={}, enable_online=False, enable_pdf=False,
            llm_callback=callback)
        self.assertEqual(action, 'ECHEC')

    def test_llm_error_extraction(self):
        """LLM retourne erreur extraction → fallback ECHEC."""
        def callback(path):
            return {'error': 'extraction'}

        _, action, _ = compute_new_name(
            self.fake_pdf, '4829473829.pdf',
            isbn_cache={}, enable_online=False, enable_pdf=False,
            llm_callback=callback)
        self.assertEqual(action, 'ECHEC')

    def test_llm_exception_no_crash(self):
        """LLM lève une exception → pas de crash, fallback ECHEC."""
        def callback(path):
            raise ConnectionError('API down')

        _, action, _ = compute_new_name(
            self.fake_pdf, '4829473829.pdf',
            isbn_cache={}, enable_online=False, enable_pdf=False,
            llm_callback=callback)
        self.assertEqual(action, 'ECHEC')

    def test_llm_empty_title(self):
        """LLM retourne titre vide → fallback."""
        def callback(path):
            return {'title': '', 'author': '', 'confidence': 0.3}

        _, action, _ = compute_new_name(
            self.fake_pdf, '4829473829.pdf',
            isbn_cache={}, enable_online=False, enable_pdf=False,
            llm_callback=callback)
        self.assertEqual(action, 'ECHEC')

    def test_no_callback_unchanged(self):
        """Sans callback LLM → comportement identique à avant."""
        _, action, _ = compute_new_name(
            self.fake_pdf, '4829473829.pdf',
            isbn_cache={}, enable_online=False, enable_pdf=False,
            llm_callback=None)
        self.assertEqual(action, 'ECHEC')

    def test_normaliser_has_priority_over_llm(self):
        """Nettoyage du nom prioritaire → LLM pas appelé si nom nettoyable."""
        called = [False]
        def callback(path):
            called[0] = True
            return {'title': 'Overridden', 'author': 'Nobody'}

        # Nom avec URL encoding → NORMALISER devrait prendre le dessus
        ugly_name = 'Introduction%20to%20Machine%20Learning%20-%20Alex%20Smola.pdf'
        ugly_path = os.path.join(self.tmp, ugly_name)
        with open(ugly_path, 'w') as f:
            f.write('content')

        _, action, _ = compute_new_name(
            ugly_path, ugly_name,
            isbn_cache={}, enable_online=False, enable_pdf=False,
            llm_callback=callback)
        self.assertEqual(action, 'NORMALISER')
        self.assertFalse(called[0], "LLM ne devrait pas être appelé si NORMALISER suffit")


class TestScanForce(unittest.TestCase):
    """Tests du flag --force dans scan() qui bypass is_name_clean()."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Créer un fichier avec un nom "propre" selon is_name_clean
        self.clean_name = 'Introduction to Algorithms - Thomas Cormen.pdf'
        self.clean_path = os.path.join(self.tmp, self.clean_name)
        with open(self.clean_path, 'w') as f:
            f.write('fake pdf content')

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_without_force_skips_clean(self):
        """Sans --force, les noms propres sont ignorés (INCHANGE)."""
        from lib.renamer import scan
        report = scan(self.tmp, enable_online=False, enable_pdf=False,
                      force=False)
        # Le rapport ne contient pas le fichier propre (il est INCHANGE)
        import csv
        with open(report) as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 0, "Fichier propre ne devrait pas être dans le rapport")

    def test_with_force_analyses_clean(self):
        """Avec --force, même les noms propres sont re-analysés."""
        from lib.renamer import scan
        report = scan(self.tmp, enable_online=False, enable_pdf=False,
                      force=True)
        import csv
        with open(report) as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1, "Fichier propre devrait être analysé avec --force")

    def test_force_with_llm_renames_clean(self):
        """--force + --llm : le LLM peut renommer un fichier 'propre'."""
        def mock_llm(path):
            return {'title': 'Introduction to Algorithms Third Edition',
                    'author': 'Thomas H. Cormen'}

        from lib.renamer import scan
        report = scan(self.tmp, enable_online=False, enable_pdf=False,
                      llm_callback=mock_llm, force=True)
        import csv
        with open(report) as f:
            rows = list(csv.DictReader(f))
        # Le LLM devrait proposer un meilleur nom
        self.assertEqual(len(rows), 1)
        # Le nom proposé devrait venir du LLM
        self.assertIn('Third Edition', rows[0]['nouveau_nom'])


if __name__ == '__main__':
    unittest.main()
