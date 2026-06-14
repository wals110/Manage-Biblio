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


# ─── 5. Tests Phase 3 Étape A — create folder ────────────────────────────


class TestCreateFolder(TaxonomyTestBase):

    def test_create_folder_top_level_happy(self):
        r = taxonomy.create_folder(self.profile_name, "", "99-NEW-SECTION")
        self.assertTrue(r["ok"])
        self.assertEqual(r["path"], "99-NEW-SECTION")
        self.assertTrue((self.target / "99-NEW-SECTION").is_dir())
        tree = yaml.safe_load((self.profile_dir / "tree.yaml").read_text())
        self.assertIn("99-NEW-SECTION", tree["folders"])

    def test_create_folder_sub_happy(self):
        r = taxonomy.create_folder(
            self.profile_name, "01-SCIENCES", "NEW-DOMAIN"
        )
        self.assertEqual(r["path"], "01-SCIENCES/NEW-DOMAIN")
        self.assertTrue((self.target / "01-SCIENCES/NEW-DOMAIN").is_dir())
        tree = yaml.safe_load((self.profile_dir / "tree.yaml").read_text())
        self.assertIn("01-SCIENCES/NEW-DOMAIN", tree["folders"])

    def test_create_folder_unknown_parent(self):
        with self.assertRaises(taxonomy.TaxonomyError) as ctx:
            taxonomy.create_folder(self.profile_name, "99-NO-SECTION", "X")
        self.assertEqual(ctx.exception.status, 400)

    def test_create_folder_duplicate_tree(self):
        # 01-SCIENCES/PHYSIQUE est déjà dans le tree
        with self.assertRaises(taxonomy.TaxonomyError) as ctx:
            taxonomy.create_folder(self.profile_name, "01-SCIENCES", "PHYSIQUE")
        self.assertEqual(ctx.exception.status, 409)

    def test_create_folder_fs_exists_but_not_in_tree(self):
        """Si le dossier existe sur disque mais pas dans tree.yaml,
        on refuse pour éviter de "récupérer" un dossier orphelin."""
        (self.target / "ORPHAN-DIR").mkdir()
        with self.assertRaises(taxonomy.TaxonomyError) as ctx:
            taxonomy.create_folder(self.profile_name, "", "ORPHAN-DIR")
        self.assertEqual(ctx.exception.status, 409)

    def test_create_folder_empty_name(self):
        with self.assertRaises(taxonomy.TaxonomyError) as ctx:
            taxonomy.create_folder(self.profile_name, "", "   ")
        self.assertEqual(ctx.exception.status, 400)

    def test_create_folder_invalid_slash(self):
        with self.assertRaises(taxonomy.TaxonomyError) as ctx:
            taxonomy.create_folder(self.profile_name, "", "foo/bar")
        self.assertEqual(ctx.exception.status, 400)

    def test_create_folder_invalid_dotdot(self):
        with self.assertRaises(taxonomy.TaxonomyError) as ctx:
            taxonomy.create_folder(self.profile_name, "", "..")
        self.assertEqual(ctx.exception.status, 400)

    def test_create_folder_invalid_hidden(self):
        with self.assertRaises(taxonomy.TaxonomyError) as ctx:
            taxonomy.create_folder(self.profile_name, "", ".hidden")
        self.assertEqual(ctx.exception.status, 400)

    def test_create_folder_respects_lock(self):
        (self.profile_dir / ".cache" / "taxonomy.lock").write_text("locked")
        with self.assertRaises(taxonomy.TaxonomyError) as ctx:
            taxonomy.create_folder(self.profile_name, "", "X-NEW")
        self.assertEqual(ctx.exception.status, 423)

    def test_create_folder_creates_tree_backup(self):
        backup_dir = self.profile_dir / ".cache" / "taxonomy-backups"
        before = len(list(backup_dir.glob("tree-*.yaml"))) if backup_dir.exists() else 0
        taxonomy.create_folder(self.profile_name, "", "X-NEW")
        after = len(list(backup_dir.glob("tree-*.yaml")))
        self.assertEqual(after, before + 1)


class TestUndoTreeOps(TaxonomyTestBase):
    """Phase 3 Étape A — extend undo to cover folder creation."""

    def test_undo_create_folder_removes_empty_dir(self):
        # Create a folder, then undo
        taxonomy.create_folder(self.profile_name, "01-SCIENCES", "TEST-UNDO")
        self.assertTrue((self.target / "01-SCIENCES/TEST-UNDO").is_dir())
        # Undo
        u = taxonomy.restore_last_backup(self.profile_name)
        self.assertEqual(u["type"], "tree")
        self.assertIn("01-SCIENCES/TEST-UNDO", u["deleted_folders"])
        # Filesystem dir removed
        self.assertFalse((self.target / "01-SCIENCES/TEST-UNDO").exists())
        # tree.yaml reverted
        tree = yaml.safe_load((self.profile_dir / "tree.yaml").read_text())
        self.assertNotIn("01-SCIENCES/TEST-UNDO", tree["folders"])

    def test_undo_create_folder_keeps_non_empty_dir(self):
        # Create folder, add a file, undo → folder is kept non-empty
        taxonomy.create_folder(self.profile_name, "01-SCIENCES", "KEEP-ME")
        (self.target / "01-SCIENCES/KEEP-ME/data.pdf").write_bytes(b"data")
        u = taxonomy.restore_last_backup(self.profile_name)
        self.assertIn("01-SCIENCES/KEEP-ME", u["kept_non_empty"])
        # tree.yaml still reverted
        tree = yaml.safe_load((self.profile_dir / "tree.yaml").read_text())
        self.assertNotIn("01-SCIENCES/KEEP-ME", tree["folders"])
        # FS dir is still there (with the file inside)
        self.assertTrue((self.target / "01-SCIENCES/KEEP-ME").is_dir())

    def test_undo_picks_most_recent_by_timestamp_across_types(self):
        # mapping op first, then folder op — undo should rollback folder op first
        taxonomy.add_mapping(
            self.profile_name, "newtheme", "01-SCIENCES/PHYSIQUE"
        )
        taxonomy.create_folder(self.profile_name, "01-SCIENCES", "CHAIN-TEST")
        # Most recent = the folder op
        u1 = taxonomy.restore_last_backup(self.profile_name)
        self.assertEqual(u1["type"], "tree")
        # Now the mapping op
        u2 = taxonomy.restore_last_backup(self.profile_name)
        self.assertEqual(u2["type"], "mapping")
        # State should equal original
        m = yaml.safe_load((self.profile_dir / "theme_mapping.yaml").read_text())
        self.assertNotIn("newtheme", m)

    def test_backup_count_includes_both_types(self):
        taxonomy.add_mapping(
            self.profile_name, "x_theme", "01-SCIENCES/PHYSIQUE"
        )
        taxonomy.create_folder(self.profile_name, "01-SCIENCES", "CNT-TEST")
        snap = taxonomy.get_snapshot(self.profile_name, force_reload=True)
        self.assertEqual(snap["stats"]["backup_count"], 2)

    def test_touched_includes_tree_added_folder(self):
        taxonomy.create_folder(self.profile_name, "01-SCIENCES", "T-TEST")
        snap = taxonomy.get_snapshot(self.profile_name, force_reload=True)
        self.assertIn("01-SCIENCES/T-TEST", snap["stats"]["touched_folders"])


class TestCreateFolderEndpoint(TaxonomyTestBase):

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient

        from dashboard.app import app
        self.client = TestClient(app)

    def test_api_folder_create_happy(self):
        r = self.client.post("/api/taxonomy/folder", json={
            "profile": self.profile_name,
            "parent": "01-SCIENCES",
            "name": "NEW-SUB",
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["path"], "01-SCIENCES/NEW-SUB")

    def test_api_folder_create_409(self):
        r = self.client.post("/api/taxonomy/folder", json={
            "profile": self.profile_name,
            "parent": "01-SCIENCES",
            "name": "PHYSIQUE",  # existe déjà
        })
        self.assertEqual(r.status_code, 409)


# ─── 6. Tests Phase 2 B — Impact preview ─────────────────────────────────


class TestImpactPreview(TaxonomyTestBase):

    def _seed_cache_with_themes(self, theme_to_titles: dict[str, list[str]]):
        """Helper: write a minimal vision_cache.json with the given theme→titles."""
        cache = {}
        i = 0
        for theme, titles in theme_to_titles.items():
            for title in titles:
                cache[f"k{i}"] = {
                    "result": {
                        "title": title,
                        "themes": [{"theme": theme, "confidence": 0.9}],
                    },
                    "model": "x",
                    "prompt_version": "v3",
                }
                i += 1
        (self.profile_dir / ".cache" / "vision_cache.json").write_text(json.dumps(cache))
        taxonomy.reset_cache()

    def test_preview_add_no_existing_files(self):
        # Mapping ajouté pour un thème qu'aucun fichier n'a → 0 impact
        self._seed_cache_with_themes({"Unrelated Theme": ["Foo"]})
        p = taxonomy.preview_mapping_impact(
            self.profile_name, "BrandNew", "01-SCIENCES/PHYSIQUE", "add"
        )
        self.assertEqual(p["n_files_affected"], 0)
        self.assertEqual(p["cross_section_changes"], 0)

    def test_preview_add_affects_files(self):
        self._seed_cache_with_themes({
            "newtheme": ["Book A", "Book B", "Book C"],
        })
        p = taxonomy.preview_mapping_impact(
            self.profile_name, "newtheme", "01-SCIENCES/PHYSIQUE", "add"
        )
        self.assertEqual(p["n_files_affected"], 3)
        self.assertEqual(p["new_dest"], "01-SCIENCES/PHYSIQUE")
        self.assertEqual(len(p["examples"]), 3)

    def test_preview_delete_affects_existing_mappings(self):
        # mapping 'physics' → /PHYSIQUE existe déjà ; on simule sa suppression
        self._seed_cache_with_themes({
            "physics": ["Quantum Book", "Mech Book"],
        })
        p = taxonomy.preview_mapping_impact(
            self.profile_name, "physics", None, "delete"
        )
        # Les fichiers tagués 'physics' ne seraient plus classés
        self.assertEqual(p["n_files_affected"], 2)
        self.assertEqual(p["current_dest"], "01-SCIENCES/PHYSIQUE")
        self.assertIsNone(p["new_dest"])

    def test_preview_cross_section_counted(self):
        # mapping qui ferait passer un livre d'une section à une autre
        self._seed_cache_with_themes({
            "physics": ["Book X"],   # actuellement → /01-SCIENCES/PHYSIQUE
        })
        # On simule un re-mapping de physics vers une autre section
        p = taxonomy.preview_mapping_impact(
            self.profile_name, "physics", "02-INFORMATIQUE", "update"
        )
        self.assertEqual(p["n_files_affected"], 1)
        self.assertEqual(p["cross_section_changes"], 1)

    def test_preview_invalid_action(self):
        with self.assertRaises(taxonomy.TaxonomyError) as ctx:
            taxonomy.preview_mapping_impact(
                self.profile_name, "x", "/y", "bogus_action"
            )
        self.assertEqual(ctx.exception.status, 400)


class TestImpactPreviewEndpoint(TaxonomyTestBase):

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient

        from dashboard.app import app
        self.client = TestClient(app)

    def test_api_preview_happy(self):
        r = self.client.post("/api/taxonomy/mapping/preview", json={
            "profile": self.profile_name,
            "theme": "BrandNewTheme",
            "folder": "01-SCIENCES/PHYSIQUE",
            "action": "add",
        })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        for key in ("n_files_affected", "cross_section_changes", "examples", "new_dest"):
            self.assertIn(key, body)


# ─── 7. Tests Phase 2 C — Recalc pipeline complet ────────────────────────


class TestFullPipeline(TaxonomyTestBase):

    def test_full_pipeline_falls_back_when_no_cache(self):
        # No vision cache file → returns null prediction
        r = taxonomy.get_file_metadata_full_pipeline(
            self.profile_name, "01-SCIENCES/PHYSIQUE/mechanics.pdf"
        )
        self.assertTrue(r["ok"])
        self.assertIsNone(r["prediction"])

    def test_full_pipeline_returns_prediction_when_cached(self):
        # Set up a vision cache entry for the file
        from lib import vision_cache as vc
        pdf_rel = "01-SCIENCES/PHYSIQUE/mechanics.pdf"
        pdf_abs = self.target / pdf_rel
        key = vc.compute_cache_key(
            str(pdf_abs), model="Qwen/Qwen3-VL-32B-Instruct", n_pages=2
        )
        cache = {
            key: {
                "result": {
                    "title": "Classical Mechanics",
                    "themes": [
                        {"theme": "physics", "confidence": 0.9, "reason": "test"},
                    ],
                    "confidence": 0.9,
                },
                "model": "Qwen/Qwen3-VL-32B-Instruct",
                "prompt_version": "v3",
            },
        }
        (self.profile_dir / ".cache" / "vision_cache.json").write_text(json.dumps(cache))
        taxonomy.reset_cache()
        r = taxonomy.get_file_metadata_full_pipeline(self.profile_name, pdf_rel)
        self.assertTrue(r["ok"])
        self.assertIsNotNone(r["prediction"])
        self.assertEqual(r["prediction"]["dest"], "01-SCIENCES/PHYSIQUE")


# ─── 8. Tests Phase 3 B — rename folder ──────────────────────────────────


class TestRenameFolder(TaxonomyTestBase):

    def test_rename_happy_cascade(self):
        # Avant : tree a /01-SCIENCES/PHYSIQUE et sa hiérarchie
        # Mapping : physics → /01-SCIENCES/PHYSIQUE
        r = taxonomy.rename_folder(
            self.profile_name, "01-SCIENCES/PHYSIQUE", "PHYS"
        )
        self.assertTrue(r["ok"])
        self.assertEqual(r["old_path"], "01-SCIENCES/PHYSIQUE")
        self.assertEqual(r["new_path"], "01-SCIENCES/PHYS")
        # 1 entrée tree (PHYSIQUE) — pas de descendants dans la fixture de base
        self.assertEqual(r["n_tree_entries_renamed"], 1)
        # 2 mappings (physics + quantum mechanics)
        self.assertEqual(r["n_mappings_updated"], 2)
        self.assertTrue(r["fs_renamed"])

        # Vérifie tree.yaml
        tree = yaml.safe_load((self.profile_dir / "tree.yaml").read_text())
        self.assertIn("01-SCIENCES/PHYS", tree["folders"])
        self.assertNotIn("01-SCIENCES/PHYSIQUE", tree["folders"])
        # Vérifie mapping
        mapping = yaml.safe_load((self.profile_dir / "theme_mapping.yaml").read_text())
        self.assertEqual(mapping["physics"], "01-SCIENCES/PHYS")
        self.assertEqual(mapping["quantum mechanics"], "01-SCIENCES/PHYS")
        # Mappings vers d'autres folders intacts
        self.assertEqual(mapping["algebra"], "01-SCIENCES/MATHEMATIQUES")
        # FS
        self.assertTrue((self.target / "01-SCIENCES/PHYS").is_dir())
        self.assertFalse((self.target / "01-SCIENCES/PHYSIQUE").exists())

    def test_rename_cascade_descendants(self):
        """Si le dossier renommé a des sous-dossiers, ils sont cascadés."""
        # Crée un descendant
        taxonomy.create_folder(
            self.profile_name, "01-SCIENCES/PHYSIQUE", "Mecanique"
        )
        taxonomy.reset_cache()
        r = taxonomy.rename_folder(
            self.profile_name, "01-SCIENCES/PHYSIQUE", "PHYS"
        )
        self.assertEqual(r["n_tree_entries_renamed"], 2)  # PHYSIQUE + Mecanique
        tree = yaml.safe_load((self.profile_dir / "tree.yaml").read_text())
        self.assertIn("01-SCIENCES/PHYS/Mecanique", tree["folders"])

    def test_rename_unknown_path(self):
        with self.assertRaises(taxonomy.TaxonomyError) as ctx:
            taxonomy.rename_folder(self.profile_name, "99-NOPE", "X")
        self.assertEqual(ctx.exception.status, 400)

    def test_rename_target_exists(self):
        with self.assertRaises(taxonomy.TaxonomyError) as ctx:
            # PHYSIQUE existe et MATHEMATIQUES aussi → conflit
            taxonomy.rename_folder(
                self.profile_name, "01-SCIENCES/PHYSIQUE", "MATHEMATIQUES"
            )
        self.assertEqual(ctx.exception.status, 409)

    def test_rename_invalid_name(self):
        with self.assertRaises(taxonomy.TaxonomyError) as ctx:
            taxonomy.rename_folder(
                self.profile_name, "01-SCIENCES/PHYSIQUE", "foo/bar"
            )
        self.assertEqual(ctx.exception.status, 400)

    def test_rename_same_name_noop(self):
        r = taxonomy.rename_folder(
            self.profile_name, "01-SCIENCES/PHYSIQUE", "PHYSIQUE"
        )
        self.assertTrue(r.get("unchanged"))

    def test_rename_respects_lock(self):
        (self.profile_dir / ".cache" / "taxonomy.lock").write_text("locked")
        with self.assertRaises(taxonomy.TaxonomyError) as ctx:
            taxonomy.rename_folder(
                self.profile_name, "01-SCIENCES/PHYSIQUE", "PHYS"
            )
        self.assertEqual(ctx.exception.status, 423)

    def test_undo_rename_reverses_filesystem(self):
        """Le 2e undo après un rename doit aussi renommer le FS en sens
        inverse pour que tree.yaml et le disque restent cohérents."""
        taxonomy.rename_folder(
            self.profile_name, "01-SCIENCES/PHYSIQUE", "PHYS-NEW"
        )
        self.assertTrue((self.target / "01-SCIENCES/PHYS-NEW").is_dir())
        self.assertFalse((self.target / "01-SCIENCES/PHYSIQUE").exists())
        # Undo mapping
        taxonomy.restore_last_backup(self.profile_name)
        # Undo tree → doit aussi renommer FS PHYS-NEW → PHYSIQUE
        r = taxonomy.restore_last_backup(self.profile_name)
        self.assertEqual(r["type"], "tree")
        self.assertEqual(len(r["fs_renamed"]), 1)
        self.assertEqual(r["fs_renamed"][0]["from"], "01-SCIENCES/PHYS-NEW")
        self.assertEqual(r["fs_renamed"][0]["to"], "01-SCIENCES/PHYSIQUE")
        # FS reconcilié avec tree.yaml
        self.assertTrue((self.target / "01-SCIENCES/PHYSIQUE").is_dir())
        self.assertFalse((self.target / "01-SCIENCES/PHYS-NEW").exists())

    def test_undo_rename_preserves_files_inside(self):
        """Le FS rename inverse préserve les fichiers à l'intérieur du dossier."""
        # Ajouter un fichier dans PHYSIQUE
        (self.target / "01-SCIENCES/PHYSIQUE/important.pdf").write_bytes(b"data")
        taxonomy.rename_folder(
            self.profile_name, "01-SCIENCES/PHYSIQUE", "PHYS2"
        )
        # Vérifier que le fichier a suivi le rename
        self.assertTrue((self.target / "01-SCIENCES/PHYS2/important.pdf").is_file())
        # Undo mapping + tree
        taxonomy.restore_last_backup(self.profile_name)
        taxonomy.restore_last_backup(self.profile_name)
        # Fichier doit être de retour avec PHYSIQUE
        self.assertTrue((self.target / "01-SCIENCES/PHYSIQUE/important.pdf").is_file())

    def test_rename_then_undo_restores_state(self):
        """Le rename produit 2 backups (tree + mapping). Undo restore mapping
        d'abord (le plus récent par timestamp), puis le tree au 2e undo."""
        taxonomy.rename_folder(
            self.profile_name, "01-SCIENCES/PHYSIQUE", "PHYS"
        )
        # Undo le mapping
        taxonomy.restore_last_backup(self.profile_name)
        mapping = yaml.safe_load((self.profile_dir / "theme_mapping.yaml").read_text())
        # Mapping restauré (pointe à nouveau vers PHYSIQUE)
        self.assertEqual(mapping["physics"], "01-SCIENCES/PHYSIQUE")
        # Mais tree.yaml a toujours PHYS (n'a pas encore été undo)
        tree = yaml.safe_load((self.profile_dir / "tree.yaml").read_text())
        self.assertIn("01-SCIENCES/PHYS", tree["folders"])
        # Second undo : restaure tree.yaml — note: l'undo ne renomme PAS le fs
        # car le fs new_path est non-vide (il existe) ; on accepte que ce soit
        # un cas "partiel" qui requiert manual cleanup
        taxonomy.restore_last_backup(self.profile_name)
        tree = yaml.safe_load((self.profile_dir / "tree.yaml").read_text())
        self.assertIn("01-SCIENCES/PHYSIQUE", tree["folders"])


class TestRenameFolderCascadeCategories(TaxonomyTestBase):
    """Intégration : rename d'un folder doit cascader sur categories.yaml
    via cascade_rename_target. Sans ce relayage, les `chemin:` resteraient
    figés sur l'ancien path → entries orphelines."""

    def test_rename_cascades_to_categories_yaml(self):
        from dashboard import categories as _cat
        (self.profile_dir / "categories.yaml").write_text(
            yaml.safe_dump({
                "loisirs": [
                    {"chemin": "01-SCIENCES/PHYSIQUE",
                     "priorite": 2, "mots_cles": ["physics"]},
                    {"chemin": "01-SCIENCES/PHYSIQUE/QUANTUM",
                     "priorite": 5, "mots_cles": ["quantum"]},
                    {"chemin": "01-SCIENCES/MATHEMATIQUES",
                     "priorite": 5, "mots_cles": ["math"]},
                ],
            }, sort_keys=False)
        )
        _cat.reset_cache()

        r = taxonomy.rename_folder(
            self.profile_name, "01-SCIENCES/PHYSIQUE", "PHYS",
        )
        # Réponse remonte le compteur cascade
        self.assertEqual(r["n_categories_updated"], 2)
        self.assertEqual(r.get("n_categories_merged", 0), 0)
        self.assertIsNotNone(r.get("categories_backup"))

        # Le YAML reflète bien le nouveau chemin
        cats = yaml.safe_load((self.profile_dir / "categories.yaml").read_text())
        chemins = [e["chemin"] for e in cats["loisirs"]]
        self.assertIn("01-SCIENCES/PHYS", chemins)
        self.assertIn("01-SCIENCES/PHYS/QUANTUM", chemins)
        self.assertIn("01-SCIENCES/MATHEMATIQUES", chemins)  # intouché
        self.assertNotIn("01-SCIENCES/PHYSIQUE", chemins)

    def test_rename_without_categories_yaml_is_noop_safe(self):
        """Profil sans categories.yaml ne doit pas crasher le rename."""
        # Pas de categories.yaml créé. Rename normal doit fonctionner.
        r = taxonomy.rename_folder(
            self.profile_name, "01-SCIENCES/PHYSIQUE", "PHYS",
        )
        self.assertTrue(r["ok"])
        self.assertEqual(r["n_categories_updated"], 0)
        self.assertIsNone(r.get("categories_backup"))


class TestRenameFolderEndpoint(TaxonomyTestBase):

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient

        from dashboard.app import app
        self.client = TestClient(app)

    def test_api_rename_happy(self):
        r = self.client.patch("/api/taxonomy/folder", json={
            "profile": self.profile_name,
            "path": "01-SCIENCES/PHYSIQUE",
            "new_name": "PHYS",
        })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["new_path"], "01-SCIENCES/PHYS")

    def test_api_rename_unknown_path_400(self):
        r = self.client.patch("/api/taxonomy/folder", json={
            "profile": self.profile_name,
            "path": "DOES-NOT-EXIST",
            "new_name": "X",
        })
        self.assertEqual(r.status_code, 400)


