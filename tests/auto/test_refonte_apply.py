#!/usr/bin/env python3
"""Tests for dashboard/agent_refonte_apply.py — apply/execute d'une refonte."""

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

from dashboard import agent_refonte_apply as ara  # noqa: E402
from dashboard import taxonomy  # noqa: E402


class ApplyTestBase(unittest.TestCase):
    """Profil fixture + target tmp + run Phase B done complet."""

    RUN_ID = "run-apply-1"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="klodo-apply-")
        self.root = Path(self.tmp)
        self.profile_dir = self.root / "profiles" / "default"
        self.target = self.root / "BIBLIO"

        # ── Profil prod
        (self.profile_dir).mkdir(parents=True)
        (self.profile_dir / "profile.yaml").write_text(
            yaml.safe_dump({"target": str(self.target)}), encoding="utf-8")
        (self.profile_dir / "tree.yaml").write_text(
            yaml.safe_dump({"folders": ["A", "B"]}), encoding="utf-8")
        (self.profile_dir / "theme_mapping.yaml").write_text(
            yaml.safe_dump({"old theme": "A"}), encoding="utf-8")

        # ── Bibliothèque physique
        for rel in ("A/sure.pdf", "A/ref.pdf", "A/lowconf.pdf", "A/stay.pdf"):
            p = self.target / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"%PDF-1.4 x")

        # ── Run Phase B done
        self.run_dir = self.profile_dir / ".cache" / "refonte" / self.RUN_ID
        (self.run_dir / "proposed").mkdir(parents=True)
        (self.run_dir / "simulation").mkdir(parents=True)
        (self.run_dir / "status.json").write_text(json.dumps({
            "run_id": self.RUN_ID, "profile": "default",
            "status": "done", "phase": "B"}), encoding="utf-8")
        (self.run_dir / "proposed" / "tree-proposed.yaml").write_text(
            yaml.safe_dump({"folders": ["A", "B/Dest", "B/Ref"]}), encoding="utf-8")
        (self.run_dir / "proposed" / "theme_mapping-proposed.yaml").write_text(
            yaml.safe_dump({"old theme": "B/Dest", "new theme": "B/Ref"}),
            encoding="utf-8")
        (self.run_dir / "proposed" / "changes.json").write_text(json.dumps({
            "creations": [{"path": "B/Dest", "rationale": "x"},
                          {"path": "B/Ref", "rationale": "x"}],
            "fusions": [], "renamings": [], "deletions": [],
            "mappings_added": [{"theme": "new theme", "folder": "B/Ref",
                                "rationale": "x"}],
        }), encoding="utf-8")
        (self.run_dir / "simulation" / "reclassify-projection.csv").write_text(
            "rel_path,current_folder,proposed_folder,changed,source,top_theme,confidence,score\n"
            "A/sure.pdf,A,B/Dest,True,LLM (theme),X,0.95,0.9\n"
            "A/ref.pdf,A,B/Ref,True,LLM (theme→refined),X,0.8,0.8\n"
            "A/lowconf.pdf,A,B/Low,True,LLM (theme),X,0.5,0.5\n"
            "A/stay.pdf,A,A,False,LLM (theme),X,0.95,0.9\n",
            encoding="utf-8")

        self.patch = mock.patch("dashboard.data.get_project_root",
                                return_value=self.root)
        self.patch.start()
        taxonomy.reset_cache()

    def tearDown(self):
        self.patch.stop()
        taxonomy.reset_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestStateAndPreview(ApplyTestBase):
    def test_default_state(self):
        st = ara.read_state("default", self.RUN_ID)
        self.assertFalse(st["adopted"])
        self.assertFalse(st["executed"])
        self.assertIsNone(st["config_backup"])
        self.assertIsNone(st["move_batch_id"])

    def test_write_then_read_state(self):
        ara.write_state("default", self.RUN_ID, {"adopted": True})
        st = ara.read_state("default", self.RUN_ID)
        self.assertTrue(st["adopted"])
        self.assertFalse(st["executed"])  # défauts préservés

    def test_preview_counts(self):
        p = ara.build_preview("default", self.RUN_ID)
        self.assertEqual(p["n_moves"], 2)
        self.assertEqual(p["n_doubt_excluded"], 1)
        self.assertEqual(p["n_stable"], 1)
        self.assertEqual(p["n_creations"], 2)
        self.assertEqual(p["n_mappings_added"], 1)
        self.assertEqual(p["n_renames"], 0)
        dests = {d["folder"]: d["n"] for d in p["top_destinations"]}
        self.assertEqual(dests, {"B/Dest": 1, "B/Ref": 1})
        self.assertIn("state", p)

    def test_preview_unknown_run_raises_404(self):
        with self.assertRaises(ara.ApplyError) as ctx:
            ara.build_preview("default", "nope")
        self.assertEqual(ctx.exception.status, 404)

    def test_assert_run_done_rejects_phase_a(self):
        (self.run_dir / "status.json").write_text(json.dumps({
            "status": "done", "phase": "A"}), encoding="utf-8")
        with self.assertRaises(ara.ApplyError) as ctx:
            ara._assert_run_phase_b_done("default", self.RUN_ID)
        self.assertEqual(ctx.exception.status, 409)

    def test_assert_run_done_rejects_running(self):
        (self.run_dir / "status.json").write_text(json.dumps({
            "status": "running", "phase": "B"}), encoding="utf-8")
        with self.assertRaises(ara.ApplyError) as ctx:
            ara._assert_run_phase_b_done("default", self.RUN_ID)
        self.assertEqual(ctx.exception.status, 409)

    def test_assert_no_op_in_progress_blocks_when_running(self):
        ara._write_progress("default", self.RUN_ID, {"op": "execute",
                                                     "status": "running"})
        with self.assertRaises(ara.ApplyError) as ctx:
            ara._assert_no_op_in_progress("default", self.RUN_ID)
        self.assertEqual(ctx.exception.status, 409)
        # done → ne bloque plus
        ara._write_progress("default", self.RUN_ID, {"op": "execute",
                                                     "status": "done"})
        ara._assert_no_op_in_progress("default", self.RUN_ID)  # no raise


