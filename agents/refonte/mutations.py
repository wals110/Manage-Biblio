"""C.1 — Tools mutables de l'agent Refonte Phase C.

Wrappers fins autour des helpers `dashboard.taxonomy` existants. Chaque
tool :
  1. Crée (ou réutilise) un backup pour le batch courant
  2. Effectue la mutation via le helper taxonomy
  3. Journalise le résultat (ok ou error)
  4. Renvoie un résumé sérialisable {batch_id, backup_dir, tool, result_data}

Les batch_ids sont passés par le caller. Si None, on en génère un nouveau
(utile pour les invocations CLI isolées). L'agent conversationnel (C.2)
groupera plusieurs mutations sous un même batch_id pour permettre un
rollback en bloc.

Tools exposés (cf. docs/refonte-agent-spec.md §C.1) :
  - add_folder
  - add_theme_mapping
  - rename_folder
  - merge_folders            (orchestration : add_folder dest + bulk_move + delete src)
  - bulk_move_files          (boucle de taxonomy.move_file)
"""

from __future__ import annotations

from typing import Any

from agents.refonte import agent_backup, agent_journal
from dashboard import taxonomy as _tax


class MutationError(Exception):
    """Erreur métier remontée par un tool mutable (validation, conflit…)."""


def _ensure_backup_for_batch(profile: str, batch_id: str) -> str:
    """Garantit qu'un backup existe pour ce batch_id, le crée sinon.

    Permet à plusieurs mutations d'un même batch de partager le snapshot
    initial — le rollback restaurera l'état AVANT la première mutation
    du batch, peu importe combien il en a eu.
    """
    existing = agent_backup.find_backup_for_batch(profile, batch_id)
    if existing:
        return existing
    return agent_backup.create_backup(profile, batch_id)


def _journal_and_raise(
    profile: str,
    *,
    tool: str,
    args: dict[str, Any],
    batch_id: str,
    backup_dir: str | None,
    exc: Exception,
) -> None:
    """Helper : journal une erreur puis re-raise comme MutationError."""
    agent_journal.append_entry(
        profile, tool=tool, args=args, result="error",
        batch_id=batch_id, backup_dir=backup_dir, error=str(exc),
    )
    raise MutationError(str(exc)) from exc


def _journal_ok(
    profile: str,
    *,
    tool: str,
    args: dict[str, Any],
    batch_id: str,
    backup_dir: str,
) -> None:
    agent_journal.append_entry(
        profile, tool=tool, args=args, result="ok",
        batch_id=batch_id, backup_dir=backup_dir,
    )


# ─── add_folder ─────────────────────────────────────────────────────────────


def add_folder(
    profile: str,
    parent: str,
    name: str,
    *,
    batch_id: str | None = None,
) -> dict[str, Any]:
    """Ajoute un nouveau dossier sous `parent` avec le nom `name`.

    Args:
        profile: Profil cible.
        parent: Path parent ("" pour racine).
        name: Nom simple du nouveau dossier.
        batch_id: UUID du batch (généré si None).

    Returns:
        {batch_id, backup_dir, tool, result_data}

    Raises:
        MutationError: si validation taxonomy échoue (dossier existant,
                       nom invalide, etc.) ou si backup échoue.
    """
    bid = batch_id or agent_journal.generate_batch_id()
    args = {"parent": parent, "name": name}
    try:
        backup_dir = _ensure_backup_for_batch(profile, bid)
    except agent_backup.BackupError as exc:
        _journal_and_raise(profile, tool="add_folder", args=args,
                           batch_id=bid, backup_dir=None, exc=exc)
    try:
        result = _tax.create_folder(profile, parent, name)
    except Exception as exc:  # noqa: BLE001 — on journalise + re-raise
        _journal_and_raise(profile, tool="add_folder", args=args,
                           batch_id=bid, backup_dir=backup_dir, exc=exc)
    _journal_ok(profile, tool="add_folder", args=args,
                batch_id=bid, backup_dir=backup_dir)
    return {
        "batch_id": bid,
        "backup_dir": backup_dir,
        "tool": "add_folder",
        "result_data": result,
    }


# ─── add_theme_mapping ──────────────────────────────────────────────────────


