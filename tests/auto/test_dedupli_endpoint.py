#!/usr/bin/env python3
"""Tests des endpoints FastAPI + helpers de dashboard.dedupli — Phase 5 UI.

Mocke lib.theme_canon.build_canon_table pour ne pas dépendre du LLM.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from fastapi.testclient import TestClient  # noqa: E402

from dashboard import data, dedupli  # noqa: E402


def _seed_profile(tmp: Path, name: str = "p") -> None:
    pdir = tmp / "profiles" / name
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "profile.yaml").write_text(f"name: {name}\n", encoding="utf-8")


def _write_canon(tmp: Path, profile: str, table: dict) -> None:
    p = tmp / "profiles" / profile / ".cache" / "theme-canon.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(table, ensure_ascii=False), encoding="utf-8")


class _DedupliBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from dashboard.app import app
        cls.client = TestClient(app)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="klodo_dedupli_"))
        _seed_profile(self.tmp, "p")
        self._patcher = mock.patch.object(
            data, "get_project_root", return_value=self.tmp,
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestGetStatus(_DedupliBase):
    def test_idle_when_no_run(self):
        r = self.client.get("/api/taxonomy/dedupli/status?profile=p")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "idle")

    def test_canon_summary_included(self):
        _write_canon(self.tmp, "p", {
            "version": 2,
            "built_at": "2026-05-27T00:00:00",
            "raw_count": 100,
            "canonical_count": 80,
            "threshold": 92,
            "clusters": [{"raw_members": ["a", "b"]}],
        })
        r = self.client.get("/api/taxonomy/dedupli/status?profile=p")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("canon_summary", body)
        self.assertEqual(body["canon_summary"]["raw_count"], 100)
        self.assertEqual(body["canon_summary"]["canonical_count"], 80)


class TestListClusters(_DedupliBase):
    def test_empty_when_no_canon(self):
        r = self.client.get("/api/taxonomy/dedupli/clusters?profile=p")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["clusters"], [])

    def test_filters_singletons_by_default(self):
        _write_canon(self.tmp, "p", {
            "version": 2,
            "clusters": [
                {"canonical": "Solo", "raw_members": ["Solo"],
                 "members": ["Solo"], "splits": [], "count_cumulative": 5},
                {"canonical": "Pair", "raw_members": ["A", "B"],
                 "members": ["A", "B"], "splits": [], "count_cumulative": 12},
            ],
        })
        r = self.client.get("/api/taxonomy/dedupli/clusters?profile=p")
        clusters = r.json()["clusters"]
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["canonical"], "Pair")

    def test_multi_only_false_returns_all(self):
        _write_canon(self.tmp, "p", {
            "version": 2,
            "clusters": [
                {"canonical": "Solo", "raw_members": ["Solo"],
                 "members": ["Solo"], "splits": [], "count_cumulative": 5},
                {"canonical": "Pair", "raw_members": ["A", "B"],
                 "members": ["A", "B"], "splits": [], "count_cumulative": 12},
            ],
        })
        r = self.client.get(
            "/api/taxonomy/dedupli/clusters?profile=p&multi_only=false"
        )
        self.assertEqual(len(r.json()["clusters"]), 2)

    def test_sorted_by_count_desc(self):
        _write_canon(self.tmp, "p", {
            "version": 2,
            "clusters": [
                {"canonical": "Small", "raw_members": ["a", "b"],
                 "members": ["a", "b"], "splits": [], "count_cumulative": 3},
                {"canonical": "Big", "raw_members": ["x", "y"],
                 "members": ["x", "y"], "splits": [], "count_cumulative": 100},
            ],
        })
        r = self.client.get("/api/taxonomy/dedupli/clusters?profile=p")
        clusters = r.json()["clusters"]
        self.assertEqual(clusters[0]["canonical"], "Big")
        self.assertEqual(clusters[1]["canonical"], "Small")


class TestUpdateCluster(_DedupliBase):
    def _seed_basic_canon(self):
        _write_canon(self.tmp, "p", {
            "version": 2,
            "raw_count": 3,
            "canonical_count": 1,
            "mapping": {
                "Machine Learning": "Machine Learning",
                "Machine learning": "Machine Learning",
                "ML": "Machine Learning",
            },
            "clusters": [
                {"canonical": "Machine Learning",
                 "raw_members": ["Machine Learning", "Machine learning", "ML"],
                 "members": ["Machine Learning", "Machine learning", "ML"],
                 "splits": [],
                 "count_cumulative": 10},
            ],
        })

    def test_update_happy_path(self):
        self._seed_basic_canon()
        r = self.client.put(
            "/api/taxonomy/dedupli/cluster",
            json={
                "profile": "p",
                "raw_members": ["Machine Learning", "Machine learning", "ML"],
                "canonical": "Machine Learning",
                "members": ["Machine Learning", "Machine learning"],
                "splits": [{"theme": "ML", "reason": "acronyme distinct"}],
            },
        )
        self.assertEqual(r.status_code, 200)
        # Vérifie le mapping résultant : ML → ML (split, identité)
        canon_path = self.tmp / "profiles" / "p" / ".cache" / "theme-canon.json"
        canon = json.loads(canon_path.read_text())
        self.assertEqual(canon["mapping"]["ML"], "ML")
        self.assertEqual(canon["mapping"]["Machine learning"], "Machine Learning")

    def test_rejects_unknown_cluster(self):
        self._seed_basic_canon()
        r = self.client.put(
            "/api/taxonomy/dedupli/cluster",
            json={
                "profile": "p",
                "raw_members": ["Unknown", "Variant"],
                "canonical": "Anything",
                "members": ["Unknown"],
                "splits": [],
            },
        )
        self.assertEqual(r.status_code, 400)

    def test_rejects_member_not_in_raw(self):
        self._seed_basic_canon()
        r = self.client.put(
            "/api/taxonomy/dedupli/cluster",
            json={
                "profile": "p",
                "raw_members": ["Machine Learning", "Machine learning", "ML"],
                "canonical": "Machine Learning",
                "members": ["Some Random Theme"],  # pas dans raw
                "splits": [],
            },
        )
        self.assertEqual(r.status_code, 400)

    def test_rejects_empty_canonical(self):
        self._seed_basic_canon()
        r = self.client.put(
            "/api/taxonomy/dedupli/cluster",
            json={
                "profile": "p",
                "raw_members": ["Machine Learning", "Machine learning", "ML"],
                "canonical": "",
                "members": ["Machine Learning"],
                "splits": [],
            },
        )
        self.assertEqual(r.status_code, 400)


class TestStartDedupli(_DedupliBase):
    def test_rejects_missing_profile(self):
        r = self.client.post(
            "/api/taxonomy/dedupli/build",
            json={"threshold": 92},
        )
        self.assertEqual(r.status_code, 400)

    def test_rejects_bad_threshold(self):
        r = self.client.post(
            "/api/taxonomy/dedupli/build",
            json={"profile": "p", "threshold": 200},
        )
        self.assertEqual(r.status_code, 400)

    def test_rejects_unknown_profile(self):
        r = self.client.post(
            "/api/taxonomy/dedupli/build",
            json={"profile": "ghost", "threshold": 92},
        )
        self.assertEqual(r.status_code, 404)

    def test_happy_path_returns_pending(self):
        # Mock le pipeline complet
        with mock.patch("lib.theme_canon.build_canon_table") as mock_build:
            mock_build.return_value = {
                "version": 2, "raw_count": 0, "canonical_count": 0,
                "mapping": {}, "clusters": [],
            }
            r = self.client.post(
                "/api/taxonomy/dedupli/build",
                json={"profile": "p", "threshold": 92},
            )
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["status"], "pending")
            # Laisse le thread daemon terminer
            time.sleep(0.3)

    def test_concurrent_run_returns_409(self):
        # On mock _run_dedupli (le cœur du thread) pour bloquer sur un Event
        # — sinon get_agent_llm() plante (pas de SILICONFLOW_API_KEY en test)
        # et le status passe instantanément à "error", invalidant le test.
        import threading as _t
        gate = _t.Event()

        def slow_run(profile, threshold):
            # Persiste un status "running" avant de bloquer
            dedupli._write_status(profile, {
                "status": "running", "phase": "judging",
                "progress": {"done": 0, "total": 100},
                "started_at": dedupli._now_iso(),
            })
            gate.wait(timeout=5)

        with mock.patch.object(dedupli, "_run_dedupli", side_effect=slow_run):
            r1 = self.client.post(
                "/api/taxonomy/dedupli/build",
                json={"profile": "p", "threshold": 92},
            )
            self.assertEqual(r1.status_code, 200)
            # Laisse le thread démarrer et écrire le status "running"
            for _ in range(20):  # max 1s
                st = dedupli.get_status("p")
                if st.get("status") in ("running", "pending"):
                    break
                time.sleep(0.05)
            # Tentative 2 pendant que la 1ère est bloquée → 409
            r2 = self.client.post(
                "/api/taxonomy/dedupli/build",
                json={"profile": "p", "threshold": 92},
            )
            self.assertEqual(r2.status_code, 409)
            # Débloque le 1er
            gate.set()
            time.sleep(0.2)


class TestZombieReap(_DedupliBase):
    def test_zombie_marked_error(self):
        # Status "running" avec un mtime ancien
        from datetime import UTC, datetime, timedelta
        old = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
        status_path = self.tmp / "profiles" / "p" / ".cache" / "dedupli" / "status.json"
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(json.dumps({
            "status": "running", "started_at": old,
            "progress": {"done": 5, "total": 100},
        }), encoding="utf-8")
        # Forcer mtime ancien
        import os as _os
        ts = (datetime.now(UTC) - timedelta(minutes=10)).timestamp()
        _os.utime(status_path, (ts, ts))

        # get_status reap automatiquement
        status = dedupli.get_status("p")
        self.assertEqual(status["status"], "error")
        self.assertIn("orphelin", status["error"])


if __name__ == "__main__":
    unittest.main()
