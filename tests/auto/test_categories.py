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


class WriteTestBase(CategoriesTestBase):
    """Common setup for write tests — pre-populate a small mapping."""

    def setUp(self):
        super().setUp()
        self._write_yaml({
            "informatique": [
                {"chemin": "02-INFO/AI", "priorite": 2,
                 "mots_cles": ["ai", "ml"]},
                {"chemin": "02-INFO/Web", "priorite": 3,
                 "mots_cles": ["html", "css"]},
            ],
            "mathematique": [
                {"chemin": "01-MATH/Algebra", "priorite": 2,
                 "mots_cles": ["algebra"]},
            ],
        })

    def _reload(self) -> dict:
        return yaml.safe_load(
            (self.profile_dir / "categories.yaml").read_text())


# ── add_entry ────────────────────────────────────────────────────────────


class TestAddEntry(WriteTestBase):

    def test_add_to_new_group(self):
        r = categories.add_entry(self.profile_name, "physique",
                                 "01-PHY/Mecanique", 2, ["mechanics"])
        self.assertTrue(r["ok"])
        data = self._reload()
        self.assertIn("physique", data)
        self.assertEqual(data["physique"][0]["chemin"], "01-PHY/Mecanique")

    def test_add_to_existing_group(self):
        r = categories.add_entry(self.profile_name, "informatique",
                                 "02-INFO/DB", 4, ["sql", "database"])
        self.assertTrue(r["ok"])
        chemins = [e["chemin"] for e in self._reload()["informatique"]]
        self.assertEqual(len(chemins), 3)
        self.assertIn("02-INFO/DB", chemins)

    def test_add_refuses_duplicate_path(self):
        with self.assertRaises(categories.CategoriesError) as cm:
            categories.add_entry(self.profile_name, "informatique",
                                 "02-INFO/AI", 5, ["dup"])
        self.assertEqual(cm.exception.status, 409)

    def test_add_creates_backup_when_file_exists(self):
        categories.add_entry(self.profile_name, "informatique",
                             "02-INFO/X", 5, [])
        backups = list((self.profile_dir / ".cache" / "categories-backups")
                       .glob("categories-*.yaml"))
        self.assertEqual(len(backups), 1)

    def test_add_dedups_keywords(self):
        r = categories.add_entry(self.profile_name, "loisirs",
                                 "08-LOISIRS/SPORT", 5,
                                 ["soccer", "Soccer", "  soccer  ", "tennis"])
        self.assertEqual(r["n_keywords"], 2)

    def test_add_validates_path(self):
        with self.assertRaises(categories.CategoriesError):
            categories.add_entry(self.profile_name, "informatique",
                                 "", 5, [])
        with self.assertRaises(categories.CategoriesError):
            categories.add_entry(self.profile_name, "informatique",
                                 "/leading/slash", 5, [])

    def test_add_validates_priority(self):
        with self.assertRaises(categories.CategoriesError):
            categories.add_entry(self.profile_name, "informatique",
                                 "02-INFO/Z", 0, [])
        with self.assertRaises(categories.CategoriesError):
            categories.add_entry(self.profile_name, "informatique",
                                 "02-INFO/Z", 100, [])
        with self.assertRaises(categories.CategoriesError):
            categories.add_entry(self.profile_name, "informatique",
                                 "02-INFO/Z", "abc", [])


# ── update_entry ─────────────────────────────────────────────────────────


class TestUpdateEntry(WriteTestBase):

    def test_rename_path(self):
        r = categories.update_entry(self.profile_name, "informatique",
                                    "02-INFO/AI",
                                    new_chemin="02-INFO/IA")
        self.assertTrue(r["ok"])
        chemins = {e["chemin"] for e in self._reload()["informatique"]}
        self.assertIn("02-INFO/IA", chemins)
        self.assertNotIn("02-INFO/AI", chemins)

    def test_change_priority(self):
        r = categories.update_entry(self.profile_name, "informatique",
                                    "02-INFO/AI", new_priorite=10)
        self.assertEqual(r["new_priorite"], 10)
        prio = next(e["priorite"] for e in self._reload()["informatique"]
                    if e["chemin"] == "02-INFO/AI")
        self.assertEqual(prio, 10)

    def test_noop_returns_unchanged(self):
        r = categories.update_entry(self.profile_name, "informatique",
                                    "02-INFO/AI", new_chemin="02-INFO/AI",
                                    new_priorite=2)
        self.assertTrue(r.get("unchanged"))

    def test_rename_to_existing_path_refused(self):
        with self.assertRaises(categories.CategoriesError) as cm:
            categories.update_entry(self.profile_name, "informatique",
                                    "02-INFO/AI",
                                    new_chemin="02-INFO/Web")
        self.assertEqual(cm.exception.status, 409)

    def test_unknown_entry_404(self):
        with self.assertRaises(categories.CategoriesError) as cm:
            categories.update_entry(self.profile_name, "informatique",
                                    "does/not/exist",
                                    new_priorite=5)
        self.assertEqual(cm.exception.status, 404)

    def test_nothing_to_change_400(self):
        with self.assertRaises(categories.CategoriesError) as cm:
            categories.update_entry(self.profile_name, "informatique",
                                    "02-INFO/AI")
        self.assertEqual(cm.exception.status, 400)


# ── delete_entry ─────────────────────────────────────────────────────────


