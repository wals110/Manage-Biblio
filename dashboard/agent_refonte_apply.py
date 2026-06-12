"""Apply/Execute d'une refonte Phase B — adoption config + déplacements.

Spec : docs/superpowers/specs/2026-06-12-refonte-apply-execute-design.md

Deux couches séquencées par run :
  ① adopt_structure  — promotion des YAML proposés vers la prod (sync, lock)
  ② start_execute    — déplacements physiques selon la projection figée
                       (thread daemon + polling apply/status.json)
Rollbacks : restore_config (snapshot agent_backup) / start_undo_moves
(journal lib/move_journal).
"""

from __future__ import annotations

import json
import shutil
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agents.refonte import agent_backup
from dashboard import data, taxonomy
from dashboard.refonte_results import select_move_rows

# ─── Erreur transport (status HTTP porté par l'exception) ────────────────


class ApplyError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ─── Chemins & état ──────────────────────────────────────────────────────

_STATE_DEFAULTS: dict[str, Any] = {
    "adopted": False,
    "adopted_at": None,
    "config_backup": None,
    "move_batch_id": None,
    "executed": False,
    "executed_at": None,
    "n_moved": 0,
    "n_failed": 0,
    "n_skipped": 0,
    "rolled_back_config": False,
    "rolled_back_moves": False,
}


def _profile_dir(profile: str) -> Path:
    return data.get_project_root() / "profiles" / profile


def _run_dir(profile: str, run_id: str) -> Path:
    return _profile_dir(profile) / ".cache" / "refonte" / run_id


def _apply_dir(profile: str, run_id: str) -> Path:
    return _run_dir(profile, run_id) / "apply"


def read_state(profile: str, run_id: str) -> dict[str, Any]:
    """state.json fusionné avec les défauts (fichier absent = état vierge)."""
    path = _apply_dir(profile, run_id) / "state.json"
    state = dict(_STATE_DEFAULTS)
    if path.exists():
        try:
            state.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    return state


