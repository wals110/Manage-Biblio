#!/usr/bin/env python3
"""Tests des endpoints FastAPI de l'agent Refonte — Phase A.4.

Vérifie :
  - GET /agent/refonte : page rendue OK
  - POST /api/agent/refonte/diagnostic : validation des inputs + kick async
  - GET /api/agent/refonte/diagnostic/<run_id> : polling + injection report

Le graphe LangGraph est mocké au niveau de `agents.refonte.build_diagnostic_graph`
pour ne pas dépendre de SiliconFlow.
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

import yaml

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from fastapi.testclient import TestClient  # noqa: E402

from dashboard import agent_refonte, data  # noqa: E402


class _FakeCompiledGraph:
    """Faux graphe compilé qui retourne un state final scripté.

    Supporte `.stream(stream_mode="values")` (utilisé par agent_refonte.py
    pour les updates progressifs) et `.invoke()` (rétrocompat).
    """

    def __init__(self, response: dict):
        self.response = response
        self.invoked_with: dict | None = None

    def invoke(self, state):
        self.invoked_with = dict(state)
        time.sleep(0.05)
        return {**state, **self.response}

    def stream(self, state, stream_mode="values"):
        """Yield states comme le ferait LangGraph en mode values."""
        self.invoked_with = dict(state)
        # 1er chunk : state initial avec llm_calls=0
        yield {**state, "llm_calls": 0}
        time.sleep(0.05)
        # Dernier chunk : state final scripté
        yield {**state, **self.response}


def _make_profile_root(tmp: Path, profile: str) -> None:
    """Crée un profil minimal sous tmp/profiles/<name>/."""
    p = tmp / "profiles" / profile
    p.mkdir(parents=True, exist_ok=True)
    (p / "profile.yaml").write_text(yaml.safe_dump({"name": profile}))
    (p / "tree.yaml").write_text(yaml.safe_dump({"folders": ["A", "B"]}))
    (p / "theme_mapping.yaml").write_text(yaml.safe_dump({"t1": "A"}))


class _AgentEndpointBase(unittest.TestCase):
    """Setup commun : redirige le project root vers un temp dir."""

    @classmethod
    def setUpClass(cls):
        from dashboard.app import app
        cls.client = TestClient(app)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="klodo_refonte_endpoint_"))
        _make_profile_root(self.tmp, "test_p")
        # Patch les 2 entrées qui résolvent le project root :
        # - data.get_project_root (utilisé par agent_refonte module)
        # - data.get_available_profiles (utilisé par la page handler)
        self._patcher = mock.patch.object(
            data, "get_project_root", return_value=self.tmp,
        )
        self._patcher.start()
        self._profiles_patcher = mock.patch.object(
            data, "get_available_profiles", return_value=["test_p"],
        )
        self._profiles_patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._profiles_patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestAgentRefontePage(_AgentEndpointBase):
    def test_page_renders(self):
        r = self.client.get("/agent/refonte?profile=test_p")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Agent Refonte", r.text)
        self.assertIn("test_p", r.text)


class TestStartDiagnostic(_AgentEndpointBase):
    def test_rejects_missing_profile(self):
        r = self.client.post("/api/agent/refonte/diagnostic", json={})
        self.assertEqual(r.status_code, 400)
        self.assertIn("profile", r.json()["error"].lower())

    def test_rejects_unknown_profile(self):
        r = self.client.post("/api/agent/refonte/diagnostic",
                             json={"profile": "ghost"})
        self.assertEqual(r.status_code, 404)
        self.assertIn("profile not found", r.json()["error"])

    def test_rejects_out_of_range_budget(self):
        r = self.client.post("/api/agent/refonte/diagnostic",
                             json={"profile": "test_p", "max_llm_calls": 99})
        self.assertEqual(r.status_code, 400)

    def test_happy_path_returns_run_id(self):
        fake = _FakeCompiledGraph({"status": "done", "report": "# OK", "llm_calls": 1})
        with mock.patch(
            "agents.refonte.build_diagnostic_graph",
            return_value=fake,
        ):
            r = self.client.post("/api/agent/refonte/diagnostic",
                                 json={"profile": "test_p"})
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertEqual(body["status"], "pending")
            self.assertEqual(body["profile"], "test_p")
            self.assertTrue(body["run_id"])
            # Attendre que le thread daemon ait fini
            time.sleep(0.3)
            status = agent_refonte.get_status("test_p", body["run_id"])
            self.assertEqual(status["status"], "done")
            self.assertEqual(status["report_md"], "# OK")


class TestGetStatus(_AgentEndpointBase):
    def test_404_when_run_unknown(self):
        r = self.client.get("/api/agent/refonte/diagnostic/nope?profile=test_p")
        self.assertEqual(r.status_code, 404)

    def test_returns_status_and_report(self):
        # On crée directement un status "done" sur disque pour isoler le test
        # du graphe.
        run_id = "fixed-run-1234"
        run_dir = (self.tmp / "profiles" / "test_p" / ".cache" / "refonte" / run_id)
        run_dir.mkdir(parents=True)
        (run_dir / "status.json").write_text(json.dumps({
            "run_id": run_id, "profile": "test_p", "status": "done",
            "started_at": "2026-05-24T10:00:00Z",
            "completed_at": "2026-05-24T10:00:42Z",
            "llm_calls": 4, "max_llm_calls": 5, "error": None,
        }))
        (run_dir / "report.md").write_text("# Diagnostic\n\nTout va bien.")

        r = self.client.get(
            f"/api/agent/refonte/diagnostic/{run_id}?profile=test_p"
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "done")
        self.assertEqual(body["llm_calls"], 4)
        self.assertIn("Tout va bien", body["report_md"])


class TestStartDiagnosticErrorPath(_AgentEndpointBase):
    def test_graph_exception_persisted_as_error(self):
        """Si le graphe lève, status.json doit refléter status=error + traceback."""
        def boom(*_args, **_kw):
            raise RuntimeError("LLM provider unavailable")

        with mock.patch("agents.refonte.build_diagnostic_graph", side_effect=boom):
            r = self.client.post("/api/agent/refonte/diagnostic",
                                 json={"profile": "test_p"})
            self.assertEqual(r.status_code, 200)
            run_id = r.json()["run_id"]
            # Wait for the daemon thread to write the error
            for _ in range(20):
                status = agent_refonte.get_status("test_p", run_id)
                if status and status.get("status") == "error":
                    break
                time.sleep(0.05)
            self.assertEqual(status["status"], "error")
            self.assertIn("LLM provider unavailable", status["error"])
            self.assertIn("traceback", status)


class TestTaxonomySubtab(_AgentEndpointBase):
    """L'agent panel doit aussi être inclus dans /taxonomy en 4e sous-onglet."""

    def test_taxonomy_page_includes_refonte_subtab(self):
        r = self.client.get("/taxonomy?profile=test_p")
        self.assertEqual(r.status_code, 200)
        # Bouton sous-onglet présent
        self.assertIn('data-view="refonte"', r.text)
        # Panneau partial inclus (containers depuis le partial)
        self.assertIn('tax-refonte-runs', r.text)
        self.assertIn('tax-refonte-start', r.text)


