#!/usr/bin/env python3
"""Tests pour dashboard/taxonomy.py — Phase 1 onglet Taxonomie.

Couvre 3 axes :
  1. Sécurité d'écriture (backup, lock, validation)
  2. Logique d'agrégation (tree, mapping reverse, themes universe)
  3. Endpoints HTTP (FastAPI TestClient)
"""

import json
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

from dashboard import data, taxonomy  # noqa: E402


# ─── Helpers ──────────────────────────────────────────────────────────────


def _make_profile(profile_dir: Path, *, target: Path,
                  tree_folders: list[str],
                  mapping: dict[str, str],
                  vision_cache: dict | None = None) -> None:
    """Set up a complete minimal profile directory tree."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "profile.yaml").write_text(
        yaml.safe_dump({
            "name": "test",
            "target": str(target),
            "llm": {"model": "Qwen/Qwen3-VL-32B-Instruct"},
            "defaults": {"pages": 2},
        })
    )
    (profile_dir / "tree.yaml").write_text(
        yaml.safe_dump({"folders": tree_folders})
    )
    (profile_dir / "theme_mapping.yaml").write_text(
        yaml.safe_dump(mapping, sort_keys=False)
    )
    cache_dir = profile_dir / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    if vision_cache is not None:
        (cache_dir / "vision_cache.json").write_text(
            json.dumps(vision_cache)
        )


def _make_target(target: Path, folders: list[str], files_per_folder: dict[str, list[str]]) -> None:
    """Create empty PDF placeholders under target for folder-count tests."""
    target.mkdir(parents=True, exist_ok=True)
    for f in folders:
        (target / f).mkdir(parents=True, exist_ok=True)
    for folder, files in files_per_folder.items():
        d = target / folder
        d.mkdir(parents=True, exist_ok=True)
        for fname in files:
            (d / fname).write_bytes(b"%PDF-1.4 placeholder")


# ─── Base test case with isolated profile + project_root ──────────────────


class TaxonomyTestBase(unittest.TestCase):
    """Common scaffolding: temp dir as project root, mock data.get_project_root()."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="klodo-tax-test-"))
        self.profiles_root = self.tmpdir / "profiles"
        self.target = self.tmpdir / "library"
        self.profile_name = "test"
        self.profile_dir = self.profiles_root / self.profile_name

        # Default minimal setup — subclasses can extend
        _make_target(
            self.target,
            folders=[
                "01-SCIENCES",
                "01-SCIENCES/PHYSIQUE",
                "01-SCIENCES/MATHEMATIQUES",
                "02-INFORMATIQUE",
            ],
            files_per_folder={
                "01-SCIENCES": ["intro.pdf"],
                "01-SCIENCES/PHYSIQUE": ["mechanics.pdf", "quantum.pdf"],
                "01-SCIENCES/MATHEMATIQUES": ["algebra.pdf"],
                "02-INFORMATIQUE": [],
            },
        )
        _make_profile(
            self.profile_dir,
            target=self.target,
            tree_folders=[
                "01-SCIENCES",
                "01-SCIENCES/PHYSIQUE",
                "01-SCIENCES/MATHEMATIQUES",
                "02-INFORMATIQUE",
            ],
            mapping={
                "physics": "01-SCIENCES/PHYSIQUE",
                "quantum mechanics": "01-SCIENCES/PHYSIQUE",
                "algebra": "01-SCIENCES/MATHEMATIQUES",
            },
        )

        # Patch get_project_root so taxonomy uses our tmpdir
        self._patcher = mock.patch.object(
            data, "get_project_root", return_value=self.tmpdir
        )
        self._patcher.start()
        taxonomy.reset_cache()

    def tearDown(self):
        self._patcher.stop()
        taxonomy.reset_cache()
        shutil.rmtree(self.tmpdir, ignore_errors=True)


# ─── 1. Tests de sécurité d'écriture ─────────────────────────────────────


