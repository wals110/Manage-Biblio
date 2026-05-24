"""Graphe Phase A — Diagnostic (lecture seule, agent ReAct + LLM).

Topologie :

    START → init → explore ←──┐
                     │       tools
                     │        ↑
                     └────────┘   (boucle ReAct tant que le LLM appelle un tool)
                     ↓ (plus de tool call)
                  write_report
                     ↓
                    END

`init` valide les inputs et amorce les `messages` avec le system prompt.
`explore` est le nœud LLM — il décide d'appeler un outil ou de terminer.
`tools` est un ToolNode standard qui exécute les tools demandés.
`write_report` fait un dernier appel LLM dédié à la mise en forme markdown.

Budget LLM borné à `max_llm_calls` (default 5 par run, configurable). Au-delà,
le graphe sort de la boucle d'exploration et passe directement à
`write_report` (pour produire un rapport partiel plutôt que de boucler à
l'infini).
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from agents.llm import get_agent_llm
from agents.refonte.state import RefonteState
from agents.refonte.tools import TOOLS

DEFAULT_MAX_LLM_CALLS = 5

SYSTEM_PROMPT = """Tu es un assistant qui analyse la taxonomie d'une bibliothèque PDF gérée par Klodo.

Ton objectif : identifier les **anomalies** dans la structure du profil et produire un rapport actionnable. Tu disposes de 6 outils en lecture seule :

  - list_folders(profile) : liste des dossiers déclarés dans tree.yaml
  - count_files_per_folder(profile) : nombre de fichiers directement présents dans chaque dossier
  - read_theme_mapping(profile) : mapping thème → dossier (theme_mapping.yaml)
  - list_themes_per_folder(profile) : inverse du mapping (dossier → thèmes mappés)
  - compute_folder_overlap(profile, folder_a, folder_b) : indice de Jaccard sur les thèmes mappés
  - get_classifier_breakdown(profile) : répartition par source (LLM theme / Keyword / LLM mapper / N3-refined / FAILED) sur le dernier classify_*.csv

Types d'anomalies à chercher :

  1. **Dossiers sous-utilisés** : count_files très bas (< 5) — candidats à fusion ou suppression
  2. **Catch-all qui débordent** : dossiers /Autres ou /Generales avec count >> moyenne — candidats à scission
  3. **Doublons sémantiques** : deux dossiers avec un Jaccard élevé (> 0.3) sur leurs thèmes mappés
  4. **Mappings orphelins** : thèmes du mapping qui pointent vers un dossier absent de tree.yaml
  5. **Couverture faible** : trop de FAILED dans get_classifier_breakdown (> 10%)

Stratégie : commence par list_folders + count_files_per_folder + read_theme_mapping pour avoir une vue d'ensemble, puis cible 1-2 zones suspectes avec compute_folder_overlap. **Tu as un budget de 5 appels d'outils max.** Sois efficace.

Quand tu as assez de matière, réponds **sans appeler d'outil** — la phase de rédaction du rapport prendra le relais."""


REPORT_PROMPT = """À partir des observations ci-dessus, rédige un rapport de diagnostic en **markdown français** structuré ainsi :

```markdown
# Diagnostic taxonomy — profil `<name>` — <date>

## Stats globales
- ...

## Anomalies détectées (par priorité)

### <Nom de catégorie d'anomalie> (<N> cas)
- ...

## Recommandations
- ...
```

Reste factuel, cite les chiffres exacts récupérés via les outils. **N'invente pas de données.** Si une zone n'a pas été explorée, dis-le explicitement."""


# ─── Nœuds ──────────────────────────────────────────────────────────────────