class TestListRunsEndpoint(_AgentEndpointBase):
    """Endpoint API utilisé par le partial pour recharger la liste sans page reload."""

    def test_returns_runs_for_profile(self):
        runs_dir = self.tmp / "profiles" / "test_p" / ".cache" / "refonte"
        runs_dir.mkdir(parents=True)
        for i, ts in enumerate(["2026-05-01T00:00:00Z", "2026-05-24T10:00:00Z"]):
            d = runs_dir / f"r{i}"
            d.mkdir()
            (d / "status.json").write_text(json.dumps({
                "run_id": f"r{i}", "profile": "test_p",
                "status": "done", "started_at": ts,
            }))
        r = self.client.get("/api/agent/refonte/runs?profile=test_p")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["profile"], "test_p")
        self.assertEqual(len(body["runs"]), 2)
        # Tri descendant
        self.assertEqual(body["runs"][0]["started_at"], "2026-05-24T10:00:00Z")

    def test_returns_empty_list_when_no_runs(self):
        r = self.client.get("/api/agent/refonte/runs?profile=test_p")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["runs"], [])

    def test_rejects_missing_profile_qs(self):
        r = self.client.get("/api/agent/refonte/runs")
        # FastAPI renvoie 422 pour les query params required manquants
        self.assertEqual(r.status_code, 422)


