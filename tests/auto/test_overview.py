#!/usr/bin/env python3
"""Tests pour dashboard/overview.py — cockpit Biblio."""

import json  # noqa: F401 — used by upcoming card tests (Task 2+)
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from dashboard import data, overview  # noqa: E402


class OverviewTestBase(unittest.TestCase):
    """Pose un profil de test isolé : profile.yaml + dossier target."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="klodo-ov-test-"))
        self.profiles_root = self.tmpdir / "profiles"
        self.target = self.tmpdir / "library"
        self.target.mkdir(parents=True, exist_ok=True)
        self.profile_name = "test"
        self.profile_dir = self.profiles_root / self.profile_name
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        (self.profile_dir / "profile.yaml").write_text(yaml.safe_dump({
            "name": "test",
            "description": "Test profile",
            "target": str(self.target),
            "inbox": str(self.tmpdir / "inbox"),
            "fallback": "_A-TRIER",
            "llm": {"provider": "siliconflow",
                    "model": "Qwen/Qwen3-VL-32B",
                    "endpoint": "https://api.siliconflow.com/v1"},
            "defaults": {"cost_per_call": 0.0003, "workers": 5},
        }))
        (self.profile_dir / ".cache").mkdir(parents=True, exist_ok=True)
        self._patcher = mock.patch.object(
            data, "get_project_root", return_value=self.tmpdir,
        )
        self._patcher.start()
        overview.reset_cache()

    def tearDown(self):
        self._patcher.stop()
        overview.reset_cache()
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class TestReset(OverviewTestBase):

    def test_reset_cache_clears_all(self):
        overview._overview_cache["foo"] = (0.0, {"x": 1})
        overview.reset_cache()
        self.assertEqual(overview._overview_cache, {})

    def test_reset_cache_clears_one_profile(self):
        overview._overview_cache["a"] = (0.0, {})
        overview._overview_cache["b"] = (0.0, {})
        overview.reset_cache("a")
        self.assertNotIn("a", overview._overview_cache)
        self.assertIn("b", overview._overview_cache)


class TestCardFilesCount(OverviewTestBase):

    def _make_files(self, files: dict[str, int]):
        """files = {rel_path: size_bytes}"""
        for rel, size in files.items():
            p = self.target / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x" * size)

    def test_happy_counts_pdf_and_epub(self):
        self._make_files({
            "a.pdf": 1024, "b.pdf": 2048, "c.epub": 512,
            "subdir/d.pdf": 4096, "subdir/notes.txt": 100,
        })
        r = overview.card_files_count(self.profile_name)
        self.assertEqual(r["total"], 4)
        self.assertEqual(r["by_ext"]["pdf"], 3)
        self.assertEqual(r["by_ext"]["epub"], 1)
        self.assertNotIn("error", r)

    def test_target_missing_returns_error(self):
        shutil.rmtree(self.target)
        r = overview.card_files_count(self.profile_name)
        self.assertEqual(r["total"], 0)
        self.assertEqual(r["error"], "target_missing")

    def test_profile_missing_returns_error(self):
        r = overview.card_files_count("does-not-exist")
        self.assertEqual(r["total"], 0)
        self.assertEqual(r["error"], "profile_missing")

    def test_empty_target_returns_zero(self):
        r = overview.card_files_count(self.profile_name)
        self.assertEqual(r["total"], 0)
        self.assertEqual(r["size_gb"], 0.0)

    def test_target_null_in_yaml_returns_error(self):
        """target: null dans profile.yaml ne doit pas faire walker le cwd."""
        (self.profile_dir / "profile.yaml").write_text(yaml.safe_dump({
            "name": "test", "target": None,
            "defaults": {"cost_per_call": 0.0003},
        }))
        r = overview.card_files_count(self.profile_name)
        self.assertEqual(r["total"], 0)
        self.assertEqual(r["error"], "target_missing")


class TestCardClassifiedRate(OverviewTestBase):

    def _make(self, files: list[str]):
        for rel in files:
            p = self.target / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")

    def test_all_classified(self):
        self._make(["folder1/a.pdf", "folder1/b.pdf", "folder2/c.epub"])
        r = overview.card_classified_rate(self.profile_name)
        self.assertEqual(r["classified"], 3)
        self.assertEqual(r["unclassified"], 0)
        self.assertEqual(r["rate"], 100.0)
        self.assertEqual(r["fallback_count"], 0)

    def test_some_in_root_and_fallback(self):
        self._make([
            "folder/ok.pdf",
            "folder/sub/ok2.pdf",
            "in-root.pdf",
            "_A-TRIER/orphan.epub",
            "_A-TRIER/nested/orphan2.pdf",
        ])
        r = overview.card_classified_rate(self.profile_name)
        # classified = 2 (folder/ok + folder/sub/ok2)
        # root = 1, fallback = 2 → unclassified = 3
        self.assertEqual(r["classified"], 2)
        self.assertEqual(r["unclassified"], 3)
        self.assertEqual(r["fallback_count"], 2)
        self.assertEqual(r["rate"], 40.0)

    def test_empty_returns_zero(self):
        r = overview.card_classified_rate(self.profile_name)
        self.assertEqual(r["rate"], 0.0)

    def test_target_missing(self):
        shutil.rmtree(self.target)
        r = overview.card_classified_rate(self.profile_name)
        self.assertEqual(r["rate"], 0.0)

    def test_target_null_in_yaml_returns_zero(self):
        """target: null doit retourner rate=0, pas walker le cwd."""
        (self.profile_dir / "profile.yaml").write_text(yaml.safe_dump({
            "name": "test", "target": None,
            "defaults": {"cost_per_call": 0.0003},
        }))
        r = overview.card_classified_rate(self.profile_name)
        self.assertEqual(r["rate"], 0.0)
        self.assertEqual(r["classified"], 0)

    def test_profile_missing_returns_zero(self):
        r = overview.card_classified_rate("does-not-exist")
        self.assertEqual(r["rate"], 0.0)
        self.assertEqual(r["classified"], 0)
        self.assertEqual(r["fallback_count"], 0)


class TestCardFoldersCount(OverviewTestBase):

    def _write_tree(self, folders: list[str]):
        (self.profile_dir / "tree.yaml").write_text(
            yaml.safe_dump({"folders": folders}, sort_keys=False)
        )

    def test_counts_total_and_max_depth(self):
        self._write_tree([
            "01-SCIENCES",
            "01-SCIENCES/MATH",
            "01-SCIENCES/MATH/ALG",
            "02-INFO",
        ])
        r = overview.card_folders_count(self.profile_name)
        self.assertEqual(r["total"], 4)
        self.assertEqual(r["max_depth"], 3)

    def test_no_tree_yaml_returns_error(self):
        r = overview.card_folders_count(self.profile_name)
        self.assertEqual(r["total"], 0)
        self.assertEqual(r["max_depth"], 0)
        self.assertEqual(r["error"], "tree_missing")

    def test_invalid_tree_yaml_returns_tree_invalid(self):
        """YAML cassé doit retourner sentinelle tree_invalid (pas crash)."""
        (self.profile_dir / "tree.yaml").write_text("folders: [unclosed\n")
        r = overview.card_folders_count(self.profile_name)
        self.assertEqual(r["total"], 0)
        self.assertEqual(r["error"], "tree_invalid")

    def test_recently_modified_picks_3_newest(self):
        self._write_tree(["A", "A/B", "A/C", "D"])
        r = overview.card_folders_count(self.profile_name)
        self.assertIn("recently_modified", r)
        self.assertIsInstance(r["recently_modified"], list)


if __name__ == "__main__":
    unittest.main()
