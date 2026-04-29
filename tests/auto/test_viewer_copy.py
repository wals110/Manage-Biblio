"""Tests pour copy_files_between_profiles et clear_destination_inbox."""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _make_profile(name: str, inbox_dir: Path):
    """Crée un objet Profile factice avec un inbox donné."""
    profile = type("P", (), {})()
    profile.name = name
    profile.inbox = str(inbox_dir)
    return profile


class TestCopyFilesBetweenProfiles(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src_inbox = self.tmp / "source" / "_INBOX"
        self.dst_inbox = self.tmp / "dest" / "_INBOX"
        self.src_inbox.mkdir(parents=True)
        self.dst_inbox.mkdir(parents=True)

        # Créer quelques fichiers dans la source
        (self.src_inbox / "a.pdf").write_bytes(b"content of a")
        (self.src_inbox / "b.pdf").write_bytes(b"content of b")
        (self.src_inbox / "c.epub").write_bytes(b"content of c")

        def mock_profile(name):
            if name == "source":
                return _make_profile("source", self.src_inbox)
            if name in ("test", "test-local"):
                return _make_profile(name, self.dst_inbox)
            raise ValueError(f"unknown profile: {name}")

        self._patcher = patch("lib.profile.Profile", side_effect=mock_profile)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self.tmp)

    def test_copy_basic(self):
        from dashboard.data import copy_files_between_profiles
        result = copy_files_between_profiles("source", "test", ["a.pdf", "b.pdf"])
        self.assertEqual(result["copied"], 2)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["errors"], [])
        self.assertTrue((self.dst_inbox / "a.pdf").exists())
        self.assertTrue((self.dst_inbox / "b.pdf").exists())

    def test_copy_refuses_default_as_destination(self):
        from dashboard.data import copy_files_between_profiles
        with self.assertRaises(ValueError):
            copy_files_between_profiles("source", "default", ["a.pdf"])

    def test_copy_skips_existing(self):
        from dashboard.data import copy_files_between_profiles
        # Créer le fichier déjà en destination
        (self.dst_inbox / "a.pdf").write_bytes(b"already here")
        result = copy_files_between_profiles("source", "test", ["a.pdf", "b.pdf"])
        self.assertEqual(result["copied"], 1)
        self.assertEqual(result["skipped"], 1)
        # Le fichier déjà présent n'est pas écrasé
        self.assertEqual((self.dst_inbox / "a.pdf").read_bytes(), b"already here")

    def test_copy_missing_source_file(self):
        from dashboard.data import copy_files_between_profiles
        result = copy_files_between_profiles("source", "test", ["missing.pdf"])
        self.assertEqual(result["copied"], 0)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("Source absente", result["errors"][0])

    def test_copy_preserves_source(self):
        from dashboard.data import copy_files_between_profiles
        copy_files_between_profiles("source", "test", ["a.pdf"])
        # La source est intacte
        self.assertTrue((self.src_inbox / "a.pdf").exists())
        self.assertEqual((self.src_inbox / "a.pdf").read_bytes(), b"content of a")

    def test_copy_path_traversal_blocked(self):
        from dashboard.data import copy_files_between_profiles
        result = copy_files_between_profiles(
            "source", "test", ["../evil.pdf", "sub/nested.pdf", "..\\win.pdf"])
        self.assertEqual(result["copied"], 0)
        self.assertEqual(len(result["errors"]), 3)
        for err in result["errors"]:
            self.assertIn("Nom invalide", err)


class TestClearDestinationInbox(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.inbox = self.tmp / "test" / "_INBOX"
        self.inbox.mkdir(parents=True)

        # Fichiers variés
        (self.inbox / "doc1.pdf").write_bytes(b"x")
        (self.inbox / "doc2.epub").write_bytes(b"x")
        (self.inbox / "notes.txt").write_text("keep me")
        (self.inbox / "image.jpg").write_bytes(b"x")

        # Cache thumbnails
        cache = self.inbox / ".thumbnail-cache"
        cache.mkdir()
        (cache / "doc1.jpg").write_bytes(b"thumb")
        (cache / "doc2.jpg").write_bytes(b"thumb")

        def mock_profile(name):
            if name in ("test", "test-local"):
                return _make_profile(name, self.inbox)
            raise ValueError(f"unknown: {name}")

        self._patcher = patch("lib.profile.Profile", side_effect=mock_profile)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self.tmp)

    def test_clear_removes_only_pdfs_and_epubs(self):
        from dashboard.data import clear_destination_inbox
        result = clear_destination_inbox("test")
        self.assertEqual(result["removed"], 2)
        self.assertFalse((self.inbox / "doc1.pdf").exists())
        self.assertFalse((self.inbox / "doc2.epub").exists())
        # Les autres sont préservés
        self.assertTrue((self.inbox / "notes.txt").exists())
        self.assertTrue((self.inbox / "image.jpg").exists())

    def test_clear_preserves_thumbnail_cache(self):
        from dashboard.data import clear_destination_inbox
        result = clear_destination_inbox("test")
        self.assertEqual(result["preserved_cache"], 2)
        cache = self.inbox / ".thumbnail-cache"
        self.assertTrue(cache.exists())
        self.assertTrue((cache / "doc1.jpg").exists())
        self.assertTrue((cache / "doc2.jpg").exists())

    def test_clear_refuses_default(self):
        from dashboard.data import clear_destination_inbox
        with self.assertRaises(ValueError):
            clear_destination_inbox("default")


if __name__ == "__main__":
    unittest.main()