class TestWriteSafety(TaxonomyTestBase):

    def test_add_mapping_creates_backup(self):
        result = taxonomy.add_mapping(
            self.profile_name, "thermodynamics", "01-SCIENCES/PHYSIQUE"
        )
        self.assertTrue(result["ok"])
        # Backup file present + horodaté
        backups = list(
            (self.profile_dir / ".cache" / "taxonomy-backups").glob("theme_mapping-*.yaml")
        )
        self.assertEqual(len(backups), 1)
        # The backup contains the PRE-write state (no 'thermodynamics')
        backup_content = yaml.safe_load(backups[0].read_text())
        self.assertNotIn("thermodynamics", backup_content)
        # Current file does contain it
        current = yaml.safe_load(
            (self.profile_dir / "theme_mapping.yaml").read_text()
        )
        self.assertIn("thermodynamics", current)
        self.assertEqual(current["thermodynamics"], "01-SCIENCES/PHYSIQUE")

    def test_add_mapping_rejects_duplicate(self):
        with self.assertRaises(taxonomy.TaxonomyError) as ctx:
            taxonomy.add_mapping(
                self.profile_name, "physics", "01-SCIENCES/MATHEMATIQUES"
            )
        self.assertEqual(ctx.exception.status, 409)
        # Original value preserved
        m = yaml.safe_load(
            (self.profile_dir / "theme_mapping.yaml").read_text()
        )
        self.assertEqual(m["physics"], "01-SCIENCES/PHYSIQUE")

    def test_add_mapping_rejects_unknown_folder(self):
        with self.assertRaises(taxonomy.TaxonomyError) as ctx:
            taxonomy.add_mapping(
                self.profile_name, "robotics", "99-FAKE-SECTION/ROBOTICS"
            )
        self.assertEqual(ctx.exception.status, 400)

    def test_add_mapping_rejects_empty_theme(self):
        with self.assertRaises(taxonomy.TaxonomyError) as ctx:
            taxonomy.add_mapping(
                self.profile_name, "   ", "01-SCIENCES/PHYSIQUE"
            )
        self.assertEqual(ctx.exception.status, 400)

    def test_add_mapping_respects_lock(self):
        lock = self.profile_dir / ".cache" / "taxonomy.lock"
        lock.write_text("locked by test")
        with self.assertRaises(taxonomy.TaxonomyError) as ctx:
            taxonomy.add_mapping(
                self.profile_name, "thermodynamics", "01-SCIENCES/PHYSIQUE"
            )
        self.assertEqual(ctx.exception.status, 423)

    def test_backup_rotation_max_20(self):
        """Le 21e backup supprime le plus ancien."""
        backup_dir = self.profile_dir / ".cache" / "taxonomy-backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        # Pre-create 20 fake backups with old timestamps
        for i in range(20):
            (backup_dir / f"theme_mapping-2025010{i//10}-{i % 10}{i % 10}{i:02d}{i:02d}.yaml").write_text(
                f"old: backup-{i}"
            )
        # Now add a real mapping which should rotate
        taxonomy.add_mapping(
            self.profile_name, "new_theme", "01-SCIENCES/PHYSIQUE"
        )
        backups = sorted(backup_dir.glob("theme_mapping-*.yaml"))
        self.assertEqual(len(backups), 20, "Rotation devrait garder exactement 20 backups")


# ─── 2. Tests de logique d'agrégation ────────────────────────────────────