def write_state(profile: str, run_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    """Merge ``updates`` dans state.json et retourne l'état complet."""
    state = read_state(profile, run_id)
    state.update(updates)
    apply_dir = _apply_dir(profile, run_id)
    apply_dir.mkdir(parents=True, exist_ok=True)
    (apply_dir / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


def _write_progress(profile: str, run_id: str, payload: dict[str, Any]) -> None:
    apply_dir = _apply_dir(profile, run_id)
    apply_dir.mkdir(parents=True, exist_ok=True)
    (apply_dir / "status.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _read_progress(profile: str, run_id: str) -> dict[str, Any] | None:
    path = _apply_dir(profile, run_id) / "status.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def get_apply_status(profile: str, run_id: str) -> dict[str, Any]:
    """state.json + status.json fusionnés (payload de polling)."""
    if not _run_dir(profile, run_id).is_dir():
        raise ApplyError(f"run not found: {run_id}", 404)
    return {
        "run_id": run_id,
        "state": read_state(profile, run_id),
        "progress": _read_progress(profile, run_id),
    }


# ─── Gating ──────────────────────────────────────────────────────────────


def _assert_run_phase_b_done(profile: str, run_id: str) -> None:
    run_dir = _run_dir(profile, run_id)
    if not run_dir.is_dir():
        raise ApplyError(f"run not found: {run_id}", 404)
    status_path = run_dir / "status.json"
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ApplyError(f"run status unreadable: {run_id}", 500) from exc
    if status.get("phase") != "B" or status.get("status") != "done":
        raise ApplyError(
            "le run doit être une proposition Phase B terminée", 409)


def _assert_no_op_in_progress(profile: str, run_id: str) -> None:
    progress = _read_progress(profile, run_id)
    if progress and progress.get("status") == "running":
        raise ApplyError("une opération est déjà en cours sur ce run", 409)


def _find_other_adopted(profile: str, run_id: str) -> str | None:
    """run_id d'un AUTRE run adopté non restauré, ou None."""
    runs_root = _profile_dir(profile) / ".cache" / "refonte"
    if not runs_root.is_dir():
        return None
    for entry in runs_root.iterdir():
        if not entry.is_dir() or entry.name == run_id:
            continue
        state_path = entry / "apply" / "state.json"
        if not state_path.exists():
            continue
        try:
            st = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if st.get("adopted"):
            return entry.name
    return None


def _target_path(profile: str) -> Path:
    target = taxonomy._profile_target_path(profile)
    if target is None or not target.exists():
        raise ApplyError(
            "target du profil introuvable (SSD non monté ?)", 500)
    return target


def _load_changes(profile: str, run_id: str) -> dict[str, Any]:
    path = _run_dir(profile, run_id) / "proposed" / "changes.json"
    if not path.exists():
        raise ApplyError("changes.json manquant pour ce run", 500)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ApplyError("changes.json illisible", 500) from exc


# ─── Preview ─────────────────────────────────────────────────────────────


def build_preview(profile: str, run_id: str) -> dict[str, Any]:
    """Compteurs pour les modals de confirmation (lecture seule)."""
    if not _run_dir(profile, run_id).is_dir():
        raise ApplyError(f"run not found: {run_id}", 404)
    changes = _load_changes(profile, run_id)
    selection = select_move_rows(_run_dir(profile, run_id))
    dest_counts = Counter(m["proposed_folder"] for m in selection["moves"])
    return {
        "run_id": run_id,
        "state": read_state(profile, run_id),
        "n_moves": selection["n_moves"],
        "n_doubt_excluded": selection["n_doubt_excluded"],
        "n_stable": selection["n_stable"],
        "top_destinations": [
            {"folder": folder, "n": n}
            for folder, n in dest_counts.most_common(10)
        ],
        "n_creations": len(changes.get("creations") or []),
        "n_renames": len(changes.get("renamings") or []),
        "n_fusions": len(changes.get("fusions") or []),
        "n_deletions": len(changes.get("deletions") or []),
        "n_mappings_added": len(changes.get("mappings_added") or []),
    }


# ─── Couche A — adoption de la structure ─────────────────────────────────

# (source dans proposed/, destination dans le profil)
_PROMOTED_FILES = (
    ("tree-proposed.yaml", "tree.yaml"),
    ("theme_mapping-proposed.yaml", "theme_mapping.yaml"),
    ("categories-proposed.yaml", "categories.yaml"),  # optionnel
)
_REQUIRED_PROPOSED = ("tree-proposed.yaml", "theme_mapping-proposed.yaml")


def adopt_structure(profile: str, run_id: str) -> dict[str, Any]:
    """Couche A : snapshot config → promotion des YAML proposés →
    mkdir des créations → reset_cache. Synchrone, sous lock."""
    with taxonomy._locks[profile]:
        _assert_run_phase_b_done(profile, run_id)
        try:
            taxonomy._check_lock_free(profile)
        except taxonomy.TaxonomyError as exc:
            raise ApplyError(str(exc), 423) from exc
        state = read_state(profile, run_id)
        if state["adopted"]:
            raise ApplyError("structure déjà adoptée pour ce run", 409)
        other = _find_other_adopted(profile, run_id)
        if other:
            raise ApplyError(
                f"un autre run est déjà adopté ({other}) — restaure sa "
                "config d'abord", 409)
        target = _target_path(profile)
        proposed_dir = _run_dir(profile, run_id) / "proposed"
        for fname in _REQUIRED_PROPOSED:
            if not (proposed_dir / fname).exists():
                raise ApplyError(f"artefact manquant : proposed/{fname}", 500)
        changes = _load_changes(profile, run_id)

        # 1. Snapshot (tree + mapping + categories, rotation 50)
        backup_name = agent_backup.create_backup(
            profile, batch_id=f"apply-{run_id}")

        # 2. Promotion des YAML proposés
        promoted: list[str] = []
        for src_name, dst_name in _PROMOTED_FILES:
            src = proposed_dir / src_name
            if not src.exists():
                continue  # categories-proposed.yaml est optionnel
            shutil.copy2(src, _profile_dir(profile) / dst_name)
            promoted.append(dst_name)

        # 3. Création physique des nouveaux dossiers
        created: list[str] = []
        for creation in changes.get("creations") or []:
            rel = (creation.get("path") or "").strip("/")
            if not rel:
                continue
            (target / rel).mkdir(parents=True, exist_ok=True)
            created.append(rel)

        # 4. État + caches
        taxonomy.reset_cache(profile)
        write_state(profile, run_id, {
            "adopted": True,
            "adopted_at": datetime.now(UTC).isoformat(),
            "config_backup": backup_name,
            "rolled_back_config": False,
        })
        return {
            "ok": True,
            "backup": backup_name,
            "promoted": promoted,
            "created_dirs": created,
        }


def restore_config(profile: str, run_id: str) -> dict[str, Any]:
    """Rollback A : restore du snapshot + suppression des dossiers créés
    SEULEMENT s'ils sont vides (jamais de suppression de contenu)."""
    with taxonomy._locks[profile]:
        state = read_state(profile, run_id)
        if not state["adopted"]:
            raise ApplyError("structure non adoptée pour ce run", 409)
        if state["executed"] and not state["rolled_back_moves"]:
            raise ApplyError(
                "déplacements exécutés — annule-les d'abord "
                "(undo-moves) avant de restaurer la config", 409)
        try:
            taxonomy._check_lock_free(profile)
        except taxonomy.TaxonomyError as exc:
            raise ApplyError(str(exc), 423) from exc
        backup_name = state.get("config_backup")
        if not backup_name:
            raise ApplyError("aucun backup enregistré pour ce run", 500)
        try:
            restored = agent_backup.restore_backup(profile, backup_name)
        except agent_backup.BackupError as exc:
            raise ApplyError(str(exc), 500) from exc

        # Dossiers créés à l'adoption : rmdir si vides (enfants d'abord)
        target = _target_path(profile)
        changes = _load_changes(profile, run_id)
        creation_paths = sorted(
            ((c.get("path") or "").strip("/")
             for c in changes.get("creations") or []),
            key=lambda p: p.count("/"), reverse=True)
        removed: list[str] = []
        kept: list[str] = []
        for rel in creation_paths:
            if not rel:
                continue
            d = target / rel
            if not d.is_dir():
                continue
            if any(d.iterdir()):
                kept.append(rel)
            else:
                d.rmdir()
                removed.append(rel)

        taxonomy.reset_cache(profile)
        write_state(profile, run_id, {
            "adopted": False,
            "rolled_back_config": True,
        })
        return {
            "ok": True,
            "restored": restored.get("restored", []),
            "removed_dirs": removed,
            "kept_nonempty": kept,
        }
