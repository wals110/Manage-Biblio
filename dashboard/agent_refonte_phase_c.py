"""Backend dashboard pour Phase C de l'agent Refonte — couche fine
au-dessus de agents.refonte.agent_journal + agent_backup, exposée par
les endpoints FastAPI.

Cette couche garde dashboard/app.py mince et permet de tester la logique
sans monter une instance FastAPI complète.
"""

from __future__ import annotations

from typing import Any

from agents.refonte import agent_backup, agent_journal
from dashboard import data


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