def _init_node(state: RefonteState) -> dict[str, Any]:
    """Valide les inputs et amorce les messages avec le system prompt."""
    if not state.get("profile"):
        return {
            "status": "error",
            "error": "profile is required",
        }
    profile = state["profile"]
    return {
        "phase": "A",
        "run_id": state.get("run_id") or str(uuid.uuid4()),
        "status": "running",
        "llm_calls": 0,
        "messages": [
            SystemMessage(content=SYSTEM_PROMPT),
            # Message utilisateur factice qui pose la question
            AIMessage(
                content=(
                    f"Analyse le profil `{profile}`. "
                    "Identifie les anomalies de la taxonomie courante."
                ),
            ),
        ],
    }


def _make_explore_node(llm_with_tools: BaseChatModel, max_calls: int):
    """Construit le nœud d'exploration ReAct paramétré par le LLM bindé."""

    def explore(state: RefonteState) -> dict[str, Any]:
        n = state.get("llm_calls", 0)
        # Garde-fou : si on a déjà brûlé le budget, on injecte un message
        # qui force le LLM à conclure sans appeler de tool
        if n >= max_calls:
            stop_msg = SystemMessage(
                content=(
                    "Budget d'appels d'outils atteint. "
                    "Conclus avec ce que tu as observé, sans appeler de nouvel outil."
                )
            )
            response = llm_with_tools.invoke(list(state["messages"]) + [stop_msg])
        else:
            response = llm_with_tools.invoke(state["messages"])
        return {
            "messages": [response],
            "llm_calls": n + 1,
        }

    return explore


def _make_write_report_node(llm: BaseChatModel):
    """Nœud final : demande au LLM de structurer un rapport markdown."""

    def write_report(state: RefonteState) -> dict[str, Any]:
        prompt = SystemMessage(content=REPORT_PROMPT)
        response = llm.invoke(list(state["messages"]) + [prompt])
        content = response.content if isinstance(response.content, str) else str(response.content)
        return {
            "messages": [response],
            "report": content,
            "status": "done",
            "llm_calls": state.get("llm_calls", 0) + 1,
        }

    return write_report


def _route_after_init(state: RefonteState) -> Literal["explore", "__end__"]:
    """Si init a posé status=error (ex. profile manquant), on court-circuite."""
    if state.get("status") == "error":
        return "__end__"
    return "explore"


def _route_after_explore(state: RefonteState) -> Literal["tools", "write_report"]:
    """Si le dernier message AI contient des tool_calls → exécute les outils.

    Sinon → l'agent estime avoir fini, on passe à la rédaction du rapport.
    """
    last = state["messages"][-1] if state.get("messages") else None
    if isinstance(last, AIMessage) and getattr(last, "tool_calls", None):
        return "tools"
    return "write_report"


# ─── Construction du graphe ───────────────────────────────────────────────


def build_diagnostic_graph(
    llm: BaseChatModel | None = None,
    *,
    max_llm_calls: int = DEFAULT_MAX_LLM_CALLS,
):
    """Construit le graphe Phase A.

    Args:
        llm: ChatModel à utiliser. Default = `get_agent_llm()` (SiliconFlow
             DeepSeek-V3.2). Permet l'injection d'un fake LLM en tests.
        max_llm_calls: Budget max d'appels LLM dans la boucle ReAct.
                       Le rapport final compte en plus (= max + 1 au pire).

    Returns:
        Compiled LangGraph prêt à `.invoke({"profile": "default"})`.
    """
    if llm is None:
        llm = get_agent_llm()
    llm_with_tools = llm.bind_tools(TOOLS)

    builder = StateGraph(RefonteState)
    builder.add_node("init", _init_node)
    builder.add_node("explore", _make_explore_node(llm_with_tools, max_llm_calls))
    builder.add_node("tools", ToolNode(TOOLS))
    builder.add_node("write_report", _make_write_report_node(llm))

    builder.add_edge(START, "init")
    builder.add_conditional_edges(
        "init",
        _route_after_init,
        {"explore": "explore", "__end__": END},
    )
    builder.add_conditional_edges(
        "explore",
        _route_after_explore,
        {"tools": "tools", "write_report": "write_report"},
    )
    builder.add_edge("tools", "explore")
    builder.add_edge("write_report", END)

    return builder.compile()