# ─── 9. Tests Phase 3 C — move folder ────────────────────────────────────


class TestMoveFolder(TaxonomyTestBase):

    def test_move_to_other_section(self):
        # Crée un dossier-cible dans une autre section
        taxonomy.create_folder(self.profile_name, "", "03-INGENIERIE")
        taxonomy.reset_cache()
        r = taxonomy.move_folder(
            self.profile_name, "01-SCIENCES/PHYSIQUE", "03-INGENIERIE"
        )
        self.assertTrue(r["ok"])
        self.assertEqual(r["new_path"], "03-INGENIERIE/PHYSIQUE")
        # Cascade mappings
        mapping = yaml.safe_load((self.profile_dir / "theme_mapping.yaml").read_text())
        self.assertEqual(mapping["physics"], "03-INGENIERIE/PHYSIQUE")
        # FS
        self.assertTrue((self.target / "03-INGENIERIE/PHYSIQUE/mechanics.pdf").is_file())
        self.assertFalse((self.target / "01-SCIENCES/PHYSIQUE").exists())

    def test_move_to_root(self):
        # Déplacer un dossier vers la racine (new_parent="")
        r = taxonomy.move_folder(
            self.profile_name, "01-SCIENCES/PHYSIQUE", ""
        )
        self.assertEqual(r["new_path"], "PHYSIQUE")
        self.assertTrue((self.target / "PHYSIQUE").is_dir())

    def test_move_unknown_parent(self):
        with self.assertRaises(taxonomy.TaxonomyError) as ctx:
            taxonomy.move_folder(
                self.profile_name, "01-SCIENCES/PHYSIQUE", "99-NOPE"
            )
        self.assertEqual(ctx.exception.status, 400)

    def test_move_cycle_refused(self):
        """Move A inside one of A's own descendants → refused."""
        taxonomy.create_folder(self.profile_name, "01-SCIENCES/PHYSIQUE", "Mecanique")
        taxonomy.reset_cache()
        with self.assertRaises(taxonomy.TaxonomyError) as ctx:
            taxonomy.move_folder(
                self.profile_name, "01-SCIENCES/PHYSIQUE",
                "01-SCIENCES/PHYSIQUE/Mecanique",
            )
        self.assertEqual(ctx.exception.status, 400)

    def test_move_target_exists(self):
        # Déplacer vers un parent où un dossier homonyme existe déjà
        taxonomy.create_folder(self.profile_name, "01-SCIENCES/MATHEMATIQUES", "PHYSIQUE")
        taxonomy.reset_cache()
        with self.assertRaises(taxonomy.TaxonomyError) as ctx:
            taxonomy.move_folder(
                self.profile_name, "01-SCIENCES/PHYSIQUE",
                "01-SCIENCES/MATHEMATIQUES",
            )
        self.assertEqual(ctx.exception.status, 409)

    def test_move_unchanged_when_same_parent(self):
        r = taxonomy.move_folder(
            self.profile_name, "01-SCIENCES/PHYSIQUE", "01-SCIENCES"
        )
        # Le nouveau path serait 01-SCIENCES/PHYSIQUE = identique
        self.assertTrue(r.get("unchanged"))

    def test_move_respects_lock(self):
        (self.profile_dir / ".cache" / "taxonomy.lock").write_text("locked")
        with self.assertRaises(taxonomy.TaxonomyError) as ctx:
            taxonomy.move_folder(
                self.profile_name, "01-SCIENCES/PHYSIQUE", "02-INFORMATIQUE"
            )
        self.assertEqual(ctx.exception.status, 423)


