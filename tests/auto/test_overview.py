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


if __name__ == "__main__":
    unittest.main()
