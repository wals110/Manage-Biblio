#!/usr/bin/env python3
"""Tests de l'apply global de rename (dashboard/rename_apply.py).

Le moteur de rename (rename_audit / commit_rename_bulk / undo_batch_for_profile)
est mocké — on ne teste que la mécanique preview/execute/undo + l'état figé.
"""

import csv
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)


def _audit(candidates):
    return {"candidates": candidates, "stats": {}, "config": {}}


class TestRenameApply(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="klodo-rna-")
        self.root = Path(self.tmp)
        self.patch = mock.patch("dashboard.data.get_project_root", return_value=self.root)
        self.patch.start()
        # exécution synchrone du thread
        import dashboard.rename_apply as ra
        self.ra = ra
        self.spawn_patch = mock.patch.object(ra, "_spawn",
                                             lambda fn, args, name: fn(*args))
        self.spawn_patch.start()

    def tearDown(self):
        mock.patch.stopall()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _proj_rows(self, profile="perso"):
        p = self.root / "profiles" / profile / ".cache" / "rename" / "apply" / "projection.csv"
        with p.open(encoding="utf-8") as f:
            return list(csv.DictReader(f))

    # ── preview ──────────────────────────────────────────────────────────
    def test_preview_keeps_only_scope_categories(self):
        cands = [
            {"rel_path": "a.pdf", "current_name": "a.pdf", "suggested_name": "Algo.pdf", "category": "placeholder"},
            {"rel_path": "b.pdf", "current_name": "b.pdf", "suggested_name": "Bayes.pdf", "category": "divergent"},
            {"rel_path": "c.pdf", "current_name": "c.pdf", "suggested_name": "C.pdf", "category": "minor_case"},
            {"rel_path": "d.pdf", "current_name": "Deep.pdf", "suggested_name": "Deep.pdf", "category": "ok"},
        ]
        with mock.patch("dashboard.rename.rename_audit", return_value=_audit(cands)):
            r = self.ra.build_preview("perso")
        self.assertEqual(r["n_planned"], 2)              # placeholder + divergent uniquement
        self.assertEqual(r["n_placeholder"], 1)
        self.assertEqual(r["n_divergent"], 1)
        rows = self._proj_rows()
        self.assertEqual([row["rel_path"] for row in rows], ["a.pdf", "b.pdf"])
        self.assertEqual(self.ra.read_state("perso")["n_planned"], 2)

    def test_preview_empty_scope(self):
        cands = [{"rel_path": "c.pdf", "current_name": "c.pdf",
                  "suggested_name": "C.pdf", "category": "minor_case"}]
        with mock.patch("dashboard.rename.rename_audit", return_value=_audit(cands)):
            r = self.ra.build_preview("perso")
        self.assertEqual(r["n_planned"], 0)
        self.assertEqual(self._proj_rows(), [])

    # ── execute ──────────────────────────────────────────────────────────
    def test_execute_replays_frozen_and_records_state(self):
        cands = [
            {"rel_path": "a.pdf", "current_name": "a.pdf", "suggested_name": "Algo.pdf", "category": "placeholder"},
            {"rel_path": "b.pdf", "current_name": "b.pdf", "suggested_name": "Bayes.pdf", "category": "divergent"},
        ]
        with mock.patch("dashboard.rename.rename_audit", return_value=_audit(cands)):
            self.ra.build_preview("perso")
        captured = {}

        def fake_bulk(profile, items, batch_id=""):
            captured["items"] = items
            return {"ok": True, "batch_id": "B1", "n_total": len(items),
                    "n_renamed": 2, "n_errors": 0, "successes": [], "errors": []}

        with mock.patch("dashboard.rename.commit_rename_bulk", side_effect=fake_bulk):
            self.ra.start_execute("perso")
        # items rejoués = la projection figée
        self.assertEqual([i["rel_path"] for i in captured["items"]], ["a.pdf", "b.pdf"])
        self.assertEqual([i["new_name"] for i in captured["items"]], ["Algo.pdf", "Bayes.pdf"])
        st = self.ra.read_state("perso")
        self.assertTrue(st["executed"])
        self.assertEqual(st["batch_id"], "B1")
        self.assertEqual(st["n_renamed"], 2)
        prog = self.ra.get_status("perso")["progress"]
        self.assertEqual(prog["status"], "done")

    def test_execute_without_preview_409(self):
        with self.assertRaises(self.ra.RenameApplyError) as ctx:
            self.ra.start_execute("perso")
        self.assertEqual(ctx.exception.status, 409)

    def test_execute_empty_projection_does_not_call_bulk(self):
        cands = [{"rel_path": "c.pdf", "current_name": "c.pdf",
                  "suggested_name": "C.pdf", "category": "minor_case"}]
        with mock.patch("dashboard.rename.rename_audit", return_value=_audit(cands)):
            self.ra.build_preview("perso")        # 0 dans le périmètre
        with mock.patch("dashboard.rename.commit_rename_bulk") as bulk:
            self.ra.start_execute("perso")
            bulk.assert_not_called()              # pas d'appel sur projection vide
        st = self.ra.read_state("perso")
        self.assertTrue(st["executed"])
        self.assertEqual(st["n_renamed"], 0)
        self.assertEqual(self.ra.get_status("perso")["progress"]["status"], "done")

    def test_execute_writes_error_status_on_failure(self):
        cands = [{"rel_path": "a.pdf", "current_name": "a.pdf",
                  "suggested_name": "Algo.pdf", "category": "placeholder"}]
        with mock.patch("dashboard.rename.rename_audit", return_value=_audit(cands)):
            self.ra.build_preview("perso")
        with mock.patch("dashboard.rename.commit_rename_bulk",
                        side_effect=RuntimeError("boom")):
            self.ra.start_execute("perso")
        prog = self.ra.get_status("perso")["progress"]
        self.assertEqual(prog["status"], "error")
        self.assertIn("boom", prog["error"])

    # ── undo ─────────────────────────────────────────────────────────────
    def test_undo_calls_undo_batch_and_resets_state(self):
        self.ra.write_state("perso", {"executed": True, "batch_id": "B1", "n_renamed": 2})
        captured = {}

        def fake_undo(profile, batch_id):
            captured["batch_id"] = batch_id
            return {"n_undone": 2, "n_errors": 0}

        with mock.patch("dashboard.rename.undo_batch_for_profile", side_effect=fake_undo):
            self.ra.start_undo("perso")
        self.assertEqual(captured["batch_id"], "B1")
        st = self.ra.read_state("perso")
        self.assertFalse(st["executed"])
        self.assertTrue(st["rolled_back"])

    def test_undo_without_execute_409(self):
        with self.assertRaises(self.ra.RenameApplyError) as ctx:
            self.ra.start_undo("perso")
        self.assertEqual(ctx.exception.status, 409)


