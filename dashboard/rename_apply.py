"""Apply global — renommer toute la bibliothèque selon l'audit rename.

Symétrique de `dashboard/reclassify_apply.py` (classification) mais pour les
NOMS de fichiers (le basename), pas les dossiers :

  preview  → fige la liste des renommages à faire (placeholder + divergents,
             hors « casse seule » et hors « marqués OK ») dans projection.csv
  execute  → rejoue le CSV figé via `rename.commit_rename_bulk` (UN batch
             journalisé, collisions/manquants skippés + rapportés)
  undo     → `rename.undo_batch_for_profile` (annule tout le batch)

État global unique par profil dans `.cache/rename/apply/`. Périmètre =
placeholder + divergent (les diffs de casse seule `minor_case` et les `ok`
sont volontairement exclus).
"""

from __future__ import annotations

import csv
import json
import threading
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dashboard import data
from dashboard import rename as rename_mod

# Périmètre du rename global : vrais renommages, sans la casse seule.
_SCOPE = ("placeholder", "divergent")

_STATE_DEFAULTS: dict[str, Any] = {
    "executed": False,
    "executed_at": None,
    "batch_id": None,
    "n_planned": 0,
    "n_renamed": 0,
    "n_errors": 0,
    "rolled_back": False,
}


class RenameApplyError(Exception):
    """Erreur métier portant un status HTTP."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _profile_dir(profile: str) -> Path:
    return data.get_project_root() / "profiles" / profile


def _apply_dir(profile: str) -> Path:
    return _profile_dir(profile) / ".cache" / "rename" / "apply"


def _projection_path(profile: str) -> Path:
    return _apply_dir(profile) / "projection.csv"


def read_state(profile: str) -> dict[str, Any]:
    path = _apply_dir(profile) / "state.json"
    state = dict(_STATE_DEFAULTS)
    if path.exists():
        try:
            state.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    return state


def write_state(profile: str, updates: dict[str, Any]) -> dict[str, Any]:
    state = read_state(profile)
    state.update(updates)
    apply_dir = _apply_dir(profile)
    apply_dir.mkdir(parents=True, exist_ok=True)
    (apply_dir / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


def _write_progress(profile: str, payload: dict[str, Any]) -> None:
    apply_dir = _apply_dir(profile)
    apply_dir.mkdir(parents=True, exist_ok=True)
    (apply_dir / "status.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _read_progress(profile: str) -> dict[str, Any] | None:
    path = _apply_dir(profile) / "status.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def get_status(profile: str) -> dict[str, Any]:
    return {"state": read_state(profile), "progress": _read_progress(profile)}


def _spawn(target: Callable, args: tuple, name: str) -> None:
    threading.Thread(target=target, args=args, daemon=True, name=name).start()


def build_preview(profile: str) -> dict[str, Any]:
    """Audit complet → fige la liste des renommages (périmètre _SCOPE) en CSV."""
    audit = rename_mod.rename_audit(profile, force_reload=True)
    rows = [{
        "rel_path": c["rel_path"],
        "old_name": c["current_name"],
        "new_name": c["suggested_name"],
        "category": c["category"],
    } for c in audit.get("candidates", []) if c.get("category") in _SCOPE]

    apply_dir = _apply_dir(profile)
    apply_dir.mkdir(parents=True, exist_ok=True)
    with _projection_path(profile).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["rel_path", "old_name", "new_name", "category"])
        writer.writeheader()
        writer.writerows(rows)
    write_state(profile, {"n_planned": len(rows)})

    by_cat = Counter(r["category"] for r in rows)
    return {
        "n_planned": len(rows),
        "n_placeholder": by_cat.get("placeholder", 0),
        "n_divergent": by_cat.get("divergent", 0),
        "sample": rows[:20],
        "state": read_state(profile),
    }


def _read_frozen(profile: str) -> list[dict]:
    path = _projection_path(profile)
    if not path.exists():
        raise RenameApplyError("aucune projection figée — lance d'abord le preview", 409)
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _run_execute(profile: str) -> dict[str, Any]:
    rows = _read_frozen(profile)
    items = [{"rel_path": r["rel_path"], "new_name": r["new_name"]} for r in rows]
    _write_progress(profile, {
        "op": "execute", "status": "running",
        "n_done": 0, "n_total": len(items), "n_failed": 0, "error": None})
    if not items:
        write_state(profile, {"executed": True, "executed_at": datetime.now(UTC).isoformat(),
                              "batch_id": None, "n_renamed": 0, "n_errors": 0,
                              "rolled_back": False})
        _write_progress(profile, {"op": "execute", "status": "done",
                        "n_done": 0, "n_total": 0, "n_failed": 0, "error": None, "errors": []})
        return {"n_renamed": 0, "n_errors": 0}
    result = rename_mod.commit_rename_bulk(profile, items)
    write_state(profile, {
        "executed": True,
        "executed_at": datetime.now(UTC).isoformat(),
        "batch_id": result["batch_id"],
        "n_renamed": result["n_renamed"],
        "n_errors": result["n_errors"],
        "rolled_back": False,
    })
    _write_progress(profile, {
        "op": "execute", "status": "done",
        "n_done": result["n_renamed"], "n_total": result["n_total"],
        "n_failed": result["n_errors"], "error": None,
        "errors": result.get("errors", [])[:50]})
    return result


def _execute_job(profile: str) -> None:
    try:
        _run_execute(profile)
    except Exception as exc:  # noqa: BLE001 — frontière de thread
        _write_progress(profile, {"op": "execute", "status": "error",
                        "n_done": 0, "n_total": 0, "n_failed": 0, "error": str(exc)})


def start_execute(profile: str) -> dict[str, Any]:
    progress = _read_progress(profile)
    if progress and progress.get("status") == "running":
        raise RenameApplyError("une opération est déjà en cours", 409)
    if not _projection_path(profile).exists():
        raise RenameApplyError("aucune projection figée — lance d'abord le preview", 409)
    _spawn(_execute_job, (profile,), f"rename-apply-{profile}")
    return {"ok": True, "profile": profile}


def _run_undo(profile: str) -> dict[str, Any]:
    state = read_state(profile)
    if not state.get("executed"):
        raise RenameApplyError("aucun renommage à annuler", 409)
    batch_id = state.get("batch_id")
    if not batch_id:
        raise RenameApplyError("aucun batch de renommage enregistré", 409)
    _write_progress(profile, {"op": "undo", "status": "running",
                    "n_done": 0, "n_total": state.get("n_renamed", 0),
                    "n_failed": 0, "error": None})
    result = rename_mod.undo_batch_for_profile(profile, batch_id)
    write_state(profile, {"executed": False, "rolled_back": True})
    n_undone = result.get("n_undone", 0)
    n_failed = result.get("n_errors", 0)
    _write_progress(profile, {"op": "undo", "status": "done",
                    "n_done": n_undone, "n_total": n_undone + n_failed,
                    "n_failed": n_failed, "error": None})
    return result


def _undo_job(profile: str) -> None:
    try:
        _run_undo(profile)
    except Exception as exc:  # noqa: BLE001 — frontière de thread
        _write_progress(profile, {"op": "undo", "status": "error",
                        "n_done": 0, "n_total": 0, "n_failed": 0, "error": str(exc)})


def start_undo(profile: str) -> dict[str, Any]:
    progress = _read_progress(profile)
    if progress and progress.get("status") == "running":
        raise RenameApplyError("une opération est déjà en cours", 409)
    if not read_state(profile).get("executed"):
        raise RenameApplyError("aucun renommage à annuler", 409)
    _spawn(_undo_job, (profile,), f"rename-undo-{profile}")
    return {"ok": True, "profile": profile}
