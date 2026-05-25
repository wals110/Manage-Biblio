#!/usr/bin/env python3
"""Tests pour Phase B (Proposition) — tool propose_changes + graphe.

3 axes :
  1. propose_changes : applique correctement créations/fusions/renommages/mappings
  2. Graph compile + invoke avec FakeLLM, propose_changes appelé une fois → done
  3. Refus d'inputs invalides (profile vide, diagnostic_run_id absent, diagnostic non-done)
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
from langchain_core.messages import AIMessage, ToolMessage

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from agents.refonte.proposition import build_proposition_graph  # noqa: E402
from agents.refonte.proposition_tools import propose_changes  # noqa: E402
from dashboard import data  # noqa: E402
from dashboard import taxonomy as tax

# ─── Fixtures ──────────────────────────────────────────────────────────────


def _make_profile(profiles_root: Path, name: str, *, target: Path,
                  folders: list[str], mapping: dict[str, str]) -> None:
    pdir = profiles_root / name
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "profile.yaml").write_text(yaml.safe_dump({"name": name, "target": str(target)}))
    (pdir / "tree.yaml").write_text(yaml.safe_dump({"folders": folders}))
    (pdir / "theme_mapping.yaml").write_text(yaml.safe_dump(mapping))
    target.mkdir(parents=True, exist_ok=True)


def _seed_diagnostic_run(profiles_root: Path, profile: str, run_id: str,
                         status: str = "done") -> None:
    """Crée un faux run de Phase A done (avec report.md minimal)."""
    rundir = profiles_root / profile / ".cache" / "refonte" / run_id
    rundir.mkdir(parents=True, exist_ok=True)
    (rundir / "status.json").write_text(json.dumps({
        "run_id": run_id, "profile": profile, "status": status, "phase": "A",
        "started_at": "2026-05-25T00:00:00+00:00",
        "completed_at": "2026-05-25T00:01:00+00:00",
        "llm_calls": 7,
    }))
    (rundir / "report.md").write_text(
        "# Diagnostic taxonomy — profil `" + profile + "` — 2026-05-25\n\n"
        "## Stats globales\n- 3 dossiers\n\n"
        "## Anomalies détectées (par priorité)\n\n"
        "### Mappings manquants critiques (1 cas)\n"
        "- **Rust** (50 fichiers) → dossier cible évident : `A/Rust`\n"
    )


class _FakeLLM:
    """LLM scripté retournant une séquence prédéfinie d'AIMessage."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.invocations: list[list] = []

    def bind_tools(self, _tools):
        return self

    def invoke(self, messages):
        self.invocations.append(list(messages))
        if not self.responses:
            return AIMessage(content="(fake LLM: no more)")
        return self.responses.pop(0)


def _ai_tool_call(name: str, args: dict) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": "call_" + name, "type": "tool_call"}],
    )


class _ProposBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="klodo_phaseb_"))
        self.profiles_root = self.tmp / "profiles"
        self.profiles_root.mkdir()
        self.target = self.tmp / "biblio"
        self._patcher = mock.patch.object(data, "get_project_root", return_value=self.tmp)
        self._patcher.start()
        tax.reset_cache()
        _make_profile(
            self.profiles_root, "p",
            target=self.target,
            folders=["A", "A/Python", "B"],
            mapping={"python programming": "A/Python", "data": "B"},
        )

    def tearDown(self):
        self._patcher.stop()
        tax.reset_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)


# ─── 1. propose_changes : sémantique ───────────────────────────────────────


