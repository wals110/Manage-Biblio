#!/usr/bin/env python3
"""Tests scaffold pour l'agent Refonte — Phase A.1.

À ce stade, on valide uniquement :
  - Le module agents/refonte/ s'importe correctement
  - Le graphe Phase A se construit et se compile
  - Le graphe s'invoque end-to-end avec un state minimal
  - Les inputs invalides (sans profile) produisent status=error

Les outils (A.2), le LLM (A.3), et l'endpoint (A.4) sont testés
dans leurs propres modules de tests.
"""

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from agents.refonte import RefonteState, build_diagnostic_graph  # noqa: E402


class TestRefonteScaffold(unittest.TestCase):
    """Pré-requis structurels avant d'ajouter les outils et le LLM."""

    def test_state_is_typed_dict(self):
        """RefonteState doit être un TypedDict utilisable comme schéma LangGraph."""
        # TypedDict subclasses expose __annotations__
        self.assertIn("profile", RefonteState.__annotations__)
        self.assertIn("phase", RefonteState.__annotations__)
        self.assertIn("run_id", RefonteState.__annotations__)
        self.assertIn("status", RefonteState.__annotations__)

    def test_graph_compiles(self):
        """Le graphe Phase A se construit sans erreur."""
        graph = build_diagnostic_graph()
        self.assertIsNotNone(graph)

    def test_graph_invoke_happy_path(self):
        """Invocation end-to-end avec profile valide → status=running, run_id généré."""
        graph = build_diagnostic_graph()
        result = graph.invoke({"profile": "test"})
        self.assertEqual(result["profile"], "test")
        self.assertEqual(result["phase"], "A")
        self.assertEqual(result["status"], "running")
        self.assertTrue(result["run_id"])  # UUID non vide
        self.assertEqual(len(result["run_id"]), 36)  # format UUID4 standard

    def test_graph_invoke_preserves_run_id(self):
        """Si run_id est fourni dans l'état initial, il est préservé (resumable)."""
        graph = build_diagnostic_graph()
        result = graph.invoke({"profile": "test", "run_id": "fixed-id-1234"})
        self.assertEqual(result["run_id"], "fixed-id-1234")

    def test_graph_invoke_rejects_missing_profile(self):
        """Sans profile, le graphe doit reporter status=error sans crasher."""
        graph = build_diagnostic_graph()
        result = graph.invoke({})
        self.assertEqual(result["status"], "error")
        self.assertIn("profile", result["error"].lower())


if __name__ == "__main__":
    unittest.main()
