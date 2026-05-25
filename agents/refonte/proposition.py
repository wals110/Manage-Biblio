"""Graphe Phase B — Proposition (read + side YAMLs).

Topologie :

    START → init → [conditional]
                    │ status=error → END (court-circuit)
                    │ ok → analyze ← ─ ┐
                              │      tools (read-only A.2 + propose_changes)
                              │       ↑
                              └───────┘   (boucle ReAct)
                              │ no tool_call OU propose_changes appelé
                              ↓
                            finalize → END

`init` :
  - valide profile + diagnostic_run_id en input
  - lit le rapport Phase A correspondant et l'injecte comme contexte
  - amorce les messages avec system + human prompts

`analyze` (= explore en Phase A) :
  - LLM avec accès aux 6 outils read-only A.2 + au tool mutable propose_changes
  - boucle ReAct jusqu'à ce que propose_changes soit appelé OU plus de tool_call

`finalize` :
  - vérifie qu'un proposal a été écrit
  - met status=done + remplit proposal_dir + proposal_summary dans le state
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from agents.llm import get_agent_llm
from agents.refonte.proposition_tools import make_proposition_tools
from agents.refonte.state import RefonteState
from agents.refonte.tools import TOOLS as READ_TOOLS
from dashboard import data

DEFAULT_MAX_LLM_CALLS = 8

SYSTEM_PROMPT_B = """Tu es l'agent IA Klodo en **Phase B (Proposition)**.

Ton objectif : à partir du **diagnostic Phase A** fourni en contexte, **proposer
une refonte concrète** de la taxonomie. Tu écris des fichiers YAML "proposed"
en side (jamais sur les YAMLs de production) qui seront ensuite simulés et,
éventuellement, appliqués par Phase C après validation utilisateur.

Tu disposes de 9 outils :

**Outils de lecture (validation / cross-check du diagnostic)** :
  - list_folders(profile)
  - count_files_per_folder(profile)
  - read_theme_mapping(profile)
  - list_themes_per_folder(profile)
  - compute_folder_overlap(profile, folder_a, folder_b)
  - get_classifier_breakdown(profile)
  - list_vision_themes(profile, top_n=50)
  - find_orphan_themes(profile, top_n=30)

**Outil mutable (écrit les YAMLs proposed — UNE seule fois par run)** :
  - propose_changes(creations, fusions, renamings, mappings_added)

Stratégie efficace (5-7 tool calls) :
  1. Lis le diagnostic ci-dessous. Il liste les anomalies (mappings manquants,
     dossiers sous-utilisés, catch-all qui débordent, doublons sémantiques).
  2. Si besoin, vérifie 1-2 cas via les outils read-only (ex. confirmer un
     thème orphelin via find_orphan_themes).
  3. Construis ta proposition complète, puis appelle propose_changes(...)
     **une seule fois** avec tous les changements groupés.

Conventions importantes pour propose_changes :
  - `creations` : nouveaux dossiers (path relatif depuis le target).
  - `fusions` : `sources` = liste de paths existants, `target` = path destination
    (peut être un path créé par `creations` ou un dossier existant).
  - `renamings` : changement de nom d'un dossier existant.
  - `mappings_added` : pour chaque thème orphelin du diagnostic, mappe-le vers
    son dossier cible évident. PRIORITÉ : c'est ça qui débloque le plus de
    fichiers mal classés. Ajoute 15-30 mappings si le diagnostic en propose
    autant.

Chaque entrée doit avoir un champ `rationale` court (1-2 phrases) qui justifie
le choix — c'est ce qui apparaîtra dans `refonte-rationale.md`.

**Règles absolues** :
  - N'invente PAS de thèmes ou de chemins : utilise uniquement ceux du
    diagnostic ou retournés par les outils de lecture.
  - Les chemins cibles des `mappings_added` doivent exister dans `tree.yaml`
    courant OU être créés via `creations`.
  - Sois EXHAUSTIF sur les mappings_added (15-30+ si le diagnostic les liste) —
    c'est l'action à plus haut ROI.

Quand tu as appelé propose_changes une fois avec succès, **réponds sans appeler
d'autre outil** — Phase B est terminée."""


# ─── Nœuds ──────────────────────────────────────────────────────────────────


def _read_diagnostic_report(profile: str, diagnostic_run_id: str) -> str | None:
    """Lit le report.md du run de diagnostic Phase A référencé."""
    report_path = (
        data.get_project_root()
        / "profiles" / profile / ".cache" / "refonte"
        / diagnostic_run_id / "report.md"
    )
    if not report_path.exists():
        return None
    try:
        return report_path.read_text(encoding="utf-8")
    except OSError:
        return None


def _init_node(state: RefonteState) -> dict[str, Any]:
    """Valide profile + diagnostic_run_id et amorce les messages.

    Lecture du rapport Phase A injectée comme contexte HumanMessage —
    l'agent Phase B s'appuie dessus pour générer la proposition.
    """
    if not state.get("profile"):
        return {"status": "error", "error": "profile is required"}
    diagnostic_run_id = state.get("diagnostic_run_id")
    if not diagnostic_run_id:
        return {
            "status": "error",
            "error": "diagnostic_run_id is required (run de Phase A à utiliser comme contexte)",
        }
    profile = state["profile"]
    diagnostic_md = _read_diagnostic_report(profile, diagnostic_run_id)
    if diagnostic_md is None:
        return {
            "status": "error",
            "error": (
                f"Rapport de diagnostic introuvable pour run_id={diagnostic_run_id} "
                f"(profile={profile}). Vérifie qu'une Phase A a bien terminé "
                f"avec status=done sur ce run avant de lancer Phase B."
            ),
        }
    return {
        "phase": "B",
        "run_id": state.get("run_id") or str(uuid.uuid4()),
        "status": "running",
        "llm_calls": 0,
        "messages": [
            SystemMessage(content=SYSTEM_PROMPT_B),
            HumanMessage(
                content=(
                    f"Voici le diagnostic Phase A du profil `{profile}` à utiliser "
                    f"comme base de ta proposition :\n\n---\n{diagnostic_md}\n---\n\n"
                    f"Propose maintenant une refonte via propose_changes. "
                    f"Sois exhaustif sur les mappings_added."
                ),
            ),
        ],
    }