def add_theme_mapping(
    profile: str,
    theme: str,
    folder: str,
    *,
    batch_id: str | None = None,
) -> dict[str, Any]:
    """Ajoute un mapping thème → dossier dans theme_mapping.yaml."""
    bid = batch_id or agent_journal.generate_batch_id()
    args = {"theme": theme, "folder": folder}
    try:
        backup_dir = _ensure_backup_for_batch(profile, bid)
    except agent_backup.BackupError as exc:
        _journal_and_raise(profile, tool="add_theme_mapping", args=args,
                           batch_id=bid, backup_dir=None, exc=exc)
    try:
        result = _tax.add_mapping(profile, theme, folder)
    except Exception as exc:  # noqa: BLE001
        _journal_and_raise(profile, tool="add_theme_mapping", args=args,
                           batch_id=bid, backup_dir=backup_dir, exc=exc)
    _journal_ok(profile, tool="add_theme_mapping", args=args,
                batch_id=bid, backup_dir=backup_dir)
    return {
        "batch_id": bid,
        "backup_dir": backup_dir,
        "tool": "add_theme_mapping",
        "result_data": result,
    }


# ─── rename_folder ──────────────────────────────────────────────────────────


def rename_folder(
    profile: str,
    old_path: str,
    new_name: str,
    *,
    batch_id: str | None = None,
) -> dict[str, Any]:
    """Renomme un dossier (taxonomy.rename_folder remappe aussi les
    mappings de thèmes qui pointaient dessus)."""
    bid = batch_id or agent_journal.generate_batch_id()
    args = {"old_path": old_path, "new_name": new_name}
    try:
        backup_dir = _ensure_backup_for_batch(profile, bid)
    except agent_backup.BackupError as exc:
        _journal_and_raise(profile, tool="rename_folder", args=args,
                           batch_id=bid, backup_dir=None, exc=exc)
    try:
        result = _tax.rename_folder(profile, old_path, new_name)
    except Exception as exc:  # noqa: BLE001
        _journal_and_raise(profile, tool="rename_folder", args=args,
                           batch_id=bid, backup_dir=backup_dir, exc=exc)
    _journal_ok(profile, tool="rename_folder", args=args,
                batch_id=bid, backup_dir=backup_dir)
    return {
        "batch_id": bid,
        "backup_dir": backup_dir,
        "tool": "rename_folder",
        "result_data": result,
    }


# ─── bulk_move_files ────────────────────────────────────────────────────────


def bulk_move_files(
    profile: str,
    rel_paths: list[str],
    dest_folder: str,
    *,
    batch_id: str | None = None,
) -> dict[str, Any]:
    """Déplace une liste de fichiers vers le même dossier de destination.

    Les fichiers sont traités séquentiellement. Si l'un échoue, on
    continue avec les suivants (best-effort) et on agrège les erreurs.
    Le journal liste chaque résultat individuel pour traçabilité.

    Args:
        profile: Profil.
        rel_paths: Liste des chemins fichiers relatifs au target du profil.
        dest_folder: Dossier de destination commun.
        batch_id: UUID partagé (optionnel).

    Returns:
        {batch_id, backup_dir, tool, result_data:
            {moved: [rel_path...], errors: [{path, error}...]}}
    """
    bid = batch_id or agent_journal.generate_batch_id()
    args = {"rel_paths": list(rel_paths), "dest_folder": dest_folder}
    try:
        backup_dir = _ensure_backup_for_batch(profile, bid)
    except agent_backup.BackupError as exc:
        _journal_and_raise(profile, tool="bulk_move_files", args=args,
                           batch_id=bid, backup_dir=None, exc=exc)
    moved: list[str] = []
    errors: list[dict[str, str]] = []
    for rel_path in rel_paths:
        try:
            _tax.move_file(profile, rel_path, dest_folder)
            moved.append(rel_path)
        except Exception as exc:  # noqa: BLE001
            errors.append({"path": rel_path, "error": str(exc)})
    # Journal global pour ce bulk (les erreurs individuelles sont dans
    # result_data — pas la peine de spammer le journal avec N entries).
    result_summary = {"moved": moved, "errors": errors}
    result = "ok" if not errors else "error"
    error_msg = (
        f"{len(errors)} of {len(rel_paths)} files failed"
        if errors else None
    )
    agent_journal.append_entry(
        profile, tool="bulk_move_files",
        args={**args, "summary": {"n_moved": len(moved), "n_errors": len(errors)}},
        result=result, batch_id=bid, backup_dir=backup_dir, error=error_msg,
    )
    return {
        "batch_id": bid,
        "backup_dir": backup_dir,
        "tool": "bulk_move_files",
        "result_data": result_summary,
    }


