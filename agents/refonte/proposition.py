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
import re
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

DEFAULT_MAX_LLM_CALLS = 6  # 6 suffit largement avec la pré-extraction d'orphelins
                            # qui supprime la phase d'exploration verbeuse

SYSTEM_PROMPT_B = """Tu es l'agent IA Klodo en **Phase B (Proposition)**.

Tu reçois en entrée :
  - le rapport markdown du diagnostic Phase A
  - **une LISTE PRÉ-EXTRAITE des mappings orphelins** (JSON) directement utilisable

Ta tâche : appeler **propose_changes** UNE FOIS avec une proposition COMPLÈTE.

═══════ Outils disponibles ═══════

**Lecture (cross-check si besoin, mais la liste pré-extraite suffit dans 90% des cas)** :
  - list_folders(profile), count_files_per_folder(profile)
  - read_theme_mapping(profile), list_themes_per_folder(profile)
  - compute_folder_overlap(profile, a, b)
  - list_vision_themes(profile, top_n=50)
  - find_orphan_themes(profile, top_n=30)

**Mutable (UNE seule fois par run, à la fin)** :
  - propose_changes(creations, fusions, renamings, mappings_added)

═══════ Contraintes IMPÉRATIVES sur propose_changes ═══════

1. **mappings_added : exhaustif, pas symbolique.** Pour CHAQUE entrée de la
   liste pré-extraite, tu DOIS produire un mapping. Si la liste contient 30
   entrées, ton appel doit avoir AU MINIMUM 25 mappings_added (tolérance : tu
   peux retirer 5 entrées que tu juges hors scope, mais tu DOIS justifier).
   Une proposition avec 0 mapping_added est un ÉCHEC.

2. **creations** : si une `target_folder` d'un orphelin n'existe pas encore
   dans tree.yaml, crée-la (ajoute-la dans `creations`).

3. **fusions / renamings** : optionnels, basés sur les anomalies de doublons
   sémantiques du diagnostic. Pas obligatoires si le diagnostic n'en a pas vu.

4. **rationale** par entrée : 1 phrase courte, factuelle. Cite le count
   d'occurrences pour les mappings_added quand pertinent.

═══════ Exemple d'appel correct ═══════

```json
{
  "creations": [
    {"path": "01-SCIENCES/CHIMIE/04-Science-des-Materiaux",
     "rationale": "78 fichiers Materials Science orphelins"}
  ],
  "mappings_added": [
    {"theme": "Functional Analysis", "folder": "01-SCIENCES/MATHEMATIQUES/02-Analyse",
     "rationale": "176 fichiers — analyse fonctionnelle classique"},
    {"theme": "Complex Analysis", "folder": "01-SCIENCES/MATHEMATIQUES/02-Analyse",
     "rationale": "114 fichiers — analyse complexe"},
    {"theme": "Group Theory", "folder": "01-SCIENCES/MATHEMATIQUES/01-Algebre",
     "rationale": "112 fichiers — algèbre"}
    /* ... 15-30 entrées au total ... */
  ]
}
```

═══════ Anti-patterns à éviter ═══════

- ❌ Ne livrer que 2-3 mappings alors que la liste pré-extraite en a 30
- ❌ Appeler des outils de lecture en boucle pour "redécouvrir" ce qui est
  déjà dans la liste pré-extraite
- ❌ Inventer des `theme` ou `folder` qui ne sont pas dans la liste ou
  dans tree.yaml

Quand tu as appelé propose_changes avec succès, **termine sans nouvel outil**."""


# ─── Parser des mappings orphelins depuis le rapport Phase A ───────────────


# Matche : "[- ]**Theme** (NN fichiers) → <reste de ligne>"
# Le `reste de ligne` est nettoyé en post-traitement (strip label éventuel +
# backticks) pour tolérer plusieurs variantes du LLM ("dossier cible évident :",
# "cible :", aucun label, etc.).
_MAPPING_LINE_RE = re.compile(
    r"^\s*[-*]?\s*\*\*([^*]+?)\*\*\s*\((\d+)\s*fichiers?\)\s*[→]\s*(.+?)\s*$",
    re.MULTILINE,
)
# Strip un préfixe optionnel "dossier cible évident :" / "cible :" / "target :"
_TARGET_LABEL_RE = re.compile(
    r"^\s*(?:dossier\s+cible\s*(?:évident)?|cible|target)\s*[:：]\s*",
    re.IGNORECASE,
)


