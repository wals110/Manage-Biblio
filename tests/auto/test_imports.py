#!/usr/bin/env python3
"""Test 2 : Imports croisés — vérifie que klodo.py exporte toutes ses fonctions."""

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from lib.logger import setup_logger

setup_logger(verbose=False)


class TestImportsLib(unittest.TestCase):
    """Vérifie que chaque module lib/ s'importe correctement."""

    def test_logger(self):
        from lib.logger import get_logger, setup_logger
        self.assertIsNotNone(setup_logger)
        self.assertIsNotNone(get_logger)

    def test_profile(self):
        from lib.profile import Profile
        self.assertIsNotNone(Profile)

    def test_checkpoint(self):
        from lib.checkpoint import CheckpointManager
        self.assertIsNotNone(CheckpointManager)

    def test_vision(self):
        from lib.vision import analyze_cover
        self.assertIsNotNone(analyze_cover)

    def test_utils(self):
        from lib.utils import (
            sanitize_filename,
        )
        self.assertIsNotNone(sanitize_filename)

    def test_classifier(self):
        from lib.classifier import (
            classify_combined,
        )
        self.assertIsNotNone(classify_combined)

    def test_refiner(self):
        from lib.refiner import (
            load_refinement_rules,
        )
        self.assertIsNotNone(load_refinement_rules)

    def test_llm_mapper(self):
        from lib.llm_mapper import (
            LLMMapper,
        )
        self.assertIsNotNone(LLMMapper)


class TestImportsKlodo(unittest.TestCase):
    """Vérifie que klodo.py et commands/ exportent toutes les fonctions attendues."""

    # Fonctions dans klodo.py (point d'entrée)
    KLODO_FUNCTIONS = [
        '_build_parser',
        'main',
    ]

    # Fonctions dans commands/ (sous-commandes)
    COMMANDS_FUNCTIONS = [
        'cmd_classify',
        'cmd_process',
        'cmd_rename',
        'cmd_refine',
        'cmd_suggest',
        'cmd_profiles',
        'cmd_init',
    ]

    # Fonctions dans commands/helpers.py
    HELPER_FUNCTIONS = [
        'process_single_file',
        'load_classifiers',
        'run_processing',
        'save_mapper_results',
        'check_inbox_safety',
        'confirm_execute',
        'safe_remove_source',
        'copy_files',
        'execute_classify',
    ]

    def test_klodo_functions_exist(self):
        """Fonctions du point d'entrée klodo.py sont accessibles."""
        import klodo
        for fn_name in self.KLODO_FUNCTIONS:
            with self.subTest(function=fn_name):
                self.assertTrue(
                    hasattr(klodo, fn_name),
                    "Fonction manquante : klodo.{}".format(fn_name))

    def test_commands_functions_exist(self):
        """Sous-commandes exportées par commands/__init__.py."""
        import commands
        for fn_name in self.COMMANDS_FUNCTIONS:
            with self.subTest(function=fn_name):
                self.assertTrue(
                    hasattr(commands, fn_name),
                    "Fonction manquante : commands.{}".format(fn_name))

    def test_helper_functions_exist(self):
        """Helpers partagés dans commands/helpers.py."""
        from commands import helpers
        for fn_name in self.HELPER_FUNCTIONS:
            with self.subTest(function=fn_name):
                fn = getattr(helpers, fn_name, None)
                self.assertTrue(callable(fn),
                                "commands.helpers.{} n'est pas callable".format(fn_name))

    def test_scan_and_classify_exists(self):
        """scan_and_classify est accessible depuis commands.classify."""
        from commands.classify import scan_and_classify
        self.assertTrue(callable(scan_and_classify))


if __name__ == '__main__':
    unittest.main()