class TestListRuns(_AgentEndpointBase):
    def test_list_sorted_by_started_at_desc(self):
        """list_runs trie par started_at descendant (plus récent en premier)."""
        runs_dir = self.tmp / "profiles" / "test_p" / ".cache" / "refonte"
        runs_dir.mkdir(parents=True)
        for i, ts in enumerate(["2026-01-01T00:00:00Z", "2026-05-24T10:00:00Z",
                                "2026-03-15T12:00:00Z"]):
            d = runs_dir / f"r{i}"
            d.mkdir()
            (d / "status.json").write_text(json.dumps({
                "run_id": f"r{i}", "profile": "test_p",
                "status": "done", "started_at": ts,
            }))
        runs = agent_refonte.list_runs("test_p")
        self.assertEqual(len(runs), 3)
        self.assertEqual(runs[0]["started_at"], "2026-05-24T10:00:00Z")
        self.assertEqual(runs[2]["started_at"], "2026-01-01T00:00:00Z")


class TestDeleteRun(_AgentEndpointBase):
    """delete_run + endpoint DELETE /api/agent/refonte/runs/<id>."""

    _DONE_UUID = "11111111-2222-3333-4444-555555555555"
    _RUNNING_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    def _seed_run(self, run_id: str, status: str = "done") -> Path:
        runs_dir = self.tmp / "profiles" / "test_p" / ".cache" / "refonte"
        runs_dir.mkdir(parents=True, exist_ok=True)
        d = runs_dir / run_id
        d.mkdir()
        (d / "status.json").write_text(json.dumps({
            "run_id": run_id, "profile": "test_p",
            "status": status, "started_at": "2026-05-25T00:00:00Z",
        }))
        (d / "report.md").write_text("# Diagnostic\n…")
        return d

    def test_delete_done_run_removes_dir(self):
        d = self._seed_run(self._DONE_UUID, status="done")
        self.assertTrue(d.exists())
        agent_refonte.delete_run("test_p", self._DONE_UUID)
        self.assertFalse(d.exists())

    def test_delete_refuses_running(self):
        # Pour éviter le reap, on touche le mtime à maintenant.
        d = self._seed_run(self._RUNNING_UUID, status="running")
        (d / "status.json").touch()
        with self.assertRaises(agent_refonte.RunBusyError):
            agent_refonte.delete_run("test_p", self._RUNNING_UUID)
        self.assertTrue(d.exists())  # toujours là

    def test_delete_refuses_bad_uuid(self):
        with self.assertRaises(ValueError):
            agent_refonte.delete_run("test_p", "../etc/passwd")

    def test_delete_unknown_run_raises(self):
        with self.assertRaises(FileNotFoundError):
            agent_refonte.delete_run("test_p", self._DONE_UUID)

    def test_endpoint_delete_happy_path(self):
        self._seed_run(self._DONE_UUID, status="done")
        r = self.client.delete(
            f"/api/agent/refonte/runs/{self._DONE_UUID}?profile=test_p"
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["run_id"], self._DONE_UUID)

    def test_endpoint_delete_404_when_missing(self):
        r = self.client.delete(
            f"/api/agent/refonte/runs/{self._DONE_UUID}?profile=test_p"
        )
        self.assertEqual(r.status_code, 404)

    def test_endpoint_delete_409_when_busy(self):
        d = self._seed_run(self._RUNNING_UUID, status="running")
        (d / "status.json").touch()
        r = self.client.delete(
            f"/api/agent/refonte/runs/{self._RUNNING_UUID}?profile=test_p"
        )
        self.assertEqual(r.status_code, 409)

    def test_endpoint_delete_400_when_uuid_bad(self):
        r = self.client.delete(
            "/api/agent/refonte/runs/not-an-uuid?profile=test_p"
        )
        self.assertEqual(r.status_code, 400)

    def test_endpoint_delete_422_when_profile_missing(self):
        # FastAPI: query param required → 422
        r = self.client.delete(
            f"/api/agent/refonte/runs/{self._DONE_UUID}"
        )
        self.assertEqual(r.status_code, 422)


