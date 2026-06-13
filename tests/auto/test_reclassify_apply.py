#!/usr/bin/env python3
"""Apply global — état, hash config, preview figé, execute, undo."""

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

from fastapi.testclient import TestClient  # noqa: E402

from dashboard import reclassify_apply as rca  # noqa: E402
from dashboard import taxonomy  # noqa: E402
from dashboard.app import app  # noqa: E402


class GlobalApplyBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="klodo-gapply-")
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
        (self.prof / "tree.yaml").write_text(
            yaml.safe_dump({"folders": ["A", "B/DL"]}), encoding="utf-8")
        for rel in ("A/a.pdf", "A/b.pdf"):
            p = self.target / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"%PDF-1.4 " + rel.encode())
        cache_dir = self.prof / ".cache"
        cache_dir.mkdir(exist_ok=True)
        import lib.vision_cache as vc
        cache = {}
        for rel, theme in (("A/a.pdf", "Deep Learning"), ("A/b.pdf", "zzz")):
            key = vc.compute_cache_key(str(self.target / rel), model="M", n_pages=2)
            cache[key] = {"result": {"theme": theme,
                                     "themes": [{"theme": theme, "confidence": 0.95}],
                                     "confidence": 0.95}}
        (cache_dir / "vision_cache.json").write_text(json.dumps(cache), encoding="utf-8")
        self.patch = mock.patch("dashboard.data.get_project_root", return_value=self.root)
        self.patch.start()
        taxonomy.reset_cache()

    def tearDown(self):
        self.patch.stop()
        taxonomy.reset_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestStateAndPending(GlobalApplyBase):
    def test_default_state(self):
        st = rca.read_state("default")
        self.assertFalse(st["executed"])
        self.assertIsNone(st["move_batch_id"])

    def test_config_hash_changes_with_mapping(self):
        h1 = rca._config_hash("default")
        (self.prof / "theme_mapping.yaml").write_text(
            yaml.safe_dump({"Deep Learning": "B/DL", "X": "A"}), encoding="utf-8")
        self.assertNotEqual(h1, rca._config_hash("default"))

    def test_pending_true_until_applied(self):
        self.assertTrue(rca.is_pending("default"))
        rca.write_state("default", {"last_applied": {"ts": "t",
                        "config_hash": rca._config_hash("default")}})
        self.assertFalse(rca.is_pending("default"))


class TestPreviewAndExecute(GlobalApplyBase):
    def test_preview_freezes_projection(self):
        p = rca.build_preview("default", include_keyword=False)
        self.assertEqual(p["n_moves"], 1)
        self.assertEqual(p["n_p1"], 1)
        self.assertEqual(p["n_p2"], 0)
        self.assertTrue((self.prof / ".cache" / "reclassify" / "apply"
                         / "projection.csv").exists())

    def test_run_moves_global_executes_frozen(self):
        rca.build_preview("default", include_keyword=False)
        result = rca._run_moves_global("default")
        self.assertEqual(result["n_moved"], 1)
        self.assertTrue((self.target / "B" / "DL" / "a.pdf").exists())
        self.assertFalse((self.target / "A" / "a.pdf").exists())
        st = rca.read_state("default")
        self.assertTrue(st["executed"])
        self.assertIsNotNone(st["move_batch_id"])
        self.assertIn("config_hash", st["last_applied"])
        self.assertTrue(list((self.root / "logs").glob("rapport_apply_*.csv")))

    def test_execute_without_preview_409(self):
        with self.assertRaises(rca.ApplyError) as ctx:
            rca._run_moves_global("default")
        self.assertEqual(ctx.exception.status, 409)

    def test_start_execute_gating_lock(self):
        rca.build_preview("default", include_keyword=False)
        (self.prof / ".cache" / "taxonomy.lock").write_text("busy")
        with self.assertRaises(rca.ApplyError) as ctx:
            rca.start_execute("default")
        self.assertEqual(ctx.exception.status, 423)

    def test_partial_failure_keeps_pending(self):
        rca.build_preview("default", include_keyword=False)
        # Force un échec : patcher execute_move_batch pour renvoyer n_failed=1
        fake = {"n_moved": 0, "n_failed": 1, "n_skipped": 0, "n_total": 1,
                "report": "rapport_apply_x.csv", "batch_id": "b1"}
        with mock.patch.object(rca.apply_engine, "execute_move_batch", return_value=fake):
            rca._run_moves_global("default")
        st = rca.read_state("default")
        self.assertIsNone(st["last_applied"])   # pas marqué → toujours pending
        self.assertTrue(rca.is_pending("default"))


