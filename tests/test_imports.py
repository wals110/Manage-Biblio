#!/usr/bin/env python3
"""Test 2 : Imports croisés — vérifie que biblio.py exporte toutes ses fonctions."""

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from lib.logger import setup_logger
setup_logger(verbose=False)


class TestImportsLib(unittest.TestCase):
    """Vérifie que chaque module lib/ s'importe correctement."""

    def test_logger(self):
        from lib.logger import setup_logger, get_logger
        self.assertIsNotNone(setup_logger)
        self.assertIsNotNone(get_logger)

    def test_profile(self):
        from lib.profile import Profile, list_profiles, init_profile
        self.assertIsNotNone(Profile)

    def test_checkpoint(self):
        from lib.checkpoint import CheckpointManager
        self.assertIsNotNone(CheckpointManager)

    def test_vision(self):
        from lib.vision import analyze_cover
        self.assertIsNotNone(analyze_cover)

    def test_utils(self):
        from lib.utils import (
            sanitize_filename, build_new_filename, is_name_already_clean,
            collect_pdf_files, save_report, print_summary,
        )
        self.assertIsNotNone(sanitize_filename)

    def test_classifier(self):
        from lib.classifier import (
            classify_by_theme, classify_combined, make_classify_fn,
            load_keyword_classifier,
        )
        self.assertIsNotNone(classify_combined)

    def test_refiner(self):
        from lib.refiner import (
            load_refinement_rules, scan_and_refine,
            save_refine_report, print_refine_summary,
        )
        self.assertIsNotNone(load_refinement_rules)

    def test_llm_mapper(self):
        from lib.llm_mapper import (
            LLMMapper, load_suggestions, apply_suggestions,
            save_suggestions_file,
        )
        self.assertIsNotNone(LLMMapper)


class TestImportsBiblio(unittest.TestCase):
    """Vérifie que biblio.py exporte toutes les fonctions attendues."""

    EXPECTED_FUNCTIONS = [
        # Pipeline
        'process_single_file',
        'scan_and_classify',
        'execute_classify',
        # Helpers extraits (refactoring)
        '_load_classifiers',
        '_run_processing',
        '_save_mapper_results',
        '_check_inbox_safety',
        '_confirm_execute',
        '_safe_remove_source',
        '_copy_files',
        '_build_parser',
        # Sous-commandes
        'cmd_classify',
        'cmd_process',
        'cmd_rename',
        'cmd_refine',
        'cmd_suggest',
        'cmd_profiles',
        'cmd_init',
        # Entry point
        'main',
    ]

    def test_all_functions_exist(self):
        """Toutes les fonctions attendues sont accessibles dans biblio."""
        import biblio
        for fn_name in self.EXPECTED_FUNCTIONS:
            with self.subTest(function=fn_name):
                self.assertTrue(
                    hasattr(biblio, fn_name),
                    "Fonction manquante : biblio.{}".format(fn_name))

    def test_functions_are_callable(self):
        """Toutes les fonctions exportées sont appelables."""
        import biblio
        for fn_name in self.EXPECTED_FUNCTIONS:
            with self.subTest(function=fn_name):
                fn = getattr(biblio, fn_name, None)
                self.assertTrue(callable(fn),
                                "biblio.{} n'est pas callable".format(fn_name))


if __name__ == '__main__':
    unittest.main()