class TestPropositionEndpoints(_AgentEndpointBase):
    """Endpoints API Phase B : POST proposition + GET tree-diff + GET simulation."""

    def _seed_phase_b_run(self, run_id: str, with_simulation: bool = True) -> None:
        """Crée un faux run Phase B done avec proposed/ + simulation/ artefacts."""
        rundir = self.tmp / "profiles" / "test_p" / ".cache" / "refonte" / run_id
        proposed = rundir / "proposed"
        proposed.mkdir(parents=True)
        # status.json
        (rundir / "status.json").write_text(json.dumps({
            "run_id": run_id, "profile": "test_p",
            "phase": "B", "status": "done",
            "started_at": "2026-05-25T00:00:00Z",
            "completed_at": "2026-05-25T00:01:00Z",
            "llm_calls": 5, "diagnostic_run_id": "diag-xxx",
        }))
        # proposed/ artefacts
        (proposed / "tree-proposed.yaml").write_text(
            "folders:\n- A\n- A/Rust\n- B/New\n",
        )
        (proposed / "theme_mapping-proposed.yaml").write_text("rust: A/Rust\n")
        (proposed / "refonte-rationale.md").write_text(
            "# Refonte taxonomy — profil `test_p` — proposition\n\n## CRÉATIONS (1)\n- `A/Rust`\n",
        )
        (proposed / "changes.json").write_text(json.dumps({
            "creations": [{"path": "A/Rust", "rationale": "rust orphelin"}],
            "fusions": [],
            "renamings": [],
            "mappings_added": [{"theme": "rust", "folder": "A/Rust", "rationale": "x"}],
        }))
        # Aussi un tree.yaml côté profil pour le diff
        (self.tmp / "profiles" / "test_p" / "tree.yaml").write_text(
            "folders:\n- A\n- A/Python\n",
        )
        if with_simulation:
            simdir = rundir / "simulation"
            simdir.mkdir()
            (simdir / "simulation-summary.json").write_text(json.dumps({
                "n_files": 100, "n_moving": 30, "n_stable": 65, "n_no_prediction": 5,
                "top_destinations": [{"folder": "A/Rust", "n_incoming": 12}],
                "top_origins": [{"folder": "B", "n_outgoing": 12}],
                "n_by_source": {"LLM (theme)": 80},
            }))
            (simdir / "reclassify-projection.csv").write_text(
                "rel_path,current_folder,proposed_folder,changed,source,top_theme,confidence,score\n"
                "a.pdf,B,A/Rust,True,LLM (theme),rust programming,0.9,1.0\n"
                "b.pdf,A,A,False,LLM (theme),x,0.5,1.0\n"
                "c.pdf,B,A/Rust,True,LLM (theme),rust programming,0.92,1.0\n",
            )

    def test_tree_diff_endpoint_returns_added_and_renames(self):
        self._seed_phase_b_run("rb-1")
        r = self.client.get("/api/agent/refonte/proposition/rb-1/tree-diff?profile=test_p")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        # A/Rust et B/New sont créés (pas dans current tree)
        self.assertIn("A/Rust", d["added"])
        self.assertIn("B/New", d["added"])
        # A/Python est supprimé (présent dans current, pas dans proposed)
        self.assertIn("A/Python", d["removed"])
        # Compte cohérent
        self.assertEqual(d["n_folders_before"], 2)
        self.assertEqual(d["n_folders_after"], 3)

    def test_tree_diff_missing_profile_qs(self):
        r = self.client.get("/api/agent/refonte/proposition/rb-X/tree-diff")
        # FastAPI 422 si query string profile manque
        self.assertEqual(r.status_code, 422)

    def test_simulation_endpoint_returns_summary_and_sample(self):
        self._seed_phase_b_run("rb-2")
        r = self.client.get("/api/agent/refonte/proposition/rb-2/simulation?profile=test_p")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["summary"]["n_files"], 100)
        self.assertEqual(d["summary"]["n_moving"], 30)
        # 2 lignes "changed=True" dans le CSV
        self.assertEqual(d["n_moves_total"], 2)
        self.assertEqual(len(d["sample_moves"]), 2)
        # La première move doit être a.pdf
        self.assertEqual(d["sample_moves"][0]["rel_path"], "a.pdf")

    def test_simulation_endpoint_404_for_phase_a_run(self):
        """Un run Phase A (sans simulation/) → 404."""
        rundir = self.tmp / "profiles" / "test_p" / ".cache" / "refonte" / "ra-1"
        rundir.mkdir(parents=True)
        (rundir / "status.json").write_text(json.dumps({
            "run_id": "ra-1", "profile": "test_p", "phase": "A", "status": "done",
        }))
        r = self.client.get("/api/agent/refonte/proposition/ra-1/simulation?profile=test_p")
        self.assertEqual(r.status_code, 404)

    def test_get_status_injects_rationale_md_for_phase_b(self):
        """GET /api/agent/refonte/diagnostic/<run_id> sur un run Phase B done
        doit injecter rationale_md (pas report_md)."""
        self._seed_phase_b_run("rb-3", with_simulation=False)
        r = self.client.get("/api/agent/refonte/diagnostic/rb-3?profile=test_p")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["phase"], "B")
        self.assertIn("rationale_md", body)
        self.assertIn("Refonte taxonomy", body["rationale_md"])
        self.assertNotIn("report_md", body)


if __name__ == "__main__":
    unittest.main()