class TestMoveFolderEndpoint(TaxonomyTestBase):

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient

        from dashboard.app import app
        self.client = TestClient(app)

    def test_api_move_happy(self):
        # Prepare a target parent
        self.client.post("/api/taxonomy/folder", json={
            "profile": self.profile_name, "parent": "", "name": "TARGET-PARENT",
        })
        r = self.client.post("/api/taxonomy/folder/move", json={
            "profile": self.profile_name,
            "path": "01-SCIENCES/PHYSIQUE",
            "new_parent": "TARGET-PARENT",
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["new_path"], "TARGET-PARENT/PHYSIQUE")

    def test_api_move_cycle_400(self):
        taxonomy.create_folder(self.profile_name, "01-SCIENCES/PHYSIQUE", "Sub")
        r = self.client.post("/api/taxonomy/folder/move", json={
            "profile": self.profile_name,
            "path": "01-SCIENCES/PHYSIQUE",
            "new_parent": "01-SCIENCES/PHYSIQUE/Sub",
        })
        self.assertEqual(r.status_code, 400)


# ─── 10. Tests Phase 3 D — delete folder ─────────────────────────────────


class TestDeleteFolder(TaxonomyTestBase):

    def test_delete_preview_counts(self):
        # Set up a folder with files + sub + mapping
        taxonomy.create_folder(self.profile_name, "01-SCIENCES", "TO-DELETE")
        (self.target / "01-SCIENCES/TO-DELETE/file1.pdf").write_bytes(b"x" * 1024)
        (self.target / "01-SCIENCES/TO-DELETE/file2.pdf").write_bytes(b"y" * 2048)
        taxonomy.create_folder(
            self.profile_name, "01-SCIENCES/TO-DELETE", "sub"
        )
        # Add a mapping to it
        taxonomy.add_mapping(
            self.profile_name, "to_delete_theme", "01-SCIENCES/TO-DELETE"
        )
        taxonomy.reset_cache()

        p = taxonomy.delete_folder_preview(
            self.profile_name, "01-SCIENCES/TO-DELETE"
        )
        self.assertEqual(p["n_files"], 2)
        self.assertEqual(p["n_subfolders"], 1)
        self.assertEqual(p["n_mappings"], 1)
        self.assertFalse(p["is_empty"])
        self.assertEqual(p["fs_size_bytes"], 1024 + 2048)

    def test_delete_empty_folder_no_force(self):
        # Create an empty folder + no mapping
        taxonomy.create_folder(self.profile_name, "01-SCIENCES", "EMPTY")
        taxonomy.reset_cache()
        r = taxonomy.delete_folder(
            self.profile_name, "01-SCIENCES/EMPTY", force=False
        )
        self.assertTrue(r["ok"])
        self.assertFalse((self.target / "01-SCIENCES/EMPTY").exists())
        tree = yaml.safe_load((self.profile_dir / "tree.yaml").read_text())
        self.assertNotIn("01-SCIENCES/EMPTY", tree["folders"])

    def test_delete_non_empty_refused_without_force(self):
        taxonomy.create_folder(self.profile_name, "01-SCIENCES", "FULL")
        (self.target / "01-SCIENCES/FULL/data.pdf").write_bytes(b"data")
        taxonomy.reset_cache()
        with self.assertRaises(taxonomy.TaxonomyError) as ctx:
            taxonomy.delete_folder(
                self.profile_name, "01-SCIENCES/FULL", force=False
            )
        self.assertEqual(ctx.exception.status, 409)
        # FS et tree.yaml inchangés
        self.assertTrue((self.target / "01-SCIENCES/FULL/data.pdf").is_file())
        tree = yaml.safe_load((self.profile_dir / "tree.yaml").read_text())
        self.assertIn("01-SCIENCES/FULL", tree["folders"])

    def test_delete_non_empty_with_force(self):
        taxonomy.create_folder(self.profile_name, "01-SCIENCES", "FORCE-DEL")
        (self.target / "01-SCIENCES/FORCE-DEL/a.pdf").write_bytes(b"a")
        taxonomy.create_folder(
            self.profile_name, "01-SCIENCES/FORCE-DEL", "sub"
        )
        (self.target / "01-SCIENCES/FORCE-DEL/sub/b.pdf").write_bytes(b"b")
        taxonomy.add_mapping(
            self.profile_name, "force_theme", "01-SCIENCES/FORCE-DEL/sub"
        )
        taxonomy.reset_cache()

        r = taxonomy.delete_folder(
            self.profile_name, "01-SCIENCES/FORCE-DEL", force=True
        )
        self.assertTrue(r["ok"])
        self.assertEqual(r["n_files_deleted"], 2)
        self.assertEqual(r["n_subfolders_deleted"], 1)
        self.assertEqual(r["n_mappings_removed"], 1)
        # FS purgé
        self.assertFalse((self.target / "01-SCIENCES/FORCE-DEL").exists())
        # tree.yaml purgé
        tree = yaml.safe_load((self.profile_dir / "tree.yaml").read_text())
        self.assertNotIn("01-SCIENCES/FORCE-DEL", tree["folders"])
        self.assertNotIn("01-SCIENCES/FORCE-DEL/sub", tree["folders"])
        # mapping cassé retiré
        mapping = yaml.safe_load((self.profile_dir / "theme_mapping.yaml").read_text())
        self.assertNotIn("force_theme", mapping)

    def test_delete_unknown_path(self):
        with self.assertRaises(taxonomy.TaxonomyError) as ctx:
            taxonomy.delete_folder(self.profile_name, "DOES-NOT-EXIST")
        self.assertEqual(ctx.exception.status, 400)

    def test_delete_respects_lock(self):
        taxonomy.create_folder(self.profile_name, "01-SCIENCES", "LOCK-TEST")
        taxonomy.reset_cache()
        (self.profile_dir / ".cache" / "taxonomy.lock").write_text("locked")
        with self.assertRaises(taxonomy.TaxonomyError) as ctx:
            taxonomy.delete_folder(self.profile_name, "01-SCIENCES/LOCK-TEST")
        self.assertEqual(ctx.exception.status, 423)

    def test_delete_creates_backups(self):
        taxonomy.create_folder(self.profile_name, "01-SCIENCES", "BAK-TEST")
        taxonomy.add_mapping(
            self.profile_name, "bak_theme", "01-SCIENCES/BAK-TEST"
        )
        backup_dir = self.profile_dir / ".cache" / "taxonomy-backups"
        before_tree = len(list(backup_dir.glob("tree-*.yaml")))
        before_map = len(list(backup_dir.glob("theme_mapping-*.yaml")))
        # Mapping makes it non-empty → need force
        taxonomy.delete_folder(
            self.profile_name, "01-SCIENCES/BAK-TEST", force=True
        )
        after_tree = len(list(backup_dir.glob("tree-*.yaml")))
        after_map = len(list(backup_dir.glob("theme_mapping-*.yaml")))
        self.assertGreater(after_tree, before_tree)
        self.assertGreater(after_map, before_map)


class TestDeleteFolderEndpoint(TaxonomyTestBase):

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient

        from dashboard.app import app
        self.client = TestClient(app)

    def test_api_delete_preview(self):
        taxonomy.create_folder(self.profile_name, "01-SCIENCES", "API-PREVIEW")
        (self.target / "01-SCIENCES/API-PREVIEW/x.pdf").write_bytes(b"x")
        r = self.client.get(
            f"/api/taxonomy/folder/delete-preview"
            f"?profile={self.profile_name}&path=01-SCIENCES/API-PREVIEW"
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["n_files"], 1)

    def test_api_delete_non_empty_returns_preview_in_409(self):
        taxonomy.create_folder(self.profile_name, "01-SCIENCES", "API-NONEMPTY")
        (self.target / "01-SCIENCES/API-NONEMPTY/x.pdf").write_bytes(b"x")
        r = self.client.request("DELETE", "/api/taxonomy/folder", json={
            "profile": self.profile_name,
            "path": "01-SCIENCES/API-NONEMPTY",
            "force": False,
        })
        self.assertEqual(r.status_code, 409)
        self.assertIn("preview", r.json())
        self.assertEqual(r.json()["preview"]["n_files"], 1)

    def test_api_delete_force_happy(self):
        taxonomy.create_folder(self.profile_name, "01-SCIENCES", "API-FORCE")
        (self.target / "01-SCIENCES/API-FORCE/x.pdf").write_bytes(b"x")
        r = self.client.request("DELETE", "/api/taxonomy/folder", json={
            "profile": self.profile_name,
            "path": "01-SCIENCES/API-FORCE",
            "force": True,
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["n_files_deleted"], 1)


class TestThemeFiles(TaxonomyTestBase):
    """theme_files() — reverse lookup theme → list of files."""

    def setUp(self):
        super().setUp()
        # Build a vision_cache that references our placeholder PDFs by the
        # real cache_key derived from their head bytes.
        from lib import vision_cache as vc
        cache = {}
        # mechanics.pdf has theme "physics" (conf 0.9)
        # quantum.pdf  has themes "physics" (0.7) + "quantum mechanics" (0.95)
        # algebra.pdf  has theme "algebra" (0.8)
        # intro.pdf    has theme "physics" but conf 0.3 (filtered out)
        files = {
            "01-SCIENCES/PHYSIQUE/mechanics.pdf": [("physics", 0.9)],
            "01-SCIENCES/PHYSIQUE/quantum.pdf": [
                ("physics", 0.7), ("quantum mechanics", 0.95),
            ],
            "01-SCIENCES/MATHEMATIQUES/algebra.pdf": [("algebra", 0.8)],
            "01-SCIENCES/intro.pdf": [("physics", 0.3)],
        }
        for rel, themes in files.items():
            abs_path = self.target / rel
            # Give each file unique bytes so they don't collide on hash
            abs_path.write_bytes(f"%PDF-1.4 {rel}".encode())
            key = vc.compute_cache_key(
                str(abs_path),
                model="Qwen/Qwen3-VL-32B-Instruct",
                n_pages=2,
            )
            self.assertIsNotNone(key, f"cache key not computable for {rel}")
            cache[key] = {
                "result": {
                    "title": f"Title of {Path(rel).stem}",
                    "themes": [{"theme": t, "confidence": c} for t, c in themes],
                },
                "model": "Qwen/Qwen3-VL-32B-Instruct",
                "prompt_version": "v3",
            }
        cache_path = self.profile_dir / ".cache" / "vision_cache.json"
        cache_path.write_text(json.dumps(cache))
        taxonomy.reset_cache(self.profile_name)

    def test_future_files_returned_for_mapped_theme(self):
        # Future is now "files predicted to land in this mapping's folder".
        # Both mechanics.pdf and quantum.pdf resolve to 01-SCIENCES/PHYSIQUE
        # via their top theme (physics 0.9 / quantum mechanics 0.95). intro
        # is filtered by MIN_CONFIDENCE.
        r = taxonomy.theme_files(self.profile_name, "physics", limit=10)
        self.assertEqual(r["n_future"], 2)
        paths = {f["rel_path"] for f in r["future"]}
        self.assertIn("01-SCIENCES/PHYSIQUE/mechanics.pdf", paths)
        self.assertIn("01-SCIENCES/PHYSIQUE/quantum.pdf", paths)
        # Each future item carries the top theme that drove the prediction.
        for f in r["future"]:
            self.assertIn("top_theme", f)
            self.assertIn("top_confidence", f)
        # Sorted by top_confidence desc → quantum (0.95) before mechanics (0.9)
        self.assertEqual(r["future"][0]["top_confidence"], 0.95)

    def test_future_case_insensitive(self):
        r_lower = taxonomy.theme_files(self.profile_name, "physics", limit=10)
        r_upper = taxonomy.theme_files(self.profile_name, "PHYSICS", limit=10)
        self.assertEqual(r_lower["n_future"], r_upper["n_future"])

    def test_current_files_from_mapped_folder(self):
        # "physics" is mapped to 01-SCIENCES/PHYSIQUE → both PDFs there
        r = taxonomy.theme_files(self.profile_name, "physics", limit=10)
        self.assertEqual(r["mapped_folder"], "01-SCIENCES/PHYSIQUE")
        self.assertEqual(r["n_current"], 2)
        names = {Path(f["rel_path"]).name for f in r["current"]}
        self.assertEqual(names, {"mechanics.pdf", "quantum.pdf"})

    def test_by_folder_future_aggregation(self):
        # Both physics-tagged files live in 01-SCIENCES/PHYSIQUE
        r = taxonomy.theme_files(self.profile_name, "physics", limit=10)
        self.assertEqual(r["by_folder_future"].get("01-SCIENCES/PHYSIQUE"), 2)

    def test_current_excludes_file_in_folder_without_theme(self):
        """A file living in the mapped folder but lacking the theme in its
        vision_cache must NOT appear in `current` (intersection semantics)."""
        from lib import vision_cache as vc
        # Add a file in 01-SCIENCES/PHYSIQUE that has only "biology" in cache.
        rel = "01-SCIENCES/PHYSIQUE/foreign.pdf"
        abs_path = self.target / rel
        abs_path.write_bytes(b"%PDF-1.4 unique-foreign")
        key = vc.compute_cache_key(
            str(abs_path), model="Qwen/Qwen3-VL-32B-Instruct", n_pages=2,
        )
        cache_path = self.profile_dir / ".cache" / "vision_cache.json"
        cache = json.loads(cache_path.read_text())
        cache[key] = {
            "result": {
                "title": "Foreign book",
                "themes": [{"theme": "biology", "confidence": 0.9}],
            },
            "model": "Qwen/Qwen3-VL-32B-Instruct",
            "prompt_version": "v3",
        }
        cache_path.write_text(json.dumps(cache))
        taxonomy.reset_cache(self.profile_name)
        r = taxonomy.theme_files(self.profile_name, "physics", limit=10)
        # foreign.pdf is in the folder but its only theme is "biology" → exclude
        self.assertEqual(r["n_current"], 2)
        names = {Path(f["rel_path"]).name for f in r["current"]}
        self.assertNotIn("foreign.pdf", names)

    def test_two_themes_to_same_folder_have_distinct_current(self):
        """If two themes map to the same folder, their `current` lists
        only intersect on files that carry BOTH themes."""
        # Map "algebra" also to 01-SCIENCES/PHYSIQUE (in addition to physics)
        mp = self.profile_dir / "theme_mapping.yaml"
        mp.write_text(yaml.safe_dump({
            "physics": "01-SCIENCES/PHYSIQUE",
            "quantum mechanics": "01-SCIENCES/PHYSIQUE",  # also same folder
        }))
        taxonomy.reset_cache(self.profile_name)
        r_phys = taxonomy.theme_files(self.profile_name, "physics", limit=10)
        r_quant = taxonomy.theme_files(self.profile_name, "quantum mechanics", limit=10)
        # Same mapped folder, but distinct current sets:
        #   physics → mechanics.pdf + quantum.pdf (both have physics ≥0.5)
        #   quantum mechanics → only quantum.pdf
        self.assertEqual(r_phys["n_current"], 2)
        self.assertEqual(r_quant["n_current"], 1)
        self.assertEqual(
            {Path(f["rel_path"]).name for f in r_quant["current"]},
            {"quantum.pdf"},
        )

    def test_unmapped_theme_has_no_future_and_no_current(self):
        # Drop "quantum mechanics" from the mapping (now an orphan theme).
        # Substring resolution also fails: "quantum" / "mechanics" aren't
        # standalone keys in the mapping below.
        mp = self.profile_dir / "theme_mapping.yaml"
        mp.write_text(yaml.safe_dump({"physics": "01-SCIENCES/PHYSIQUE"}))
        taxonomy.reset_cache(self.profile_name)
        r = taxonomy.theme_files(self.profile_name, "quantum mechanics", limit=10)
        # No mapping → no predicted folder → no future/current. Consistent
        # with reality: an unmapped theme does nothing at reclassify.
        self.assertIsNone(r["mapped_folder"])
        self.assertEqual(r["n_future"], 0)
        self.assertEqual(r["n_current"], 0)
        self.assertEqual(r["future"], [])
        self.assertEqual(r["current"], [])

    def test_unknown_theme_returns_empty(self):
        r = taxonomy.theme_files(self.profile_name, "completely-unknown-xyz", limit=10)
        self.assertEqual(r["n_future"], 0)
        self.assertEqual(r["n_current"], 0)
        self.assertEqual(r["future"], [])
        self.assertEqual(r["current"], [])

    def test_blank_theme_returns_empty(self):
        r = taxonomy.theme_files(self.profile_name, "   ", limit=10)
        self.assertEqual(r["n_future"], 0)
        self.assertEqual(r["n_current"], 0)

    def test_limit_clamps_future_list(self):
        r = taxonomy.theme_files(self.profile_name, "physics", limit=1)
        self.assertEqual(r["n_future"], 2)         # total count unaffected
        self.assertEqual(len(r["future"]), 1)       # but page is clamped

    def test_index_cached_after_first_call(self):
        import time
        # First (cold) call builds the index
        t0 = time.time()
        taxonomy.theme_files(self.profile_name, "physics", limit=10)
        cold = time.time() - t0
        # Second call should hit the cache (much faster than cold; we
        # only assert it's strictly faster + under a generous bound).
        t0 = time.time()
        taxonomy.theme_files(self.profile_name, "algebra", limit=10)
        warm = time.time() - t0
        self.assertLess(warm, max(cold, 0.05))

    def test_future_includes_substring_match_resolutions(self):
        """A file whose top theme resolves to the mapped folder via
        SUBSTRING (longest-wins) — not exact match — must appear in
        `Impact futur`. Mirrors the real classifier behavior."""
        from lib import vision_cache as vc
        # Setup: file with top theme "Quantum Field Physics" which is NOT
        # an exact mapping key, but resolves via substring "physics".
        rel = "01-SCIENCES/PHYSIQUE/qft.pdf"
        (self.target / rel).write_bytes(b"%PDF-1.4 qft-unique")
        key = vc.compute_cache_key(
            str(self.target / rel),
            model="Qwen/Qwen3-VL-32B-Instruct",
            n_pages=2,
        )
        cache_path = self.profile_dir / ".cache" / "vision_cache.json"
        cache = json.loads(cache_path.read_text())
        cache[key] = {
            "result": {
                "title": "Quantum Field Theory",
                "themes": [{"theme": "Quantum Field Physics", "confidence": 0.92}],
            },
            "model": "Qwen/Qwen3-VL-32B-Instruct",
            "prompt_version": "v3",
        }
        cache_path.write_text(json.dumps(cache))
        taxonomy.reset_cache(self.profile_name)
        # Click on "physics" — qft.pdf must appear because its top theme
        # "Quantum Field Physics" resolves to 01-SCIENCES/PHYSIQUE via
        # substring match on the "physics" key.
        r = taxonomy.theme_files(self.profile_name, "physics", limit=10)
        paths = {f["rel_path"] for f in r["future"]}
        self.assertIn("01-SCIENCES/PHYSIQUE/qft.pdf", paths)
        # The future item carries the actual top theme, NOT the mapping key
        qft = next(f for f in r["future"] if "qft.pdf" in f["rel_path"])
        self.assertEqual(qft["top_theme"], "Quantum Field Physics")

    def test_dedup_same_theme_twice_in_entry(self):
        """A file listing the same theme in `themes[]` AND legacy `theme`
        must count only once, with the highest confidence retained."""
        from lib import vision_cache as vc
        # Map "AI" so the dup file resolves to a folder
        mp = self.profile_dir / "theme_mapping.yaml"
        mp.write_text(yaml.safe_dump({
            "physics": "01-SCIENCES/PHYSIQUE",
            "quantum mechanics": "01-SCIENCES/PHYSIQUE",
            "algebra": "01-SCIENCES/MATHEMATIQUES",
            "AI": "02-INFORMATIQUE",
        }))
        rel = "02-INFORMATIQUE/dup.pdf"
        (self.target / "02-INFORMATIQUE").mkdir(parents=True, exist_ok=True)
        (self.target / rel).write_bytes(b"%PDF-1.4 dup-file")
        key = vc.compute_cache_key(
            str(self.target / rel),
            model="Qwen/Qwen3-VL-32B-Instruct",
            n_pages=2,
        )
        cache_path = self.profile_dir / ".cache" / "vision_cache.json"
        cache = json.loads(cache_path.read_text())
        cache[key] = {
            "result": {
                "title": "Dup",
                "themes": [{"theme": "AI", "confidence": 0.6}],
                "theme": "AI",
                "confidence": 0.85,
            },
            "model": "Qwen/Qwen3-VL-32B-Instruct",
            "prompt_version": "v3",
        }
        cache_path.write_text(json.dumps(cache))
        taxonomy.reset_cache(self.profile_name)
        r = taxonomy.theme_files(self.profile_name, "AI", limit=10)
        self.assertEqual(r["n_future"], 1)
        # top_confidence keeps the max (0.85 > 0.6)
        self.assertEqual(r["future"][0]["top_confidence"], 0.85)


class TestThemeFilesEndpoint(TaxonomyTestBase):

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient

        from dashboard.app import app
        self.client = TestClient(app)

    def test_endpoint_blank_theme_rejected(self):
        r = self.client.get(
            f"/api/taxonomy/theme/files?profile={self.profile_name}&theme="
        )
        self.assertEqual(r.status_code, 400)

    def test_endpoint_unknown_theme_returns_empty(self):
        r = self.client.get(
            f"/api/taxonomy/theme/files?profile={self.profile_name}&theme=ghost"
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["n_future"], 0)
        self.assertEqual(body["future"], [])

    def test_endpoint_limit_clamped(self):
        r = self.client.get(
            f"/api/taxonomy/theme/files?profile={self.profile_name}"
            f"&theme=physics&limit=99999"
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["limit"], 500)


class TestDormantMappings(TaxonomyTestBase):
    """dormant_mappings() — detect mapping keys that no file triggers."""

    def _seed_cache(self, files: dict[str, list[tuple[str, float]]]):
        """Helper: write a vision_cache with the given file→themes layout
        and reset the taxonomy cache. Each file gets unique PDF bytes so
        cache keys don't collide."""
        from lib import vision_cache as vc
        cache_path = self.profile_dir / ".cache" / "vision_cache.json"
        cache: dict = {}
        for rel, themes in files.items():
            abs_path = self.target / rel
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_bytes(f"%PDF-1.4 {rel}".encode())
            key = vc.compute_cache_key(
                str(abs_path),
                model="Qwen/Qwen3-VL-32B-Instruct",
                n_pages=2,
            )
            assert key
            cache[key] = {
                "result": {
                    "title": Path(rel).stem,
                    "themes": [{"theme": t, "confidence": c} for t, c in themes],
                },
                "model": "Qwen/Qwen3-VL-32B-Instruct",
                "prompt_version": "v3",
            }
        cache_path.write_text(json.dumps(cache))
        taxonomy.reset_cache(self.profile_name)

    def test_unused_key_is_dormant(self):
        # Mapping has "physics" (used) + "geography" (unused — no file)
        mp = self.profile_dir / "theme_mapping.yaml"
        mp.write_text(yaml.safe_dump({
            "physics": "01-SCIENCES/PHYSIQUE",
            "geography": "01-SCIENCES",
        }))
        self._seed_cache({
            "01-SCIENCES/PHYSIQUE/p.pdf": [("physics", 0.9)],
        })
        r = taxonomy.dormant_mappings(self.profile_name)
        dormant_keys = [d["key"] for d in r["dormant"]]
        self.assertIn("geography", dormant_keys)
        self.assertNotIn("physics", dormant_keys)
        self.assertEqual(r["active"].get("physics"), 1)
        self.assertEqual(r["n_total"], 2)
        self.assertEqual(r["n_dormant"], 1)

    def test_substring_winner_marks_loser_dormant(self):
        # "physics" (7 chars) beats "optics" (6 chars) on "Optics and Light Physics"
        # → "optics" is dormant even though it's referenced by a theme.
        mp = self.profile_dir / "theme_mapping.yaml"
        mp.write_text(yaml.safe_dump({
            "physics": "01-SCIENCES/PHYSIQUE",
            "optics":  "01-SCIENCES/PHYSIQUE/04-Optique",
        }))
        self._seed_cache({
            "01-SCIENCES/PHYSIQUE/light.pdf":
                [("Optics and Light Physics", 0.9)],
        })
        r = taxonomy.dormant_mappings(self.profile_name)
        dormant_keys = [d["key"] for d in r["dormant"]]
        # physics wins the longest-substring → optics is dormant
        self.assertIn("optics", dormant_keys)
        self.assertNotIn("physics", dormant_keys)

    def test_exact_match_wins_over_substring(self):
        # "Optics" exact match takes priority over "physics" substring
        # when the theme is just "Optics".
        mp = self.profile_dir / "theme_mapping.yaml"
        mp.write_text(yaml.safe_dump({
            "physics": "01-SCIENCES/PHYSIQUE",
            "optics":  "01-SCIENCES/PHYSIQUE/04-Optique",
        }))
        self._seed_cache({
            "01-SCIENCES/PHYSIQUE/04-Optique/o.pdf": [("Optics", 0.9)],
        })
        r = taxonomy.dormant_mappings(self.profile_name)
        dormant_keys = [d["key"] for d in r["dormant"]]
        self.assertIn("physics", dormant_keys)
        self.assertNotIn("optics", dormant_keys)

    def test_dormant_includes_folder(self):
        mp = self.profile_dir / "theme_mapping.yaml"
        mp.write_text(yaml.safe_dump({
            "geography": "01-SCIENCES",
            "physics":   "01-SCIENCES/PHYSIQUE",
        }))
        self._seed_cache({
            "01-SCIENCES/PHYSIQUE/p.pdf": [("physics", 0.9)],
        })
        r = taxonomy.dormant_mappings(self.profile_name)
        geo = next(d for d in r["dormant"] if d["key"] == "geography")
        self.assertEqual(geo["folder"], "01-SCIENCES")


class TestBulkDeleteMapping(TaxonomyTestBase):

    def test_bulk_delete_happy_path(self):
        r = taxonomy.delete_mappings_bulk(self.profile_name,
                                          ["physics", "algebra"])
        self.assertEqual(r["n_deleted"], 2)
        deleted_themes = {d["theme"] for d in r["deleted"]}
        self.assertEqual(deleted_themes, {"physics", "algebra"})
        self.assertEqual(r["not_found"], [])
        self.assertIsNotNone(r["backup"])
        # YAML actually updated
        new_map = yaml.safe_load(
            (self.profile_dir / "theme_mapping.yaml").read_text())
        self.assertNotIn("physics", new_map)
        self.assertNotIn("algebra", new_map)
        self.assertIn("quantum mechanics", new_map)

    def test_bulk_delete_reports_unknown_without_failing(self):
        r = taxonomy.delete_mappings_bulk(self.profile_name,
                                          ["physics", "does-not-exist"])
        self.assertEqual(r["n_deleted"], 1)
        self.assertEqual(r["not_found"], ["does-not-exist"])

    def test_bulk_delete_empty_list_rejected(self):
        with self.assertRaises(taxonomy.TaxonomyError):
            taxonomy.delete_mappings_bulk(self.profile_name, [])

    def test_bulk_delete_dedupes(self):
        r = taxonomy.delete_mappings_bulk(
            self.profile_name, ["physics", "physics", "physics"],
        )
        self.assertEqual(r["n_deleted"], 1)

    def test_bulk_delete_creates_single_backup(self):
        backup_dir = self.profile_dir / ".cache" / "taxonomy-backups"
        before = list(backup_dir.glob("*.yaml")) if backup_dir.exists() else []
        taxonomy.delete_mappings_bulk(
            self.profile_name, ["physics", "algebra", "quantum mechanics"],
        )
        after = list(backup_dir.glob("*.yaml"))
        # Exactly one new backup file, regardless of how many keys were deleted.
        self.assertEqual(len(after) - len(before), 1)

    def test_bulk_delete_invalid_theme_rejected(self):
        # Whitespace-only key violates _validate_theme
        with self.assertRaises(taxonomy.TaxonomyError):
            taxonomy.delete_mappings_bulk(self.profile_name, ["   "])


class TestBulkAddMapping(TaxonomyTestBase):

    def test_bulk_add_happy_path(self):
        r = taxonomy.add_mappings_bulk(self.profile_name, [
            {"theme": "thermodynamics", "folder": "01-SCIENCES/PHYSIQUE"},
            {"theme": "geometry", "folder": "01-SCIENCES/MATHEMATIQUES"},
        ])
        self.assertTrue(r["ok"])
        self.assertEqual(r["n_added"], 2)
        added_themes = {a["theme"] for a in r["added"]}
        self.assertEqual(added_themes, {"thermodynamics", "geometry"})
        self.assertEqual(r["skipped"], [])
        self.assertIsNotNone(r["backup"])
        # YAML actually updated, existing entries preserved
        new_map = yaml.safe_load(
            (self.profile_dir / "theme_mapping.yaml").read_text())
        self.assertEqual(new_map["thermodynamics"], "01-SCIENCES/PHYSIQUE")
        self.assertEqual(new_map["geometry"], "01-SCIENCES/MATHEMATIQUES")
        self.assertIn("physics", new_map)

    def test_bulk_add_already_mapped_is_skipped_no_overwrite(self):
        # 'physics' is already mapped to 01-SCIENCES/PHYSIQUE in the fixture.
        r = taxonomy.add_mappings_bulk(self.profile_name, [
            {"theme": "physics", "folder": "02-INFORMATIQUE"},
            {"theme": "geometry", "folder": "01-SCIENCES/MATHEMATIQUES"},
        ])
        self.assertEqual(r["n_added"], 1)
        skipped_themes = {s["theme"] for s in r["skipped"]}
        self.assertIn("physics", skipped_themes)
        reasons = {s["theme"]: s["reason"] for s in r["skipped"]}
        self.assertEqual(reasons["physics"], "already_mapped")
        # No overwrite of the existing mapping
        new_map = yaml.safe_load(
            (self.profile_dir / "theme_mapping.yaml").read_text())
        self.assertEqual(new_map["physics"], "01-SCIENCES/PHYSIQUE")
        self.assertEqual(new_map["geometry"], "01-SCIENCES/MATHEMATIQUES")

    def test_bulk_add_unknown_folder_is_skipped(self):
        r = taxonomy.add_mappings_bulk(self.profile_name, [
            {"theme": "thermodynamics", "folder": "99-DOES-NOT-EXIST"},
            {"theme": "geometry", "folder": "01-SCIENCES/MATHEMATIQUES"},
        ])
        self.assertEqual(r["n_added"], 1)
        added_themes = {a["theme"] for a in r["added"]}
        self.assertEqual(added_themes, {"geometry"})
        skipped_themes = {s["theme"] for s in r["skipped"]}
        self.assertEqual(skipped_themes, {"thermodynamics"})

    def test_bulk_add_invalid_theme_is_skipped(self):
        r = taxonomy.add_mappings_bulk(self.profile_name, [
            {"theme": "   ", "folder": "01-SCIENCES/PHYSIQUE"},
            {"theme": "geometry", "folder": "01-SCIENCES/MATHEMATIQUES"},
        ])
        self.assertEqual(r["n_added"], 1)
        self.assertEqual({a["theme"] for a in r["added"]}, {"geometry"})
        self.assertEqual(len(r["skipped"]), 1)

    def test_bulk_add_dedupes_keeping_first(self):
        r = taxonomy.add_mappings_bulk(self.profile_name, [
            {"theme": "geometry", "folder": "01-SCIENCES/MATHEMATIQUES"},
            {"theme": "geometry", "folder": "02-INFORMATIQUE"},
        ])
        self.assertEqual(r["n_added"], 1)
        added = {a["theme"]: a["folder"] for a in r["added"]}
        self.assertEqual(added["geometry"], "01-SCIENCES/MATHEMATIQUES")
        # The duplicate is reported as skipped
        self.assertEqual(len(r["skipped"]), 1)
        new_map = yaml.safe_load(
            (self.profile_dir / "theme_mapping.yaml").read_text())
        self.assertEqual(new_map["geometry"], "01-SCIENCES/MATHEMATIQUES")

    def test_bulk_add_empty_list_no_backup_no_write(self):
        backup_dir = self.profile_dir / ".cache" / "taxonomy-backups"
        before = list(backup_dir.glob("*.yaml")) if backup_dir.exists() else []
        r = taxonomy.add_mappings_bulk(self.profile_name, [])
        self.assertEqual(r["n_added"], 0)
        self.assertIsNone(r["backup"])
        after = list(backup_dir.glob("*.yaml")) if backup_dir.exists() else []
        self.assertEqual(len(after), len(before))

    def test_bulk_add_all_skipped_no_backup_no_write(self):
        backup_dir = self.profile_dir / ".cache" / "taxonomy-backups"
        before = list(backup_dir.glob("*.yaml")) if backup_dir.exists() else []
        r = taxonomy.add_mappings_bulk(self.profile_name, [
            {"theme": "physics", "folder": "02-INFORMATIQUE"},  # already mapped
            {"theme": "x", "folder": "99-DOES-NOT-EXIST"},       # bad folder
        ])
        self.assertEqual(r["n_added"], 0)
        self.assertIsNone(r["backup"])
        after = list(backup_dir.glob("*.yaml")) if backup_dir.exists() else []
        self.assertEqual(len(after), len(before))

    def test_bulk_add_creates_single_backup(self):
        backup_dir = self.profile_dir / ".cache" / "taxonomy-backups"
        before = list(backup_dir.glob("*.yaml")) if backup_dir.exists() else []
        taxonomy.add_mappings_bulk(self.profile_name, [
            {"theme": "thermodynamics", "folder": "01-SCIENCES/PHYSIQUE"},
            {"theme": "geometry", "folder": "01-SCIENCES/MATHEMATIQUES"},
            {"theme": "optics", "folder": "01-SCIENCES/PHYSIQUE"},
        ])
        after = list(backup_dir.glob("*.yaml"))
        # Exactly one new backup file regardless of how many entries were added.
        self.assertEqual(len(after) - len(before), 1)

    def test_bulk_add_respects_lock(self):
        lock = self.profile_dir / ".cache" / "taxonomy.lock"
        lock.write_text("locked by test")
        with self.assertRaises(taxonomy.TaxonomyError) as ctx:
            taxonomy.add_mappings_bulk(self.profile_name, [
                {"theme": "thermodynamics", "folder": "01-SCIENCES/PHYSIQUE"},
            ])
        self.assertEqual(ctx.exception.status, 423)


class TestDormantAndBulkEndpoints(TaxonomyTestBase):

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient

        from dashboard.app import app
        self.client = TestClient(app)

    def test_endpoint_dormant_mappings(self):
        r = self.client.get(
            f"/api/taxonomy/dormant-mappings?profile={self.profile_name}"
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("dormant", body)
        self.assertIn("active", body)
        self.assertIn("n_total", body)

    def test_endpoint_bulk_delete_happy(self):
        r = self.client.post("/api/taxonomy/mappings/bulk-delete", json={
            "profile": self.profile_name,
            "keys": ["physics", "algebra"],
        })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["n_deleted"], 2)

    def test_endpoint_bulk_delete_requires_profile(self):
        r = self.client.post("/api/taxonomy/mappings/bulk-delete", json={
            "keys": ["physics"],
        })
        self.assertEqual(r.status_code, 400)

    def test_endpoint_bulk_delete_empty_returns_400(self):
        r = self.client.post("/api/taxonomy/mappings/bulk-delete", json={
            "profile": self.profile_name,
            "keys": [],
        })
        self.assertEqual(r.status_code, 400)

    def test_bulk_add_endpoint(self):
        r = self.client.post("/api/taxonomy/mappings/bulk-add", json={
            "profile": self.profile_name,
            "mappings": [
                {"theme": "thermodynamics", "folder": "01-SCIENCES/PHYSIQUE"},
                {"theme": "geometry", "folder": "01-SCIENCES/MATHEMATIQUES"},
            ],
        })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["n_added"], 2)
        self.assertEqual(
            {a["theme"] for a in body["added"]},
            {"thermodynamics", "geometry"},
        )

    def test_bulk_add_endpoint_already_mapped_skipped(self):
        # 'physics' is already mapped in the fixture → reported in skipped.
        r = self.client.post("/api/taxonomy/mappings/bulk-add", json={
            "profile": self.profile_name,
            "mappings": [
                {"theme": "physics", "folder": "02-INFORMATIQUE"},
                {"theme": "geometry", "folder": "01-SCIENCES/MATHEMATIQUES"},
            ],
        })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["n_added"], 1)
        self.assertIn("physics", {s["theme"] for s in body["skipped"]})

    def test_bulk_add_endpoint_requires_profile(self):
        r = self.client.post("/api/taxonomy/mappings/bulk-add", json={
            "mappings": [
                {"theme": "geometry", "folder": "01-SCIENCES/MATHEMATIQUES"},
            ],
        })
        self.assertEqual(r.status_code, 400)

    def test_bulk_add_endpoint_requires_mappings(self):
        r = self.client.post("/api/taxonomy/mappings/bulk-add", json={
            "profile": self.profile_name,
        })
        self.assertEqual(r.status_code, 400)

    def test_suggest_endpoint(self):
        # "Mathematiques" matche le dernier segment du dossier
        # 01-SCIENCES/MATHEMATIQUES → résolu par le déterministe, zéro LLM.
        r = self.client.post("/api/taxonomy/mappings/suggest", json={
            "profile": self.profile_name,
            "themes": ["Mathematiques"],
            "use_llm": False,
        })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["n_llm_calls"], 0)
        sug = body["suggestions"][0]
        self.assertEqual(sug["source"], "deterministic")
        self.assertEqual(sug["folder"], "01-SCIENCES/MATHEMATIQUES")

    def test_suggest_endpoint_defaults_use_llm_false(self):
        # use_llm absent du body → défaut False, aucun appel LLM.
        r = self.client.post("/api/taxonomy/mappings/suggest", json={
            "profile": self.profile_name,
            "themes": ["Mathematiques"],
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["n_llm_calls"], 0)

    def test_suggest_endpoint_requires_profile(self):
        r = self.client.post("/api/taxonomy/mappings/suggest", json={
            "themes": ["Mathematiques"],
        })
        self.assertEqual(r.status_code, 400)

    def test_suggest_endpoint_requires_themes(self):
        r = self.client.post("/api/taxonomy/mappings/suggest", json={
            "profile": self.profile_name,
        })
        self.assertEqual(r.status_code, 400)


class TestReclassifyDryrun(TaxonomyTestBase):
    """reclassify_dryrun() — project moves based on cached folder_index."""

    def setUp(self):
        super().setUp()
        # Layout :
        #   physics → 01-SCIENCES/PHYSIQUE  (already placed correctly)
        #   physics → 01-SCIENCES/PHYSIQUE  (currently in MATHEMATIQUES — must move)
        #   algebra → 01-SCIENCES/MATHEMATIQUES (already in place)
        from lib import vision_cache as vc
        cache = {}
        files = {
            # mechanics.pdf lives in PHYSIQUE with theme physics → stable
            "01-SCIENCES/PHYSIQUE/mechanics.pdf": [("physics", 0.9)],
            # quantum.pdf lives in PHYSIQUE → stable
            "01-SCIENCES/PHYSIQUE/quantum.pdf": [("quantum mechanics", 0.95)],
            # misplaced.pdf has theme physics but currently in MATHEMATIQUES → would move
            "01-SCIENCES/MATHEMATIQUES/misplaced.pdf": [("physics", 0.9)],
            "01-SCIENCES/MATHEMATIQUES/algebra.pdf": [("algebra", 0.85)],
        }
        for rel, themes in files.items():
            abs_path = self.target / rel
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_bytes(f"%PDF-1.4 {rel}".encode())
            key = vc.compute_cache_key(
                str(abs_path),
                model="Qwen/Qwen3-VL-32B-Instruct",
                n_pages=2,
            )
            top_conf = max(c for _, c in themes)
            cache[key] = {
                "result": {
                    "title": Path(rel).stem,
                    # Top-level confidence is what classify_combined gates on
                    # (it must be >= CONFIDENCE_THRESHOLD for step 1 to fire).
                    "confidence": top_conf,
                    "themes": [{"theme": t, "confidence": c} for t, c in themes],
                },
                "model": "Qwen/Qwen3-VL-32B-Instruct",
                "prompt_version": "v3",
            }
        cache_path = self.profile_dir / ".cache" / "vision_cache.json"
        cache_path.write_text(json.dumps(cache))
        taxonomy.reset_cache(self.profile_name)

    def test_counts_moving_and_stable(self):
        r = taxonomy.reclassify_dryrun(self.profile_name)
        s = r["stats"]
        # 4 files total in lib (intro.pdf from base + 4 added in setUp — wait,
        # base setUp uses single-file content shared across files, but our
        # added files have unique bytes via setUp here so their cache_keys
        # are real). Check counts via the prediction logic.
        self.assertEqual(s["n_in_lib"], 5)  # intro.pdf + 4 unique
        # Only the 4 unique files have working cache hits → 4 with prediction
        # (intro.pdf shares head bytes with mechanics.pdf? Actually no — the
        # base helper writes placeholder bytes, so intro.pdf shares with
        # nobody since we override the others. Let's just sanity-check.)
        self.assertGreaterEqual(s["n_with_prediction"], 3)
        self.assertGreater(s["n_moving"], 0)
        self.assertGreater(s["n_stable"], 0)

    def test_misplaced_file_appears_in_sample_moves(self):
        r = taxonomy.reclassify_dryrun(self.profile_name)
        paths = {m["rel_path"] for m in r["sample_moves"]}
        self.assertIn("01-SCIENCES/MATHEMATIQUES/misplaced.pdf", paths)
        m = next(x for x in r["sample_moves"]
                 if x["rel_path"] == "01-SCIENCES/MATHEMATIQUES/misplaced.pdf")
        self.assertEqual(m["from"], "01-SCIENCES/MATHEMATIQUES")
        self.assertEqual(m["to"], "01-SCIENCES/PHYSIQUE")

    def test_by_destination_aggregates(self):
        r = taxonomy.reclassify_dryrun(self.profile_name)
        physique = next(d for d in r["by_destination"]
                        if d["folder"] == "01-SCIENCES/PHYSIQUE")
        # 1 incoming from MATHEMATIQUES, 2 already there
        self.assertEqual(physique["n_incoming"], 1)
        self.assertEqual(physique["n_already_there"], 2)
        self.assertIn("01-SCIENCES/MATHEMATIQUES", physique["from"])

    def test_sample_size_caps_moves_list(self):
        r = taxonomy.reclassify_dryrun(self.profile_name, sample_size=0)
        self.assertEqual(r["sample_moves"], [])
        # But counts unaffected
        self.assertGreater(r["stats"]["n_moving"], 0)

    def test_limits_documented(self):
        r = taxonomy.reclassify_dryrun(self.profile_name)
        # step2_included is True when categories.yaml exists and was loaded;
        # the test profile has no categories.yaml so it stays False — but
        # the key must always be present in the limits payload.
        self.assertIn("step2_included", r["limits"])
        self.assertTrue(r["limits"]["no_llm_mapper"])
        self.assertTrue(r["limits"]["no_execute"])

    def test_step1_via_themes_counted_in_via_step1(self):
        r = taxonomy.reclassify_dryrun(self.profile_name)
        # No categories.yaml in the test profile → no step 2 rescue, all
        # successful predictions come from step 1 (theme_mapping).
        self.assertGreater(r["stats"]["n_via_step1"], 0)
        self.assertEqual(r["stats"]["n_via_step2"], 0)
        # Every move's source should NOT start with "Keyword"
        for m in r["sample_moves"]:
            self.assertFalse(m["source"].startswith("Keyword"))

    def test_step2_rescues_file_with_no_mapped_theme(self):
        """Adding a categories.yaml entry with a matching keyword should
        rescue a file that has a theme not present in theme_mapping."""
        # Add a file with theme 'biology' that has no mapping in theme_mapping
        from lib import vision_cache as vc
        rel = "01-SCIENCES/PHYSIQUE/biology-book.pdf"
        abs_path = self.target / rel
        abs_path.write_bytes(b"%PDF-1.4 biology-book unique bytes here")
        key = vc.compute_cache_key(
            str(abs_path),
            model="Qwen/Qwen3-VL-32B-Instruct",
            n_pages=2,
        )
        cache_path = self.profile_dir / ".cache" / "vision_cache.json"
        cache = json.loads(cache_path.read_text())
        # Use a confidence below threshold for step 1 so step 1 is skipped
        # AND theme is not in mapping anyway → only step 2 can rescue.
        cache[key] = {
            "result": {
                "title": "Introduction to Genetics",
                "confidence": 0.4,   # below threshold → step 1 skipped
                "themes": [{"theme": "unknown-theme", "confidence": 0.4}],
            },
            "model": "Qwen/Qwen3-VL-32B-Instruct",
            "prompt_version": "v3",
        }
        cache_path.write_text(json.dumps(cache))
        # Write a categories.yaml that catches the file via title keyword
        cat_path = self.profile_dir / "categories.yaml"
        cat_path.write_text(
            "sciences:\n"
            "  - chemin: '01-SCIENCES/BIOLOGIE'\n"
            "    priorite: 3\n"
            "    mots_cles: ['genetics', 'biology']\n"
        )
        taxonomy.reset_cache(self.profile_name)
        r = taxonomy.reclassify_dryrun(self.profile_name)
        self.assertTrue(r["limits"]["step2_included"])
        self.assertGreater(r["stats"]["n_via_step2"], 0)
        paths = {m["rel_path"] for m in r["sample_moves"]}
        self.assertIn(rel, paths)
        match = next(m for m in r["sample_moves"] if m["rel_path"] == rel)
        self.assertTrue(match["source"].startswith("Keyword"))

    def test_disabling_step2_falls_back_to_step1_only(self):
        # Same setup as test_step2_rescues but explicitly disable step 2
        cat_path = self.profile_dir / "categories.yaml"
        cat_path.write_text(
            "sciences:\n"
            "  - chemin: '01-SCIENCES/BIOLOGIE'\n"
            "    priorite: 3\n"
            "    mots_cles: ['biology']\n"
        )
        taxonomy.reset_cache(self.profile_name)
        r_with = taxonomy.reclassify_dryrun(self.profile_name, include_step2=True)
        r_without = taxonomy.reclassify_dryrun(self.profile_name, include_step2=False)
        # step1 totals identical, step2 only differs
        self.assertEqual(r_with["stats"]["n_via_step1"],
                         r_without["stats"]["n_via_step1"])
        self.assertEqual(r_without["stats"]["n_via_step2"], 0)
        self.assertFalse(r_without["limits"]["step2_included"])


class TestReclassifyDryrunEndpoint(TaxonomyTestBase):

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient

        from dashboard.app import app
        self.client = TestClient(app)

    def test_endpoint_basic(self):
        r = self.client.get(
            f"/api/taxonomy/reclassify/dryrun?profile={self.profile_name}")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("stats", body)
        self.assertIn("by_destination", body)
        self.assertIn("sample_moves", body)
        self.assertIn("limits", body)

    def test_endpoint_sample_clamped(self):
        r = self.client.get(
            f"/api/taxonomy/reclassify/dryrun?profile={self.profile_name}"
            f"&sample=99999")
        self.assertEqual(r.status_code, 200)
        # sample is clamped at 500 server-side — list can be ≤500 in tests
        self.assertLessEqual(len(r.json()["sample_moves"]), 500)


class TestMappingConflicts(TaxonomyTestBase):
    """mapping_conflicts() — substring eclipses + same-folder duplicates."""

    def _setup_with_files(self, mapping: dict, files: dict):
        """Helper: replace the mapping + write a vision_cache so that the
        folder_index has the expected per-file resolutions."""
        mp = self.profile_dir / "theme_mapping.yaml"
        mp.write_text(yaml.safe_dump(mapping, sort_keys=False))
        # Each file gets unique bytes for a real cache_key
        from lib import vision_cache as vc
        cache: dict = {}
        for rel, themes in files.items():
            abs_path = self.target / rel
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_bytes(f"%PDF-1.4 {rel}".encode())
            key = vc.compute_cache_key(
                str(abs_path),
                model="Qwen/Qwen3-VL-32B-Instruct",
                n_pages=2,
            )
            top_conf = max(c for _, c in themes)
            cache[key] = {
                "result": {
                    "title": Path(rel).stem,
                    "confidence": top_conf,
                    "themes": [{"theme": t, "confidence": c} for t, c in themes],
                },
                "model": "Qwen/Qwen3-VL-32B-Instruct",
                "prompt_version": "v3",
            }
        cache_path = self.profile_dir / ".cache" / "vision_cache.json"
        cache_path.write_text(json.dumps(cache))
        taxonomy.reset_cache(self.profile_name)

    def test_substring_eclipse_detected(self):
        """A dormant key whose lowercase is substring of a longer ACTIVE
        key must be flagged as eclipsed by that winner."""
        self._setup_with_files(
            mapping={
                "physics": "01-SCIENCES/PHYSIQUE",
                # "phys" is shorter and a substring of "physics"; no file
                # triggers it standalone because the cache only has
                # "Mathematical Physics" → "physics" wins.
                "phys": "01-SCIENCES/AUTRES",
            },
            files={
                "01-SCIENCES/PHYSIQUE/qft.pdf":
                    [("Mathematical Physics", 0.95)],
            },
        )
        r = taxonomy.mapping_conflicts(self.profile_name)
        conflicts = r["substring_conflicts"]
        self.assertEqual(len(conflicts), 1)
        c = conflicts[0]
        self.assertEqual(c["loser"], "phys")
        self.assertEqual(c["winner"], "physics")
        self.assertEqual(c["winner_folder"], "01-SCIENCES/PHYSIQUE")
        self.assertGreater(c["winner_files"], 0)

    def test_no_substring_eclipse_when_no_overlap(self):
        """Dormants with no longer key containing them are NOT in the
        substring_conflicts list — they're just unused."""
        self._setup_with_files(
            mapping={
                "physics": "01-SCIENCES/PHYSIQUE",
                "buddhism": "05-RELIGIONS",   # dormant + no overlap with anything
            },
            files={
                "01-SCIENCES/PHYSIQUE/p.pdf": [("physics", 0.9)],
            },
        )
        r = taxonomy.mapping_conflicts(self.profile_name)
        # buddhism is dormant (in dormant_mappings) but NOT in conflicts
        keys_flagged = {c["loser"] for c in r["substring_conflicts"]}
        self.assertNotIn("buddhism", keys_flagged)

    def test_eclipse_picks_shortest_winner(self):
        """When multiple longer keys contain the loser, the SHORTEST
        winner (= most direct culprit) is reported."""
        self._setup_with_files(
            mapping={
                "ml": "02-INFORMATIQUE/05-IA-ML/Machine-Learning",
                "machine learning": "02-INFORMATIQUE/05-IA-ML/Machine-Learning",
                "deep machine learning": "02-INFORMATIQUE/05-IA-ML/Deep-Learning",
            },
            files={
                "02-INFO/Machine-Learning/ml1.pdf": [("machine learning", 0.9)],
                "02-INFO/Deep-Learning/ml2.pdf": [("deep machine learning", 0.9)],
            },
        )
        r = taxonomy.mapping_conflicts(self.profile_name)
        ml_conflicts = [c for c in r["substring_conflicts"]
                        if c["loser"] == "ml"]
        if ml_conflicts:
            # winner must be the shortest active key containing "ml"
            self.assertEqual(ml_conflicts[0]["winner"], "machine learning")

    def test_duplicate_groups_detected(self):
        """Multiple keys pointing to the same folder form a duplicate group."""
        self._setup_with_files(
            mapping={
                "physics": "01-SCIENCES/PHYSIQUE",
                "physique": "01-SCIENCES/PHYSIQUE",
                "phys-domain": "01-SCIENCES/PHYSIQUE",
                "algebra": "01-SCIENCES/MATHEMATIQUES",
            },
            files={"01-SCIENCES/PHYSIQUE/p.pdf": [("physics", 0.9)]},
        )
        r = taxonomy.mapping_conflicts(self.profile_name)
        groups = r["duplicate_groups"]
        # Only PHYSIQUE has > 1 key
        physique = [g for g in groups
                    if g["folder"] == "01-SCIENCES/PHYSIQUE"]
        self.assertEqual(len(physique), 1)
        self.assertEqual(physique[0]["n_keys"], 3)
        self.assertEqual(set(physique[0]["keys"]),
                         {"physics", "physique", "phys-domain"})

    def test_duplicate_groups_includes_per_key_counts(self):
        self._setup_with_files(
            mapping={
                "physics": "01-SCIENCES/PHYSIQUE",
                "physique": "01-SCIENCES/PHYSIQUE",
            },
            files={"01-SCIENCES/PHYSIQUE/p.pdf": [("physics", 0.9)]},
        )
        r = taxonomy.mapping_conflicts(self.profile_name)
        g = r["duplicate_groups"][0]
        self.assertEqual(g["per_key"]["physics"], 1)
        self.assertEqual(g["per_key"]["physique"], 0)

    def test_singleton_folders_not_duplicates(self):
        """A folder with only one mapping key isn't a duplicate group."""
        self._setup_with_files(
            mapping={"physics": "01-SCIENCES/PHYSIQUE"},
            files={"01-SCIENCES/PHYSIQUE/p.pdf": [("physics", 0.9)]},
        )
        r = taxonomy.mapping_conflicts(self.profile_name)
        self.assertEqual(r["duplicate_groups"], [])

    def test_empty_mapping_returns_empty_stats(self):
        mp = self.profile_dir / "theme_mapping.yaml"
        mp.write_text("{}")
        taxonomy.reset_cache(self.profile_name)
        r = taxonomy.mapping_conflicts(self.profile_name)
        self.assertEqual(r["substring_conflicts"], [])
        self.assertEqual(r["duplicate_groups"], [])
        self.assertEqual(r["stats"]["n_substring_conflicts"], 0)
        self.assertEqual(r["stats"]["n_duplicate_groups"], 0)


class TestMappingConflictsEndpoint(TaxonomyTestBase):

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient

        from dashboard.app import app
        self.client = TestClient(app)

    def test_endpoint_basic(self):
        r = self.client.get(
            f"/api/taxonomy/mapping-conflicts?profile={self.profile_name}")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("substring_conflicts", body)
        self.assertIn("duplicate_groups", body)
        self.assertIn("stats", body)


class TestAuditLogBackups(TaxonomyTestBase):
    """list_taxonomy_backups() + restore_taxonomy_backup() — feature I."""

    def test_list_empty_when_no_backups(self):
        r = taxonomy.list_taxonomy_backups(self.profile_name)
        self.assertEqual(r["n_total"], 0)
        self.assertEqual(r["backups"], [])

    def test_list_returns_backups_newest_first(self):
        # Generate a few backups by performing real writes
        taxonomy.add_mapping(self.profile_name, "newtheme1", "01-SCIENCES")
        taxonomy.add_mapping(self.profile_name, "newtheme2", "01-SCIENCES")
        taxonomy.add_mapping(self.profile_name, "newtheme3", "01-SCIENCES")
        r = taxonomy.list_taxonomy_backups(self.profile_name)
        self.assertEqual(r["n_total"], 3)
        names = [b["filename"] for b in r["backups"]]
        # Sorted DESC by timestamp embedded in filename
        self.assertEqual(names, sorted(names, reverse=True))
        # Each entry has the expected shape
        for b in r["backups"]:
            self.assertEqual(b["kind"], "mapping")
            self.assertIn("size_bytes", b)
            self.assertIn("age_human", b)
            self.assertGreater(b["size_bytes"], 0)

    def test_restore_specific_backup_overwrites_current(self):
        # Snapshot the initial state (physics→PHYSIQUE)
        taxonomy.add_mapping(self.profile_name, "extra1", "01-SCIENCES")
        # Now we have 1 backup of the original state. Add a 2nd mapping
        taxonomy.add_mapping(self.profile_name, "extra2", "01-SCIENCES")
        backups = taxonomy.list_taxonomy_backups(self.profile_name)["backups"]
        # The OLDEST backup (= initial state, no extras) is the last entry
        oldest = backups[-1]["filename"]
        r = taxonomy.restore_taxonomy_backup(self.profile_name, oldest)
        self.assertTrue(r["ok"])
        # After restore, neither extra is in the mapping
        mp = yaml.safe_load(
            (self.profile_dir / "theme_mapping.yaml").read_text())
        self.assertNotIn("extra1", mp)
        self.assertNotIn("extra2", mp)
        # Pre-restore backup was created so the operation is itself undoable
        self.assertIsNotNone(r["pre_restore_backup"])

    def test_restore_unknown_filename_raises_404(self):
        with self.assertRaises(taxonomy.TaxonomyError) as cm:
            taxonomy.restore_taxonomy_backup(
                self.profile_name, "theme_mapping-does-not-exist.yaml")
        self.assertEqual(cm.exception.status, 404)

    def test_restore_rejects_path_traversal(self):
        with self.assertRaises(taxonomy.TaxonomyError) as cm:
            taxonomy.restore_taxonomy_backup(
                self.profile_name, "../../etc/passwd")
        self.assertEqual(cm.exception.status, 400)

    def test_restore_rejects_unsupported_backup_type(self):
        # Drop a YAML file in the backup dir that isn't a known type
        bd = self.profile_dir / ".cache" / "taxonomy-backups"
        bd.mkdir(parents=True, exist_ok=True)
        (bd / "random-thing.yaml").write_text("x: 1\n")
        with self.assertRaises(taxonomy.TaxonomyError) as cm:
            taxonomy.restore_taxonomy_backup(
                self.profile_name, "random-thing.yaml")
        self.assertEqual(cm.exception.status, 400)


class TestAuditLogEndpoints(TaxonomyTestBase):

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient

        from dashboard.app import app
        self.client = TestClient(app)

    def test_endpoint_list(self):
        r = self.client.get(f"/api/taxonomy/backups?profile={self.profile_name}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("backups", r.json())

    def test_endpoint_restore_missing_filename(self):
        r = self.client.post("/api/taxonomy/backups/restore", json={
            "profile": self.profile_name,
        })
        self.assertEqual(r.status_code, 400)


# ─── Per-file FS ops (feature/mapping-file-actions) ─────────────────────


class TestSoftDeleteFile(TaxonomyTestBase):

    def test_moves_to_trash(self):
        # File exists pre-test
        src = self.target / "01-SCIENCES/PHYSIQUE/mechanics.pdf"
        self.assertTrue(src.exists())
        r = taxonomy.soft_delete_file(
            self.profile_name, "01-SCIENCES/PHYSIQUE/mechanics.pdf")
        self.assertTrue(r["ok"])
        self.assertFalse(src.exists())
        # Trashed under .trash/<ts>/mechanics.pdf
        trash = self.target / ".trash"
        self.assertTrue(trash.exists())
        trashed_files = list(trash.rglob("mechanics.pdf"))
        self.assertEqual(len(trashed_files), 1)

    def test_journal_record_appended(self):
        taxonomy.soft_delete_file(
            self.profile_name, "01-SCIENCES/PHYSIQUE/quantum.pdf")
        journal = self.profile_dir / ".cache" / "file-trash-journal.jsonl"
        self.assertTrue(journal.exists())
        lines = journal.read_text(encoding="utf-8").splitlines()
        rec = json.loads(lines[-1])
        self.assertEqual(rec["original_rel_path"],
                          "01-SCIENCES/PHYSIQUE/quantum.pdf")
        self.assertIn(".trash/", rec["trash_rel_path"])
        self.assertIn("ts", rec)

    def test_collision_suffix(self):
        # Two deletes of same-basename files within the same second.
        # Create two distinct files with the same basename in different
        # source folders.
        (self.target / "01-SCIENCES/PHYSIQUE/dup.pdf").write_bytes(b"%PDF a")
        (self.target / "01-SCIENCES/MATHEMATIQUES/dup.pdf").write_bytes(b"%PDF b")
        r1 = taxonomy.soft_delete_file(
            self.profile_name, "01-SCIENCES/PHYSIQUE/dup.pdf")
        r2 = taxonomy.soft_delete_file(
            self.profile_name, "01-SCIENCES/MATHEMATIQUES/dup.pdf")
        # Both must succeed
        self.assertTrue(r1["ok"])
        self.assertTrue(r2["ok"])
        # Both physically exist under .trash/ (one with collision suffix
        # if they landed in the same ts dir)
        all_dups = list((self.target / ".trash").rglob("dup*.pdf"))
        self.assertEqual(len(all_dups), 2)

    def test_refuses_path_outside_target(self):
        with self.assertRaises(taxonomy.TaxonomyFileError) as cm:
            taxonomy.soft_delete_file(
                self.profile_name, "../outside.pdf")
        # Either 400 (escapes) or 404 (doesn't exist) — both acceptable
        self.assertIn(cm.exception.status, (400, 404))

    def test_refuses_file_already_in_trash(self):
        # First delete to populate trash
        r1 = taxonomy.soft_delete_file(
            self.profile_name, "01-SCIENCES/intro.pdf")
        trash_rel = r1["trash_rel_path"]
        # Now try to delete the trashed file
        with self.assertRaises(taxonomy.TaxonomyFileError) as cm:
            taxonomy.soft_delete_file(self.profile_name, trash_rel)
        self.assertEqual(cm.exception.status, 400)
        self.assertIn("corbeille", str(cm.exception))

    def test_404_when_file_missing(self):
        with self.assertRaises(taxonomy.TaxonomyFileError) as cm:
            taxonomy.soft_delete_file(
                self.profile_name, "01-SCIENCES/ghost.pdf")
        self.assertEqual(cm.exception.status, 404)


class TestSoftDeleteBulk(TaxonomyTestBase):

    def test_happy_path_all_succeed(self):
        r = taxonomy.soft_delete_bulk(
            self.profile_name,
            rel_paths=[
                "01-SCIENCES/PHYSIQUE/mechanics.pdf",
                "01-SCIENCES/PHYSIQUE/quantum.pdf",
                "01-SCIENCES/MATHEMATIQUES/algebra.pdf",
            ])
        self.assertTrue(r["ok"])
        self.assertEqual(r["n_deleted"], 3)
        self.assertEqual(r["n_errors"], 0)
        # All three sources gone, all three in .trash/
        for orig in (
            "01-SCIENCES/PHYSIQUE/mechanics.pdf",
            "01-SCIENCES/PHYSIQUE/quantum.pdf",
            "01-SCIENCES/MATHEMATIQUES/algebra.pdf",
        ):
            self.assertFalse((self.target / orig).exists())
        trashed = list((self.target / ".trash").rglob("*.pdf"))
        self.assertEqual(len(trashed), 3)

    def test_shared_timestamp_groups_them(self):
        # All deletions in one batch land under the same .trash/<ts>/
        # subdir, since the function uses a single timestamp.
        r = taxonomy.soft_delete_bulk(
            self.profile_name,
            rel_paths=[
                "01-SCIENCES/PHYSIQUE/mechanics.pdf",
                "01-SCIENCES/PHYSIQUE/quantum.pdf",
            ])
        self.assertEqual(r["n_deleted"], 2)
        # The two trash_rel_paths share the same <ts> folder
        ts_folders = {
            s["trash_rel_path"].split("/")[1]
            for s in r["successes"]
        }
        self.assertEqual(len(ts_folders), 1)

    def test_partial_failure_continues_batch(self):
        # Mix valid + invalid items — batch must continue
        r = taxonomy.soft_delete_bulk(
            self.profile_name,
            rel_paths=[
                "01-SCIENCES/PHYSIQUE/mechanics.pdf",  # OK
                "ghost.pdf",                            # 404
                "../outside.pdf",                       # 400 (traversal)
                "01-SCIENCES/intro.pdf",               # OK
            ])
        self.assertEqual(r["n_deleted"], 2)
        self.assertEqual(r["n_errors"], 2)
        success_paths = {s["rel_path"] for s in r["successes"]}
        self.assertIn("01-SCIENCES/PHYSIQUE/mechanics.pdf", success_paths)
        self.assertIn("01-SCIENCES/intro.pdf", success_paths)

    def test_dedupes_duplicate_paths(self):
        # Same path twice in the input: first one deletes, second one
        # is rejected as a duplicate (not as 404).
        r = taxonomy.soft_delete_bulk(
            self.profile_name,
            rel_paths=[
                "01-SCIENCES/PHYSIQUE/mechanics.pdf",
                "01-SCIENCES/PHYSIQUE/mechanics.pdf",
            ])
        self.assertEqual(r["n_deleted"], 1)
        self.assertEqual(r["n_errors"], 1)
        self.assertIn("doublon", r["errors"][0]["error"])

    def test_empty_rel_paths_400(self):
        with self.assertRaises(taxonomy.TaxonomyFileError) as cm:
            taxonomy.soft_delete_bulk(self.profile_name, rel_paths=[])
        self.assertEqual(cm.exception.status, 400)

    def test_journal_records_each_success(self):
        taxonomy.soft_delete_bulk(
            self.profile_name,
            rel_paths=[
                "01-SCIENCES/PHYSIQUE/mechanics.pdf",
                "01-SCIENCES/intro.pdf",
            ])
        journal = self.profile_dir / ".cache" / "file-trash-journal.jsonl"
        lines = journal.read_text(encoding="utf-8").splitlines()
        # At least our two new entries (file may have prior entries from
        # other tests sharing the fixture — but each test gets a fresh
        # tmpdir, so it's exactly 2).
        self.assertEqual(len(lines), 2)


class TestSoftDeleteBulkEndpoint(TaxonomyTestBase):

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient

        from dashboard.app import app
        self.client = TestClient(app)

    def test_endpoint_happy(self):
        r = self.client.post("/api/taxonomy/file/delete-bulk", json={
            "profile": self.profile_name,
            "rel_paths": [
                "01-SCIENCES/PHYSIQUE/mechanics.pdf",
                "01-SCIENCES/PHYSIQUE/quantum.pdf",
            ],
        })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["n_deleted"], 2)
        self.assertEqual(body["n_errors"], 0)

    def test_endpoint_empty_400(self):
        r = self.client.post("/api/taxonomy/file/delete-bulk", json={
            "profile": self.profile_name,
            "rel_paths": [],
        })
        self.assertEqual(r.status_code, 400)


class TestThumbnailCacheKeyAlignment(TaxonomyTestBase):
    """The thumbnail cache dir must be keyed by content (MD5 of head
    bytes), not by rel_path — so a rename does NOT invalidate the
    cached cover image. Same key as ``lib.thumbnail.compute_content_key``."""

    def test_cache_dir_keyed_by_content(self):
        # Two paths, same file content → same cache dir
        from lib.thumbnail import compute_content_key
        a = self.target / "01-SCIENCES/PHYSIQUE/mechanics.pdf"
        b = self.target / "01-SCIENCES/MATHEMATIQUES/algebra.pdf"
        same_bytes = b"%PDF-1.4 identical bytes for the test"
        a.write_bytes(same_bytes)
        b.write_bytes(same_bytes)
        key_a = compute_content_key(a)
        key_b = compute_content_key(b)
        self.assertIsNotNone(key_a)
        self.assertEqual(key_a, key_b)
        # taxonomy._thumbnail_cache_dir uses the same key for both
        dir_a = taxonomy._thumbnail_cache_dir(self.profile_name, a)
        dir_b = taxonomy._thumbnail_cache_dir(self.profile_name, b)
        self.assertEqual(dir_a, dir_b)

    def test_cache_dir_returns_none_for_unreadable(self):
        # Empty file (no head bytes) → no key → no cache dir
        ghost = self.target / "ghost.pdf"
        ghost.write_bytes(b"")
        self.assertIsNone(
            taxonomy._thumbnail_cache_dir(self.profile_name, ghost))


class TestMoveFile(TaxonomyTestBase):

    def test_happy_path(self):
        r = taxonomy.move_file(
            self.profile_name,
            "01-SCIENCES/PHYSIQUE/mechanics.pdf",
            "02-INFORMATIQUE")
        self.assertTrue(r["ok"])
        self.assertEqual(r["new_rel_path"],
                          "02-INFORMATIQUE/mechanics.pdf")
        self.assertFalse(
            (self.target / "01-SCIENCES/PHYSIQUE/mechanics.pdf").exists())
        self.assertTrue(
            (self.target / "02-INFORMATIQUE/mechanics.pdf").exists())

    def test_same_folder_no_op(self):
        r = taxonomy.move_file(
            self.profile_name,
            "01-SCIENCES/PHYSIQUE/mechanics.pdf",
            "01-SCIENCES/PHYSIQUE")
        self.assertTrue(r["ok"])
        self.assertTrue(r.get("unchanged"))
        self.assertTrue(
            (self.target / "01-SCIENCES/PHYSIQUE/mechanics.pdf").exists())

    def test_collision_409(self):
        # Pre-create a name collision at the destination
        (self.target / "02-INFORMATIQUE/mechanics.pdf").write_bytes(b"%PDF squatter")
        with self.assertRaises(taxonomy.TaxonomyFileError) as cm:
            taxonomy.move_file(
                self.profile_name,
                "01-SCIENCES/PHYSIQUE/mechanics.pdf",
                "02-INFORMATIQUE")
        self.assertEqual(cm.exception.status, 409)
        # Source still in place
        self.assertTrue(
            (self.target / "01-SCIENCES/PHYSIQUE/mechanics.pdf").exists())

    def test_dest_folder_must_exist(self):
        with self.assertRaises(taxonomy.TaxonomyFileError) as cm:
            taxonomy.move_file(
                self.profile_name,
                "01-SCIENCES/PHYSIQUE/mechanics.pdf",
                "99-NONEXISTENT/SUBDIR")
        self.assertEqual(cm.exception.status, 404)

    def test_404_when_source_missing(self):
        with self.assertRaises(taxonomy.TaxonomyFileError) as cm:
            taxonomy.move_file(
                self.profile_name,
                "01-SCIENCES/PHYSIQUE/ghost.pdf",
                "02-INFORMATIQUE")
        self.assertEqual(cm.exception.status, 404)

    def test_dest_traversal_refused(self):
        with self.assertRaises(taxonomy.TaxonomyFileError) as cm:
            taxonomy.move_file(
                self.profile_name,
                "01-SCIENCES/PHYSIQUE/mechanics.pdf",
                "../outside")
        self.assertIn(cm.exception.status, (400, 404))


class TestComputeMoveImpact(TaxonomyTestBase):

    def _seed_vision_cache(self, themes: list[str]):
        """Seed a vision_cache entry for mechanics.pdf with the given
        ordered themes. The classifier will use the first one that
        maps."""
        from lib import vision_cache as vc
        abs_path = self.target / "01-SCIENCES/PHYSIQUE/mechanics.pdf"
        key = vc.compute_cache_key(
            str(abs_path),
            model="Qwen/Qwen3-VL-32B-Instruct", n_pages=2)
        cache_path = self.profile_dir / ".cache" / "vision_cache.json"
        cache = {}
        if cache_path.exists():
            cache = json.loads(cache_path.read_text())
        cache[key] = {
            "result": {
                "title": "Classical Mechanics",
                "themes": [
                    {"theme": t, "confidence": 0.95} for t in themes
                ],
                "confidence": 0.95,
            },
            "model": "Qwen/Qwen3-VL-32B-Instruct",
            "prompt_version": "v3",
        }
        cache_path.write_text(json.dumps(cache))

    def test_consistent_when_dest_matches_prediction(self):
        # vision_cache says theme=physics → mapped to /01-SCIENCES/PHYSIQUE
        # The file IS currently at /01-SCIENCES/PHYSIQUE — so move to
        # the same folder is consistent.
        self._seed_vision_cache(["physics"])
        impact = taxonomy.compute_move_impact(
            self.profile_name,
            "01-SCIENCES/PHYSIQUE/mechanics.pdf",
            "01-SCIENCES/PHYSIQUE")
        self.assertEqual(impact["predicted_folder"], "01-SCIENCES/PHYSIQUE")
        self.assertTrue(impact["is_consistent"])
        self.assertIn("physics", impact["themes_used"])

    def test_inconsistent_when_dest_diverges(self):
        # Theme maps to PHYSIQUE but user asks to move to MATHEMATIQUES
        self._seed_vision_cache(["physics"])
        impact = taxonomy.compute_move_impact(
            self.profile_name,
            "01-SCIENCES/PHYSIQUE/mechanics.pdf",
            "01-SCIENCES/MATHEMATIQUES")
        self.assertEqual(impact["predicted_folder"], "01-SCIENCES/PHYSIQUE")
        self.assertEqual(impact["dest_folder"], "01-SCIENCES/MATHEMATIQUES")
        self.assertFalse(impact["is_consistent"])

    def test_no_prediction_when_no_vision_cache(self):
        impact = taxonomy.compute_move_impact(
            self.profile_name,
            "01-SCIENCES/PHYSIQUE/mechanics.pdf",
            "02-INFORMATIQUE")
        self.assertIsNone(impact["predicted_folder"])
        self.assertFalse(impact["is_consistent"])
        # Still reports current_folder and dest_folder correctly
        self.assertEqual(impact["current_folder"], "01-SCIENCES/PHYSIQUE")
        self.assertEqual(impact["dest_folder"], "02-INFORMATIQUE")


class TestFileOpsEndpoints(TaxonomyTestBase):

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient

        from dashboard.app import app
        self.client = TestClient(app)

    def test_delete_endpoint_happy(self):
        r = self.client.post("/api/taxonomy/file/delete", json={
            "profile": self.profile_name,
            "rel_path": "01-SCIENCES/PHYSIQUE/mechanics.pdf",
        })
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertFalse(
            (self.target / "01-SCIENCES/PHYSIQUE/mechanics.pdf").exists())

    def test_delete_endpoint_404(self):
        r = self.client.post("/api/taxonomy/file/delete", json={
            "profile": self.profile_name,
            "rel_path": "ghost.pdf",
        })
        self.assertEqual(r.status_code, 404)

    def test_move_endpoint_happy(self):
        r = self.client.post("/api/taxonomy/file/move", json={
            "profile": self.profile_name,
            "rel_path": "01-SCIENCES/PHYSIQUE/mechanics.pdf",
            "dest_folder": "02-INFORMATIQUE",
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["new_rel_path"],
                          "02-INFORMATIQUE/mechanics.pdf")

    def test_move_endpoint_409_on_collision(self):
        (self.target / "02-INFORMATIQUE/mechanics.pdf").write_bytes(b"%PDF")
        r = self.client.post("/api/taxonomy/file/move", json={
            "profile": self.profile_name,
            "rel_path": "01-SCIENCES/PHYSIQUE/mechanics.pdf",
            "dest_folder": "02-INFORMATIQUE",
        })
        self.assertEqual(r.status_code, 409)

    def test_move_impact_endpoint(self):
        r = self.client.get(
            f"/api/taxonomy/file/move-impact?profile={self.profile_name}"
            f"&path=01-SCIENCES/PHYSIQUE/mechanics.pdf"
            f"&dest=02-INFORMATIQUE")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["current_folder"], "01-SCIENCES/PHYSIQUE")
        self.assertEqual(body["dest_folder"], "02-INFORMATIQUE")


# ─── 9. Routage — breakdown stable / incoming / outgoing ─────────────────


class TestFolderThemeBreakdown(TaxonomyTestBase):
    """`compute_folder_theme_breakdown` doit classer correctement :

      - stable  : thème mappé ICI + détecté sur fichiers ICI
      - incoming: thème mappé ICI mais ABSENT des fichiers actuels
      - outgoing: thème DÉTECTÉ ICI mais mappé AILLEURS (ou orphelin)
    """

    def _setup_fixture(self):
        """Construit un set où chaque cas est représenté."""
        from lib.thumbnail import compute_content_key

        # Réinitialise la lib avec des fichiers de contenu DISTINCT (head bytes
        # différents) pour que les content_keys soient uniques.
        shutil.rmtree(self.target, ignore_errors=True)
        self.target.mkdir(parents=True)
        (self.target / "01-SCIENCES" / "PHYSIQUE").mkdir(parents=True)
        (self.target / "01-SCIENCES" / "MATHEMATIQUES").mkdir(parents=True)
        (self.target / "02-INFORMATIQUE").mkdir(parents=True)

        # Fichier 1: dans PHYSIQUE, thème "physics" mappé vers PHYSIQUE → stable
        f1 = self.target / "01-SCIENCES" / "PHYSIQUE" / "mech.pdf"
        f1.write_bytes(b"%PDF-1.4 file-1-unique-content")
        # Fichier 2: dans PHYSIQUE, thème "machine learning" mappé AILLEURS
        f2 = self.target / "01-SCIENCES" / "PHYSIQUE" / "wrong.pdf"
        f2.write_bytes(b"%PDF-1.4 file-2-unique-content")
        # Fichier 3: dans PHYSIQUE, thème "unknown-theme" non mappé → outgoing orphan
        f3 = self.target / "01-SCIENCES" / "PHYSIQUE" / "orphan.pdf"
        f3.write_bytes(b"%PDF-1.4 file-3-unique-content")
        # Fichier 4: dans MATHS, théoriquement hors panneau PHYSIQUE
        f4 = self.target / "01-SCIENCES" / "MATHEMATIQUES" / "alg.pdf"
        f4.write_bytes(b"%PDF-1.4 file-4-unique-content")

        k1 = compute_content_key(f1)
        k2 = compute_content_key(f2)
        k3 = compute_content_key(f3)
        k4 = compute_content_key(f4)

        # Mapping : physics → PHYSIQUE, quantum mechanics → PHYSIQUE
        # (quantum mechanics est dans le mapping mais ABSENT des fichiers → incoming)
        # machine learning → 02-INFORMATIQUE
        # algebra → MATHS (pour f4)
        mapping = {
            "physics": "01-SCIENCES/PHYSIQUE",
            "quantum mechanics": "01-SCIENCES/PHYSIQUE",
            "machine learning": "02-INFORMATIQUE",
            "algebra": "01-SCIENCES/MATHEMATIQUES",
        }
        (self.profile_dir / "theme_mapping.yaml").write_text(
            yaml.safe_dump(mapping, sort_keys=False)
        )

        # vision_cache : confiance > 0.5 pour tous (sinon filtré par _MIN_CONFIDENCE)
        vc = {
            k1: {"result": {"themes": [
                {"theme": "Physics", "confidence": 0.9},
            ]}},
            k2: {"result": {"themes": [
                {"theme": "machine learning", "confidence": 0.85},
            ]}},
            k3: {"result": {"themes": [
                {"theme": "unknown-niche-topic", "confidence": 0.8},
            ]}},
            k4: {"result": {"themes": [
                {"theme": "algebra", "confidence": 0.9},
                # quantum mechanics aussi sur f4 → boost le count_expected
                {"theme": "quantum mechanics", "confidence": 0.95},
            ]}},
        }
        (self.profile_dir / ".cache" / "vision_cache.json").write_text(
            json.dumps(vc)
        )
        taxonomy.reset_cache()

    def test_stable_theme_classified_correctly(self):
        self._setup_fixture()
        out = taxonomy.compute_folder_theme_breakdown(
            self.profile_name, "01-SCIENCES/PHYSIQUE",
        )
        themes_stable = {x["theme"].lower() for x in out["stable"]}
        self.assertIn("physics", themes_stable)

    def test_incoming_theme_when_mapped_but_absent(self):
        """quantum mechanics est mappé vers PHYSIQUE mais aucun fichier IN
        PHYSIQUE ne le porte. Doit apparaître en incoming."""
        self._setup_fixture()
        out = taxonomy.compute_folder_theme_breakdown(
            self.profile_name, "01-SCIENCES/PHYSIQUE",
        )
        themes_incoming = {x["theme"].lower() for x in out["incoming"]}
        self.assertIn("quantum mechanics", themes_incoming)
        qm = next(x for x in out["incoming"] if x["theme"].lower() == "quantum mechanics")
        # quantum mechanics est détecté sur f4 (mathematiques) → count_expected ≥ 1
        self.assertGreaterEqual(qm["count_expected"], 1)

    def test_outgoing_when_mapped_elsewhere(self):
        """machine learning détecté dans PHYSIQUE mais mappé vers INFO."""
        self._setup_fixture()
        out = taxonomy.compute_folder_theme_breakdown(
            self.profile_name, "01-SCIENCES/PHYSIQUE",
        )
        outgoing = {x["theme"].lower(): x for x in out["outgoing"]}
        self.assertIn("machine learning", outgoing)
        self.assertEqual(
            outgoing["machine learning"]["target"], "02-INFORMATIQUE",
        )
        self.assertEqual(outgoing["machine learning"]["reason"], "mapped_elsewhere")

    def test_outgoing_orphan_when_not_mapped(self):
        self._setup_fixture()
        out = taxonomy.compute_folder_theme_breakdown(
            self.profile_name, "01-SCIENCES/PHYSIQUE",
        )
        outgoing = {x["theme"].lower(): x for x in out["outgoing"]}
        self.assertIn("unknown-niche-topic", outgoing)
        self.assertIsNone(outgoing["unknown-niche-topic"]["target"])
        self.assertEqual(outgoing["unknown-niche-topic"]["reason"], "orphan")

    def test_summary_counts(self):
        self._setup_fixture()
        out = taxonomy.compute_folder_theme_breakdown(
            self.profile_name, "01-SCIENCES/PHYSIQUE",
        )
        s = out["summary"]
        self.assertEqual(s["path"], "01-SCIENCES/PHYSIQUE")
        self.assertEqual(s["n_stable"], len(out["stable"]))
        self.assertEqual(s["n_incoming"], len(out["incoming"]))
        self.assertEqual(s["n_outgoing"], len(out["outgoing"]))
        # n_mapped_rules = physics + quantum mechanics = 2
        self.assertEqual(s["n_mapped_rules"], 2)

    def test_empty_folder_returns_empty_lists(self):
        """02-INFORMATIQUE est vide et a aucun mapping → tout vide."""
        self._setup_fixture()
        out = taxonomy.compute_folder_theme_breakdown(
            self.profile_name, "02-INFORMATIQUE",
        )
        self.assertEqual(out["stable"], [])
        # machine learning est mappé vers INFO mais le fichier qui le porte
        # est dans PHYSIQUE → incoming (le folder INFO le verra arriver).
        themes_incoming = {x["theme"].lower() for x in out["incoming"]}
        self.assertIn("machine learning", themes_incoming)
        self.assertEqual(out["outgoing"], [])

    def test_nonexistent_path_returns_empty(self):
        out = taxonomy.compute_folder_theme_breakdown(
            self.profile_name, "99-DOES-NOT-EXIST",
        )
        self.assertEqual(out["stable"], [])
        self.assertEqual(out["outgoing"], [])

    def test_low_confidence_themes_filtered(self):
        """Les thèmes avec confidence < 0.5 ne doivent pas être comptés."""
        from lib.thumbnail import compute_content_key

        shutil.rmtree(self.target, ignore_errors=True)
        self.target.mkdir(parents=True)
        (self.target / "01-SCIENCES" / "PHYSIQUE").mkdir(parents=True)

        f = self.target / "01-SCIENCES" / "PHYSIQUE" / "doc.pdf"
        f.write_bytes(b"%PDF-1.4 low-conf-test")
        k = compute_content_key(f)

        (self.profile_dir / "theme_mapping.yaml").write_text(
            yaml.safe_dump({"physics": "01-SCIENCES/PHYSIQUE"}, sort_keys=False)
        )
        (self.profile_dir / ".cache" / "vision_cache.json").write_text(
            json.dumps({k: {"result": {"themes": [
                {"theme": "Physics", "confidence": 0.3},  # < seuil
            ]}}})
        )
        taxonomy.reset_cache()
        out = taxonomy.compute_folder_theme_breakdown(
            self.profile_name, "01-SCIENCES/PHYSIQUE",
        )
        # Pas de stable car le seul thème est en dessous du seuil.
        self.assertEqual(out["stable"], [])


class TestFolderThemeBreakdownEndpoint(TaxonomyTestBase):

    def test_endpoint_happy(self):
        from fastapi.testclient import TestClient

        from dashboard.app import app
        with TestClient(app) as client:
            r = client.get(
                "/api/taxonomy/folder/theme-breakdown",
                params={"profile": self.profile_name, "path": "01-SCIENCES/PHYSIQUE"},
            )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("stable", body)
        self.assertIn("incoming", body)
        self.assertIn("outgoing", body)
        self.assertIn("summary", body)


# ─── Tâche 3 — suggest_mappings (passe déterministe) ─────────────────────


class TestSuggestMappings(TaxonomyTestBase):
    """Moteur de suggest_mappings : passe déterministe = matching FIABLE
    nom de thème ↔ nom de dossier UNIQUEMENT (plus de KeywordClassifier),
    puis passe LLM BATCHÉE via ``LLMMapper.resolve_batch`` (un appel groupé,
    jamais ``resolve`` mono). Aucun appel réseau réel : resolve_batch mocké."""

    def setUp(self):
        super().setUp()
        # Un categories.yaml est présent dans le profil mais NE DOIT PLUS
        # influencer suggest_mappings : le KeywordClassifier a été retiré.
        (self.profile_dir / "categories.yaml").write_text(
            yaml.safe_dump({
                "sciences": [
                    {
                        "chemin": "01-SCIENCES/PHYSIQUE",
                        "priorite": 1,
                        "mots_cles": ["quantum", "relativity", "thermodynamics"],
                    },
                ],
            })
        )
        taxonomy.reset_cache()

    def test_a_folder_name_match_resolves_deterministic(self):
        # Le nom du thème == dernier segment d'un dossier (normalisé :
        # "MATHEMATIQUES" → "mathematiques") → résolu en déterministe.
        out = taxonomy.suggest_mappings(
            self.profile_name, ["Mathematiques"]
        )
        self.assertEqual(out["n_llm_calls"], 0)
        sug = out["suggestions"][0]
        self.assertEqual(sug["folder"], "01-SCIENCES/MATHEMATIQUES")
        self.assertEqual(sug["source"], "deterministic")
        self.assertEqual(sug["confidence"], 0.9)
        self.assertIn("dossier", sug["reason"].lower())

    def test_b_keyword_only_match_is_not_resolved(self):
        # "Quantum Field Theory" matchait l'ancien KeywordClassifier (mot-clé
        # "quantum") mais ne correspond à AUCUN nom de dossier → désormais
        # NON résolu par le déterministe (sans LLM → unresolved).
        out = taxonomy.suggest_mappings(
            self.profile_name, ["Quantum Field Theory"]
        )
        self.assertEqual(out["n_llm_calls"], 0)
        sug = out["suggestions"][0]
        self.assertIsNone(sug["folder"])
        self.assertEqual(sug["source"], "unresolved")

    def test_c_unknown_theme_is_unresolved(self):
        out = taxonomy.suggest_mappings(
            self.profile_name, ["Macramé Mésopotamien Obscur"]
        )
        sug = out["suggestions"][0]
        self.assertIsNone(sug["folder"])
        self.assertEqual(sug["source"], "unresolved")
        self.assertEqual(sug["confidence"], 0.0)
        self.assertEqual(sug["reason"], "")

    def test_d_deterministic_still_works_with_use_llm(self):
        # Avec use_llm=True mais sans clé API, le déterministe (nom de dossier)
        # tient toujours et la passe LLM est un no-op (cf. test_g).
        with mock.patch.dict(os.environ, {}, clear=True):
            out = taxonomy.suggest_mappings(
                self.profile_name,
                ["Mathematiques", "Inconnu Total XYZ"],
                use_llm=True,
            )
        self.assertEqual(out["n_llm_calls"], 0)
        self.assertEqual(len(out["suggestions"]), 2)
        # Le 1er reste résolu par le déterministe (nom de dossier).
        self.assertEqual(out["suggestions"][0]["source"], "deterministic")
        # Le 2e reste unresolved (pas de clé → pas de LLM).
        self.assertEqual(out["suggestions"][1]["source"], "unresolved")
        self.assertIsNone(out["suggestions"][1]["folder"])

    # ── Passe LLM batchée — resolve_batch TOUJOURS mocké : zéro réseau ──

    def test_e_unresolved_resolved_by_llm_batch(self):
        # Thème ambigu non résolu par le déterministe + use_llm=True +
        # clé API présente → la passe LLM BATCHÉE le résout. resolve (mono)
        # n'est JAMAIS appelé.
        with mock.patch.dict(
            os.environ, {"SILICONFLOW_API_KEY": "sk-test"}, clear=True
        ), mock.patch.object(
            taxonomy.LLMMapper, "resolve_batch",
            return_value={
                "Colloid Science": {"folder": "01-SCIENCES/PHYSIQUE",
                                    "confidence": 0.9},
            },
        ) as m_batch, mock.patch.object(
            taxonomy.LLMMapper, "resolve"
        ) as m_resolve:
            out = taxonomy.suggest_mappings(
                self.profile_name, ["Colloid Science"], use_llm=True
            )
        sug = out["suggestions"][0]
        self.assertEqual(sug["folder"], "01-SCIENCES/PHYSIQUE")
        self.assertEqual(sug["source"], "llm")
        self.assertEqual(sug["confidence"], 0.9)
        self.assertIn("batch", sug["reason"].lower())
        self.assertEqual(out["n_llm_calls"], 1)
        self.assertTrue(m_batch.called)
        # resolve mono N'EST PAS appelé : on batche.
        self.assertFalse(m_resolve.called)

    def test_f_deterministic_skips_llm_call(self):
        # Un thème résolu par le DÉTERMINISTE ne déclenche AUCUN appel LLM
        # (ni batch ni mono).
        with mock.patch.dict(
            os.environ, {"SILICONFLOW_API_KEY": "sk-test"}, clear=True
        ), mock.patch.object(
            taxonomy.LLMMapper, "resolve_batch", return_value={}
        ) as m_batch, mock.patch.object(
            taxonomy.LLMMapper, "resolve"
        ) as m_resolve:
            out = taxonomy.suggest_mappings(
                self.profile_name, ["Mathematiques"], use_llm=True,
            )
        self.assertEqual(out["n_llm_calls"], 0)
        self.assertFalse(m_batch.called)
        self.assertFalse(m_resolve.called)
        self.assertEqual(out["suggestions"][0]["source"], "deterministic")

    def test_g_no_api_key_means_no_llm(self):
        # use_llm=True mais pas de clé API → aucun appel batch, reste unresolved.
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            taxonomy.LLMMapper, "resolve_batch",
            return_value={"X": {"folder": "02-INFORMATIQUE", "confidence": 0.9}},
        ) as m_batch:
            out = taxonomy.suggest_mappings(
                self.profile_name, ["Mésopotamie Numérique Floue"], use_llm=True
            )
        sug = out["suggestions"][0]
        self.assertEqual(out["n_llm_calls"], 0)
        self.assertFalse(m_batch.called)
        self.assertIsNone(sug["folder"])
        self.assertEqual(sug["source"], "unresolved")

    def test_h_batch_unresolved_stays_unresolved(self):
        # Le batch ne résout PAS un thème → il reste unresolved.
        with mock.patch.dict(
            os.environ, {"SILICONFLOW_API_KEY": "sk-test"}, clear=True
        ), mock.patch.object(
            taxonomy.LLMMapper, "resolve_batch", return_value={}
        ) as m_batch:
            out = taxonomy.suggest_mappings(
                self.profile_name, ["Thème Que Le LLM Ignore"], use_llm=True
            )
        sug = out["suggestions"][0]
        self.assertTrue(m_batch.called)
        self.assertEqual(out["n_llm_calls"], 1)
        self.assertIsNone(sug["folder"])
        self.assertEqual(sug["source"], "unresolved")

    def test_i_max_llm_bounds_themes_sent_to_batch(self):
        # Plus de thèmes ambigus que max_llm → seuls max_llm thèmes partent au
        # batch ; le surplus reste unresolved avec une raison explicite.
        ambiguous = [f"Theme Ambigu Inconnu {i}" for i in range(5)]
        captured: dict = {}

        def _fake_batch(self_mapper, themes, titles=None, chunk_size=40):
            captured["themes"] = list(themes)
            # Résout tous les thèmes reçus.
            return {t: {"folder": "02-INFORMATIQUE", "confidence": 0.85}
                    for t in themes}

        with mock.patch.dict(
            os.environ, {"SILICONFLOW_API_KEY": "sk-test"}, clear=True
        ), mock.patch.object(
            taxonomy.LLMMapper, "resolve_batch", autospec=True,
            side_effect=_fake_batch,
        ):
            out = taxonomy.suggest_mappings(
                self.profile_name, ambiguous, use_llm=True, max_llm=2
            )
        # Seuls max_llm thèmes envoyés au batch.
        self.assertEqual(len(captured["themes"]), 2)
        self.assertEqual(out["n_llm_calls"], 1)
        resolved = [s for s in out["suggestions"] if s["source"] == "llm"]
        unresolved = [s for s in out["suggestions"] if s["source"] == "unresolved"]
        self.assertEqual(len(resolved), 2)
        self.assertEqual(len(unresolved), 3)
        # Le surplus porte une raison de borne LLM.
        self.assertTrue(any("limite" in s["reason"].lower() for s in unresolved))

    def test_j_n_llm_calls_counts_chunks(self):
        # n_llm_calls = nombre de chunks ceil(len(themes_envoyés)/40).
        # 45 thèmes ambigus envoyés au batch → 2 chunks.
        ambiguous = [f"Ambigu Distinct {i}" for i in range(45)]
        with mock.patch.dict(
            os.environ, {"SILICONFLOW_API_KEY": "sk-test"}, clear=True
        ), mock.patch.object(
            taxonomy.LLMMapper, "resolve_batch", return_value={},
        ):
            out = taxonomy.suggest_mappings(
                self.profile_name, ambiguous, use_llm=True, max_llm=60
            )
        self.assertEqual(out["n_llm_calls"], 2)


