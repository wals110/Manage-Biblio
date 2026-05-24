#!/usr/bin/env python3
"""Tests pour le graphe diagnostic de l'agent Refonte — Phase A.3.

Toutes les invocations passent par un **FakeLLM** scripté — aucune dépendance
à SiliconFlow ou à une clé API en CI. Le smoke test avec vrai LLM est laissé
à un script séparé dans tests/manual/ (à venir avec A.5).

Couvre :
  - Wiring du graphe (compile + nœuds attendus)
  - Boucle ReAct quand le LLM appelle un tool
  - Court-circuit vers write_report si pas de tool call
  - Garde-fou budget : injection du stop_msg quand max_llm_calls atteint
  - Refus si profile manquant
  - Factory llm.py : erreur si SILICONFLOW_API_KEY absente
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml
from langchain_core.messages import AIMessage, ToolMessage

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from agents import llm as agent_llm  # noqa: E402
from agents.refonte import build_diagnostic_graph  # noqa: E402
from dashboard import data  # noqa: E402
from dashboard import taxonomy as tax

# ─── Fake LLM ──────────────────────────────────────────────────────────────


class _FakeLLM:
    """LLM scripté qui retourne une séquence prédéfinie d'AIMessage.

    `bind_tools` est un no-op (la liste d'outils est ignorée — c'est nous
    qui décidons quoi répondre).
    """

    def __init__(self, responses: list[AIMessage]):
        self.responses = list(responses)
        self.invocations: list[list] = []

    def bind_tools(self, _tools):
        return self

    def invoke(self, messages):
        self.invocations.append(list(messages))
        if not self.responses:
            return AIMessage(content="(fake LLM: no more scripted responses)")
        return self.responses.pop(0)


def _ai_tool_call(name: str, args: dict, content: str = "") -> AIMessage:
    """Construit un AIMessage avec un tool_call (format LangChain)."""
    return AIMessage(
        content=content,
        tool_calls=[
            {
                "name": name,
                "args": args,
                "id": "call_" + name,
                "type": "tool_call",
            }
        ],
    )


def _make_profile(profiles_root: Path, name: str, *, target: Path, folders: list[str],
                  mapping: dict[str, str]) -> None:
    pdir = profiles_root / name
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "profile.yaml").write_text(yaml.safe_dump({"name": name, "target": str(target)}))
    (pdir / "tree.yaml").write_text(yaml.safe_dump({"folders": folders}))
    (pdir / "theme_mapping.yaml").write_text(yaml.safe_dump(mapping))
    target.mkdir(parents=True, exist_ok=True)


# ─── Tests graphe ──────────────────────────────────────────────────────────


class _GraphTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="klodo_refonte_diag_"))
        self.profiles_root = self.tmp / "profiles"
        self.profiles_root.mkdir()
        self.target = self.tmp / "biblio"
        self._patcher = mock.patch.object(data, "get_project_root", return_value=self.tmp)
        self._patcher.start()
        tax.reset_cache()
        # Profil minimal pour que les tools puissent s'exécuter sans crasher
        _make_profile(
            self.profiles_root, "test_p",
            target=self.target,
            folders=["A", "B", "Autres"],
            mapping={"t1": "A", "t2": "B"},
        )

    def tearDown(self):
        self._patcher.stop()
        tax.reset_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestDiagnosticGraph(_GraphTestBase):
    def test_no_tool_call_routes_to_write_report(self):
        """LLM répond direct sans tool → on saute à write_report."""
        fake = _FakeLLM([
            AIMessage(content="J'estime que la taxonomie est OK."),  # explore
            AIMessage(content="# Diagnostic\nRien à signaler."),     # write_report
        ])
        graph = build_diagnostic_graph(llm=fake, max_llm_calls=5)
        result = graph.invoke({"profile": "test_p"})

        self.assertEqual(result["status"], "done")
        self.assertEqual(result["phase"], "A")
        self.assertIn("Diagnostic", result["report"])
        self.assertEqual(len(fake.invocations), 2)  # explore + write_report

    def test_react_loop_calls_tool_then_reports(self):
        """LLM appelle 1 tool puis termine → ReAct: explore → tools → explore → report."""
        fake = _FakeLLM([
            _ai_tool_call("list_folders", {"profile": "test_p"}),    # 1er explore : tool call
            AIMessage(content="Vu, taxonomie correcte."),            # 2e explore : no tool call
            AIMessage(content="# Diagnostic\nProfil = test_p."),     # write_report
        ])
        graph = build_diagnostic_graph(llm=fake, max_llm_calls=5)
        result = graph.invoke({"profile": "test_p"})

        self.assertEqual(result["status"], "done")
        # ToolMessage avec le résultat de list_folders doit être dans l'historique
        tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        self.assertEqual(len(tool_messages), 1)
        # Le contenu doit ressembler à la liste des folders
        self.assertIn("A", tool_messages[0].content)

    def test_budget_enforced_stop_message_injected(self):
        """Au-delà de max_llm_calls, le stop_msg force la conclusion."""
        # On simule 3 calls explore qui réclament un tool, avec budget=2
        fake = _FakeLLM([
            _ai_tool_call("list_folders", {"profile": "test_p"}),
            _ai_tool_call("read_theme_mapping", {"profile": "test_p"}),
            # 3e explore : on est au-delà du budget, stop_msg injecté ;
            # le LLM doit répondre sans tool_call
            AIMessage(content="OK je conclus."),
            AIMessage(content="# Diagnostic\nrapport partiel."),
        ])
        graph = build_diagnostic_graph(llm=fake, max_llm_calls=2)
        result = graph.invoke({"profile": "test_p"})
        self.assertEqual(result["status"], "done")
        # Vérifie que le dernier invoke d'explore (3e) contient le stop_msg
        # (le 3e invoke est l'index 2)
        third_invoke_msgs = fake.invocations[2]
        self.assertTrue(
            any("Budget" in str(getattr(m, "content", "")) for m in third_invoke_msgs),
            "Le stop_msg devrait être injecté quand le budget est dépassé",
        )

    def test_rejects_missing_profile(self):
        """Sans profile, status=error et aucun appel LLM (court-circuit après init)."""
        fake = _FakeLLM([])
        graph = build_diagnostic_graph(llm=fake, max_llm_calls=5)
        result = graph.invoke({})
        self.assertEqual(result["status"], "error")
        self.assertIn("profile", result["error"].lower())
        self.assertEqual(len(fake.invocations), 0)  # 0 appel LLM
        self.assertNotIn("report", result)


# ─── Tests factory llm.py ──────────────────────────────────────────────────


class TestAgentLLMFactory(unittest.TestCase):
    def test_raises_without_api_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                agent_llm.get_agent_llm()
            self.assertIn("SILICONFLOW_API_KEY", str(ctx.exception))

    def test_uses_default_model_when_not_set(self):
        with mock.patch.dict(os.environ, {"SILICONFLOW_API_KEY": "fake-key"}, clear=True):
            llm = agent_llm.get_agent_llm()
            self.assertEqual(llm.model_name, agent_llm.DEFAULT_MODEL)

    def test_env_override_model(self):
        with mock.patch.dict(os.environ, {
            "SILICONFLOW_API_KEY": "fake-key",
            "KLODO_AGENT_MODEL": "zai-org/GLM-4.6",
        }, clear=True):
            llm = agent_llm.get_agent_llm()
            self.assertEqual(llm.model_name, "zai-org/GLM-4.6")

    def test_explicit_model_arg_wins(self):
        with mock.patch.dict(os.environ, {
            "SILICONFLOW_API_KEY": "fake-key",
            "KLODO_AGENT_MODEL": "env-model",
        }, clear=True):
            llm = agent_llm.get_agent_llm(model="explicit-model")
            self.assertEqual(llm.model_name, "explicit-model")


if __name__ == "__main__":
    unittest.main()
