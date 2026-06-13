"""Apply global — synchroniser la bibliothèque avec la config live.

Spec : docs/superpowers/specs/2026-06-13-reclassify-apply-global-design.md

Couche B uniquement (pas d'adoption — la config est déjà live) :
  preview  → fige la projection (build_reclassify_projection) en CSV
  execute  → rejoue le CSV figé via le moteur partagé apply_engine
  undo     → move_journal.undo_batch
État global unique par profil (pas de run_id).
"""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dashboard import apply_engine, data, taxonomy
from dashboard.apply_engine import ApplyError  # ré-export
from lib import move_journal

_STATE_DEFAULTS: dict[str, Any] = {
    "executed": False,
    "executed_at": None,
    "move_batch_id": None,
    "include_keyword": False,
    "n_moved": 0,
    "n_failed": 0,
    "n_skipped": 0,
    "rolled_back_moves": False,
    "last_applied": None,
}

_CONFIG_FILES = ("theme_mapping.yaml", "categories.yaml", "tree.yaml")


def _profile_dir(profile: str) -> Path:
    return data.get_project_root() / "profiles" / profile


def _apply_dir(profile: str) -> Path:
    return _profile_dir(profile) / ".cache" / "reclassify" / "apply"


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


def _config_hash(profile: str) -> str:
    h = hashlib.sha256()
    for fname in _CONFIG_FILES:
        p = _profile_dir(profile) / fname
        h.update(p.read_bytes() if p.exists() else b"")
        h.update(b"\0")
    canon = _profile_dir(profile) / ".cache" / "theme-canon.json"
    h.update(canon.read_bytes() if canon.exists() else b"")
    return h.hexdigest()


def is_pending(profile: str) -> bool:
    """True si la CONFIG a changé depuis le dernier apply (instantané).

    Couvre theme_mapping/categories/tree/theme-canon uniquement — PAS
    vision_cache ni profile.yaml (model/pages). C'est un signal « as-tu
    édité ta config », pas « un reclassify déplacerait-il quelque chose »
    (ce compteur coûteux reste calculé à la demande dans le preview).
    """
    last = read_state(profile).get("last_applied") or {}
    return last.get("config_hash") != _config_hash(profile)


def build_preview(profile: str, include_keyword: bool) -> dict[str, Any]:
    """Calcule la projection live, l'écrit figée en CSV, retourne les
    compteurs pour la modale."""
    try:
        taxonomy._check_lock_free(profile)
    except taxonomy.TaxonomyError as exc:
        raise ApplyError(str(exc), 423) from exc
    apply_engine.target_path(profile)
    moves = taxonomy.build_reclassify_projection(profile, include_keyword)
    apply_dir = _apply_dir(profile)
    apply_dir.mkdir(parents=True, exist_ok=True)
    with _projection_path(profile).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "rel_path", "current_folder", "proposed_folder", "source",
            "top_theme", "confidence", "signal"])
        writer.writeheader()
        writer.writerows(moves)
    write_state(profile, {"include_keyword": include_keyword})
    n_p1 = sum(1 for m in moves if m["signal"] == "p1")
    n_p2 = sum(1 for m in moves if m["signal"] == "p2")
    from collections import Counter
    dest_counts = Counter(m["proposed_folder"] for m in moves)
    return {
        "n_moves": len(moves), "n_p1": n_p1, "n_p2": n_p2,
        "include_keyword": include_keyword,
        "top_destinations": [{"folder": d, "n": n}
                             for d, n in dest_counts.most_common(10)],
        "state": read_state(profile),
        "pending": is_pending(profile),
    }


