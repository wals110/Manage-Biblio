#!/usr/bin/env python3
"""Tests Explorateur (Vision/LLM mockés, zéro SSD réel)."""
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


def _make_profile(root: Path, profile: str, files: dict, mapping: dict, categories: str = "{}"):
    """files: {nom_fichier: theme|None}. None → pas d'entrée vision_cache (non analysé)."""
    import lib.vision_cache as vc
    prof = root / "profiles" / profile
    (prof / ".cache").mkdir(parents=True)
    target = root / "LIB"
    target.mkdir(exist_ok=True)
    (prof / "profile.yaml").write_text(yaml.safe_dump(
        {"target": str(target), "llm": {"model": "M"}, "defaults": {"pages": 2}}), encoding="utf-8")
    (prof / "theme_mapping.yaml").write_text(yaml.safe_dump(mapping), encoding="utf-8")
    (prof / "categories.yaml").write_text(categories, encoding="utf-8")
    (prof / "tree.yaml").write_text(yaml.safe_dump({"folders": list(set(mapping.values()))}), encoding="utf-8")
    cache: dict = {}
    for fname, theme in files.items():
        fp = target / fname
        fp.write_bytes(b"%PDF-1.4 " + fname.encode())
        if theme is not None:
            key = vc.compute_cache_key(str(fp), model="M", n_pages=2)
            vc.store(cache, key, {"theme": theme,
                                  "themes": [{"theme": theme, "confidence": 0.9}],
                                  "confidence": 0.9}, "M")
    vc.save_cache(prof / ".cache" / "vision_cache.json", cache)
    return target


class TestScanAnalyzedAndProgress(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="klodo-expl-")
        self.root = Path(self.tmp)
        _make_profile(self.root, "p",
                      {"a.pdf": "Deep Learning", "b.pdf": None},
                      mapping={"deep learning": "01-Info/ML"})
        # Mirror patching from test_reclassify_apply.py and test_taxonomy.py:
        # taxonomy calls data.get_project_root() via _profile_dir().
        self.patch = mock.patch("dashboard.data.get_project_root", return_value=self.root)
        self.patch.start()
        # Reset taxonomy in-memory caches to pick up our fixture
        from dashboard import taxonomy
        taxonomy.reset_cache()

    def tearDown(self):
        mock.patch.stopall()
        from dashboard import taxonomy
        taxonomy.reset_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reclassify_include_keyword_constant(self):
        from dashboard import taxonomy
        self.assertIs(taxonomy.RECLASSIFY_INCLUDE_KEYWORD, True)

    def test_scan_marks_analyzed_per_file(self):
        from dashboard import taxonomy
        rows = {r["rel_path"]: r for r in taxonomy._scan_and_classify("p", include_step2=True)}
        self.assertTrue(rows["a.pdf"]["analyzed"])
        self.assertFalse(rows["b.pdf"]["analyzed"])

    def test_scan_reports_progress(self):
        from dashboard import taxonomy
        seen = []
        taxonomy._scan_and_classify("p", include_step2=True,
                                    on_progress=lambda d, t: seen.append((d, t)))
        self.assertTrue(seen)
        self.assertEqual(seen[-1], (len(seen), len(seen)))   # done == total au dernier tick


class TestBuildProjection(unittest.TestCase):
    def _rows(self):
        return [
            {"rel_path": "a.pdf", "current_folder": "_INBOX", "predicted_folder": "01-Info/ML",
             "source": "LLM (theme)", "score": 0.9, "top_theme": "Deep Learning",
             "top_confidence": 0.9, "analyzed": True},                       # bouge, p1
            {"rel_path": "b.pdf", "current_folder": "01-Info/ML", "predicted_folder": "01-Info/ML",
             "source": "Keyword (x)", "score": 0.5, "top_theme": "ml",
             "top_confidence": 0.5, "analyzed": True},                       # stable, p2
            {"rel_path": "c.pdf", "current_folder": "_INBOX", "predicted_folder": "",
             "source": "", "score": 0.0, "top_theme": "", "top_confidence": 0.0,
             "analyzed": True},                                              # orphelin
            {"rel_path": "d.pdf", "current_folder": "_INBOX", "predicted_folder": "02-Maths",
             "source": "Keyword (nom)", "score": 0.4, "top_theme": "", "top_confidence": 0.0,
             "analyzed": False},                                             # non analysé MAIS bouge (P2 nom)
        ]

    def test_build_projection_maps_fields_and_summary(self):
        from dashboard import explorer
        with mock.patch("dashboard.taxonomy._scan_and_classify", return_value=self._rows()):
            out = explorer.build_projection("p")
        self.assertTrue(out["ok"])
        f = {x["rel_path"]: x for x in out["files"]}
        self.assertEqual(f["a.pdf"]["signal"], "p1")
        self.assertEqual(f["b.pdf"]["signal"], "p2")
        self.assertIsNone(f["c.pdf"]["signal"])                # pas de prédiction → null
        self.assertEqual(f["a.pdf"]["confidence"], 0.9)        # mappé depuis top_confidence
        self.assertEqual(f["d.pdf"]["analyzed"], False)
        s = out["summary"]
        self.assertEqual(s["n_total"], 4)
        self.assertEqual(s["n_moving"], 2)                     # a + d
        self.assertEqual(s["n_stable"], 1)                     # b
        self.assertEqual(s["n_no_prediction"], 1)              # c (analysé, sans pred)
        self.assertEqual(s["n_unanalyzed"], 1)                 # d
        self.assertEqual(out["flag_keyword"], True)


class TestProjectionCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="klodo-expl-c-")
        self.root = Path(self.tmp)
        (self.root / "profiles" / "p" / ".cache").mkdir(parents=True)
        self.patch = mock.patch("dashboard.data.get_project_root", return_value=self.root)
        self.patch.start()

    def tearDown(self):
        from dashboard import explorer
        explorer._projection_cache.clear()
        mock.patch.stopall()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_build(self):
        return {"ok": True, "files": [{"rel_path": "a.pdf"}],
                "summary": {"n_total": 1, "n_moving": 0, "n_stable": 0,
                            "n_no_prediction": 0, "n_unanalyzed": 0},
                "flag_keyword": True}

    def test_get_projection_builds_then_serves_cache(self):
        from dashboard import explorer
        with mock.patch.object(explorer, "build_projection", return_value=self._fake_build()), \
             mock.patch.object(explorer.apply_engine, "spawn",
                               side_effect=lambda fn, args, name: fn(*args)), \
             mock.patch.object(explorer, "_fresh_hash", return_value="h1"):
            explorer.get_projection("p")               # déclenche le build (spawn synchrone)
            second = explorer.get_projection("p")     # servi par le cache
        self.assertEqual(second["status"], "ready")
        self.assertEqual(second["files"], [{"rel_path": "a.pdf"}])

    def test_stale_hash_triggers_rebuild(self):
        from dashboard import explorer
        explorer._projection_cache["p"] = {"fresh_hash": "OLD", "data": self._fake_build()}
        with mock.patch.object(explorer, "build_projection", return_value=self._fake_build()) as b, \
             mock.patch.object(explorer.apply_engine, "spawn",
                               side_effect=lambda fn, args, name: fn(*args)), \
             mock.patch.object(explorer, "_fresh_hash", return_value="NEW"):
            explorer.get_projection("p")               # périmé → rebuild (spawn synchrone)
            out = explorer.get_projection("p")         # maintenant frais
        b.assert_called()
        self.assertEqual(out["status"], "ready")

    def test_already_building_does_not_respawn(self):
        from dashboard import explorer
        explorer._write_status("p", {"status": "building", "n_done": 0, "n_total": 0, "error": None})
        with mock.patch.object(explorer.apply_engine, "spawn") as spawn, \
             mock.patch.object(explorer, "_fresh_hash", return_value="X"):
            out = explorer.get_projection("p")        # cache absent MAIS déjà building
        spawn.assert_not_called()
        self.assertEqual(out["status"], "building")


class TestExplorerRoutes(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        from dashboard.app import app
        self.client = TestClient(app)

    def test_projection_route_building(self):
        from dashboard import explorer
        with mock.patch.object(explorer, "get_projection",
                               return_value={"status": "building"}) as g:
            r = self.client.get("/api/explorer/projection?profile=p")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "building")
        g.assert_called_once_with("p")

    def test_status_route(self):
        from dashboard import explorer
        with mock.patch.object(explorer, "get_build_status",
                               return_value={"status": "ready", "n_done": 3, "n_total": 3}):
            r = self.client.get("/api/explorer/status?profile=p")
        self.assertEqual(r.json()["n_total"], 3)

    def test_refresh_route(self):
        from dashboard import explorer
        with mock.patch.object(explorer, "refresh", return_value={"ok": True}) as rf:
            r = self.client.post("/api/explorer/refresh?profile=p")
        self.assertEqual(r.status_code, 200)
        rf.assert_called_once_with("p")


class TestExplorerPage(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        from dashboard.app import app
        self.client = TestClient(app)

    def test_explorer_page_renders(self):
        r = self.client.get("/explorer")
        self.assertEqual(r.status_code, 200)
        body = r.text
        self.assertIn("onb-expl-toggle", body)            # interrupteur Maintenant/Après
        self.assertIn("explorer.js", body)
        self.assertIn("reclassify_apply.js", body)        # module apply partagé chargé

    def test_explorer_in_nav(self):
        r = self.client.get("/")
        self.assertIn('href="/explorer"', r.text)


class TestOnboardingHandoff(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        from dashboard.app import app
        self.client = TestClient(app)

    def test_onboarding_no_longer_auto_moves(self):
        body = self.client.get("/onboarding").text
        self.assertNotIn("moveClassified", body)          # l'ancien flux a disparu
        self.assertIn("/explorer?profile=", body)         # handoff vers l'Explorateur


if __name__ == "__main__":
    unittest.main()
