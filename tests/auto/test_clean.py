#!/usr/bin/env python3
"""Test : cmd_clean — nettoyage du cache profil."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from lib.logger import setup_logger

setup_logger(verbose=False)

from commands.misc import cmd_clean


class FakeProfile:
    """Profil minimal pour les tests clean."""

    def __init__(self, cache_dir: str) -> None:
        self.name = 'test'
        self.cache_dir = cache_dir


def _make_args(target: str, execute: bool = False, verbose: bool = False) -> MagicMock:
    args = MagicMock()
    args.target = target
    args.execute = execute
    args.verbose = verbose
    return args


class TestCleanClassify(unittest.TestCase):
    """Nettoyage du checkpoint classify."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='klodo-clean-')
        self.cache_dir = os.path.join(self.tmpdir, '.cache')
        os.makedirs(self.cache_dir)
        self.profile = FakeProfile(self.cache_dir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_clean_classify_removes_progress(self):
        """clean classify --execute supprime progress.json."""
        path = os.path.join(self.cache_dir, 'progress.json')
        with open(path, 'w') as f:
            json.dump({'data': {}}, f)

        cmd_clean(_make_args('classify', execute=True), self.profile)
        self.assertFalse(os.path.exists(path))

    def test_clean_classify_keeps_rename(self):
        """clean classify ne touche pas progress_rename.json."""
        rename_path = os.path.join(self.cache_dir, 'progress_rename.json')
        with open(rename_path, 'w') as f:
            json.dump({'data': {}}, f)

        cmd_clean(_make_args('classify', execute=True), self.profile)
        self.assertTrue(os.path.exists(rename_path))

    def test_clean_classify_dryrun(self):
        """clean classify sans --execute ne supprime rien."""
        path = os.path.join(self.cache_dir, 'progress.json')
        with open(path, 'w') as f:
            json.dump({'data': {}}, f)

        cmd_clean(_make_args('classify', execute=False), self.profile)
        self.assertTrue(os.path.exists(path))


class TestCleanRename(unittest.TestCase):
    """Nettoyage du checkpoint rename."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='klodo-clean-')
        self.cache_dir = os.path.join(self.tmpdir, '.cache')
        os.makedirs(self.cache_dir)
        self.profile = FakeProfile(self.cache_dir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_clean_rename_removes_progress_rename(self):
        """clean rename --execute supprime progress_rename.json."""
        path = os.path.join(self.cache_dir, 'progress_rename.json')
        with open(path, 'w') as f:
            json.dump({'data': {}}, f)

        cmd_clean(_make_args('rename', execute=True), self.profile)
        self.assertFalse(os.path.exists(path))

    def test_clean_rename_keeps_classify(self):
        """clean rename ne touche pas progress.json."""
        classify_path = os.path.join(self.cache_dir, 'progress.json')
        with open(classify_path, 'w') as f:
            json.dump({'data': {}}, f)

        cmd_clean(_make_args('rename', execute=True), self.profile)
        self.assertTrue(os.path.exists(classify_path))


class TestCleanProgress(unittest.TestCase):
    """Nettoyage des deux checkpoints."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='klodo-clean-')
        self.cache_dir = os.path.join(self.tmpdir, '.cache')
        os.makedirs(self.cache_dir)
        self.profile = FakeProfile(self.cache_dir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_clean_progress_removes_both(self):
        """clean progress --execute supprime les deux checkpoints."""
        for name in ('progress.json', 'progress_rename.json'):
            with open(os.path.join(self.cache_dir, name), 'w') as f:
                json.dump({'data': {}}, f)

        cmd_clean(_make_args('progress', execute=True), self.profile)
        self.assertFalse(os.path.exists(os.path.join(self.cache_dir, 'progress.json')))
        self.assertFalse(os.path.exists(os.path.join(self.cache_dir, 'progress_rename.json')))

    def test_clean_progress_keeps_isbn(self):
        """clean progress ne touche pas isbn_cache.json."""
        isbn_path = os.path.join(self.cache_dir, 'isbn_cache.json')
        with open(isbn_path, 'w') as f:
            json.dump({}, f)

        cmd_clean(_make_args('progress', execute=True), self.profile)
        self.assertTrue(os.path.exists(isbn_path))


class TestCleanIsbn(unittest.TestCase):
    """Nettoyage du cache ISBN."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='klodo-clean-')
        self.cache_dir = os.path.join(self.tmpdir, '.cache')
        os.makedirs(self.cache_dir)
        self.profile = FakeProfile(self.cache_dir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_clean_isbn_removes_cache(self):
        """clean isbn --execute supprime isbn_cache.json."""
        path = os.path.join(self.cache_dir, 'isbn_cache.json')
        with open(path, 'w') as f:
            json.dump({'978-0-123': {'title': 'Test'}}, f)

        cmd_clean(_make_args('isbn', execute=True), self.profile)
        self.assertFalse(os.path.exists(path))

    def test_clean_isbn_keeps_progress(self):
        """clean isbn ne touche pas les checkpoints."""
        for name in ('progress.json', 'progress_rename.json'):
            with open(os.path.join(self.cache_dir, name), 'w') as f:
                json.dump({'data': {}}, f)

        cmd_clean(_make_args('isbn', execute=True), self.profile)
        self.assertTrue(os.path.exists(os.path.join(self.cache_dir, 'progress.json')))
        self.assertTrue(os.path.exists(os.path.join(self.cache_dir, 'progress_rename.json')))


class TestCleanAll(unittest.TestCase):
    """Nettoyage complet."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='klodo-clean-')
        self.cache_dir = os.path.join(self.tmpdir, '.cache')
        os.makedirs(self.cache_dir)
        self.profile = FakeProfile(self.cache_dir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_clean_all_removes_everything(self):
        """clean all --execute supprime tous les fichiers de cache."""
        for name in ('progress.json', 'progress_rename.json', 'isbn_cache.json'):
            with open(os.path.join(self.cache_dir, name), 'w') as f:
                json.dump({}, f)

        cmd_clean(_make_args('all', execute=True), self.profile)
        self.assertFalse(os.path.exists(os.path.join(self.cache_dir, 'progress.json')))
        self.assertFalse(os.path.exists(os.path.join(self.cache_dir, 'progress_rename.json')))
        self.assertFalse(os.path.exists(os.path.join(self.cache_dir, 'isbn_cache.json')))


class TestCleanEmpty(unittest.TestCase):
    """Nettoyage sur cache vide."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='klodo-clean-')
        self.cache_dir = os.path.join(self.tmpdir, '.cache')
        os.makedirs(self.cache_dir)
        self.profile = FakeProfile(self.cache_dir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_clean_empty_cache(self):
        """clean all sur cache vide ne crash pas."""
        cmd_clean(_make_args('all', execute=True), self.profile)

    def test_clean_dryrun_empty(self):
        """clean all dry-run sur cache vide ne crash pas."""
        cmd_clean(_make_args('all', execute=False), self.profile)


if __name__ == '__main__':
    unittest.main()