class TestTreeHierarchy(TaxonomyTestBase):

    def test_tree_hierarchy_structure(self):
        snap = taxonomy.get_snapshot(self.profile_name, force_reload=True)
        tree = snap["tree"]
        # Root has top-level sections as children
        names = [c["name"] for c in tree["children"]]
        self.assertIn("01-SCIENCES", names)
        self.assertIn("02-INFORMATIQUE", names)
        # 01-SCIENCES contains PHYSIQUE and MATHEMATIQUES as children
        sciences = next(c for c in tree["children"] if c["name"] == "01-SCIENCES")
        sub_names = [c["name"] for c in sciences["children"]]
        self.assertIn("PHYSIQUE", sub_names)
        self.assertIn("MATHEMATIQUES", sub_names)
        # file_count is direct files only
        self.assertEqual(sciences["file_count"], 1)  # intro.pdf
        physique = next(c for c in sciences["children"] if c["name"] == "PHYSIQUE")
        self.assertEqual(physique["file_count"], 2)  # mechanics.pdf + quantum.pdf

    def test_mapping_reverse_inverts_correctly(self):
        snap = taxonomy.get_snapshot(self.profile_name, force_reload=True)
        mbf = snap["mapping_by_folder"]
        # /PHYSIQUE should have both 'physics' and 'quantum mechanics'
        self.assertIn("01-SCIENCES/PHYSIQUE", mbf)
        self.assertEqual(
            sorted(mbf["01-SCIENCES/PHYSIQUE"]),
            ["physics", "quantum mechanics"],
        )
        # /MATHEMATIQUES should have 'algebra'
        self.assertEqual(mbf["01-SCIENCES/MATHEMATIQUES"], ["algebra"])

    def test_aggregate_themes_filters_low_confidence(self):
        # Build a vision cache with mixed confidence
        cache = {
            "k1": {
                "result": {
                    "title": "Book A",
                    "themes": [
                        {"theme": "Quantum Physics", "confidence": 0.9},
                        {"theme": "Low Signal", "confidence": 0.3},  # filtered
                    ],
                },
                "model": "x", "prompt_version": "v3",
            },
        }
        (self.profile_dir / ".cache" / "vision_cache.json").write_text(json.dumps(cache))
        taxonomy.reset_cache()
        snap = taxonomy.get_snapshot(self.profile_name, force_reload=True)
        names = [t["theme"] for t in snap["themes_llm"]]
        self.assertIn("Quantum Physics", names)
        self.assertNotIn("Low Signal", names)

    def test_aggregate_themes_identifies_orphans(self):
        cache = {
            "k1": {
                "result": {
                    "title": "Mapped Book",
                    "themes": [{"theme": "physics", "confidence": 0.9}],
                },
                "model": "x", "prompt_version": "v3",
            },
            "k2": {
                "result": {
                    "title": "Orphan Book",
                    "themes": [{"theme": "underwater_basket_weaving", "confidence": 0.9}],
                },
                "model": "x", "prompt_version": "v3",
            },
        }
        (self.profile_dir / ".cache" / "vision_cache.json").write_text(json.dumps(cache))
        taxonomy.reset_cache()
        snap = taxonomy.get_snapshot(self.profile_name, force_reload=True)
        by_theme = {t["theme"]: t for t in snap["themes_llm"]}
        self.assertFalse(by_theme["physics"]["is_orphan"])
        self.assertEqual(by_theme["physics"]["mapped_to"], "01-SCIENCES/PHYSIQUE")
        self.assertTrue(by_theme["underwater_basket_weaving"]["is_orphan"])
        self.assertIsNone(by_theme["underwater_basket_weaving"]["mapped_to"])

    def test_get_file_metadata_returns_prediction(self):
        # Cache a vision result for a known file
        pdf_rel = "01-SCIENCES/PHYSIQUE/mechanics.pdf"
        pdf_abs = self.target / pdf_rel
        # Compute the same cache key the production code uses
        from lib import vision_cache as vc
        key = vc.compute_cache_key(str(pdf_abs), model="Qwen/Qwen3-VL-32B-Instruct", n_pages=2)
        cache = {
            key: {
                "result": {
                    "title": "Classical Mechanics",
                    "author": "Test Author",
                    "language": "en",
                    "confidence": 0.92,
                    "themes": [
                        {"theme": "physics", "confidence": 0.9, "reason": "test"},
                        {"theme": "underwater", "confidence": 0.5, "reason": "test"},
                    ],
                },
                "model": "Qwen/Qwen3-VL-32B-Instruct",
                "prompt_version": "v3",
            },
        }
        (self.profile_dir / ".cache" / "vision_cache.json").write_text(json.dumps(cache))
        taxonomy.reset_cache()
        meta = taxonomy.get_file_metadata(self.profile_name, pdf_rel)
        self.assertTrue(meta["ok"])
        self.assertIsNotNone(meta["vision"])
        self.assertEqual(meta["vision"]["title"], "Classical Mechanics")
        # Prediction prefers the specific path '/PHYSIQUE' over orphan 'underwater'
        self.assertIsNotNone(meta["prediction"])
        self.assertEqual(meta["prediction"]["dest"], "01-SCIENCES/PHYSIQUE")
        self.assertEqual(meta["prediction"]["used_theme"], "physics")


