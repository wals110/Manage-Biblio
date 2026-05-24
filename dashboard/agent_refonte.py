"""Backend du dashboard pour l'agent Refonte — Phase A.

Wrappe l'invocation du graphe `agents.refonte.build_diagnostic_graph` dans
un thread pour ne pas bloquer le serveur, persiste l'état d'avancement
dans `profile/<name>/.cache/refonte/<run_id>/`, et expose des helpers
synchrones pour les endpoints FastAPI.

État persisté par run (un sous-dossier par UUID4) :
  - status.json : {status, started_at, completed_at, error, llm_calls, profile, max_llm_calls}
  - report.md   : rapport markdown final (uniquement si status=="done")
"""

from __future__ import annotations

import json
import threading
import traceback
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dashboard import data
from dashboard import taxonomy as tax


def _runs_dir(profile: str) -> Path:
    """Dossier racine des runs de refonte pour un profil."""
    base = data.get_project_root() / "profiles" / profile / ".cache" / "refonte"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _run_dir(profile: str, run_id: str) -> Path:
    d = _runs_dir(profile) / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _status_path(profile: str, run_id: str) -> Path:
    return _run_dir(profile, run_id) / "status.json"


def _report_path(profile: str, run_id: str) -> Path:
    return _run_dir(profile, run_id) / "report.md"


def _write_status(profile: str, run_id: str, payload: dict[str, Any]) -> None:
    path = _status_path(profile, run_id)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_status(profile: str, run_id: str) -> dict[str, Any] | None:
    path = _status_path(profile, run_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ─── API publique (appelée par dashboard/app.py) ────────────────────────


def start_diagnostic(profile: str, max_llm_calls: int = 5) -> dict[str, Any]:
    """Démarre un diagnostic en arrière-plan et retourne le run_id.

    Valide les inputs en synchrone (profile existe + taxonomy.yaml présent),
    puis lance le graphe dans un thread daemon. Le caller reçoit
    immédiatement un run_id à poller via `get_status`.

    Args:
        profile: Nom du profil (doit exister sous profiles/).
        max_llm_calls: Budget d'appels LLM pour la boucle ReAct (default 5).

    Returns:
        {"run_id": str, "status": "pending", "profile": str}

    Raises:
        ValueError: si profile vide.
        FileNotFoundError: si profile inexistant.
    """
    if not profile:
        raise ValueError("profile is required")
    profile_path = data.get_project_root() / "profiles" / profile
    if not profile_path.is_dir():
        raise FileNotFoundError(f"profile not found: {profile}")

    run_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    initial = {
        "run_id": run_id,
        "profile": profile,
        "status": "pending",
        "started_at": now,
        "completed_at": None,
        "llm_calls": 0,
        "max_llm_calls": max_llm_calls,
        "error": None,
    }
    _write_status(profile, run_id, initial)

    thread = threading.Thread(
        target=_run_diagnostic,
        args=(profile, run_id, max_llm_calls),
        daemon=True,
        name=f"refonte-{run_id[:8]}",
    )
    thread.start()

    return {"run_id": run_id, "status": "pending", "profile": profile}


def get_status(profile: str, run_id: str) -> dict[str, Any] | None:
    """Lit le status d'un run + injecte le contenu du report si disponible.

    Returns:
        Dict avec les clés de status.json + clé `report_md` si status=="done".
        None si le run est introuvable.
    """
    status = _read_status(profile, run_id)
    if status is None:
        return None
    if status.get("status") == "done":
        rpath = _report_path(profile, run_id)
        if rpath.exists():
            try:
                status["report_md"] = rpath.read_text(encoding="utf-8")
            except OSError:
                status["report_md"] = ""
    return status


def list_runs(profile: str, limit: int = 20) -> list[dict[str, Any]]:
    """Liste les runs récents pour un profil, triés par started_at desc."""
    base = _runs_dir(profile)
    runs: list[dict[str, Any]] = []
    for run_dir in base.iterdir():
        if not run_dir.is_dir():
            continue
        sp = run_dir / "status.json"
        if not sp.exists():
            continue
        try:
            data_ = json.loads(sp.read_text(encoding="utf-8"))
            runs.append(data_)
        except (OSError, json.JSONDecodeError):
            continue
    runs.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    return runs[:limit]


# ─── Exécution thread ───────────────────────────────────────────────────


def _run_diagnostic(profile: str, run_id: str, max_llm_calls: int) -> None:
    """Lance le graphe dans le thread. Mute toutes les erreurs vers status.json."""
    # Reset cache snapshot — sinon les outils risquent de voir un snapshot stale
    # si le profil a été modifié entre 2 runs.
    try:
        tax.reset_cache(profile)
    except Exception:  # pragma: no cover — defensive
        pass

    started_at = datetime.now(UTC).isoformat()
    status_payload = _read_status(profile, run_id) or {}
    status_payload.update({"status": "running", "started_at": started_at})
    _write_status(profile, run_id, status_payload)

    # Import différé : on évite de payer le coût (langchain-openai) au boot du dashboard
    from agents.refonte import build_diagnostic_graph

    try:
        graph = build_diagnostic_graph(max_llm_calls=max_llm_calls)
        result = graph.invoke({"profile": profile, "run_id": run_id})

        completed_at = datetime.now(UTC).isoformat()
        report = result.get("report") or ""
        if report:
            _report_path(profile, run_id).write_text(report, encoding="utf-8")

        status_payload.update({
            "status": result.get("status", "done"),
            "completed_at": completed_at,
            "llm_calls": result.get("llm_calls", 0),
            "error": result.get("error"),
        })
        _write_status(profile, run_id, status_payload)
    except Exception as exc:
        status_payload.update({
            "status": "error",
            "completed_at": datetime.now(UTC).isoformat(),
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        })
        _write_status(profile, run_id, status_payload)
