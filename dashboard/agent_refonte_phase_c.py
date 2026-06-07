"""Backend dashboard pour Phase C de l'agent Refonte — couche fine
au-dessus de agents.refonte.agent_journal + agent_backup, exposée par
les endpoints FastAPI.

Cette couche garde dashboard/app.py mince et permet de tester la logique
sans monter une instance FastAPI complète.
"""

from __future__ import annotations

from typing import Any

from agents.refonte import agent_backup, agent_journal, mutations
from dashboard import data

# ─── Feature flag UI ───────────────────────────────────────────────────
# Si False, le sous-onglet Refonte du dashboard masque tous les éléments
# Phase C (bouton "Nouvelle conversation", sidebar Conversations, vue
# chat, timeline mutations). Les endpoints API et tout le code Python
# restent intacts — seule la découverte UI est désactivée.
#
# Désactivé le 2026-06-07 (cf. discussion : pas assez de valeur ajoutée
# vs UI atomique existante ; chat agentique limité à 5 tools, pas de
# delete, 1 mutation/tour). Les actions disponibles dans le sub-tab
# restent : "Lancer un diagnostic" (Phase A) + "Proposer une refonte"
# (Phase B). Le code Phase C est conservé pour re-activation future.
PHASE_C_UI_ENABLED: bool = False


class RollbackError(Exception):
    """Tentative de rollback impossible (batch inexistant, déjà rollback, etc.)."""


def list_batches(profile: str) -> list[dict[str, Any]]:
    """Wrap agent_journal.list_batches — pour symétrie avec les autres
    helpers du wrapper."""
    return agent_journal.list_batches(profile)