class TestAdoptStructure(ApplyTestBase):
    def test_adopt_promotes_yaml_and_creates_dirs(self):
        result = ara.adopt_structure("default", self.RUN_ID)
        self.assertTrue(result["ok"])
        # tree.yaml promu
        tree = yaml.safe_load((self.profile_dir / "tree.yaml").read_text())
        self.assertIn("B/Dest", tree["folders"])
        # mapping promu
        mapping = yaml.safe_load(
            (self.profile_dir / "theme_mapping.yaml").read_text())
        self.assertEqual(mapping["old theme"], "B/Dest")
        self.assertEqual(mapping["new theme"], "B/Ref")
        # dossiers physiques créés
        self.assertTrue((self.target / "B" / "Dest").is_dir())
        self.assertTrue((self.target / "B" / "Ref").is_dir())
        # état + backup
        st = ara.read_state("default", self.RUN_ID)
        self.assertTrue(st["adopted"])
        self.assertIsNotNone(st["config_backup"])
        backup_dir = (self.profile_dir / ".cache" / "taxonomy-backups"
                      / st["config_backup"])
        self.assertTrue((backup_dir / "tree.yaml").exists())

    def test_adopt_promotes_categories_when_present(self):
        (self.run_dir / "proposed" / "categories-proposed.yaml").write_text(
            yaml.safe_dump({"informatique": [{"folder": "B/Dest"}]}),
            encoding="utf-8")
        ara.adopt_structure("default", self.RUN_ID)
        self.assertTrue((self.profile_dir / "categories.yaml").exists())

    def test_adopt_without_categories_proposed_is_fine(self):
        ara.adopt_structure("default", self.RUN_ID)
        self.assertFalse((self.profile_dir / "categories.yaml").exists())

    def test_adopt_twice_raises_409(self):
        ara.adopt_structure("default", self.RUN_ID)
        with self.assertRaises(ara.ApplyError) as ctx:
            ara.adopt_structure("default", self.RUN_ID)
        self.assertEqual(ctx.exception.status, 409)

    def test_adopt_blocked_by_other_adopted_run(self):
        other = self.profile_dir / ".cache" / "refonte" / "run-other" / "apply"
        other.mkdir(parents=True)
        (other / "state.json").write_text(
            json.dumps({"adopted": True}), encoding="utf-8")
        with self.assertRaises(ara.ApplyError) as ctx:
            ara.adopt_structure("default", self.RUN_ID)
        self.assertEqual(ctx.exception.status, 409)

    def test_adopt_blocked_by_lock(self):
        lock = self.profile_dir / ".cache" / "taxonomy.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("busy")
        with self.assertRaises(ara.ApplyError) as ctx:
            ara.adopt_structure("default", self.RUN_ID)
        self.assertEqual(ctx.exception.status, 423)

    def test_adopt_missing_target_raises_500(self):
        shutil.rmtree(self.target)
        with self.assertRaises(ara.ApplyError) as ctx:
            ara.adopt_structure("default", self.RUN_ID)
        self.assertEqual(ctx.exception.status, 500)

    def test_adopt_rejects_traversal_creation_path(self):
        """Un path de création contenant ../ ne doit jamais s'échapper du target."""
        changes_path = self.run_dir / "proposed" / "changes.json"
        changes = json.loads(changes_path.read_text(encoding="utf-8"))
        changes["creations"].append({"path": "../../escape", "rationale": "x"})
        changes_path.write_text(json.dumps(changes), encoding="utf-8")
        with self.assertRaises(ara.ApplyError) as ctx:
            ara.adopt_structure("default", self.RUN_ID)
        self.assertEqual(ctx.exception.status, 400)
        self.assertFalse((self.root / "escape").exists())