class TestProposeChanges(_ProposBase):
    def test_writes_three_artifacts(self):
        result = propose_changes(
            profile="p",
            run_id="r-1",
            creations=[{"path": "A/Rust", "rationale": "Rust orphelin"}],
            mappings_added=[
                {"theme": "rust", "folder": "A/Rust", "rationale": "thème orphelin"},
            ],
        )
        # Les 3 fichiers existent
        for key in ("tree_proposed_path", "mapping_proposed_path", "rationale_path"):
            self.assertTrue(Path(result[key]).exists(), f"{key} missing on disk")
        # Compte cohérent
        self.assertEqual(result["n_creations"], 1)
        self.assertEqual(result["n_mappings_added"], 1)

    def test_creations_added_to_tree(self):
        propose_changes(
            profile="p", run_id="r-2",
            creations=[{"path": "A/Rust", "rationale": "x"}],
        )
        tree_path = self.tmp / "profiles" / "p" / ".cache" / "refonte" / "r-2" / "proposed" / "tree-proposed.yaml"
        folders = yaml.safe_load(tree_path.read_text())["folders"]
        self.assertIn("A/Rust", folders)
        self.assertIn("A", folders)  # existant préservé

    def test_renaming_replaces_in_tree_and_mapping(self):
        propose_changes(
            profile="p", run_id="r-3",
            renamings=[{"old_path": "A/Python", "new_path": "A/Python3", "rationale": "x"}],
        )
        rundir = self.tmp / "profiles" / "p" / ".cache" / "refonte" / "r-3" / "proposed"
        folders = yaml.safe_load((rundir / "tree-proposed.yaml").read_text())["folders"]
        mapping = yaml.safe_load((rundir / "theme_mapping-proposed.yaml").read_text())
        self.assertIn("A/Python3", folders)
        self.assertNotIn("A/Python", folders)
        # Le mapping existant qui pointait sur A/Python doit suivre
        self.assertEqual(mapping["python programming"], "A/Python3")

    def test_fusion_consolidates_in_tree_and_mapping(self):
        propose_changes(
            profile="p", run_id="r-4",
            fusions=[
                {"sources": ["A", "B"], "target": "ALL", "rationale": "tout fusionné"},
            ],
        )
        rundir = self.tmp / "profiles" / "p" / ".cache" / "refonte" / "r-4" / "proposed"
        folders = yaml.safe_load((rundir / "tree-proposed.yaml").read_text())["folders"]
        mapping = yaml.safe_load((rundir / "theme_mapping-proposed.yaml").read_text())
        self.assertIn("ALL", folders)
        self.assertNotIn("A", folders)
        self.assertNotIn("B", folders)
        # Les mappings sur A et B doivent pointer sur ALL
        # (sauf "python programming" qui pointait sur A/Python qui n'est pas dans les sources)
        self.assertEqual(mapping["data"], "ALL")

    def test_rationale_md_has_sections(self):
        propose_changes(
            profile="p", run_id="r-5",
            creations=[{"path": "X", "rationale": "x"}],
            fusions=[{"sources": ["A"], "target": "X", "rationale": "y"}],
            renamings=[{"old_path": "B", "new_path": "C", "rationale": "z"}],
            mappings_added=[{"theme": "t1", "folder": "X", "rationale": "w"}],
        )
        rationale = (self.tmp / "profiles" / "p" / ".cache" / "refonte" / "r-5"
                     / "proposed" / "refonte-rationale.md").read_text()
        self.assertIn("CRÉATIONS (1)", rationale)
        self.assertIn("FUSIONS (1)", rationale)
        self.assertIn("RENOMMAGES (1)", rationale)
        self.assertIn("MAPPINGS AJOUTÉS (1)", rationale)


# ─── 2. Graphe Phase B avec FakeLLM ────────────────────────────────────────


