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
from agents.refonte.tools import find_orphan_themes
from dashboard import data

DEFAULT_MAX_LLM_CALLS = 6  # 6 suffit largement avec la pré-extraction d'orphelins
                            # qui supprime la phase d'exploration verbeuse

SYSTEM_PROMPT_B = """Tu es l'agent IA Klodo en **Phase B (Proposition)**.

Tu reçois en entrée :
  - le rapport markdown du diagnostic Phase A
  - **3 listes JSON pré-extraites** (mappings orphelins, dossiers sous-utilisés,
    catch-all qui débordent) directement utilisables

Ta tâche : appeler **propose_changes** UNE FOIS avec une proposition COMPLÈTE
qui adresse les 4 types d'anomalies du diagnostic, pas seulement les mappings.

═══════ Outils disponibles ═══════

**Lecture (cross-check si besoin)** :
  - list_folders, count_files_per_folder, read_theme_mapping,
    list_themes_per_folder, compute_folder_overlap, get_classifier_breakdown,
    list_vision_themes(top_n=50), find_orphan_themes(top_n=30)

**Mutable (UNE seule fois par run, à la fin)** :
  - propose_changes(creations, fusions, renamings, deletions, mappings_added)

═══════ Contraintes par type d'anomalie ═══════

**1. mappings_added — DRIVEN par la liste pré-extraite « orphan mappings »**

Pour CHAQUE entrée de cette liste, tu DOIS produire un mapping. Si la liste
contient N entrées, ton appel doit avoir au moins **N-10** mappings_added.
Une proposition avec 0 mapping_added est un ÉCHEC. La liste peut contenir
30, 100, 200 entrées — adapte-toi au volume, ne tronque pas.

Pour chaque entrée la liste peut contenir un champ `target_folder_suggested`
(héritage du diagnostic markdown) — utilise-le quand il est présent. Si
absent (orphelins au-delà du top 30 du diagnostic), tu choisis toi-même la
destination en t'appuyant sur tree.yaml et le sens du thème.

**2. creations — DRIVEN par les thèmes orphelins à fort volume (stratégie
agressive : l'humain filtrera ensuite via l'UI)**

Règle quantitative : **pour CHAQUE thème orphelin avec `count ≥ 30`,
propose un folder dédié dans `creations` SAUF si un folder existant correspond
PRÉCISÉMENT au thème (homonymie ou quasi-homonymie)**, pas juste sémantiquement
voisin. Le `target_folder_suggested` du diagnostic est un point de départ mais
PAS une obligation — il pointait souvent vers un parent générique faute de
mieux.

Exemples du jeu de critères :

  ❌ MAPPING TROP LÂCHE (créer un folder à la place) :
    - `Penetration Testing` (73) → `Securite-Crypto` : trop générique, créer
      `Securite-Crypto/Penetration-Testing`
    - `Materials Science` (78) → `Chimie-Physique` : sujet distinct, créer
      `Sciences-des-Materiaux` au bon niveau
    - `Database Administration` (56) → `Bases-de-Donnees` : créer
      `Bases-de-Donnees/Administration`

  ✅ MAPPING LÉGITIME (le folder existe et désigne EXACTEMENT le thème) :
    - `Functional Analysis` (176) → `MATHEMATIQUES/02-Analyse` : Analyse
      fonctionnelle EST l'analyse, mappe.
    - `Web Services` (10) → `Reseaux-Telecom/Web` : count faible + sujet
      bien recouvert.

La destination d'un mapping peut aussi être un folder nouveau de `creations` :
si tu crées `Securite-Crypto/Penetration-Testing`, le mapping
`Penetration Testing → Securite-Crypto/Penetration-Testing` est cohérent.

Aussi : si un catch-all est trop gros, créer des sous-dossiers où ses fichiers
iront naturellement.

**Volume attendu** : sur une biblio de 18k fichiers avec 200 thèmes orphelins,
on attend **30-60 creations** (pas 4). Si tu en proposes < 20, tu n'as pas
appliqué la règle « count ≥ 30 → folder dédié ». L'humain filtrera les
suggestions qu'il juge superflues — sois généreux, pas timide.

**3. deletions — DRIVEN par les dossiers sous-utilisés sans rescousse**

Pour CHAQUE dossier de la liste « sous-utilisés », évalue :
  - Si un thème orphelin pointe naturellement vers lui → ajoute un mapping
    (cas le plus fréquent : le dossier est vide *parce que* le mapping
    manque) → PAS de deletion
  - Si le dossier est vide ET aucun thème orphelin n'évoque sémantiquement
    son nom → propose une `deletion` (folder mort)
  - Si plusieurs dossiers sous-utilisés sont proches sémantiquement →
    propose une `fusion`

**4. fusions — DRIVEN par les doublons sémantiques + les catch-all**

  - Doublons sémantiques (Jaccard élevé) → fusion (ou renaming si l'un
    des deux a un meilleur nom)
  - Catch-all trop gros : crée les sous-catégories via `creations` +
    ajoute des mappings_added vers les sous-catégories. Pas de fusion ici.

**5. renamings — opportuniste**

Si un dossier a un nom peu informatif ou redondant (ex. `/Generale`
contenant 2 000 fichiers très ciblés), propose un renaming.

═══════ rationale par entrée ═══════

1 phrase courte, factuelle. Cite les counts d'occurrences pour les
mappings_added quand pertinent. Pour les deletions, justifie pourquoi
**aucun** orphelin ne s'y raccroche.

═══════ Exemple d'appel correct ═══════

```json
{
  "creations": [
    {"path": "01-SCIENCES/CHIMIE/04-Science-des-Materiaux",
     "rationale": "78 fichiers Materials Science orphelins, sujet distinct de Chimie-Physique"},
    {"path": "02-INFORMATIQUE/10-Securite-Crypto/Penetration-Testing",
     "rationale": "73 fichiers Penetration Testing — sous-dossier dédié plutôt que diluer dans Securite-Crypto"},
    {"path": "02-INFORMATIQUE/07-Bases-de-Donnees/Administration",
     "rationale": "56 fichiers Database Administration"},
    /* ... une création par thème ≥ 30 sans homonyme exact ... */
  ],
  "deletions": [
    {"path": "08-LOISIRS/DESSIN",
     "rationale": "0 fichier et aucun thème orphelin lié au dessin observé"}
  ],
  "fusions": [
    {"sources": ["07-LANGUES/AUTRES"], "target": "07-LANGUES",
     "rationale": "AUTRES vide, on consolide à la racine LANGUES"}
  ],
  "mappings_added": [
    {"theme": "Functional Analysis", "folder": "01-SCIENCES/MATHEMATIQUES/02-Analyse",
     "rationale": "176 fichiers — homonymie exacte avec le folder Analyse"},
    {"theme": "Penetration Testing", "folder": "02-INFORMATIQUE/10-Securite-Crypto/Penetration-Testing",
     "rationale": "73 fichiers — pointe sur le nouveau folder créé ci-dessus"},
    /* ... une entrée par item de la liste orphan mappings ... */
  ]
}
```

═══════ Anti-patterns à éviter ═══════

- ❌ Ne livrer que 2-3 mappings alors que la liste pré-extraite en a 50+
- ❌ Ne proposer que 4 créations alors que 30+ thèmes orphelins ont count ≥ 30
  — c'est l'erreur la plus fréquente. Sois généreux sur `creations`, l'humain
  filtrera ensuite. Préférer trop que pas assez.
- ❌ Mapper un gros thème (count ≥ 30) vers un folder existant **générique**
  juste parce qu'il existe (`Penetration Testing → Securite-Crypto`).
  La règle : folder dédié sauf homonymie exacte.
- ❌ Ignorer les dossiers sous-utilisés et les catch-all (toi seul peux
  décider "delete vs. map vs. fuse")
- ❌ Appeler des outils en boucle pour "redécouvrir" ce qui est déjà
  dans les listes pré-extraites
- ❌ Inventer des `theme` ou `folder` qui ne sont ni dans les listes
  pré-extraites ni dans tree.yaml

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


# Matche les listes type :
#   - **02-INFORMATIQUE/03-Langages-Programmation/Python** (0 fichier) ↔ ...
#   - **/Path/Folder** (1 234 fichiers) – description
# La capture extrait : folder_path + count + reste (description).
_FOLDER_COUNT_RE = re.compile(
    r"^\s*(?:[-*]|\d+\.)?\s*\*\*([^*]+?)\*\*\s*\(([\d\s]+?)\s*fichiers?\)\s*"
    r"(?:[↔–\-])\s*(.+?)\s*$",
    re.MULTILINE,
)


def _parse_int_with_spaces(s: str) -> int | None:
    """Parse '4 489' → 4489 (le LLM utilise parfois des espaces comme thousands)."""
    try:
        return int(s.replace(" ", "").replace(" ", ""))
    except (ValueError, AttributeError):
        return None


def _extract_section(report_md: str, header_regex: str) -> str:
    """Extrait le contenu d'une section h3 du rapport markdown.

    Retourne le texte entre le header matché et le prochain h2/h3, ou ''
    si le header n'est pas trouvé.
    """
    h_re = re.compile(r"^###\s+" + header_regex, re.MULTILINE | re.IGNORECASE)
    m = h_re.search(report_md)
    if not m:
        return ""
    start = m.end()
    # Cherche le prochain h2/h3
    next_h = re.search(r"^##+\s+", report_md[start:], re.MULTILINE)
    end = start + next_h.start() if next_h else len(report_md)
    return report_md[start:end]


def parse_underutilized_folders_from_report(report_md: str) -> list[dict[str, Any]]:
    """Extrait les dossiers sous-utilisés (avec ou sans thèmes orphelins).

    Cherche la section `### Dossiers sous-utilisés...` et parse les lignes
    `**folder_path** (N fichier(s)) ↔/– description`.

    Returns:
        Liste de {"folder": str, "count": int, "note": str}.
    """
    if not report_md:
        return []
    section = _extract_section(report_md, r"dossiers?\s+sous[-\s]?utilis")
    if not section:
        return []
    results: list[dict[str, Any]] = []
    for m in _FOLDER_COUNT_RE.finditer(section):
        folder = m.group(1).strip().strip("`")
        count = _parse_int_with_spaces(m.group(2))
        note = m.group(3).strip()
        if folder and count is not None:
            results.append({"folder": folder, "count": count, "note": note})
    return results


def parse_catchall_folders_from_report(report_md: str) -> list[dict[str, Any]]:
    """Extrait les dossiers catch-all qui débordent.

    Cherche la section `### Catch-all qui débordent` et parse les lignes
    `**folder_path** (N fichiers) – description`.
    """
    if not report_md:
        return []
    section = _extract_section(report_md, r"catch[-\s]?all\s+qui\s+d[ée]bordent")
    if not section:
        return []
    results: list[dict[str, Any]] = []
    for m in _FOLDER_COUNT_RE.finditer(section):
        folder = m.group(1).strip().strip("`")
        count = _parse_int_with_spaces(m.group(2))
        note = m.group(3).strip()
        if folder and count is not None:
            results.append({"folder": folder, "count": count, "note": note})
    return results


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
    # Pré-extraction des 3 catégories d'anomalies — on sert au LLM des
    # listes JSON pré-mâchées plutôt que de lui faire ré-extraire du markdown
    # (ça avait produit des propositions vides en premier essai 2026-05-25).
    #
    # Orphan mappings : appel direct à find_orphan_themes (cap 200) pour
    # avoir une vue exhaustive — le markdown du diagnostic en cite seulement
    # ~30 par soucis de lisibilité humaine, ce qui plafonnait Phase B à 58 %
    # de couverture sur le profil default. On enrichit chaque orphelin avec
    # la suggestion de target_folder du diagnostic quand elle existe (top 30) ;
    # pour les suivants le LLM B propose lui-même la destination.
    try:
        all_orphans = find_orphan_themes(profile, top_n=200)
    except Exception:  # noqa: BLE001 — on dégrade gracieusement
        all_orphans = []
    markdown_orphans = parse_orphan_mappings_from_report(diagnostic_md)
    suggested_targets = {m["theme"]: m["target_folder"] for m in markdown_orphans}
    orphans: list[dict[str, Any]] = []
    if all_orphans:
        for o in all_orphans:
            theme = o.get("theme", "")
            if not theme:
                continue
            entry: dict[str, Any] = {
                "theme": theme,
                "count": o.get("count", 0),
            }
            target = suggested_targets.get(theme)
            if target:
                entry["target_folder_suggested"] = target
            # sample_titles volontairement omis : ne change pas la décision
            # du LLM (le nom du thème suffit) et gonflait le payload de 65 %
            # (40 k chars → 14 k chars sur top 200) ce qui poussait le 1er
            # appel LLM B à timeout côté SiliconFlow (cf. run 3f290cee).
            orphans.append(entry)
    else:
        # Fallback : find_orphan_themes a échoué (pas de vision_cache, IO error)
        # → on retombe sur l'extraction markdown du diagnostic. Pas exhaustif
        # mais évite de partir avec une proposition vide.
        for m in markdown_orphans:
            orphans.append({
                "theme": m["theme"],
                "count": m["count"],
                "target_folder_suggested": m["target_folder"],
            })

    underutilized = parse_underutilized_folders_from_report(diagnostic_md)
    catchall = parse_catchall_folders_from_report(diagnostic_md)

    expected_min = max(0, len(orphans) - 10)  # tolérance ±10 sur les mappings

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
            f"**Liste 1/3 — Mappings orphelins** (n={len(orphans)}) :",
            "```json",
            json.dumps(orphans, ensure_ascii=False, indent=2),
            "```",
            "",
            f"→ `mappings_added` doit contenir AU MINIMUM **{expected_min}** "
            f"entrées issues de cette liste (idéalement {len(orphans)}).",
            "",
        ]

    if underutilized:
        human_content_parts += [
            f"**Liste 2/3 — Dossiers sous-utilisés** (n={len(underutilized)}) :",
            "```json",
            json.dumps(underutilized, ensure_ascii=False, indent=2),
            "```",
            "",
            "→ Pour chaque entrée, décide : `mappings_added` (si un thème orphelin "
            "s'y rattache), `deletions` (si vide ET aucun orphelin lié), "
            "ou `fusions` (si plusieurs sont sémantiquement proches).",
            "",
        ]

    if catchall:
        human_content_parts += [
            f"**Liste 3/3 — Catch-all qui débordent** (n={len(catchall)}) :",
            "```json",
            json.dumps(catchall, ensure_ascii=False, indent=2),
            "```",
            "",
            "→ Pour chaque catch-all, crée les sous-catégories via `creations` + "
            "ajoute des `mappings_added` qui redirigent les thèmes vers ces "
            "sous-catégories. La fusion ne convient PAS ici (on veut désengorger, "
            "pas consolider davantage).",
            "",
        ]

    if not (orphans or underutilized or catchall):
        human_content_parts += [
            "(Aucune anomalie pré-extraite — soit le diagnostic n'en mentionne pas, "
            "soit le format ne match pas les parsers. Examine le markdown ci-dessus.)",
            "",
        ]

    human_content_parts += [
        "Appelle `propose_changes` **une fois**, avec une proposition complète",
        "qui couvre `mappings_added`, `creations`, `deletions`, `fusions`, et "
        "`renamings` (selon ce que les listes ci-dessus suggèrent).",
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