def _read_frozen_moves(profile: str) -> list[dict]:
    path = _projection_path(profile)
    if not path.exists():
        raise ApplyError("aucune projection figée — lance d'abord le preview", 409)
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _run_moves_global(profile: str) -> dict[str, Any]:
    moves = _read_frozen_moves(profile)
    target = apply_engine.target_path(profile)
    result = apply_engine.execute_move_batch(
        target, moves, _profile_dir(profile),
        on_progress=lambda p: _write_progress(profile, {**p, "op": "execute"}))
    state_update = {
        "executed": True,
        "executed_at": datetime.now(UTC).isoformat(),
        "move_batch_id": result["batch_id"],
        "n_moved": result["n_moved"],
        "n_failed": result["n_failed"],
        "n_skipped": result["n_skipped"],
        "rolled_back_moves": False,
    }
    # last_applied (→ pending=false) seulement si AUCUN échec I/O réel :
    # un échec garde le badge « config modifiée » pour signaler le travail
    # restant. Les skips stale/collision sont tolérés.
    if result["n_failed"] == 0:
        state_update["last_applied"] = {
            "ts": datetime.now(UTC).isoformat(),
            "config_hash": _config_hash(profile)}
    write_state(profile, state_update)
    _write_progress(profile, {
        "op": "execute", "status": "done",
        "n_done": result["n_total"], "n_total": result["n_total"],
        "n_failed": result["n_failed"], "n_skipped": result["n_skipped"],
        "error": None, "report": result["report"]})
    return result


def _execute_job(profile: str) -> None:
    try:
        _run_moves_global(profile)
    except Exception as exc:  # noqa: BLE001
        _write_progress(profile, {"op": "execute", "status": "error",
                        "n_done": 0, "n_total": 0, "n_failed": 0,
                        "n_skipped": 0, "error": str(exc)})
    finally:
        taxonomy._lock_file(profile).unlink(missing_ok=True)
        taxonomy.reset_cache(profile)


def start_execute(profile: str) -> dict[str, Any]:
    with taxonomy._locks[profile]:
        progress = _read_progress(profile)
        if progress and progress.get("status") == "running":
            raise ApplyError("une opération est déjà en cours", 409)
        if not _projection_path(profile).exists():
            raise ApplyError("aucune projection figée — lance d'abord le preview", 409)
        try:
            taxonomy._check_lock_free(profile)
        except taxonomy.TaxonomyError as exc:
            raise ApplyError(str(exc), 423) from exc
        apply_engine.target_path(profile)
        lock = taxonomy._lock_file(profile)
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("reclassify-apply\n", encoding="utf-8")
        apply_engine.spawn(_execute_job, (profile,), f"reclassify-apply-{profile}")
        return {"ok": True, "profile": profile}


def _run_undo_global(profile: str) -> dict[str, Any]:
    state = read_state(profile)
    if not state["executed"]:
        raise ApplyError("aucun déplacement à annuler", 409)
    batch_id = state.get("move_batch_id")
    if not batch_id:
        raise ApplyError("aucun batch de moves enregistré", 409)
    _write_progress(profile, {"op": "undo", "status": "running",
                    "n_done": 0, "n_total": state.get("n_moved", 0),
                    "n_failed": 0, "n_skipped": 0, "error": None})
    result = move_journal.undo_batch(_profile_dir(profile), batch_id)
    write_state(profile, {"executed": False, "rolled_back_moves": True})
    _write_progress(profile, {"op": "undo", "status": "done",
                    "n_done": result["n_undone"],
                    "n_total": result["n_undone"] + result["n_failed"],
                    "n_failed": result["n_failed"], "n_skipped": 0, "error": None})
    return result


def _undo_job(profile: str) -> None:
    try:
        _run_undo_global(profile)
    except Exception as exc:  # noqa: BLE001
        _write_progress(profile, {"op": "undo", "status": "error",
                        "n_done": 0, "n_total": 0, "n_failed": 0,
                        "n_skipped": 0, "error": str(exc)})
    finally:
        taxonomy._lock_file(profile).unlink(missing_ok=True)
        taxonomy.reset_cache(profile)


def start_undo(profile: str) -> dict[str, Any]:
    with taxonomy._locks[profile]:
        progress = _read_progress(profile)
        if progress and progress.get("status") == "running":
            raise ApplyError("une opération est déjà en cours", 409)
        if not read_state(profile)["executed"]:
            raise ApplyError("aucun déplacement à annuler", 409)
        try:
            taxonomy._check_lock_free(profile)
        except taxonomy.TaxonomyError as exc:
            raise ApplyError(str(exc), 423) from exc
        lock = taxonomy._lock_file(profile)
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("reclassify-apply-undo\n", encoding="utf-8")
        apply_engine.spawn(_undo_job, (profile,), f"reclassify-undo-{profile}")
        return {"ok": True, "profile": profile}
