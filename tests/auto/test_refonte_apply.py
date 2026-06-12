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


if __name__ == "__main__":
    unittest.main()
