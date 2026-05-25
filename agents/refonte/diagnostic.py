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
from datetime import UTC, datetime
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from agents.llm import get_agent_llm
from agents.refonte.state import RefonteState
from agents.refonte.tools import TOOLS

DEFAULT_MAX_LLM_CALLS = 6

SYSTEM_PROMPT = """Tu es un assistant qui analyse la taxonomie d'une bibliothèque PDF gérée par Klodo.

Ton objectif : identifier les **anomalies** dans la structure du profil et produire un rapport actionnable. Tu disposes de 8 outils en lecture seule :

**Signal structure (tree.yaml + theme_mapping.yaml + FS) :**
  - list_folders(profile) : liste des dossiers déclarés dans tree.yaml
  - count_files_per_folder(profile) : nombre de fichiers directement présents dans chaque dossier
  - read_theme_mapping(profile) : mapping thème → dossier (theme_mapping.yaml)
  - list_themes_per_folder(profile) : inverse du mapping (dossier → thèmes mappés)
  - compute_folder_overlap(profile, folder_a, folder_b) : indice de Jaccard sur les thèmes mappés
  - get_classifier_breakdown(profile) : répartition par source sur le dernier classify_*.csv

**Signal observation (vision_cache.json — ce que le LLM Vision a réellement vu dans les fichiers) :**
  - list_vision_themes(profile, top_n=50) : top N thèmes observés avec count + mapping actuel + sample_titles
  - find_orphan_themes(profile, top_n=30) : thèmes observés N fois mais SANS mapping (candidats au remappage)

**⚠ Arguments imposés pour la cohérence inter-runs** :
- Pour `find_orphan_themes`, **appelle toujours avec `top_n=30`** (jamais 10 ou 5) — tu as besoin de la vue exhaustive, pas d'un échantillon. Tronquer à 10 produit un rapport sous-développé.
- Pour `list_vision_themes` si tu en as besoin, `top_n=50` minimum.

**⚠ Règle de raisonnement essentielle — croiser structure et observation :**

Un dossier vide n'est **PAS** automatiquement "à supprimer". Vérifie d'abord les thèmes orphelins : si tu vois des thèmes liés sémantiquement à ce dossier qui sont mappés ailleurs (ou orphelins), le vrai problème est un mapping manquant, pas un dossier inutile.

Exemple type : folder `/02-INFORMATIQUE/03-Langages-Programmation/Python` contient 0 fichier, MAIS find_orphan_themes retourne 'python programming' (412 occurrences), 'django' (87), 'flask' (54). Recommandation correcte : **ajouter ces mappings**, pas supprimer le folder. Les 412 fichiers Python sont probablement classés dans `/Autres` faute de mapping.

Types d'anomalies à chercher :

  1. **Mappings manquants** (haute priorité) : thèmes orphelins fréquents (count >> 10) qui ont un dossier cible évident dans tree.yaml — à mapper en priorité
  2. **Dossiers sous-utilisés AVEC ou SANS hint thème** : croise count_files=0/bas + find_orphan_themes. Recommande "ajouter mapping" plutôt que "supprimer" si des thèmes orphelins matchent sémantiquement
  3. **Catch-all qui débordent** : dossiers /Autres ou /Generales avec count >> moyenne — candidats à scission (et leurs thèmes orphelins révèlent souvent quoi sortir)
  4. **Doublons sémantiques** : deux dossiers avec un Jaccard élevé (> 0.3)
  5. **Couverture faible** : trop de FAILED dans get_classifier_breakdown (> 10%)

Stratégie efficace en 5-6 tool calls :
  1. list_folders + count_files_per_folder (vue d'ensemble structure)
  2. find_orphan_themes (signal observation principal — top 30 suffit)
  3. read_theme_mapping OU list_vision_themes selon ce qui manque
  4. 1-2 compute_folder_overlap ciblés si tu suspectes des doublons

Quand tu as assez de matière, réponds **sans appeler d'outil** — la phase de rédaction du rapport prendra le relais."""


