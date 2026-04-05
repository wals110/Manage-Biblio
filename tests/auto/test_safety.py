#!/usr/bin/env python3
"""Test 3 : _check_inbox_safety — empêche inbox = target ou inbox = fallback."""

import os
import shutil
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from lib.logger import setup_logger

setup_logger(verbose=False)

from commands.helpers import check_inbox_safety as _check_inbox_safety
from lib.exceptions import SafetyError


class TestCheckInboxSafety(unittest.TestCase):
    """Tests de sécurité : _check_inbox_safety bloque les configs dangereuses."""

    def setUp(self):
        """Crée une arborescence temporaire inbox/target/fallback."""
        self.tmp = tempfile.mkdtemp()
        self.inbox = os.path.join(self.tmp, 'INBOX')
        self.target = os.path.join(self.tmp, 'BIBLIO')
        self.fallback_dir = os.path.join(self.target, '_A-TRIER')
        os.makedirs(self.inbox)
        os.makedirs(self.target)
        os.makedirs(self.fallback_dir)

    def tearDown(self):
        """Nettoie les dossiers temporaires."""
        shutil.rmtree(self.tmp)

    def test_normal_config_passes(self):
        """Config valide (inbox != target != fallback) → pas d'erreur."""
        _check_inbox_safety(self.inbox, self.target, '_A-TRIER')

    def test_inbox_equals_target_blocks(self):
        """inbox == target → SafetyError."""
        with self.assertRaises(SafetyError):
            _check_inbox_safety(self.target, self.target, '_A-TRIER')

    def test_inbox_equals_fallback_blocks(self):
        """inbox == fallback → SafetyError."""
        with self.assertRaises(SafetyError):
            _check_inbox_safety(self.fallback_dir, self.target, '_A-TRIER')

    def test_symlink_inbox_to_target_blocks(self):
        """Symlink inbox → target → SafetyError (détection via realpath)."""
        link = os.path.join(self.tmp, 'link_to_target')
        os.symlink(self.target, link)
        with self.assertRaises(SafetyError):
            _check_inbox_safety(link, self.target, '_A-TRIER')

    def test_symlink_inbox_to_fallback_blocks(self):
        """Symlink inbox → fallback → SafetyError."""
        link = os.path.join(self.tmp, 'link_to_fallback')
        os.symlink(self.fallback_dir, link)
        with self.assertRaises(SafetyError):
            _check_inbox_safety(link, self.target, '_A-TRIER')

    def test_different_fallback_name(self):
        """Fallback avec un nom custom → détection correcte."""
        custom_fallback = os.path.join(self.target, 'NON_CLASSE')
        os.makedirs(custom_fallback)
        # inbox != custom_fallback → OK
        _check_inbox_safety(self.inbox, self.target, 'NON_CLASSE')
        # inbox == custom_fallback → bloque
        with self.assertRaises(SafetyError):
            _check_inbox_safety(custom_fallback, self.target, 'NON_CLASSE')


if __name__ == '__main__':
    unittest.main()