def _make_analyze_node(llm_with_tools: BaseChatModel, max_calls: int):
    """Boucle ReAct Phase B — équivalent du `explore` de Phase A."""

    def analyze(state: RefonteState) -> dict[str, Any]:
        n = state.get("llm_calls", 0)
        if n >= max_calls:
            stop_msg = SystemMessage(
                content=(
                    "Budget d'appels atteint. Si tu n'as pas encore appelé "
                    "propose_changes, fais-le maintenant avec ce que tu as en "
                    "main, sinon termine sans nouvel appel d'outil."
                ),
            )
            response = llm_with_tools.invoke(list(state["messages"]) + [stop_msg])
        else:
            response = llm_with_tools.invoke(state["messages"])
        return {
            "messages": [response],
            "llm_calls": n + 1,
        }

    return analyze


def _route_after_init(state: RefonteState) -> Literal["analyze", "__end__"]:
    if state.get("status") == "error":
        return "__end__"
    return "analyze"


def _route_after_analyze(state: RefonteState) -> Literal["tools", "finalize"]:
    """Si l'AI dernier message a des tool_calls → exécuter les outils.
    Sinon → finalize.
    """
    last = state["messages"][-1] if state.get("messages") else None
    if isinstance(last, AIMessage) and getattr(last, "tool_calls", None):
        return "tools"
    return "finalize"


def _finalize_node(state: RefonteState) -> dict[str, Any]:
    """Termine le run — vérifie qu'un proposal a bien été écrit."""
    messages = list(state.get("messages") or [])
    # Cherche le dernier ToolMessage venant d'un appel propose_changes
    proposal_result = None
    for m in reversed(messages):
        if isinstance(m, ToolMessage) and m.name == "propose_changes":
            try:
                proposal_result = json.loads(m.content) if isinstance(m.content, str) else m.content
            except (json.JSONDecodeError, TypeError):
                proposal_result = {"raw": str(m.content)}
            break
    if not proposal_result:
        return {
            "status": "error",
            "error": (
                "L'agent n'a pas appelé propose_changes — aucune proposition "
                "n'a été générée. Probablement un problème de prompt ou de modèle."
            ),
        }
    # Récupère le proposal_dir depuis le tool result
    tree_path = proposal_result.get("tree_proposed_path", "")
    proposal_dir = str(Path(tree_path).parent) if tree_path else ""
    return {
        "status": "done",
        "proposal_dir": proposal_dir,
        "proposal_summary": {
            "n_creations": proposal_result.get("n_creations", 0),
            "n_fusions": proposal_result.get("n_fusions", 0),
            "n_renamings": proposal_result.get("n_renamings", 0),
            "n_mappings_added": proposal_result.get("n_mappings_added", 0),
            "n_folders_after": proposal_result.get("n_folders_after", 0),
            "n_mappings_after": proposal_result.get("n_mappings_after", 0),
        },
    }


# ─── Construction du graphe ───────────────────────────────────────────────


def build_proposition_graph(
    profile: str,
    run_id: str,
    llm: BaseChatModel | None = None,
    *,
    max_llm_calls: int = DEFAULT_MAX_LLM_CALLS,
):
    """Construit le graphe Phase B pour un run spécifique.

    `profile` + `run_id` sont nécessaires au build pour binder le tool
    propose_changes en closure (le LLM ne les voit jamais). En revanche
    `diagnostic_run_id` est lu depuis le state par `_init_node` — le caller
    le passe via `graph.invoke({"profile": ..., "diagnostic_run_id": ...})`.

    Args:
        profile: Nom du profil cible.
        run_id: UUID du run Phase B (généré par le caller).
        llm: ChatModel. Default = get_agent_llm() (SiliconFlow DeepSeek V3.2).
        max_llm_calls: Budget pour la boucle ReAct (default 8, plus haut que
                       Phase A car la proposition est plus complexe).

    Returns:
        Compiled LangGraph.
    """
    if llm is None:
        llm = get_agent_llm()
    tools = READ_TOOLS + make_proposition_tools(profile, run_id)
    llm_with_tools = llm.bind_tools(tools)

    builder = StateGraph(RefonteState)
    builder.add_node("init", _init_node)
    builder.add_node("analyze", _make_analyze_node(llm_with_tools, max_llm_calls))
    builder.add_node("tools", ToolNode(tools))
    builder.add_node("finalize", _finalize_node)

    builder.add_edge(START, "init")
    builder.add_conditional_edges(
        "init",
        _route_after_init,
        {"analyze": "analyze", "__end__": END},
    )
    builder.add_conditional_edges(
        "analyze",
        _route_after_analyze,
        {"tools": "tools", "finalize": "finalize"},
    )
    builder.add_edge("tools", "analyze")
    builder.add_edge("finalize", END)

    return builder.compile()


__all__ = ["build_proposition_graph", "DEFAULT_MAX_LLM_CALLS"]