def parse_orphan_mappings_from_report(report_md: str) -> list[dict[str, Any]]:
    """Extrait la liste structurée des mappings orphelins du rapport Phase A.

    Cherche les lignes du format produit par REPORT_PROMPT_TEMPLATE de Phase A :
      - **Theme name** (NN fichiers) → dossier cible évident : `path/to/folder`

    Le parser est tolérant : il accepte "dossier cible :" / "cible :" / aucun
    label, et nettoie les backticks/guillemets résiduels autour du path.

    Args:
        report_md: Le contenu markdown du rapport (report.md).

    Returns:
        Liste de dicts `{"theme": str, "count": int, "target_folder": str}`,
        ordonnés comme dans le rapport (typiquement par count desc).
        Liste vide si aucun pattern matché (rapport mal formé / langue
        inattendue / hallucination).
    """
    if not report_md:
        return []
    results: list[dict[str, Any]] = []
    for m in _MAPPING_LINE_RE.finditer(report_md):
        theme = m.group(1).strip()
        try:
            count = int(m.group(2))
        except ValueError:
            continue
        raw_target = m.group(3)
        # Strip label éventuel ("dossier cible évident :" etc.)
        target = _TARGET_LABEL_RE.sub("", raw_target).strip()
        # Strip backticks/guillemets externes (ex. `01-SCIENCES/...` → 01-SCIENCES/...)
        target = target.strip("` '\"")
        # Strip ponctuation finale qui pourrait avoir collé (virgule, point)
        target = target.rstrip(".,;")
        if not target or not theme:
            continue
        results.append({"theme": theme, "count": count, "target_folder": target})
    return results


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
    # Pré-extraction des mappings orphelins du rapport — on les sert au LLM
    # en JSON pré-mâché plutôt que de lui faire ré-extraire du markdown.
    orphans = parse_orphan_mappings_from_report(diagnostic_md)
    orphans_json = json.dumps(orphans, ensure_ascii=False, indent=2)
    expected_min = max(0, len(orphans) - 5)  # tolérance : -5 du total

    human_content_parts = [
        f"Voici le diagnostic Phase A du profil `{profile}` :",
        "",
        "---",
        diagnostic_md,
        "---",
        "",
    ]
    if orphans:
        human_content_parts += [
            f"**Mappings orphelins pré-extraits** (n={len(orphans)}) — utilise CETTE liste",
            "comme base de `mappings_added`, **pas le markdown ci-dessus** :",
            "",
            "```json",
            orphans_json,
            "```",
            "",
            "**Contrainte stricte** : ton appel à `propose_changes` doit contenir",
            f"AU MINIMUM **{expected_min} mappings_added** issus de cette liste (ou {len(orphans)}",
            "si tu juges qu'aucun n'est hors scope). Pour chaque target_folder qui",
            "n'existe pas encore dans `tree.yaml`, ajoute une entrée dans `creations`.",
        ]
    else:
        human_content_parts += [
            "(Aucun mapping orphelin pré-extrait du rapport — soit le diagnostic",
            "n'en mentionne pas, soit le format ne match pas le parser. Examine",
            "le markdown ci-dessus et/ou utilise `find_orphan_themes(profile, top_n=30)`.)",
        ]
    human_content_parts += [
        "",
        "Appelle `propose_changes` **une fois**, avec une proposition complète.",
    ]

    return {
        "phase": "B",
        "run_id": state.get("run_id") or str(uuid.uuid4()),
        "status": "running",
        "llm_calls": 0,
        "messages": [
            SystemMessage(content=SYSTEM_PROMPT_B),
            HumanMessage(content="\n".join(human_content_parts)),
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
    """Termine le run — vérifie qu'un proposal a bien été écrit + simule.

    Après vérification du proposal, déclenche automatiquement
    `simulate_reclassify(...)` pour produire le CSV de projection. Comme
    c'est du Python pur (no LLM), c'est rapide et c'est plus simple pour
    l'utilisateur de tout voir d'un coup.
    """
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

    # Simulation reclassify avec le mapping proposé (no LLM, ~5-15s sur 18k files)
    simulation_summary: dict[str, Any] = {}
    if proposal_dir:
        try:
            from agents.refonte.simulator import simulate_reclassify
            profile = state.get("profile", "")
            simulation_summary = simulate_reclassify(profile, proposal_dir)
        except Exception as exc:  # pragma: no cover — best-effort
            # La simulation n'est pas critique pour considérer la proposition
            # produite ; on log dans le summary plutôt que d'échouer le run.
            simulation_summary = {
                "error": f"{type(exc).__name__}: {exc}",
                "n_files": 0,
            }

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
        "simulation_summary": simulation_summary,
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