# ─── 3. Tests des endpoints HTTP ─────────────────────────────────────────


class TestEndpoints(TaxonomyTestBase):

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from dashboard.app import app
        self.client = TestClient(app)

    def test_taxonomy_page_loads(self):
        r = self.client.get(f"/taxonomy?profile={self.profile_name}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Taxonomie", r.text)

    def test_api_snapshot(self):
        r = self.client.get(f"/api/taxonomy/snapshot?profile={self.profile_name}")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        for key in ("tree", "mapping_by_folder", "themes_llm", "stats", "folders"):
            self.assertIn(key, body)
        self.assertEqual(body["profile"], self.profile_name)

    def test_api_mapping_post_happy(self):
        r = self.client.post(
            "/api/taxonomy/mapping",
            json={
                "profile": self.profile_name,
                "theme": "Thermodynamics",
                "folder": "01-SCIENCES/PHYSIQUE",
            },
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["folder"], "01-SCIENCES/PHYSIQUE")
        # Verify written
        mapping = yaml.safe_load(
            (self.profile_dir / "theme_mapping.yaml").read_text()
        )
        self.assertEqual(mapping["Thermodynamics"], "01-SCIENCES/PHYSIQUE")

    def test_api_mapping_post_bad_input(self):
        # Missing profile
        r1 = self.client.post(
            "/api/taxonomy/mapping",
            json={"profile": "", "theme": "x", "folder": "01-SCIENCES"},
        )
        self.assertEqual(r1.status_code, 400)
        # Unknown folder
        r2 = self.client.post(
            "/api/taxonomy/mapping",
            json={"profile": self.profile_name, "theme": "x",
                  "folder": "99-DOES-NOT-EXIST"},
        )
        self.assertEqual(r2.status_code, 400)
        # Duplicate
        r3 = self.client.post(
            "/api/taxonomy/mapping",
            json={"profile": self.profile_name, "theme": "physics",
                  "folder": "01-SCIENCES/MATHEMATIQUES"},
        )
        self.assertEqual(r3.status_code, 409)

    def test_api_file_metadata(self):
        # Existing file but no cache → ok=true, vision=null
        r = self.client.get(
            f"/api/taxonomy/file/metadata"
            f"?profile={self.profile_name}"
            f"&path=01-SCIENCES/PHYSIQUE/mechanics.pdf"
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["file"]["exists"])
        self.assertIsNone(body["vision"])


# ─── 4. Tests Phase 2 — update / delete / undo ───────────────────────────