class TestPropositionGraph(_ProposBase):
    def test_happy_path_calls_propose_then_finalizes(self):
        _seed_diagnostic_run(self.profiles_root, "p", "diag-1")
        fake = _FakeLLM([
            # 1er analyze : appelle propose_changes
            _ai_tool_call("propose_changes", {
                "creations": [{"path": "A/Rust", "rationale": "thème orphelin"}],
                "mappings_added": [
                    {"theme": "rust", "folder": "A/Rust", "rationale": "cf diagnostic"},
                ],
            }),
            # 2e analyze : LLM termine sans tool_call
            AIMessage(content="Proposition envoyée."),
        ])
        graph = build_proposition_graph(profile="p", run_id="b-1", llm=fake, max_llm_calls=5)
        result = graph.invoke({
            "profile": "p", "run_id": "b-1", "diagnostic_run_id": "diag-1",
        })
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["proposal_summary"]["n_creations"], 1)
        self.assertEqual(result["proposal_summary"]["n_mappings_added"], 1)
        # Le proposal_dir contient bien les 3 artefacts
        pdir = Path(result["proposal_dir"])
        for fname in ("tree-proposed.yaml", "theme_mapping-proposed.yaml",
                      "refonte-rationale.md"):
            self.assertTrue((pdir / fname).exists())
        # ToolMessage retourné dans l'historique
        tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        self.assertEqual(len(tool_msgs), 1)
        self.assertEqual(tool_msgs[0].name, "propose_changes")

    def test_no_propose_call_returns_error(self):
        """Si l'agent termine sans jamais appeler propose_changes → status=error."""
        _seed_diagnostic_run(self.profiles_root, "p", "diag-2")
        fake = _FakeLLM([
            AIMessage(content="Je n'ai rien à proposer."),  # pas de tool_call
        ])
        graph = build_proposition_graph(profile="p", run_id="b-2", llm=fake, max_llm_calls=5)
        result = graph.invoke({
            "profile": "p", "run_id": "b-2", "diagnostic_run_id": "diag-2",
        })
        self.assertEqual(result["status"], "error")
        self.assertIn("propose_changes", result["error"])

    def test_missing_profile_short_circuits(self):
        fake = _FakeLLM([])
        graph = build_proposition_graph(profile="p", run_id="b-3", llm=fake, max_llm_calls=5)
        result = graph.invoke({"run_id": "b-3", "diagnostic_run_id": "anything"})
        self.assertEqual(result["status"], "error")
        self.assertIn("profile", result["error"].lower())
        self.assertEqual(len(fake.invocations), 0)

    def test_missing_diagnostic_run_id_short_circuits(self):
        fake = _FakeLLM([])
        graph = build_proposition_graph(profile="p", run_id="b-4", llm=fake, max_llm_calls=5)
        result = graph.invoke({"profile": "p", "run_id": "b-4"})
        self.assertEqual(result["status"], "error")
        self.assertIn("diagnostic_run_id", result["error"])

    def test_unknown_diagnostic_returns_error(self):
        fake = _FakeLLM([])
        graph = build_proposition_graph(profile="p", run_id="b-5", llm=fake, max_llm_calls=5)
        result = graph.invoke({
            "profile": "p", "run_id": "b-5", "diagnostic_run_id": "ghost-id",
        })
        self.assertEqual(result["status"], "error")
        self.assertIn("Rapport de diagnostic introuvable", result["error"])


# ─── 3. Backend dashboard start_proposition ────────────────────────────────


class TestStartProposition(_ProposBase):
    def test_rejects_missing_profile(self):
        from dashboard import agent_refonte
        with self.assertRaises(ValueError) as ctx:
            agent_refonte.start_proposition("", "any-id")
        self.assertIn("profile", str(ctx.exception).lower())

    def test_rejects_unknown_diagnostic(self):
        from dashboard import agent_refonte
        with self.assertRaises(FileNotFoundError) as ctx:
            agent_refonte.start_proposition("p", "ghost-run")
        self.assertIn("diagnostic run not found", str(ctx.exception).lower())

    def test_rejects_diagnostic_not_done(self):
        from dashboard import agent_refonte
        _seed_diagnostic_run(self.profiles_root, "p", "diag-pending", status="pending")
        with self.assertRaises(ValueError) as ctx:
            agent_refonte.start_proposition("p", "diag-pending")
        self.assertIn("'pending'", str(ctx.exception))

    def test_happy_path_returns_run_id_and_thread_runs(self):
        """Avec un graphe mocké, start_proposition retourne et le thread écrit."""
        from dashboard import agent_refonte
        _seed_diagnostic_run(self.profiles_root, "p", "diag-3")

        # Fake graph used by _run_proposition via import
        class _FakeGraph:
            def stream(self, state, stream_mode="values"):
                yield {**state, "llm_calls": 0}
                yield {
                    **state,
                    "llm_calls": 2,
                    "status": "done",
                    "proposal_dir": "/tmp/fake",
                    "proposal_summary": {"n_creations": 1, "n_mappings_added": 0},
                }

        with mock.patch(
            "agents.refonte.proposition.build_proposition_graph",
            return_value=_FakeGraph(),
        ):
            result = agent_refonte.start_proposition("p", "diag-3")
            self.assertEqual(result["status"], "pending")
            self.assertEqual(result["phase"], "B")
            self.assertEqual(result["diagnostic_run_id"], "diag-3")
            self.assertTrue(result["run_id"])
            # Attendre que le thread écrive le status final
            time.sleep(0.4)
            status = agent_refonte.get_status("p", result["run_id"])
            self.assertEqual(status["status"], "done")
            self.assertEqual(status["proposal_summary"]["n_creations"], 1)


if __name__ == "__main__":
    unittest.main()