class TestRenameApplyEndpoints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="klodo-rnae-")
        self.root = Path(self.tmp)
        mock.patch("dashboard.data.get_project_root", return_value=self.root).start()
        import dashboard.rename_apply as ra
        self.ra = ra
        mock.patch.object(ra, "_spawn", lambda fn, args, name: fn(*args)).start()
        from fastapi.testclient import TestClient

        from dashboard.app import app
        self.client = TestClient(app)

    def tearDown(self):
        mock.patch.stopall()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_preview_then_execute_then_undo_flow(self):
        cands = [{"rel_path": "a.pdf", "current_name": "a.pdf",
                  "suggested_name": "Algo.pdf", "category": "placeholder"}]
        with mock.patch("dashboard.rename.rename_audit", return_value=_audit(cands)):
            r = self.client.post("/api/rename/apply/preview", json={"profile": "perso"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["n_planned"], 1)

        with mock.patch("dashboard.rename.commit_rename_bulk",
                        return_value={"ok": True, "batch_id": "B1", "n_total": 1,
                                      "n_renamed": 1, "n_errors": 0, "successes": [], "errors": []}):
            e = self.client.post("/api/rename/apply/execute", json={"profile": "perso"})
        self.assertEqual(e.status_code, 200)
        st = self.client.get("/api/rename/apply/status", params={"profile": "perso"}).json()
        self.assertTrue(st["state"]["executed"])
        self.assertEqual(st["progress"]["status"], "done")

        with mock.patch("dashboard.rename.undo_batch_for_profile",
                        return_value={"n_undone": 1, "n_errors": 0}):
            u = self.client.post("/api/rename/apply/undo", json={"profile": "perso"})
        self.assertEqual(u.status_code, 200)
        self.assertFalse(self.ra.read_state("perso")["executed"])

    def test_execute_without_preview_409(self):
        r = self.client.post("/api/rename/apply/execute", json={"profile": "perso"})
        self.assertEqual(r.status_code, 409)

    def test_missing_profile_400(self):
        r = self.client.post("/api/rename/apply/preview", json={})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
