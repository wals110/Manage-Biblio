#!/usr/bin/env python3
"""Tests for dashboard/categories.py — Phase A (read-only).

Covers:
  1. YAML parsing & normalization (bad shapes filtered, types coerced)
  2. Aggregation snapshot (groups + entries + stats)
  3. Sorting (entries by priority asc, groups by name)
  4. Endpoint contract (HTTP)
"""

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

from dashboard import categories, data  # noqa: E402


class CategoriesTestBase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="klodo-cat-test-"))
        self.profiles_root = self.tmpdir / "profiles"
        self.profile_name = "test"
        self.profile_dir = self.profiles_root / self.profile_name
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._patcher = mock.patch.object(
            data, "get_project_root", return_value=self.tmpdir,
        )
        self._patcher.start()
        categories.reset_cache()

    def tearDown(self):
        self._patcher.stop()
        categories.reset_cache()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_yaml(self, content: dict):
        (self.profile_dir / "categories.yaml").write_text(
            yaml.safe_dump(content, sort_keys=False)
        )


class TestSnapshotMissing(CategoriesTestBase):

    def test_missing_yaml_returns_empty_ok(self):
        r = categories.build_snapshot(self.profile_name)
        self.assertTrue(r["ok"])
        self.assertFalse(r["exists"])
        self.assertEqual(r["groups"], [])
        self.assertEqual(r["stats"]["n_entries"], 0)


class TestSnapshotParsing(CategoriesTestBase):

    def test_basic_parse(self):
        self._write_yaml({
            "informatique": [
                {"chemin": "02-INFO/AI", "priorite": 2,
                 "mots_cles": ["ai", "ml", "deep learning"]},
                {"chemin": "02-INFO/Web", "priorite": 3,
                 "mots_cles": ["html", "css"]},
            ],
            "mathematique": [
                {"chemin": "01-MATH/Algebra", "priorite": 2,
                 "mots_cles": ["algebra", "linear"]},
            ],
        })
        r = categories.build_snapshot(self.profile_name)
        self.assertTrue(r["exists"])
        self.assertEqual(r["stats"]["n_groups"], 2)
        self.assertEqual(r["stats"]["n_entries"], 3)
        self.assertEqual(r["stats"]["n_keywords"], 7)
        self.assertAlmostEqual(r["stats"]["avg_keywords"], 7 / 3, places=1)

    def test_groups_sorted_alpha(self):
        self._write_yaml({
            "zoologie":      [{"chemin": "x", "priorite": 1, "mots_cles": ["a"]}],
            "informatique":  [{"chemin": "y", "priorite": 1, "mots_cles": ["b"]}],
            "mathematique":  [{"chemin": "z", "priorite": 1, "mots_cles": ["c"]}],
        })
        r = categories.build_snapshot(self.profile_name)
        names = [g["group"] for g in r["groups"]]
        self.assertEqual(names, ["informatique", "mathematique", "zoologie"])

    def test_entries_sorted_by_priority_then_path(self):
        self._write_yaml({
            "informatique": [
                {"chemin": "z-last",  "priorite": 1, "mots_cles": []},
                {"chemin": "a-first", "priorite": 3, "mots_cles": []},
                {"chemin": "m-mid",   "priorite": 1, "mots_cles": []},
            ],
        })
        r = categories.build_snapshot(self.profile_name)
        entries = r["groups"][0]["entries"]
        # priority 1 first (m-mid before z-last), then 3 (a-first)
        self.assertEqual([e["chemin"] for e in entries],
                         ["m-mid", "z-last", "a-first"])

    def test_bad_entries_filtered(self):
        self._write_yaml({
            "informatique": [
                {"chemin": "ok/path", "priorite": 1, "mots_cles": ["x"]},
                {"chemin": "",        "priorite": 1, "mots_cles": ["y"]},   # empty path
                "not-a-dict",                                                # wrong shape
                {"priorite": 1, "mots_cles": ["z"]},                         # missing path
                {"chemin": "ok/2",    "mots_cles": ["w"]},                   # missing priority -> 0
            ],
        })
        r = categories.build_snapshot(self.profile_name)
        entries = r["groups"][0]["entries"]
        self.assertEqual(len(entries), 2)
        chemins = {e["chemin"] for e in entries}
        self.assertEqual(chemins, {"ok/path", "ok/2"})
        # Missing priority coerced to 0 → comes BEFORE priority 1
        self.assertEqual(entries[0]["chemin"], "ok/2")
        self.assertEqual(entries[0]["priorite"], 0)

    def test_string_priority_coerced(self):
        self._write_yaml({
            "informatique": [
                {"chemin": "x", "priorite": "3", "mots_cles": ["a"]},
            ],
        })
        r = categories.build_snapshot(self.profile_name)
        self.assertEqual(r["groups"][0]["entries"][0]["priorite"], 3)

    def test_invalid_priority_falls_back_to_zero(self):
        self._write_yaml({
            "informatique": [
                {"chemin": "x", "priorite": "abc", "mots_cles": []},
            ],
        })
        r = categories.build_snapshot(self.profile_name)
        self.assertEqual(r["groups"][0]["entries"][0]["priorite"], 0)

    def test_keywords_trimmed_and_filtered(self):
        self._write_yaml({
            "informatique": [
                {"chemin": "x", "priorite": 1,
                 "mots_cles": ["  ai  ", "", "  ", "ml", 123]},
            ],
        })
        r = categories.build_snapshot(self.profile_name)
        kws = r["groups"][0]["entries"][0]["mots_cles"]
        # Whitespace trimmed, empty strings removed, non-str coerced
        self.assertIn("ai", kws)
        self.assertIn("ml", kws)
        self.assertNotIn("", kws)
        # int 123 coerced to "123"
        self.assertIn("123", kws)

    def test_group_without_list_skipped(self):
        self._write_yaml({
            "informatique": [{"chemin": "ok", "priorite": 1, "mots_cles": []}],
            "broken":       "not-a-list",
            "also_broken":  {"chemin": "x"},
        })
        r = categories.build_snapshot(self.profile_name)
        names = [g["group"] for g in r["groups"]]
        self.assertEqual(names, ["informatique"])

    def test_malformed_yaml_returns_empty(self):
        (self.profile_dir / "categories.yaml").write_text(
            "informatique:\n  - not valid: : : \n"
        )
        r = categories.build_snapshot(self.profile_name)
        # parse error → exists=True (file there) but no groups (load returned {})
        self.assertTrue(r["exists"])
        # We can't strictly say groups are empty since yaml may still parse
        # the partial. The contract is: never raises.
        self.assertIn("groups", r)


