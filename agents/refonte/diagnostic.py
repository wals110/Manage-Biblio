"""Graphe Phase A — Diagnostic (lecture seule).

À ce stade (A.1 scaffolding), le graphe est minimal :

    START → init → END

Le nœud `init` ne fait que valider les inputs et marquer `status="running"`.
Les phases suivantes (A.2 → A.5) ajouteront :

    A.2 → ToolNode avec les 6 outils read-only
    A.3 → Nœud LLM `analyze` + nœud `write_report`
    A.4 → Streaming SSE pour le frontend

Le graphe minimal sert à valider :
- Que LangGraph s'installe et compile dans l'environnement Klodo
- Que le pattern `StateGraph[RefonteState]` est viable
- Que les tests peuvent invoquer un graphe end-to-end sans LLM
"""

from __future__ import annotations

import uuid
from typing import Any

from langgraph.graph import END, START, StateGraph

from agents.refonte.state import RefonteState


def _init_node(state: RefonteState) -> dict[str, Any]:
    """Nœud d'initialisation — valide les inputs et amorce la run.

    Génère un `run_id` si absent. Refuse de tourner sans `profile`.
    """
    if not state.get("profile"):
        return {
            "status": "error",
            "error": "profile is required",
        }
    return {
        "phase": "A",
        "run_id": state.get("run_id") or str(uuid.uuid4()),
        "status": "running",
    }


def build_diagnostic_graph():
    """Construit le graphe Phase A.

    Returns:
        Compiled LangGraph (CompiledStateGraph) prêt à `.invoke(initial_state)`.
    """
    builder = StateGraph(RefonteState)
    builder.add_node("init", _init_node)
    builder.add_edge(START, "init")
    builder.add_edge("init", END)
    return builder.compile()
