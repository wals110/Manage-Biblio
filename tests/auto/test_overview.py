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
        from dashboard import overview as ov
        ov._overview_cache["foo"] = (0.0, {"x": 1})
        ov.reset_cache()
        self.assertEqual(ov._overview_cache, {})


if __name__ == "__main__":
    unittest.main()
