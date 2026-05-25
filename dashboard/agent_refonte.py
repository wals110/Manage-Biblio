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
    """Lit le status d'un run + injecte le contenu pertinent si disponible.

    Pour les runs **Phase A** (diagnostic) `done` : injecte `report_md`.
    Pour les runs **Phase B** (proposition) `done` : injecte `rationale_md`
    (le markdown de `refonte-rationale.md`). Les artefacts plus volumineux
    (tree-diff, simulation CSV) sont exposés par des endpoints dédiés pour
    éviter de gonfler le payload de polling.

    Returns:
        Dict avec les clés de status.json + clés `report_md` ou `rationale_md`
        selon le contexte. None si le run est introuvable.
    """
    status = _read_status(profile, run_id)
    if status is None:
        return None
    if status.get("status") == "done":
        phase = status.get("phase", "A")
        if phase == "B":
            rationale_path = _proposal_rationale_path(profile, run_id)
            if rationale_path and rationale_path.exists():
                try:
                    status["rationale_md"] = rationale_path.read_text(encoding="utf-8")
                except OSError:
                    status["rationale_md"] = ""
        else:
            rpath = _report_path(profile, run_id)
            if rpath.exists():
                try:
                    status["report_md"] = rpath.read_text(encoding="utf-8")
                except OSError:
                    status["report_md"] = ""
    return status


def _proposal_rationale_path(profile: str, run_id: str) -> Path | None:
    """Chemin attendu du refonte-rationale.md pour un run Phase B."""
    p = _run_dir(profile, run_id) / "proposed" / "refonte-rationale.md"
    return p


def get_proposition_tree_diff(profile: str, run_id: str) -> dict[str, Any]:
    """Compare tree.yaml courant et tree-proposed.yaml du run B.

    Returns:
        {
            "added": list[str],     # dossiers présents en proposed mais pas en current
            "removed": list[str],   # absents en proposed mais en current
            "renamed": list[{old, new, rationale}],  # depuis changes.json
            "fused": list[{sources, target, rationale}],  # depuis changes.json
            "n_folders_before": int,
            "n_folders_after": int,
        }
    """
    import yaml as _yaml
    proposed_dir = _run_dir(profile, run_id) / "proposed"
    tree_proposed = proposed_dir / "tree-proposed.yaml"
    changes_json = proposed_dir / "changes.json"

    # Charge tree courant via la fonction taxonomy
    current_folders = set(tax._load_tree(profile))
    proposed_folders: set[str] = set()
    if tree_proposed.exists():
        try:
            raw = _yaml.safe_load(tree_proposed.read_text(encoding="utf-8")) or {}
            proposed_folders = set(raw.get("folders") or [])
        except _yaml.YAMLError:
            proposed_folders = set()

    renamed: list[dict[str, Any]] = []
    fused: list[dict[str, Any]] = []
    if changes_json.exists():
        try:
            ch = json.loads(changes_json.read_text(encoding="utf-8"))
            renamed = [
                {"old": r.get("old_path"), "new": r.get("new_path"),
                 "rationale": r.get("rationale", "")}
                for r in (ch.get("renamings") or [])
            ]
            fused = [
                {"sources": f.get("sources", []), "target": f.get("target"),
                 "rationale": f.get("rationale", "")}
                for f in (ch.get("fusions") or [])
            ]
        except (OSError, json.JSONDecodeError):
            pass

    # On retire des "added/removed" les paires couvertes par renames/fusions
    # (sinon on les double-compte côté UI).
    rename_old = {r["old"] for r in renamed if r.get("old")}
    rename_new = {r["new"] for r in renamed if r.get("new")}
    fusion_sources = {s for f in fused for s in (f.get("sources") or [])}
    fusion_targets = {f["target"] for f in fused if f.get("target")}

    added = sorted(
        f for f in (proposed_folders - current_folders)
        if f not in rename_new and f not in fusion_targets
    )
    removed = sorted(
        f for f in (current_folders - proposed_folders)
        if f not in rename_old and f not in fusion_sources
    )

    return {
        "added": added,
        "removed": removed,
        "renamed": renamed,
        "fused": fused,
        "n_folders_before": len(current_folders),
        "n_folders_after": len(proposed_folders),
    }