# ─── merge_folders ──────────────────────────────────────────────────────────


def merge_folders(
    profile: str,
    src_paths: list[str],
    dest_path: str,
    *,
    batch_id: str | None = None,
) -> dict[str, Any]:
    """Fusionne plusieurs dossiers sources dans un seul dossier destination.

    Workflow :
      1. Crée le dossier `dest_path` s'il n'existe pas (via create_folder)
      2. Pour chaque src_path : déplace TOUS les fichiers vers dest_path
         puis supprime le dossier source (delete_folder force=True)
      3. Les mappings de thèmes pointant vers les src sont remappés vers
         dest (logique déléguée à taxonomy.delete_folder dans son flux)

    Args:
        profile: Profil cible.
        src_paths: Liste de dossiers sources à fusionner.
        dest_path: Dossier destination (sera créé si absent).
        batch_id: UUID partagé.

    Returns:
        {batch_id, backup_dir, tool, result_data:
            {created_dest: bool, sources_merged: [path...],
             sources_failed: [{path, error}...]}}
    """
    bid = batch_id or agent_journal.generate_batch_id()
    args = {"src_paths": list(src_paths), "dest_path": dest_path}
    try:
        backup_dir = _ensure_backup_for_batch(profile, bid)
    except agent_backup.BackupError as exc:
        _journal_and_raise(profile, tool="merge_folders", args=args,
                           batch_id=bid, backup_dir=None, exc=exc)
    # 1. Crée dest si absent
    created_dest = False
    snapshot = _tax.get_snapshot(profile, force_reload=True)
    existing_folders = {f["path"] for f in snapshot.get("folders", [])}
    if dest_path not in existing_folders:
        # Décompose dest_path en parent/name pour create_folder
        if "/" in dest_path:
            parent, name = dest_path.rsplit("/", 1)
        else:
            parent, name = "", dest_path
        try:
            _tax.create_folder(profile, parent, name)
            created_dest = True
        except Exception as exc:  # noqa: BLE001
            _journal_and_raise(profile, tool="merge_folders", args=args,
                               batch_id=bid, backup_dir=backup_dir, exc=exc)
    # 2. Pour chaque source, move all files + delete folder
    sources_merged: list[str] = []
    sources_failed: list[dict[str, str]] = []
    for src in src_paths:
        try:
            # Délègue au flux taxonomy : delete_folder déclenche le
            # mécanisme de migration des thèmes mappés vers dest_path.
            # Note : delete_folder ne déplace PAS les fichiers — on doit
            # le faire à la main avant.
            # limit=10000 = tous les fichiers en pratique (les bibliothèques
            # avec un dossier de 10k fichiers sortent de notre scope d'usage)
            listing = _tax.list_files_in_folder(profile, src, limit=10000)
            for f in listing.get("files", []):
                _tax.move_file(profile, f["rel_path"], dest_path)
            _tax.delete_folder(profile, src, force=True)
            sources_merged.append(src)
        except Exception as exc:  # noqa: BLE001
            sources_failed.append({"path": src, "error": str(exc)})
    result_summary = {
        "created_dest": created_dest,
        "sources_merged": sources_merged,
        "sources_failed": sources_failed,
    }
    result = "ok" if not sources_failed else "error"
    error_msg = (
        f"{len(sources_failed)} of {len(src_paths)} sources failed"
        if sources_failed else None
    )
    agent_journal.append_entry(
        profile, tool="merge_folders",
        args={**args, "summary": {"n_merged": len(sources_merged),
                                  "n_failed": len(sources_failed)}},
        result=result, batch_id=bid, backup_dir=backup_dir, error=error_msg,
    )
    return {
        "batch_id": bid,
        "backup_dir": backup_dir,
        "tool": "merge_folders",
        "result_data": result_summary,
    }