class TestRestoreConfig(ApplyTestBase):
    def test_restore_brings_back_yaml_and_removes_empty_created_dirs(self):
        ara.adopt_structure("default", self.RUN_ID)
        result = ara.restore_config("default", self.RUN_ID)
        self.assertTrue(result["ok"])
        mapping = yaml.safe_load(
            (self.profile_dir / "theme_mapping.yaml").read_text())
        self.assertEqual(mapping, {"old theme": "A"})
        self.assertFalse((self.target / "B" / "Dest").exists())
        st = ara.read_state("default", self.RUN_ID)
        self.assertFalse(st["adopted"])
        self.assertTrue(st["rolled_back_config"])

    def test_restore_preserves_nonempty_created_dirs(self):
        ara.adopt_structure("default", self.RUN_ID)
        keeper = self.target / "B" / "Dest" / "manual.pdf"
        keeper.write_bytes(b"%PDF-1.4 x")
        result = ara.restore_config("default", self.RUN_ID)
        self.assertTrue((self.target / "B" / "Dest").is_dir())
        self.assertIn("B/Dest", result["kept_nonempty"])

    def test_restore_not_adopted_raises_409(self):
        with self.assertRaises(ara.ApplyError) as ctx:
            ara.restore_config("default", self.RUN_ID)
        self.assertEqual(ctx.exception.status, 409)

    def test_restore_blocked_after_execute(self):
        ara.adopt_structure("default", self.RUN_ID)
        ara.write_state("default", self.RUN_ID, {"executed": True})
        with self.assertRaises(ara.ApplyError) as ctx:
            ara.restore_config("default", self.RUN_ID)
        self.assertEqual(ctx.exception.status, 409)

    def test_restore_allowed_after_undo_moves(self):
        """Chemin légitime : executed puis rolled_back_moves → restore OK."""
        ara.adopt_structure("default", self.RUN_ID)
        ara.write_state("default", self.RUN_ID,
                        {"executed": True, "rolled_back_moves": True})
        result = ara.restore_config("default", self.RUN_ID)
        self.assertTrue(result["ok"])