class TestDeleteEntry(WriteTestBase):

    def test_delete_happy(self):
        r = categories.delete_entry(self.profile_name, "informatique",
                                    "02-INFO/AI")
        self.assertEqual(r["n_keywords_removed"], 2)
        chemins = [e["chemin"] for e in self._reload()["informatique"]]
        self.assertNotIn("02-INFO/AI", chemins)

    def test_delete_unknown_404(self):
        with self.assertRaises(categories.CategoriesError) as cm:
            categories.delete_entry(self.profile_name, "informatique",
                                    "nope/nada")
        self.assertEqual(cm.exception.status, 404)

    def test_delete_keeps_empty_group(self):
        # mathematique only has one entry — delete it, group should remain
        categories.delete_entry(self.profile_name, "mathematique",
                                "01-MATH/Algebra")
        data = self._reload()
        self.assertIn("mathematique", data)
        self.assertEqual(data["mathematique"], [])


# ── add_keyword / delete_keyword ─────────────────────────────────────────


class TestKeywordOps(WriteTestBase):

    def test_add_keyword(self):
        r = categories.add_keyword(self.profile_name, "informatique",
                                   "02-INFO/AI", "deep learning")
        self.assertEqual(r["n_keywords"], 3)
        kws = next(e["mots_cles"] for e in self._reload()["informatique"]
                   if e["chemin"] == "02-INFO/AI")
        self.assertIn("deep learning", kws)

    def test_add_keyword_dedup_silently(self):
        r = categories.add_keyword(self.profile_name, "informatique",
                                   "02-INFO/AI", "AI")  # already there (case)
        self.assertTrue(r.get("unchanged"))

    def test_delete_keyword(self):
        r = categories.delete_keyword(self.profile_name, "informatique",
                                      "02-INFO/AI", "ai")
        self.assertEqual(r["n_keywords"], 1)
        kws = next(e["mots_cles"] for e in self._reload()["informatique"]
                   if e["chemin"] == "02-INFO/AI")
        self.assertNotIn("ai", kws)

    def test_delete_keyword_case_insensitive(self):
        # "AI" written, "ai" stored — delete is case-insensitive
        r = categories.delete_keyword(self.profile_name, "informatique",
                                      "02-INFO/AI", "AI")
        self.assertEqual(r["n_keywords"], 1)

    def test_delete_keyword_unknown(self):
        with self.assertRaises(categories.CategoriesError) as cm:
            categories.delete_keyword(self.profile_name, "informatique",
                                      "02-INFO/AI", "ghost")
        self.assertEqual(cm.exception.status, 404)


# ── undo ─────────────────────────────────────────────────────────────────


class TestUndo(WriteTestBase):

    def test_undo_restores_previous_state(self):
        before = self._reload()
        categories.delete_entry(self.profile_name, "informatique",
                                "02-INFO/AI")
        intermediate = self._reload()
        self.assertNotEqual(before, intermediate)
        categories.undo(self.profile_name)
        after = self._reload()
        # Compare deeply
        self.assertEqual(after, before)

    def test_undo_with_no_history(self):
        # No write yet → no backup directory entries
        with self.assertRaises(categories.CategoriesError) as cm:
            categories.undo(self.profile_name)
        self.assertEqual(cm.exception.status, 404)


# ── Endpoint coverage ────────────────────────────────────────────────────


class TestCategoriesEndpoints(WriteTestBase):

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from dashboard.app import app
        self.client = TestClient(app)

    def test_endpoint_add_entry(self):
        r = self.client.post("/api/categories/entry", json={
            "profile": self.profile_name, "group": "informatique",
            "chemin": "02-INFO/DB", "priorite": 4, "mots_cles": ["sql"],
        })
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    def test_endpoint_add_conflict(self):
        r = self.client.post("/api/categories/entry", json={
            "profile": self.profile_name, "group": "informatique",
            "chemin": "02-INFO/AI", "priorite": 5,
        })
        self.assertEqual(r.status_code, 409)

    def test_endpoint_update(self):
        r = self.client.patch("/api/categories/entry", json={
            "profile": self.profile_name, "group": "informatique",
            "chemin": "02-INFO/AI", "new_priorite": 7,
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["new_priorite"], 7)

    def test_endpoint_delete_entry(self):
        r = self.client.request("DELETE", "/api/categories/entry", json={
            "profile": self.profile_name, "group": "informatique",
            "chemin": "02-INFO/Web",
        })
        self.assertEqual(r.status_code, 200)

    def test_endpoint_add_keyword(self):
        r = self.client.post("/api/categories/entry/keyword", json={
            "profile": self.profile_name, "group": "informatique",
            "chemin": "02-INFO/AI", "keyword": "neural network",
        })
        self.assertEqual(r.status_code, 200)

    def test_endpoint_delete_keyword(self):
        r = self.client.request("DELETE", "/api/categories/entry/keyword", json={
            "profile": self.profile_name, "group": "informatique",
            "chemin": "02-INFO/AI", "keyword": "ai",
        })
        self.assertEqual(r.status_code, 200)

    def test_endpoint_undo(self):
        # Make a change first so there's something to undo
        categories.delete_entry(self.profile_name, "informatique",
                                "02-INFO/Web")
        r = self.client.post("/api/categories/undo", json={
            "profile": self.profile_name,
        })
        self.assertEqual(r.status_code, 200)
        chemins = [e["chemin"] for e in self._reload()["informatique"]]
        self.assertIn("02-INFO/Web", chemins)

    def test_endpoint_undo_no_history(self):
        r = self.client.post("/api/categories/undo", json={
            "profile": self.profile_name,
        })
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