REPORT_PROMPT_TEMPLATE = """\
Rédige maintenant le rapport de diagnostic en **markdown français**, en commençant DIRECTEMENT par le titre de niveau 1 ci-dessous. **Aucun préambule, aucune répétition de ces instructions.**

Première ligne attendue (exacte) :
# Diagnostic taxonomy — profil `{profile}` — {date}

Structure obligatoire à suivre ensuite :

## Stats globales
- 3 à 5 puces concises (nombre de dossiers, fichiers, mappings, etc.)

## Anomalies détectées (par priorité)

### Mappings manquants critiques (<N> cas)
- **Liste exhaustive** : pour chaque thème orphelin retourné par find_orphan_themes avec count ≥ 40, écrire une ligne. Vise 20-30 items minimum si le profil en a autant.
- Format par ligne : `**<Thème>** (<count> fichiers) → dossier cible évident : <chemin>`
- Pas de "10+" ou "etc." — la liste doit être complète à partir des données récupérées.

### Dossiers sous-utilisés avec thèmes orphelins correspondants (<N> cas)
- Pour chaque dossier avec count_files = 0 ou < 5, vérifier s'il y a un thème orphelin dont le nom évoque sémantiquement ce dossier (ex. folder `/Python` ↔ orphan `python programming`). Liste tous les cas, pas seulement 3.

### Catch-all qui débordent (<N> cas)
- Dossiers /Autres ou /Generales avec count >> moyenne. Pour chaque, mentionner les thèmes orphelins qui devraient sortir vers d'autres dossiers.

### Doublons sémantiques (<N> cas ou "non analysé")
- Si compute_folder_overlap a été appelé : reporter les résultats. Sinon : "(non analysé)".

### Couverture faible (<N>% ou "non analysé")
- Si get_classifier_breakdown a été appelé : taux de FAILED. Sinon : "(non analysé)".

## Recommandations
- 5 à 8 actions concrètes (pas seulement 3), format impératif ("Mapper X vers Y", "Scinder le catch-all Z", etc.)
- Chaque recommandation : effort estimé (faible/moyen/élevé) entre parenthèses

Règles strictes :
- **Liste exhaustive, pas un résumé** : si find_orphan_themes a retourné 30 items, le rapport doit en citer la majorité (pas s'arrêter à 10). La verbosité fait la valeur du diagnostic.
- N'invente AUCUN chiffre — utilise uniquement ce que les outils ont retourné
- Si une zone n'a pas été explorée, écris explicitement "(non analysé)"
- Pas de placeholders <...> dans la sortie finale
- Pas de ```markdown``` autour du rapport — le rapport EST déjà markdown"""


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
            # Demande utilisateur : c'est important que ce soit un HumanMessage
            # (pas un AIMessage). Si on met un AIMessage ici, le LLM croit qu'il
            # a déjà parlé et termine immédiatement sans appeler d'outil — on
            # se retrouve avec un rapport vide marqué `done` (bug observé en
            # production 2026-05-25).
            HumanMessage(
                content=(
                    f"Analyse le profil `{profile}` de Klodo. "
                    "Démarre par list_folders, count_files_per_folder et "
                    "find_orphan_themes(top_n=30) pour avoir la vue d'ensemble, "
                    "puis identifie les anomalies. Termine par un rapport markdown "
                    "complet et exhaustif."
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
        messages = list(state.get("messages") or [])
        # Garde-fou : si l'agent n'a appelé AUCUN outil pendant la phase
        # explore, on a aucun signal pour produire un rapport sérieux. Sortir
        # en status=error plutôt que d'écrire un rapport garbage marqué done.
        tool_results_count = sum(1 for m in messages if isinstance(m, ToolMessage))
        if tool_results_count == 0:
            return {
                "status": "error",
                "error": (
                    "L'agent n'a appelé aucun outil pendant la phase d'exploration. "
                    "Cela indique généralement un problème de prompt ou de modèle LLM. "
                    "Relance le diagnostic ou essaie un autre modèle via KLODO_AGENT_MODEL."
                ),
                "llm_calls": state.get("llm_calls", 0),
            }

        # Injecte la date courante + le nom de profil dans le prompt — le LLM
        # n'a pas la notion de "today" et hallucinerait sinon une date arbitraire.
        prompt_text = REPORT_PROMPT_TEMPLATE.format(
            profile=state.get("profile", "?"),
            date=datetime.now(UTC).date().isoformat(),
        )
        prompt = SystemMessage(content=prompt_text)
        response = llm.invoke(messages + [prompt])
        content = response.content if isinstance(response.content, str) else str(response.content)
        # Nettoyage minimal : strip whitespace au début, et coupe tout ce qui
        # précède le titre `# Diagnostic` au cas où le LLM répète des instructions
        # avant le rapport (cas observé en smoke test 2026-05-25).
        if "# Diagnostic" in content:
            content = content[content.index("# Diagnostic"):]
        content = content.lstrip()
        # Garde-fou 2 : rapport trop court → probable problème (LLM coupé ou perdu)
        if len(content) < 200:
            return {
                "messages": [response],
                "status": "error",
                "error": (
                    f"Rapport produit trop court ({len(content)} chars). "
                    "Le LLM n'a probablement pas exploité les résultats des outils. "
                    "Relance le diagnostic."
                ),
                "report": content,
                "llm_calls": state.get("llm_calls", 0) + 1,
            }
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