class TestExecuteMoves(ApplyTestBase):
    def setUp(self):
        super().setUp()
        ara.adopt_structure("default", self.RUN_ID)
        # logs/ du projet mocké → tmpdir
        (self.root / "logs").mkdir(exist_ok=True)

    def test_run_moves_moves_safe_files_only(self):
        result = ara._run_moves("default", self.RUN_ID)
        self.assertEqual(result["n_moved"], 2)
        self.assertEqual(result["n_failed"], 0)
        self.assertTrue((self.target / "B" / "Dest" / "sure.pdf").exists())
        self.assertTrue((self.target / "B" / "Ref" / "ref.pdf").exists())
        self.assertFalse((self.target / "A" / "sure.pdf").exists())
        # exclus : doute + stable restent en place
        self.assertTrue((self.target / "A" / "lowconf.pdf").exists())
        self.assertTrue((self.target / "A" / "stay.pdf").exists())

    def test_run_moves_journals_each_move(self):
        ara._run_moves("default", self.RUN_ID)
        from lib import move_journal as mj
        recs = mj.read_journal(self.profile_dir)
        self.assertEqual(len(recs), 2)
        st = ara.read_state("default", self.RUN_ID)
        self.assertEqual({r["batch"] for r in recs}, {st["move_batch_id"]})

    def test_run_moves_skips_stale_and_collision_without_abort(self):
        # stale : source disparue
        (self.target / "A" / "sure.pdf").unlink()
        # collision : destination occupée
        dest = self.target / "B" / "Ref" / "ref.pdf"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"%PDF-1.4 other")
        result = ara._run_moves("default", self.RUN_ID)
        self.assertEqual(result["n_moved"], 0)
        self.assertEqual(result["n_skipped"], 2)
        # l'original de la collision n'a pas bougé ni été écrasé
        self.assertTrue((self.target / "A" / "ref.pdf").exists())
        self.assertEqual(dest.read_bytes(), b"%PDF-1.4 other")

    def test_run_moves_writes_report_csv(self):
        ara._run_moves("default", self.RUN_ID)
        reports = list((self.root / "logs").glob("rapport_apply_*.csv"))
        self.assertEqual(len(reports), 1)
        content = reports[0].read_text(encoding="utf-8")
        self.assertIn("A/sure.pdf", content)
        self.assertIn("moved", content)

    def test_run_moves_prunes_empty_source_dirs(self):
        # A devient vide si on retire les 2 fichiers restants
        (self.target / "A" / "lowconf.pdf").unlink()
        (self.target / "A" / "stay.pdf").unlink()
        ara._run_moves("default", self.RUN_ID)
        self.assertFalse((self.target / "A").exists())
        self.assertTrue(self.target.exists())  # jamais le target lui-même

    def test_run_moves_updates_state(self):
        ara._run_moves("default", self.RUN_ID)
        st = ara.read_state("default", self.RUN_ID)
        self.assertTrue(st["executed"])
        self.assertEqual(st["n_moved"], 2)
        self.assertIsNotNone(st["move_batch_id"])
        progress = ara._read_progress("default", self.RUN_ID)
        self.assertEqual(progress["status"], "done")

    def test_start_execute_gating(self):
        # non adopté → 409
        ara.write_state("default", self.RUN_ID, {"adopted": False})
        with self.assertRaises(ara.ApplyError) as ctx:
            ara.start_execute("default", self.RUN_ID)
        self.assertEqual(ctx.exception.status, 409)
        # déjà exécuté → 409
        ara.write_state("default", self.RUN_ID,
                        {"adopted": True, "executed": True})
        with self.assertRaises(ara.ApplyError) as ctx:
            ara.start_execute("default", self.RUN_ID)
        self.assertEqual(ctx.exception.status, 409)

    def test_start_execute_blocked_by_lock(self):
        lock = self.profile_dir / ".cache" / "taxonomy.lock"
        lock.write_text("busy")
        with self.assertRaises(ara.ApplyError) as ctx:
            ara.start_execute("default", self.RUN_ID)
        self.assertEqual(ctx.exception.status, 423)

    def test_execute_job_releases_lock_even_on_error(self):
        lock = self.profile_dir / ".cache" / "taxonomy.lock"
        lock.write_text("refonte-apply")
        with mock.patch.object(ara, "_run_moves",
                               side_effect=RuntimeError("boom")):
            ara._execute_job("default", self.RUN_ID)
        self.assertFalse(lock.exists())
        progress = ara._read_progress("default", self.RUN_ID)
        self.assertEqual(progress["status"], "error")
        self.assertIn("boom", progress["error"])


if __name__ == "__main__":
    unittest.main()
