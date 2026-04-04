#!/usr/bin/env python3
"""Test 5 : _build_parser — vérifie le parsing de toutes les sous-commandes CLI."""

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from lib.logger import setup_logger

setup_logger(verbose=False)

from klodo import _build_parser


class TestBuildParser(unittest.TestCase):
    """Tests du parser argparse construit par _build_parser."""

    def setUp(self):
        self.parser = _build_parser()

    # ── Commandes de base ──

    def test_no_command(self):
        """Sans sous-commande → command = None."""
        args = self.parser.parse_args([])
        self.assertIsNone(args.command)

    def test_version(self):
        """--version → affiche et quitte."""
        with self.assertRaises(SystemExit) as ctx:
            self.parser.parse_args(['--version'])
        self.assertEqual(ctx.exception.code, 0)

    # ── process ──

    def test_process_with_path(self):
        """process /path → path et command corrects."""
        args = self.parser.parse_args(['process', '/tmp/inbox'])
        self.assertEqual(args.command, 'process')
        self.assertEqual(args.path, '/tmp/inbox')

    def test_process_without_path(self):
        """process sans path → path = None (utilise inbox du profil)."""
        args = self.parser.parse_args(['process'])
        self.assertEqual(args.command, 'process')
        self.assertIsNone(args.path)

    def test_process_all_options(self):
        """process avec toutes les options."""
        args = self.parser.parse_args([
            'process', '/tmp/inbox',
            '--profile', 'custom',
            '--execute', '--verbose',
            '--workers', '8', '--max', '100',
            '--delay', '0.5',
            '--reset', '--retry-errors', '--reclassify',
            '--progress-file', 'custom.json',
        ])
        self.assertEqual(args.profile, 'custom')
        self.assertTrue(args.execute)
        self.assertTrue(args.verbose)
        self.assertEqual(args.workers, 8)
        self.assertEqual(args.max, 100)
        self.assertEqual(args.delay, 0.5)
        self.assertTrue(args.reset)
        self.assertTrue(args.retry_errors)
        self.assertTrue(args.reclassify)
        self.assertEqual(args.progress_file, 'custom.json')

    # ── classify ──

    def test_classify_basic(self):
        """classify → commande et défauts corrects."""
        args = self.parser.parse_args(['classify'])
        self.assertEqual(args.command, 'classify')
        self.assertIsNone(args.path)
        self.assertFalse(args.execute)
        self.assertEqual(args.workers, 0)

    def test_classify_with_execute(self):
        """classify --execute → flag activé."""
        args = self.parser.parse_args(['classify', '--execute'])
        self.assertTrue(args.execute)

    # ── rename ──

    def test_rename_basic(self):
        """rename avec path."""
        args = self.parser.parse_args(['rename', '/tmp/src'])
        self.assertEqual(args.command, 'rename')
        self.assertEqual(args.path, '/tmp/src')

    def test_rename_options(self):
        """rename --no-online --no-pdf."""
        args = self.parser.parse_args(['rename', '/tmp/src', '--no-online', '--no-pdf'])
        self.assertTrue(args.no_online)
        self.assertTrue(args.no_pdf)

    def test_rename_no_llm_options(self):
        """rename n'a PAS d'options LLM (--workers, etc.)."""
        with self.assertRaises(SystemExit):
            self.parser.parse_args(['rename', '--workers', '5'])

    # ── refine ──

    def test_refine_basic(self):
        """refine sans path."""
        args = self.parser.parse_args(['refine'])
        self.assertEqual(args.command, 'refine')
        self.assertIsNone(args.path)

    def test_refine_with_execute(self):
        """refine --execute --verbose."""
        args = self.parser.parse_args(['refine', '--execute', '--verbose'])
        self.assertTrue(args.execute)
        self.assertTrue(args.verbose)

    # ── profiles ──

    def test_profiles(self):
        """profiles → commande simple sans options."""
        args = self.parser.parse_args(['profiles'])
        self.assertEqual(args.command, 'profiles')

    # ── suggest ──

    def test_suggest_review(self):
        """suggest seul → mode review (pas --apply)."""
        args = self.parser.parse_args(['suggest'])
        self.assertEqual(args.command, 'suggest')
        self.assertFalse(args.apply)

    def test_suggest_apply(self):
        """suggest --apply --execute."""
        args = self.parser.parse_args(['suggest', '--apply', '--execute'])
        self.assertTrue(args.apply)
        self.assertTrue(args.execute)

    # ── init ──

    def test_init(self):
        """init mon-profil --target /path."""
        args = self.parser.parse_args(['init', 'mon-profil', '--target', '/Volumes/SSD/LIB'])
        self.assertEqual(args.command, 'init')
        self.assertEqual(args.name, 'mon-profil')
        self.assertEqual(args.target, '/Volumes/SSD/LIB')

    def test_init_requires_target(self):
        """init sans --target → erreur."""
        with self.assertRaises(SystemExit):
            self.parser.parse_args(['init', 'test'])

    # ── Défauts ──

    def test_default_profile(self):
        """--profile par défaut = 'default'."""
        args = self.parser.parse_args(['classify'])
        self.assertEqual(args.profile, 'default')

    def test_default_delay(self):
        """--delay par défaut = 0.2."""
        args = self.parser.parse_args(['classify'])
        self.assertEqual(args.delay, 0.2)

    def test_default_workers(self):
        """--workers par défaut = 0 (utilise défaut profil)."""
        args = self.parser.parse_args(['classify'])
        self.assertEqual(args.workers, 0)

    def test_verbose_short_flag(self):
        """-v = --verbose."""
        args = self.parser.parse_args(['classify', '-v'])
        self.assertTrue(args.verbose)

    def test_workers_short_flag(self):
        """-w 4 = --workers 4."""
        args = self.parser.parse_args(['classify', '-w', '4'])
        self.assertEqual(args.workers, 4)


if __name__ == '__main__':
    unittest.main()