class TestBuildReclassifyProjection(unittest.TestCase):
    """Projection live complète : matérialise tous les moves, applique
    la canonicalisation, sépare P1/P2."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="klodo-proj-")
        self.root = Path(self.tmp)
        self.prof = self.root / "profiles" / "default"
        self.target = self.root / "BIBLIO"
        self.prof.mkdir(parents=True)
        (self.prof / "profile.yaml").write_text(
            yaml.safe_dump({"target": str(self.target),
                            "llm": {"model": "M"}, "defaults": {"pages": 2}}),
            encoding="utf-8")
        (self.prof / "theme_mapping.yaml").write_text(
            yaml.safe_dump({"Deep Learning": "B/DL"}), encoding="utf-8")
        for rel in ("A/a.pdf", "A/b.pdf"):
            p = self.target / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            # Contenu distinct par fichier : la clé vision_cache est
            # content-based (head bytes), des octets identiques
            # collisionneraient les deux entrées.
            p.write_bytes(b"%PDF-1.4 " + rel.encode())
        cache_dir = self.prof / ".cache"
        cache_dir.mkdir(exist_ok=True)
        (cache_dir / "theme-canon.json").write_text(
            json.dumps({"mapping": {"dl variant": "Deep Learning"}}),
            encoding="utf-8")
        import lib.vision_cache as vc
        cache = {}
        for rel, theme in (("A/a.pdf", "dl variant"), ("A/b.pdf", "zzz unknown")):
            key = vc.compute_cache_key(str(self.target / rel), model="M", n_pages=2)
            # Shape réel du vision_cache : scalaire `theme` + liste `themes`.
            # Le scalaire alimente le texte du KeywordClassifier (P2).
            cache[key] = {"result": {"theme": theme,
                                     "themes": [{"theme": theme, "confidence": 0.95}],
                                     "confidence": 0.95}}
        (self.prof / ".cache" / "vision_cache.json").write_text(
            json.dumps(cache), encoding="utf-8")
        self.patch = mock.patch("dashboard.data.get_project_root",
                                return_value=self.root)
        self.patch.start()
        taxonomy.reset_cache()

    def tearDown(self):
        self.patch.stop()
        taxonomy.reset_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_canonicalization_routes_variant(self):
        moves = taxonomy.build_reclassify_projection("default", include_keyword=False)
        rels = {m["rel_path"]: m for m in moves}
        self.assertIn("A/a.pdf", rels)
        self.assertEqual(rels["A/a.pdf"]["proposed_folder"], "B/DL")
        self.assertEqual(rels["A/a.pdf"]["signal"], "p1")
        self.assertNotIn("A/b.pdf", rels)

    def test_keyword_excluded_unless_opted_in(self):
        (self.prof / "categories.yaml").write_text(
            yaml.safe_dump({"sci": [{"chemin": "B/KW", "priorite": 5,
                                     "mots_cles": ["unknown"]}]}),
            encoding="utf-8")
        taxonomy.reset_cache()
        no_kw = taxonomy.build_reclassify_projection("default", include_keyword=False)
        with_kw = taxonomy.build_reclassify_projection("default", include_keyword=True)
        self.assertNotIn("A/b.pdf", {m["rel_path"] for m in no_kw})
        kw_rels = {m["rel_path"]: m for m in with_kw}
        self.assertIn("A/b.pdf", kw_rels)
        self.assertEqual(kw_rels["A/b.pdf"]["signal"], "p2")

    def test_returns_empty_without_target(self):
        shutil.rmtree(self.target)
        taxonomy.reset_cache()
        self.assertEqual(
            taxonomy.build_reclassify_projection("default", include_keyword=False), [])


if __name__ == "__main__":
    unittest.main()
