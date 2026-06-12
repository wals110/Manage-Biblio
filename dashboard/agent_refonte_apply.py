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
from collections import Counter
from pathlib import Path
from typing import Any

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
