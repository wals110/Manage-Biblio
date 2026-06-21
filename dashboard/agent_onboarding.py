"""Wrapper dashboard de l'agent Onboarding : scan synchrone + analyse/proposition
en thread daemon + polling status.json. Calqué sur dashboard/agent_refonte.py.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dashboard import data

# Segment de chemin sûr : alphanumérique en tête, puis alnum/_/- (pas de '/',
# pas de '..'). Les noms de profil et run_id viennent de l'HTTP (non fiables) et
# sont interpolés dans des chemins disque — on bloque toute traversée.
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _is_safe_segment(value: str) -> bool:
    return bool(_SAFE_SEGMENT.match(value or ""))


def _status_path(profile: str, run_id: str) -> Path:
    # Résolution PURE (aucun mkdir) : un read (get_status) ne doit pas créer de
    # dossier pour un run_id inconnu. La création est faite par _write_status.
    return (data.get_project_root() / "profiles" / profile / ".cache"
            / "onboarding" / run_id / "status.json")


def _write_status(profile: str, run_id: str, payload: dict[str, Any]) -> None:
    p = _status_path(profile, run_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def get_status(profile: str, run_id: str) -> dict[str, Any] | None:
    if not (_is_safe_segment(profile) and _is_safe_segment(run_id)):
        return None
    p = _status_path(profile, run_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _spawn(target: Callable, args: tuple, name: str) -> None:
    threading.Thread(target=target, args=args, daemon=True, name=name).start()


def scan(inbox_path: str) -> dict[str, Any]:
    """Scan & estimation (synchrone, pas de LLM)."""
    from agents.onboarding import scan as _scan
    r = _scan.scan_directory(inbox_path)
    r["estimate"] = _scan.estimate_cost(r["n_files"], cost_per_call=0.00034, n_pages=2)
    return r


def _run(profile: str, run_id: str) -> None:
    from agents.onboarding import proposition
    try:
        def on_progress(done: int, total: int) -> None:
            _write_status(profile, run_id, {
                "run_id": run_id, "profile": profile, "status": "running",
                "phase": "vision", "n_done": done, "n_total": total, "error": None})
        cov = proposition.build_proposal(profile, on_progress)
        _write_status(profile, run_id, {
            "run_id": run_id, "profile": profile, "status": "done",
            "completed_at": datetime.now(UTC).isoformat(),
            "coverage": cov["coverage"], "stats": cov["stats"],
            "by_destination": cov.get("by_destination", []),
            "warning": cov.get("warning"), "error": None})
    except Exception as exc:  # noqa: BLE001 — frontière de thread
        _write_status(profile, run_id, {
            "run_id": run_id, "profile": profile, "status": "error", "error": str(exc)})


def start_onboarding(profile_name: str, inbox_path: str) -> dict[str, Any]:
    """Crée le profil brouillon puis lance l'analyse/proposition en thread.

    Cycle de vie / reprise : si `build_proposal` échoue en cours de route, le
    profil brouillon reste sur disque (flag `onboarding_draft: true`) avec un
    état partiel et un statut `error`. Une nouvelle tentative bute alors sur le
    `FileExistsError` ci-dessous — l'utilisateur doit supprimer le profil
    partiel avant de relancer. Le garde `exists()` n'est pas mutex-protégé
    (TOCTOU sur deux starts concurrents) — acceptable pour un dashboard
    mono-utilisateur, cohérent avec agent_refonte.
    """
    from lib import profile as _profile
    if not _is_safe_segment(profile_name):
        raise ValueError(f"invalid profile name: {profile_name!r}")
    pdir = data.get_project_root() / "profiles" / profile_name
    if pdir.exists():
        raise FileExistsError(f"profile already exists: {profile_name}")
    _profile.create_draft_profile(profile_name, inbox_path)
    run_id = str(uuid.uuid4())
    _write_status(profile_name, run_id, {
        "run_id": run_id, "profile": profile_name, "status": "pending",
        "started_at": datetime.now(UTC).isoformat(), "error": None})
    _spawn(_run, (profile_name, run_id), f"onboarding-{run_id[:8]}")
    return {"run_id": run_id, "profile": profile_name, "status": "pending"}


def finalize(profile: str) -> dict[str, Any]:
    """Retire le flag onboarding_draft."""
    from lib import profile as _profile
    if not _is_safe_segment(profile):
        raise ValueError(f"invalid profile name: {profile!r}")
    _profile.set_onboarding_draft(profile, False)
    return {"ok": True, "profile": profile}
