"""C.2 — Agent conversationnel Phase C.

Stack validée par user :
  - LangGraph MemorySaver + persistance JSON manuelle par conv_id
  - Confirmation explicite tour-par-tour (apply/skip/modify)
  - Pas de streaming SSE → polling toutes les 1.5s côté UI
  - Flow minimal : parse_intent → propose → wait_confirm → execute → end

Le graphe a 2 entrées distinctes :
  1. `run_user_message(state, text)` — texte libre → parse_intent → propose
  2. `run_user_response(state, action, ...)` — apply/skip/modify

Persistence : `profiles/<p>/.cache/refonte/conversations/<conv_id>/state.json`.
On stocke le state complet (messages + champs custom) pour permettre
audit + reprise. Le checkpointer LangGraph n'est PAS utilisé — on persiste
nous-mêmes à chaque tour pour simplicité.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from agents.refonte import mutations
from dashboard import data

ConvStatus = Literal[
    "idle",                # rien en cours, attend nouveau msg user
    "thinking",            # LLM en train de parser/proposer
    "awaiting_confirm",    # propose_mutation prêt, attend apply/skip/modify
    "executing",           # mutation en cours
    "done",                # mutation appliquée
    "error",               # erreur quelconque
]


class MutationProposal(BaseModel):
    """Sortie structurée du LLM parse_intent."""

    tool: str | None = Field(
        description=(
            "Nom du tool à invoquer parmi add_folder, add_theme_mapping, "
            "rename_folder, merge_folders, bulk_move_files. None si "
            "l'intention n'est pas claire ou hors scope."
        )
    )
    args: dict[str, Any] = Field(
        default_factory=dict,
        description="Arguments nommés du tool (cf. signature dans mutations.py)",
    )
    preview_text: str = Field(
        description=(
            "Explication courte en français de ce qui sera changé, "
            "destinée à l'utilisateur (1-3 phrases). Utilisé pour "
            "afficher dans le chat avant validation."
        )
    )
    confidence: Literal["high", "medium", "low"] = Field(
        description=(
            "high si tool/args sont sûrs, medium si plausibles mais "
            "ambigus, low si demande à clarifier"
        )
    )
    notes: str = Field(
        default="",
        description="Notes/questions si la confidence n'est pas high",
    )


_SYSTEM_PROMPT = """Tu es l'agent conversationnel Phase C de Klodo Refonte.

Tu reçois un message utilisateur exprimant l'intention de modifier la
taxonomie (créer/renommer/fusionner un dossier, ajouter un mapping de
thème, déplacer des fichiers en lot).

Ta tâche : produire une SEULE proposition de mutation parmi ces 5 tools :

1. add_folder(parent, name)
   Crée un nouveau dossier `name` sous `parent` ("" pour racine).
   Ex : "Crée le dossier 'Rust' dans 02-INFORMATIQUE/03-Langages" →
   {tool: "add_folder", args: {parent: "02-INFORMATIQUE/03-Langages", name: "Rust"}}

2. add_theme_mapping(theme, folder)
   Ajoute un mapping thème → dossier dans theme_mapping.yaml.
   Ex : "Mappe 'Rust Programming' vers 02-INFORMATIQUE/03-Langages/Rust" →
   {tool: "add_theme_mapping", args: {theme: "Rust Programming", folder: "02-INFORMATIQUE/03-Langages/Rust"}}

3. rename_folder(old_path, new_name)
   Renomme un dossier (et remappe les thèmes affectés).
   Ex : "Renomme le dossier ML en Machine-Learning" →
   {tool: "rename_folder", args: {old_path: "...ML", new_name: "Machine-Learning"}}

4. merge_folders(src_paths, dest_path)
   Fusionne des dossiers sources dans un seul (crée dest si absent,
   déplace tous les fichiers, supprime les sources).
   Ex : "Fusionne /A/X et /A/Y dans /A/XY" →
   {tool: "merge_folders", args: {src_paths: ["/A/X", "/A/Y"], dest_path: "/A/XY"}}

5. bulk_move_files(rel_paths, dest_folder)
   Déplace une liste de fichiers vers un dossier commun.
   Ex : "Déplace les 3 fichiers Rust vers Rust folder" →
   {tool: "bulk_move_files", args: {rel_paths: [...], dest_folder: "..."}}