class TestUpdateDeleteUndo(TaxonomyTestBase):

    def test_update_mapping_happy(self):
        result = taxonomy.update_mapping(
            self.profile_name, "physics", "01-SCIENCES/MATHEMATIQUES"
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["previous_folder"], "01-SCIENCES/PHYSIQUE")
        m = yaml.safe_load((self.profile_dir / "theme_mapping.yaml").read_text())
        self.assertEqual(m["physics"], "01-SCIENCES/MATHEMATIQUES")

    def test_update_mapping_unknown_theme(self):
        with self.assertRaises(taxonomy.TaxonomyError) as ctx:
            taxonomy.update_mapping(
                self.profile_name, "does_not_exist", "01-SCIENCES/PHYSIQUE"
            )
        self.assertEqual(ctx.exception.status, 404)

    def test_update_mapping_unchanged_no_backup(self):
        """Re-mapping vers la même cible ne doit pas créer de backup inutile."""
        backup_dir = self.profile_dir / ".cache" / "taxonomy-backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        before = len(list(backup_dir.glob("*.yaml")))
        r = taxonomy.update_mapping(
            self.profile_name, "physics", "01-SCIENCES/PHYSIQUE"  # même cible
        )
        self.assertTrue(r["unchanged"])
        after = len(list(backup_dir.glob("*.yaml")))
        self.assertEqual(before, after)

    def test_delete_mapping_happy(self):
        result = taxonomy.delete_mapping(self.profile_name, "physics")
        self.assertTrue(result["ok"])
        self.assertEqual(result["previous_folder"], "01-SCIENCES/PHYSIQUE")
        m = yaml.safe_load((self.profile_dir / "theme_mapping.yaml").read_text())
        self.assertNotIn("physics", m)
        # autres clés intactes
        self.assertIn("algebra", m)

    def test_delete_mapping_unknown_theme(self):
        with self.assertRaises(taxonomy.TaxonomyError) as ctx:
            taxonomy.delete_mapping(self.profile_name, "does_not_exist")
        self.assertEqual(ctx.exception.status, 404)

    def test_undo_restores_last_backup(self):
        # Ajout d'un nouveau mapping → crée backup A
        taxonomy.add_mapping(
            self.profile_name, "thermodynamics", "01-SCIENCES/PHYSIQUE"
        )
        current = yaml.safe_load((self.profile_dir / "theme_mapping.yaml").read_text())
        self.assertIn("thermodynamics", current)
        # Undo → restore l'état d'avant ajout + supprime ce backup de la chaîne
        r = taxonomy.restore_last_backup(self.profile_name)
        self.assertTrue(r["ok"])
        self.assertIn("restored_from", r)
        restored = yaml.safe_load((self.profile_dir / "theme_mapping.yaml").read_text())
        self.assertNotIn("thermodynamics", restored)
        # Plus de backups disponibles (chain consommé)
        backups = list((self.profile_dir / ".cache" / "taxonomy-backups").glob("*.yaml"))
        self.assertEqual(len(backups), 0)

    def test_undo_no_backup_returns_404(self):
        with self.assertRaises(taxonomy.TaxonomyError) as ctx:
            taxonomy.restore_last_backup(self.profile_name)
        self.assertEqual(ctx.exception.status, 404)

    def test_undo_chain_two_levels(self):
        """Undo deux fois doit ramener à l'état avant les 2 modifications."""
        original = yaml.safe_load((self.profile_dir / "theme_mapping.yaml").read_text())
        # Ajout 1
        taxonomy.add_mapping(self.profile_name, "thermo1", "01-SCIENCES/PHYSIQUE")
        # Ajout 2
        taxonomy.add_mapping(self.profile_name, "thermo2", "01-SCIENCES/PHYSIQUE")
        # Undo 1 → enlève thermo2
        taxonomy.restore_last_backup(self.profile_name)
        intermediate = yaml.safe_load((self.profile_dir / "theme_mapping.yaml").read_text())
        self.assertIn("thermo1", intermediate)
        self.assertNotIn("thermo2", intermediate)
        # Undo 2 → enlève thermo1
        taxonomy.restore_last_backup(self.profile_name)
        final = yaml.safe_load((self.profile_dir / "theme_mapping.yaml").read_text())
        self.assertEqual(final, original)


class TestUpdateDeleteUndoEndpoints(TaxonomyTestBase):

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from dashboard.app import app
        self.client = TestClient(app)

    def test_api_mapping_patch(self):
        r = self.client.patch("/api/taxonomy/mapping", json={
            "profile": self.profile_name,
            "theme": "physics",
            "folder": "01-SCIENCES/MATHEMATIQUES",
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["previous_folder"], "01-SCIENCES/PHYSIQUE")

    def test_api_mapping_delete(self):
        r = self.client.request("DELETE", "/api/taxonomy/mapping", json={
            "profile": self.profile_name, "theme": "physics",
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["previous_folder"], "01-SCIENCES/PHYSIQUE")

    def test_api_undo(self):
        # add then undo via API
        self.client.post("/api/taxonomy/mapping", json={
            "profile": self.profile_name, "theme": "thermo", "folder": "01-SCIENCES/PHYSIQUE",
        })
        r = self.client.post("/api/taxonomy/undo", json={"profile": self.profile_name})
        self.assertEqual(r.status_code, 200)
        self.assertIn("restored_from", r.json())

    def test_api_undo_no_backup_404(self):
        r = self.client.post("/api/taxonomy/undo", json={"profile": self.profile_name})
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