def get_proposition_simulation(
    profile: str,
    run_id: str,
    sample_limit: int = 50,
) -> dict[str, Any] | None:
    """Lit le simulation-summary.json + N lignes de la projection CSV.

    Args:
        profile: Nom du profil.
        run_id: UUID du run Phase B.
        sample_limit: Nombre max de lignes du CSV à inclure (par défaut 50).
                      Le CSV complet peut faire 18k+ lignes — gardé côté serveur.

    Returns:
        {
            "summary": dict (contenu de simulation-summary.json),
            "sample_moves": list[dict] (jusqu'à `sample_limit` lignes où changed=True),
            "n_moves_total": int,
        }
        None si la simulation n'a pas tourné (run Phase A par ex.).
    """
    import csv as _csv
    sim_dir = _run_dir(profile, run_id) / "simulation"
    summary_path = sim_dir / "simulation-summary.json"
    csv_path = sim_dir / "reclassify-projection.csv"
    if not summary_path.exists():
        return None
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    moves: list[dict[str, Any]] = []
    n_moves_total = 0
    if csv_path.exists():
        try:
            with csv_path.open(encoding="utf-8", newline="") as f:
                reader = _csv.DictReader(f)
                for row in reader:
                    if row.get("changed", "").lower() == "true":
                        n_moves_total += 1
                        if len(moves) < sample_limit:
                            moves.append(row)
        except (OSError, _csv.Error):
            pass
    return {
        "summary": summary,
        "sample_moves": moves,
        "n_moves_total": n_moves_total,
    }


def list_runs(profile: str, limit: int = 20) -> list[dict[str, Any]]:
    """Liste les runs récents pour un profil, triés par started_at desc.

    Effet de bord : avant de retourner, marque automatiquement les runs
    "zombies" comme `error` (cf. _reap_zombie_runs).
    """
    base = _runs_dir(profile)
    _reap_zombie_runs(base)
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


# Seuil au-delà duquel un run "running" est considéré orphelin (zombie).
# Tient compte d'un write_report qui peut durer jusqu'à ~3 min + marge.
_ZOMBIE_THRESHOLD_S = 5 * 60