Règles :
  - Une seule proposition par message user (pas de mutation enchaînée).
  - Si l'intention est ambiguë ou hors scope, mets tool=None,
    confidence="low", et explique dans `notes`.
  - `preview_text` doit être destiné à l'utilisateur : court, clair,
    en français.
  - Si on te demande de supprimer un dossier, refuse (hors scope —
    delete_folder n'est pas encore exposé) et explique pourquoi.
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def new_conv_id() -> str:
    return str(uuid.uuid4())


def make_state(profile: str, conv_id: str | None = None) -> dict[str, Any]:
    """État initial d'une conversation."""
    return {
        "profile": profile,
        "conv_id": conv_id or new_conv_id(),
        "status": "idle",
        "messages": [],          # liste de {role, content, ts}
        "proposed_mutation": None,  # {tool, args, preview_text, confidence, notes, batch_id}
        "mutation_result": None,
        "error": None,
        "llm_calls": 0,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }


def _conv_dir(profile: str, conv_id: str) -> Path:
    return (
        data.get_project_root()
        / "profiles" / profile / ".cache" / "refonte"
        / "conversations" / conv_id
    )


def _state_path(profile: str, conv_id: str) -> Path:
    return _conv_dir(profile, conv_id) / "state.json"


def save_state(state: dict[str, Any]) -> None:
    """Persiste l'état de la conversation (écriture atomique)."""
    profile = state["profile"]
    conv_id = state["conv_id"]
    state = {**state, "updated_at": now_iso()}
    path = _state_path(profile, conv_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


def load_state(profile: str, conv_id: str) -> dict[str, Any] | None:
    """Charge l'état d'une conversation. None si introuvable/corrompu."""
    path = _state_path(profile, conv_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_conversations(profile: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """Liste les conversations du profil triées par updated_at DESC."""
    root = (
        data.get_project_root()
        / "profiles" / profile / ".cache" / "refonte" / "conversations"
    )
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        state_file = entry / "state.json"
        if not state_file.exists():
            continue
        try:
            s = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append({
            "conv_id": s.get("conv_id", entry.name),
            "status": s.get("status", "?"),
            "created_at": s.get("created_at"),
            "updated_at": s.get("updated_at"),
            "n_messages": len(s.get("messages", [])),
        })
    out.sort(key=lambda c: c.get("updated_at") or "", reverse=True)
    return out[:limit]


def _append_message(state: dict, role: str, content: str) -> None:
    state["messages"].append({
        "role": role,
        "content": content,
        "ts": now_iso(),
    })


def run_user_message(
    state: dict[str, Any],
    text: str,
    llm: BaseChatModel,
    *,
    max_llm_calls: int = 5,
) -> dict[str, Any]:
    """Traite un nouveau message user → produit une proposition de mutation.

    Pas de re-prompt si la conversation est déjà en awaiting_confirm —
    le caller doit d'abord appeler run_user_response("skip") ou similaire.

    Args:
        state: état conv (sera muté).
        text: message texte de l'utilisateur.
        llm: ChatOpenAI configuré.
        max_llm_calls: budget par conversation (cumul).

    Returns:
        L'état mis à jour (même objet, retour pour fluidité).
    """
    if state.get("status") == "awaiting_confirm":
        # Sécurité : on refuse un nouveau message si une proposition
        # est encore en attente — sinon on perd la proposition silencieusement.
        state["status"] = "error"
        state["error"] = (
            "une proposition est déjà en attente de confirmation — "
            "réponds d'abord apply/skip/modify"
        )
        save_state(state)
        return state

    _append_message(state, "user", text)
    state["status"] = "thinking"
    state["proposed_mutation"] = None
    state["mutation_result"] = None
    state["error"] = None
    save_state(state)

    if state["llm_calls"] >= max_llm_calls:
        state["status"] = "error"
        state["error"] = f"budget llm épuisé ({max_llm_calls} appels max)"
        save_state(state)
        return state

    try:
        structured = llm.with_structured_output(
            MutationProposal, method="function_calling",
        )
        result: MutationProposal = structured.invoke([
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=text),
        ])
        state["llm_calls"] += 1
    except Exception as exc:  # noqa: BLE001
        state["status"] = "error"
        state["error"] = f"LLM error: {type(exc).__name__}: {exc}"
        save_state(state)
        return state

    if not result.tool or result.confidence == "low":
        # Pas de proposition exécutable — on affiche le message au user
        # pour clarifier et reste idle.
        assistant_msg = (
            f"Je ne suis pas sûr de comprendre. {result.notes}\n\n"
            "Tu peux reformuler ?"
        )
        if result.tool:
            assistant_msg = (
                f"{result.preview_text}\n\nMais je ne suis pas sûr : "
                f"{result.notes}\n\nClarifie ?"
            )
        _append_message(state, "assistant", assistant_msg)
        state["status"] = "idle"
        save_state(state)
        return state

    # Proposition prête → wait_confirm
    from agents.refonte.agent_journal import generate_batch_id
    proposed = {
        "tool": result.tool,
        "args": result.args,
        "preview_text": result.preview_text,
        "confidence": result.confidence,
        "notes": result.notes,
        "batch_id": generate_batch_id(),
    }
    state["proposed_mutation"] = proposed
    state["status"] = "awaiting_confirm"
    _append_message(state, "assistant", result.preview_text)
    save_state(state)
    return state


def run_user_response(
    state: dict[str, Any],
    action: Literal["apply", "skip", "modify"],
    *,
    modification_text: str | None = None,
    llm: BaseChatModel | None = None,
) -> dict[str, Any]:
    """Traite une réponse à un wait_confirm.

    Args:
        state: état conv (sera muté).
        action: "apply" exécute la mutation, "skip" rejette, "modify"
                relance parse_intent avec le texte fourni.
        modification_text: requis si action="modify" — nouveau message
                           user qui modifie la proposition.
        llm: requis si action="modify".

    Returns:
        L'état mis à jour.
    """
    if state.get("status") != "awaiting_confirm":
        state["status"] = "error"
        state["error"] = "aucune proposition en attente de confirmation"
        save_state(state)
        return state

    proposed = state.get("proposed_mutation")
    if not proposed:
        state["status"] = "error"
        state["error"] = "état incohérent : awaiting_confirm sans proposed_mutation"
        save_state(state)
        return state

    if action == "skip":
        _append_message(state, "user", "(skip)")
        _append_message(state, "assistant", "Proposition rejetée.")
        state["status"] = "idle"
        state["proposed_mutation"] = None
        save_state(state)
        return state

    if action == "modify":
        if not modification_text or not llm:
            state["status"] = "error"
            state["error"] = "modify requires modification_text and llm"
            save_state(state)
            return state
        # Reset awaiting_confirm puis re-run parse_intent
        state["status"] = "idle"
        state["proposed_mutation"] = None
        save_state(state)
        return run_user_message(state, modification_text, llm)

    # action == "apply"
    _append_message(state, "user", "(apply)")
    state["status"] = "executing"
    save_state(state)
    try:
        result = _invoke(state["profile"], proposed)
    except mutations.MutationError as exc:
        state["status"] = "error"
        state["error"] = f"mutation failed: {exc}"
        _append_message(state, "assistant", f"❌ Échec : {exc}")
        save_state(state)
        return state

    state["status"] = "done"
    state["mutation_result"] = result
    _append_message(
        state, "assistant",
        f"✓ Mutation appliquée (batch {proposed['batch_id'][:8]}). "
        "Rollback disponible depuis la timeline.",
    )
    save_state(state)
    return state


def _invoke(profile: str, proposed: dict[str, Any]) -> dict[str, Any]:
    """Helper isolé pour facile à mocker dans les tests."""
    tool = proposed["tool"]
    args = proposed["args"]
    batch_id = proposed["batch_id"]
    dispatch = {
        "add_folder": mutations.add_folder,
        "add_theme_mapping": mutations.add_theme_mapping,
        "rename_folder": mutations.rename_folder,
        "bulk_move_files": mutations.bulk_move_files,
        "merge_folders": mutations.merge_folders,
    }
    fn = dispatch.get(tool)
    if fn is None:
        raise mutations.MutationError(f"unknown tool: {tool}")
    return fn(profile, batch_id=batch_id, **args)