def list_entries(
    profile: str,
    *,
    batch_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Wrap agent_journal.list_entries."""
    return agent_journal.list_entries(profile, batch_id=batch_id, limit=limit)


def rollback_batch(profile: str, batch_id: str) -> dict[str, Any]:
    """Restaure les YAML de production depuis le backup d'un batch.

    Policy : on rollback UNIQUEMENT le batch ciblé (pas en cascade). Si
    des batches plus récents existent, on warn dans la réponse mais on
    procède quand même — c'est le user qui valide.

    Workflow :
      1. Validation : profile + batch_id non vides
      2. Vérifie qu'on a bien un batch correspondant dans le journal
      3. Refuse si le batch a déjà été rolled back
      4. Trouve le backup_dir associé via find_backup_for_batch
      5. Restaure les YAML
      6. Journalise l'undo avec result selon succès/échec
      7. Renvoie un résumé avec liste des fichiers restaurés + flag warn
         si des batches plus récents existent

    Raises:
        ValueError: inputs invalides.
        RollbackError: batch introuvable, déjà rollback, ou backup absent.
    """
    if not profile:
        raise ValueError("profile is required")
    if not batch_id:
        raise ValueError("batch_id is required")

    # Validation du profil
    profile_dir = data.get_project_root() / "profiles" / profile
    if not profile_dir.is_dir():
        raise FileNotFoundError(f"profile not found: {profile!r}")

    batches = agent_journal.list_batches(profile)
    matching = next((b for b in batches if b["batch_id"] == batch_id), None)
    if matching is None:
        raise RollbackError(f"batch {batch_id!r} not found in journal")
    if matching.get("rolled_back"):
        raise RollbackError(
            f"batch {batch_id!r} has already been rolled back"
        )

    backup_dir = agent_backup.find_backup_for_batch(profile, batch_id)
    if backup_dir is None:
        # Cas pathologique : journal a une entrée mais le backup physique
        # a disparu (rotation trop agressive, suppression manuelle…).
        raise RollbackError(
            f"no backup directory found for batch {batch_id!r} "
            "(rotated out or manually deleted)"
        )

    try:
        result = agent_backup.restore_backup(profile, backup_dir)
    except agent_backup.BackupError as exc:
        # Journalise l'échec pour traçabilité, puis raise
        agent_journal.record_undo(profile, batch_id, result="error",
                                  error=str(exc))
        raise RollbackError(str(exc)) from exc

    # Journalise le succès
    agent_journal.record_undo(profile, batch_id, result="ok")

    # Détecte si des batches plus récents existent (warning, pas erreur)
    # `batches` est trié recent→ancien. Le batch ciblé est dans la liste,
    # ceux avant lui dans la liste sont plus récents.
    newer_count = 0
    for b in batches:
        if b["batch_id"] == batch_id:
            break
        if b.get("rolled_back"):
            continue
        newer_count += 1

    return {
        "profile": profile,
        "batch_id": batch_id,
        "backup_dir": backup_dir,
        "restored_files": result["restored"],
        "warning_newer_batches": newer_count,
    }


# ─── Tools mutables (C.1) ───────────────────────────────────────────────────


# Dispatcher tool_name → fonction. Permet d'avoir un seul endpoint
# /api/agent/refonte/c/mutate qui prend `tool` en body et route vers le
# bon helper, plutôt que N endpoints presque identiques. Plus simple à
# wirer côté agent conversationnel (C.2) qui appelle un seul endpoint.
_TOOL_DISPATCH: dict[str, Any] = {
    "add_folder": mutations.add_folder,
    "add_theme_mapping": mutations.add_theme_mapping,
    "rename_folder": mutations.rename_folder,
    "bulk_move_files": mutations.bulk_move_files,
    "merge_folders": mutations.merge_folders,
}


def list_available_tools() -> list[str]:
    """Liste des noms d'outils mutables disponibles."""
    return sorted(_TOOL_DISPATCH.keys())


def invoke_mutation(
    profile: str,
    tool: str,
    args: dict[str, Any],
    *,
    batch_id: str | None = None,
) -> dict[str, Any]:
    """Dispatche un appel mutation vers le bon helper.

    Args:
        profile: Profil cible.
        tool: Nom du tool (cf. list_available_tools).
        args: Arguments nommés à passer au tool (validés par le tool).
        batch_id: UUID partagé (généré si None).

    Raises:
        ValueError: profile vide ou tool inconnu.
        FileNotFoundError: profil inexistant.
        mutations.MutationError: validation taxonomy ou backup échoué.
    """
    if not profile:
        raise ValueError("profile is required")
    if tool not in _TOOL_DISPATCH:
        raise ValueError(
            f"unknown tool {tool!r}, available: {list_available_tools()}"
        )
    profile_dir = data.get_project_root() / "profiles" / profile
    if not profile_dir.is_dir():
        raise FileNotFoundError(f"profile not found: {profile!r}")
    fn = _TOOL_DISPATCH[tool]
    return fn(profile, batch_id=batch_id, **args)


# ─── Conversations (C.2) ────────────────────────────────────────────────────


def start_conversation(profile: str) -> dict[str, Any]:
    """Crée et persiste une nouvelle conversation pour le profil.

    Raises:
        ValueError: profile vide.
        FileNotFoundError: profil inexistant.
    """
    from agents.refonte import dialog
    if not profile:
        raise ValueError("profile is required")
    profile_dir = data.get_project_root() / "profiles" / profile
    if not profile_dir.is_dir():
        raise FileNotFoundError(f"profile not found: {profile!r}")
    state = dialog.make_state(profile)
    dialog.save_state(state)
    return state


def send_user_message(
    profile: str,
    conv_id: str,
    text: str,
) -> dict[str, Any]:
    """Envoie un message texte libre dans une conversation existante.

    Raises:
        ValueError: profile/conv_id/text vide.
        FileNotFoundError: conversation introuvable.
    """
    from agents.llm import get_agent_llm
    from agents.refonte import dialog
    if not profile or not conv_id or not text:
        raise ValueError("profile, conv_id and text are required")
    state = dialog.load_state(profile, conv_id)
    if state is None:
        raise FileNotFoundError(f"conversation not found: {conv_id!r}")
    llm = get_agent_llm()
    return dialog.run_user_message(state, text, llm)


def send_user_response(
    profile: str,
    conv_id: str,
    action: str,
    *,
    modification_text: str | None = None,
) -> dict[str, Any]:
    """Répond apply/skip/modify à une proposition en attente.

    Raises:
        ValueError: action invalide.
        FileNotFoundError: conversation introuvable.
    """
    from agents.llm import get_agent_llm
    from agents.refonte import dialog
    if action not in ("apply", "skip", "modify"):
        raise ValueError("action must be 'apply', 'skip' or 'modify'")
    state = dialog.load_state(profile, conv_id)
    if state is None:
        raise FileNotFoundError(f"conversation not found: {conv_id!r}")
    llm = get_agent_llm() if action == "modify" else None
    return dialog.run_user_response(
        state, action,  # type: ignore[arg-type]
        modification_text=modification_text,
        llm=llm,
    )


def get_conversation(profile: str, conv_id: str) -> dict[str, Any] | None:
    """Lit l'état d'une conversation (None si introuvable)."""
    from agents.refonte import dialog
    return dialog.load_state(profile, conv_id)


def list_conversations(
    profile: str,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Liste les conversations du profil triées par updated_at DESC."""
    from agents.refonte import dialog
    return dialog.list_conversations(profile, limit=limit)