class TestUndoGlobal(GlobalApplyBase):
    def test_undo_restores(self):
        rca.build_preview("default", include_keyword=False)
        rca._run_moves_global("default")
        result = rca._run_undo_global("default")
        self.assertEqual(result["n_undone"], 1)
        self.assertTrue((self.target / "A" / "a.pdf").exists())
        st = rca.read_state("default")
        self.assertFalse(st["executed"])
        self.assertTrue(st["rolled_back_moves"])

    def test_undo_nothing_409(self):
        with self.assertRaises(rca.ApplyError) as ctx:
            rca._run_undo_global("default")
        self.assertEqual(ctx.exception.status, 409)


class TestGlobalApplyEndpoints(GlobalApplyBase):
    def setUp(self):
        super().setUp()
        (self.root / "logs").mkdir(exist_ok=True)
        self.client = TestClient(app)

    def test_preview_endpoint(self):
        r = self.client.get("/api/taxonomy/reclassify/apply/preview?profile=default")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["n_moves"], 1)

    def test_pending_endpoint(self):
        r = self.client.get("/api/taxonomy/reclassify/apply/pending?profile=default")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["pending"])

    def test_execute_requires_preview_409(self):
        r = self.client.post("/api/taxonomy/reclassify/apply/execute",
                             json={"profile": "default"})
        self.assertEqual(r.status_code, 409)

    def test_full_flow(self):
        self.client.get("/api/taxonomy/reclassify/apply/preview?profile=default")
        sync = lambda target, args, name: target(*args)  # noqa: E731
        with mock.patch.object(rca.apply_engine, "spawn", sync):
            r = self.client.post("/api/taxonomy/reclassify/apply/execute",
                                 json={"profile": "default"})
        self.assertEqual(r.status_code, 200)
        s = self.client.get("/api/taxonomy/reclassify/apply/status?profile=default")
        self.assertTrue(s.json()["state"]["executed"])
        with mock.patch.object(rca.apply_engine, "spawn", sync):
            u = self.client.post("/api/taxonomy/reclassify/apply/undo",
                                 json={"profile": "default"})
        self.assertEqual(u.status_code, 200)

    def test_execute_missing_profile_400(self):
        r = self.client.post("/api/taxonomy/reclassify/apply/execute", json={})
        self.assertEqual(r.status_code, 400)

    def test_preview_endpoint_with_keyword(self):
        (self.prof / "categories.yaml").write_text(
            yaml.safe_dump(
                {"sci": [{"chemin": "B/KW", "priorite": 5, "mots_cles": ["zzz"]}]}),
            encoding="utf-8")
        taxonomy.reset_cache()
        no_kw = self.client.get(
            "/api/taxonomy/reclassify/apply/preview?profile=default&keyword=false").json()
        with_kw = self.client.get(
            "/api/taxonomy/reclassify/apply/preview?profile=default&keyword=true").json()
        # b.pdf (theme "zzz") routé par mot-clé "zzz" → P2, inclus seulement avec keyword=true
        self.assertEqual(no_kw["n_p2"], 0)
        self.assertEqual(with_kw["n_p2"], 1)
        self.assertTrue(with_kw["n_moves"] >= with_kw["n_p1"] + 1)


if __name__ == "__main__":
    unittest.main()
