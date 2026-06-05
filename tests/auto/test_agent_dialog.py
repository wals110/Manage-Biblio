#!/usr/bin/env python3
"""Tests pour agents.refonte.dialog — C.2 Phase C.

Stratégie : on mock le LLM (`structured_llm.invoke`) pour scripter les
MutationProposal retournées. On mock aussi `_invoke` pour intercepter
les mutations sans toucher au FS.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from agents.refonte import dialog, mutations  # noqa: E402
from dashboard import data  # noqa: E402


class _DialogBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="klodo_dialog_"))
        (self.tmp / "profiles" / "p").mkdir(parents=True)
        self._patcher = mock.patch.object(
            data, "get_project_root", return_value=self.tmp,
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mock_llm(self, proposal: dialog.MutationProposal):
        mock_llm = mock.MagicMock()
        mock_structured = mock.MagicMock()
        mock_llm.with_structured_output.return_value = mock_structured
        mock_structured.invoke.return_value = proposal
        return mock_llm


# ─── Setup et persistence ──────────────────────────────────────────────────


class TestStateAndPersistence(_DialogBase):
    def test_make_state_defaults(self):
        s = dialog.make_state("p")
        self.assertEqual(s["profile"], "p")
        self.assertEqual(s["status"], "idle")
        self.assertEqual(s["messages"], [])
        self.assertEqual(s["llm_calls"], 0)
        # conv_id auto
        self.assertIsNotNone(s["conv_id"])
        self.assertEqual(len(s["conv_id"]), 36)

    def test_save_then_load_roundtrip(self):
        s = dialog.make_state("p", conv_id="abc")
        s["messages"].append({"role": "user", "content": "hi", "ts": "X"})
        dialog.save_state(s)
        loaded = dialog.load_state("p", "abc")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["messages"], s["messages"])

    def test_load_missing_returns_none(self):
        self.assertIsNone(dialog.load_state("p", "ghost"))

    def test_list_conversations_sorted(self):
        # 3 conv avec updated_at différents
        for i, cid in enumerate(["a", "b", "c"]):
            s = dialog.make_state("p", conv_id=cid)
            # Force updated_at distinct via fake ts
            s["updated_at"] = f"2026-05-3{i}T00:00:00+00:00"
            path = dialog._state_path("p", cid)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(s), encoding="utf-8")
        listed = dialog.list_conversations("p")
        # Plus récent en tête (c)
        self.assertEqual([c["conv_id"] for c in listed], ["c", "b", "a"])

    def test_list_conversations_empty(self):
        self.assertEqual(dialog.list_conversations("p"), [])


# ─── run_user_message — happy path ─────────────────────────────────────────


class TestRunUserMessageHappy(_DialogBase):
    def test_proposes_and_awaits_confirm(self):
        llm = self._mock_llm(dialog.MutationProposal(
            tool="add_folder",
            args={"parent": "", "name": "Rust"},
            preview_text="Je vais créer le dossier Rust à la racine.",
            confidence="high",
        ))
        s = dialog.make_state("p")
        result = dialog.run_user_message(s, "Crée Rust à la racine", llm)
        self.assertEqual(result["status"], "awaiting_confirm")
        prop = result["proposed_mutation"]
        self.assertEqual(prop["tool"], "add_folder")
        self.assertEqual(prop["args"], {"parent": "", "name": "Rust"})
        # batch_id généré et stable au moment du wait_confirm
        self.assertIsNotNone(prop["batch_id"])
        # 2 messages (user + assistant preview)
        self.assertEqual(len(result["messages"]), 2)
        self.assertEqual(result["messages"][0]["role"], "user")
        self.assertEqual(result["messages"][1]["role"], "assistant")
        self.assertEqual(result["llm_calls"], 1)


# ─── run_user_message — cas d'erreur / clarification ──────────────────────


class TestRunUserMessageEdgeCases(_DialogBase):
    def test_low_confidence_stays_idle(self):
        llm = self._mock_llm(dialog.MutationProposal(
            tool=None,
            args={},
            preview_text="",
            confidence="low",
            notes="quel dossier précisément ?",
        ))
        s = dialog.make_state("p")
        result = dialog.run_user_message(s, "fais quelque chose", llm)
        self.assertEqual(result["status"], "idle")
        self.assertIsNone(result["proposed_mutation"])
        # 2 messages : user + clarification
        self.assertIn("quel dossier", result["messages"][1]["content"].lower())

    def test_refuses_new_message_when_awaiting_confirm(self):
        s = dialog.make_state("p")
        s["status"] = "awaiting_confirm"
        s["proposed_mutation"] = {"tool": "add_folder"}
        llm = mock.MagicMock()
        result = dialog.run_user_message(s, "autre intention", llm)
        self.assertEqual(result["status"], "error")
        self.assertIn("déjà en attente", result["error"])
        # LLM jamais appelé
        llm.with_structured_output.assert_not_called()

    def test_budget_exhausted(self):
        llm = mock.MagicMock()
        s = dialog.make_state("p")
        s["llm_calls"] = 5
        result = dialog.run_user_message(s, "test", llm, max_llm_calls=5)
        self.assertEqual(result["status"], "error")
        self.assertIn("budget", result["error"])

    def test_llm_exception_propagated_as_error(self):
        mock_llm = mock.MagicMock()
        mock_structured = mock.MagicMock()
        mock_llm.with_structured_output.return_value = mock_structured
        mock_structured.invoke.side_effect = RuntimeError("LLM down")
        s = dialog.make_state("p")
        result = dialog.run_user_message(s, "test", mock_llm)
        self.assertEqual(result["status"], "error")
        self.assertIn("LLM down", result["error"])


# ─── run_user_response : apply / skip / modify ─────────────────────────────


class TestRunUserResponseApply(_DialogBase):
    def _seed_proposal(self):
        s = dialog.make_state("p")
        s["status"] = "awaiting_confirm"
        s["proposed_mutation"] = {
            "tool": "add_folder",
            "args": {"parent": "", "name": "X"},
            "preview_text": "...",
            "confidence": "high",
            "notes": "",
            "batch_id": "batch-uuid",
        }
        s["messages"].append({"role": "user", "content": "create X", "ts": "_"})
        s["messages"].append({"role": "assistant", "content": "...", "ts": "_"})
        return s

    @mock.patch.object(dialog, "_invoke")
    def test_apply_calls_invoke_and_marks_done(self, mock_invoke):
        mock_invoke.return_value = {"tool": "add_folder", "batch_id": "..."}
        s = self._seed_proposal()
        result = dialog.run_user_response(s, "apply")
        self.assertEqual(result["status"], "done")
        self.assertIsNotNone(result["mutation_result"])
        # invoke a bien été appelé avec le proposed
        mock_invoke.assert_called_once()
        args = mock_invoke.call_args[0]
        self.assertEqual(args[0], "p")
        self.assertEqual(args[1]["tool"], "add_folder")

    @mock.patch.object(dialog, "_invoke")
    def test_apply_handles_mutation_error(self, mock_invoke):
        mock_invoke.side_effect = mutations.MutationError("folder exists")
        s = self._seed_proposal()
        result = dialog.run_user_response(s, "apply")
        self.assertEqual(result["status"], "error")
        self.assertIn("folder exists", result["error"])
        # Message assistant explique l'erreur
        last_msg = result["messages"][-1]
        self.assertEqual(last_msg["role"], "assistant")
        self.assertIn("Échec", last_msg["content"])


class TestRunUserResponseSkip(_DialogBase):
    def test_skip_clears_proposal(self):
        s = dialog.make_state("p")
        s["status"] = "awaiting_confirm"
        s["proposed_mutation"] = {"tool": "add_folder", "args": {}}
        result = dialog.run_user_response(s, "skip")
        self.assertEqual(result["status"], "idle")
        self.assertIsNone(result["proposed_mutation"])


class TestRunUserResponseModify(_DialogBase):
    def test_modify_reruns_parse_with_new_text(self):
        llm = self._mock_llm(dialog.MutationProposal(
            tool="add_folder", args={"parent": "/A", "name": "X"},
            preview_text="...", confidence="high",
        ))
        s = dialog.make_state("p")
        s["status"] = "awaiting_confirm"
        s["proposed_mutation"] = {"tool": "add_folder", "args": {}}
        result = dialog.run_user_response(
            s, "modify",
            modification_text="plutôt dans /A",
            llm=llm,
        )
        # Nouvelle proposition acceptée
        self.assertEqual(result["status"], "awaiting_confirm")
        self.assertEqual(result["proposed_mutation"]["args"]["parent"], "/A")

    def test_modify_requires_text_and_llm(self):
        s = dialog.make_state("p")
        s["status"] = "awaiting_confirm"
        s["proposed_mutation"] = {"tool": "add_folder", "args": {}}
        result = dialog.run_user_response(s, "modify")
        self.assertEqual(result["status"], "error")
        self.assertIn("modification_text", result["error"])


class TestRunUserResponseRejections(_DialogBase):
    def test_rejects_when_not_awaiting_confirm(self):
        s = dialog.make_state("p")  # idle
        result = dialog.run_user_response(s, "apply")
        self.assertEqual(result["status"], "error")
        self.assertIn("aucune proposition", result["error"])


# ─── Persistence après chaque tour ─────────────────────────────────────────


class TestPersistenceAfterEachStep(_DialogBase):
    def test_run_user_message_persists(self):
        llm = self._mock_llm(dialog.MutationProposal(
            tool="add_folder", args={"parent": "", "name": "X"},
            preview_text="...", confidence="high",
        ))
        s = dialog.make_state("p", conv_id="conv-1")
        dialog.run_user_message(s, "create X", llm)
        # State.json existe et contient bien la proposition
        loaded = dialog.load_state("p", "conv-1")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["status"], "awaiting_confirm")
        self.assertEqual(loaded["proposed_mutation"]["tool"], "add_folder")


if __name__ == "__main__":
    unittest.main()