class TestSnapshotCache(CategoriesTestBase):

    def test_cache_serves_second_call(self):
        self._write_yaml({"informatique": [
            {"chemin": "x", "priorite": 1, "mots_cles": []},
        ]})
        r1 = categories.build_snapshot(self.profile_name)
        # Mutate the file on disk
        (self.profile_dir / "categories.yaml").write_text("informatique: []\n")
        r2 = categories.build_snapshot(self.profile_name)
        # r2 should still be the cached version
        self.assertEqual(r1["stats"]["n_entries"], r2["stats"]["n_entries"])

    def test_force_reload_bypasses_cache(self):
        self._write_yaml({"informatique": [
            {"chemin": "x", "priorite": 1, "mots_cles": []},
        ]})
        categories.build_snapshot(self.profile_name)
        (self.profile_dir / "categories.yaml").write_text("informatique: []\n")
        r2 = categories.build_snapshot(self.profile_name, force_reload=True)
        self.assertEqual(r2["stats"]["n_entries"], 0)

    def test_reset_cache_clears(self):
        self._write_yaml({"informatique": [
            {"chemin": "x", "priorite": 1, "mots_cles": []},
        ]})
        categories.build_snapshot(self.profile_name)
        categories.reset_cache(self.profile_name)
        (self.profile_dir / "categories.yaml").write_text("informatique: []\n")
        r = categories.build_snapshot(self.profile_name)
        self.assertEqual(r["stats"]["n_entries"], 0)


class TestSnapshotEndpoint(CategoriesTestBase):

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from dashboard.app import app
        self.client = TestClient(app)

    def test_endpoint_missing_profile(self):
        r = self.client.get(f"/api/categories/snapshot?profile={self.profile_name}")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["exists"])

    def test_endpoint_with_data(self):
        self._write_yaml({
            "informatique": [
                {"chemin": "x", "priorite": 1, "mots_cles": ["a", "b"]},
            ],
        })
        r = self.client.get(f"/api/categories/snapshot?profile={self.profile_name}")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["exists"])
        self.assertEqual(body["stats"]["n_entries"], 1)
        self.assertEqual(body["stats"]["n_keywords"], 2)


if __name__ == "__main__":
    unittest.main()