def _reap_zombie_runs(runs_dir: Path) -> None:
    """Marque comme `error` les runs en status `running`/`pending` qui n'ont
    pas eu d'update depuis _ZOMBIE_THRESHOLD_S secondes.

    Use case : si le process dashboard est restart pendant qu'un thread
    daemon de run était actif, le thread meurt mais status.json reste à
    `running` pour toujours. Cette fonction, appelée à chaque list_runs
    (effet de bord), récupère ces zombies au lieu de les laisser pourrir.

    Heuristique simple : mtime du status.json — si pas d'update depuis 5min
    alors que c'est censé être en cours, c'est mort. Les runs très lents
    (write_report = 60-120s) sont à l'abri (notre stream() écrit après
    chaque node, donc le mtime se rafraîchit régulièrement).
    """
    now = datetime.now(UTC)
    for run_dir in runs_dir.iterdir():
        if not run_dir.is_dir():
            continue
        sp = run_dir / "status.json"
        if not sp.exists():
            continue
        try:
            data_ = json.loads(sp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data_.get("status") not in ("running", "pending"):
            continue
        # Calcul de l'âge depuis dernière update
        try:
            mtime = datetime.fromtimestamp(sp.stat().st_mtime, tz=UTC)
        except OSError:
            continue
        age_s = (now - mtime).total_seconds()
        if age_s < _ZOMBIE_THRESHOLD_S:
            continue
        # Zombie détecté → mark error
        data_.update({
            "status": "error",
            "completed_at": now.isoformat(),
            "error": (
                f"Run orphelin détecté : aucun progrès depuis {int(age_s)}s. "
                "Probablement tué par un redémarrage du dashboard. Relance "
                "le diagnostic."
            ),
        })
        try:
            sp.write_text(json.dumps(data_, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            continue


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
        # `stream(stream_mode="values")` yields the full state after each node
        # transition. On écrit le status.json après chaque update pour que le
        # polling UI voie progresser llm_calls + current_node en quasi-temps réel
        # (le rapport entier prend 60-90s, sinon "0/5" reste affiché tout du long).
        result: dict[str, Any] = {}
        for state_snapshot in graph.stream(
            {"profile": profile, "run_id": run_id},
            stream_mode="values",
        ):
            result = state_snapshot
            if isinstance(state_snapshot, dict):
                # Met à jour les champs visibles côté UI
                status_payload["llm_calls"] = state_snapshot.get("llm_calls", 0)
                # Heuristique pour `current_node` : on ne l'a pas directement
                # mais on peut déduire selon ce qu'il y a dans le state
                if state_snapshot.get("report"):
                    status_payload["current_node"] = "write_report"
                elif state_snapshot.get("llm_calls", 0) > 0:
                    status_payload["current_node"] = "explore"
                else:
                    status_payload["current_node"] = "init"
                _write_status(profile, run_id, status_payload)

        completed_at = datetime.now(UTC).isoformat()
        report = result.get("report") or ""
        if report:
            _report_path(profile, run_id).write_text(report, encoding="utf-8")

        status_payload.update({
            "status": result.get("status", "done"),
            "completed_at": completed_at,
            "llm_calls": result.get("llm_calls", 0),
            "error": result.get("error"),
            "current_node": None,
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


# ═══════════════════════════════════════════════════════════════════════════
#  Phase B — Proposition
# ═══════════════════════════════════════════════════════════════════════════
#
# Même pattern que Phase A : start_proposition() lance un thread daemon qui
# invoque build_proposition_graph().stream(...) et écrit status.json à chaque
# transition. Les artefacts produits (tree-proposed.yaml + theme_mapping-
# proposed.yaml + refonte-rationale.md) sont écrits par le tool propose_changes
# dans `.cache/refonte/<run_id>/proposed/`.


def start_proposition(
    profile: str,
    diagnostic_run_id: str,
    max_llm_calls: int = 8,
) -> dict[str, Any]:
    """Démarre un run de Phase B basé sur un diagnostic Phase A existant.

    Args:
        profile: Nom du profil.
        diagnostic_run_id: UUID d'un run Phase A `done` à utiliser comme contexte.
        max_llm_calls: Budget LLM (default 8 — la proposition est plus complexe).

    Returns:
        {"run_id": str, "status": "pending", "profile": str, "phase": "B",
         "diagnostic_run_id": str}

    Raises:
        ValueError: profile vide ou diagnostic_run_id vide.
        FileNotFoundError: profile inconnu, ou diagnostic absent / pas en `done`.
    """
    if not profile:
        raise ValueError("profile is required")
    if not diagnostic_run_id:
        raise ValueError("diagnostic_run_id is required")
    profile_path = data.get_project_root() / "profiles" / profile
    if not profile_path.is_dir():
        raise FileNotFoundError(f"profile not found: {profile}")
    # Vérifie que le diagnostic existe et est en `done`
    diag_status = _read_status(profile, diagnostic_run_id)
    if not diag_status:
        raise FileNotFoundError(
            f"diagnostic run not found: {diagnostic_run_id} (profile={profile})"
        )
    if diag_status.get("status") != "done":
        raise ValueError(
            f"diagnostic run {diagnostic_run_id} is in status "
            f"'{diag_status.get('status')}' (must be 'done' to base Phase B on it)"
        )

    run_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    initial = {
        "run_id": run_id,
        "profile": profile,
        "phase": "B",
        "diagnostic_run_id": diagnostic_run_id,
        "status": "pending",
        "started_at": now,
        "completed_at": None,
        "llm_calls": 0,
        "max_llm_calls": max_llm_calls,
        "error": None,
    }
    _write_status(profile, run_id, initial)

    thread = threading.Thread(
        target=_run_proposition,
        args=(profile, run_id, diagnostic_run_id, max_llm_calls),
        daemon=True,
        name=f"refonte-B-{run_id[:8]}",
    )
    thread.start()

    return {
        "run_id": run_id,
        "status": "pending",
        "profile": profile,
        "phase": "B",
        "diagnostic_run_id": diagnostic_run_id,
    }


def _run_proposition(
    profile: str,
    run_id: str,
    diagnostic_run_id: str,
    max_llm_calls: int,
) -> None:
    """Lance le graphe Phase B dans le thread. Mute toutes erreurs vers status.json."""
    try:
        tax.reset_cache(profile)
    except Exception:  # pragma: no cover — defensive
        pass

    started_at = datetime.now(UTC).isoformat()
    status_payload = _read_status(profile, run_id) or {}
    status_payload.update({"status": "running", "started_at": started_at})
    _write_status(profile, run_id, status_payload)

    # Import différé (langchain-openai chargé seulement à l'invocation)
    from agents.refonte.proposition import build_proposition_graph

    try:
        graph = build_proposition_graph(
            profile=profile,
            run_id=run_id,
            max_llm_calls=max_llm_calls,
        )
        result: dict[str, Any] = {}
        for state_snapshot in graph.stream(
            {
                "profile": profile,
                "run_id": run_id,
                "diagnostic_run_id": diagnostic_run_id,
            },
            stream_mode="values",
        ):
            result = state_snapshot
            if isinstance(state_snapshot, dict):
                status_payload["llm_calls"] = state_snapshot.get("llm_calls", 0)
                if state_snapshot.get("proposal_dir"):
                    status_payload["current_node"] = "finalize"
                elif state_snapshot.get("llm_calls", 0) > 0:
                    status_payload["current_node"] = "analyze"
                else:
                    status_payload["current_node"] = "init"
                _write_status(profile, run_id, status_payload)

        completed_at = datetime.now(UTC).isoformat()
        status_payload.update({
            "status": result.get("status", "done"),
            "completed_at": completed_at,
            "llm_calls": result.get("llm_calls", 0),
            "error": result.get("error"),
            "current_node": None,
            "proposal_dir": result.get("proposal_dir"),
            "proposal_summary": result.get("proposal_summary"),
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
