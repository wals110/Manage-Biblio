"""Backend dashboard pour le chantier Dédupli des thèmes (Phase 5 UI).

Wrappe l'invocation de `lib.theme_canon.build_canon_table` dans un thread,
persiste l'avancement dans `profiles/<p>/.cache/dedupli/status.json`, et
expose des helpers synchrones pour les endpoints FastAPI :
  - start_dedupli(profile) — lance un build async
  - get_status(profile)    — lit l'avancement
  - list_clusters(profile) — liste les clusters issus de theme-canon.json
  - update_cluster(profile, payload) — édition manuelle (members/splits/canonical)

État persisté dans `dedupli/status.json` :
  {
    "status": "pending|running|done|error",
    "phase": "extracting|clustering|judging|done",
    "progress": {"done": N, "total": M},
    "started_at": ..., "completed_at": ..., "error": ...
  }
"""

from __future__ import annotations

import json
import threading
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dashboard import data

# Seuil au-delà duquel un run "running" est considéré orphelin (zombie).
# Avec parallélisation 8 workers, on_progress est appelé toutes les 1-2s
# en moyenne — 2 min sans update est très conservateur (≥ 60 updates ratés).
_ZOMBIE_THRESHOLD_S = 2 * 60


def _dedupli_dir(profile: str) -> Path:
    base = data.get_project_root() / "profiles" / profile / ".cache" / "dedupli"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _status_path(profile: str) -> Path:
    return _dedupli_dir(profile) / "status.json"


def _canon_path(profile: str) -> Path:
    return data.get_project_root() / "profiles" / profile / ".cache" / "theme-canon.json"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _read_status(profile: str) -> dict[str, Any] | None:
    path = _status_path(profile)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_status(profile: str, payload: dict[str, Any]) -> None:
    path = _status_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _reap_zombie(profile: str) -> None:
    """Marque comme `error` un run zombie (running sans update >threshold).

    Cas typique : dashboard redémarré pendant un run. Le thread daemon
    meurt mais status.json reste "running" éternellement.
    """
    status = _read_status(profile)
    if not status or status.get("status") not in ("running", "pending"):
        return
    path = _status_path(profile)
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return
    age = (datetime.now(UTC) - mtime).total_seconds()
    if age < _ZOMBIE_THRESHOLD_S:
        return
    status.update({
        "status": "error",
        "completed_at": _now_iso(),
        "error": (
            f"Run orphelin : aucun progrès depuis {int(age)}s. "
            "Probablement tué par un redémarrage du dashboard. Relance "
            "la dédupli."
        ),
    })
    _write_status(profile, status)


def reset_running_at_boot() -> None:
    """Marque comme `error` tous les runs `running`/`pending` au démarrage
    du dashboard.

    Justification : tout thread daemon est mort par construction quand le
    process redémarre. Inutile d'attendre _ZOMBIE_THRESHOLD_S pour le
    constater — on connaît la vérité tout de suite. Appelé une fois au
    boot par dashboard.app.

    Parcours tous les profils sous profiles/<X>/.cache/dedupli/status.json
    et reset ceux qui sont en running/pending.
    """
    profiles_root = data.get_project_root() / "profiles"
    if not profiles_root.is_dir():
        return
    for profile_dir in profiles_root.iterdir():
        if not profile_dir.is_dir():
            continue
        status_path = profile_dir / ".cache" / "dedupli" / "status.json"
        if not status_path.exists():
            continue
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if status.get("status") not in ("running", "pending"):
            continue
        status.update({
            "status": "error",
            "completed_at": _now_iso(),
            "error": "Dashboard redémarré pendant le run (thread daemon perdu).",
        })
        try:
            _write_status(profile_dir.name, status)
        except OSError:
            pass


class _CancelledError(Exception):
    """Levée par le callback on_progress quand le user a demandé un cancel.

    Capturée par _run_dedupli pour faire transitionner le status vers
    "cancelled" proprement.
    """


def restore_initial_themes(profile: str) -> dict[str, Any]:
    """Supprime theme-canon.json pour revenir aux thèmes bruts du vision_cache.

    `theme-canon.json` est consommé en runtime par
    `dashboard.taxonomy._aggregate_themes_llm`. Sa suppression rétablit
    instantanément le comportement d'origine sans toucher au vision_cache
    (source de vérité immuable) ni au cache canonicalizer (peut être
    réutilisé pour un futur build).

    Refuse si un run dédupli est en cours pour éviter une race.

    Raises:
        ValueError: profile vide.
        RuntimeError: un run est en cours (running/pending).
    """
    if not profile:
        raise ValueError("profile is required")
    _reap_zombie(profile)
    status = _read_status(profile)
    if status and status.get("status") in ("running", "pending"):
        raise RuntimeError(
            "un run est en cours — annule-le d'abord avant de restaurer"
        )
    canon_path = _canon_path(profile)
    existed = canon_path.exists()
    if existed:
        canon_path.unlink()
    return {"profile": profile, "restored": existed}


def cancel_dedupli(profile: str) -> dict[str, Any]:
    """Demande l'annulation d'un run en cours.

    Pose `cancel_requested=True` dans status.json — le thread daemon lit
    ce flag dans son callback on_progress et raise _CancelledError au
    prochain check. Les appels LLM déjà en vol terminent (pas de kill
    brutal), mais aucun nouveau n'est lancé.

    Raises:
        ValueError: profile vide.
        FileNotFoundError: aucun run actuel ou status absent.
        RuntimeError: le run n'est pas en running/pending (déjà done/error/cancelled).
    """
    if not profile:
        raise ValueError("profile is required")
    status = _read_status(profile)
    if not status:
        raise FileNotFoundError(f"no dedupli status for profile {profile!r}")
    if status.get("status") not in ("running", "pending"):
        raise RuntimeError(
            f"cannot cancel: status is {status.get('status')!r}"
        )
    status["cancel_requested"] = True
    _write_status(profile, status)
    return {"profile": profile, "status": "cancelling"}


def get_status(profile: str) -> dict[str, Any]:
    """Lit le status courant (+ reap si nécessaire). Inclut un résumé du
    canon si présent (raw_count, canonical_count, built_at)."""
    _reap_zombie(profile)
    status = _read_status(profile) or {"status": "idle"}
    # Enrichit avec un résumé du canon si présent
    canon_path = _canon_path(profile)
    if canon_path.exists():
        try:
            canon = json.loads(canon_path.read_text(encoding="utf-8"))
            status["canon_summary"] = {
                "built_at": canon.get("built_at"),
                "raw_count": canon.get("raw_count", 0),
                "canonical_count": canon.get("canonical_count", 0),
                "threshold": canon.get("threshold"),
                "mode": canon.get("mode"),
                "n_clusters": len(canon.get("clusters", [])),
            }
        except (OSError, json.JSONDecodeError):
            pass
    return status


def list_clusters(profile: str, *, multi_only: bool = True) -> list[dict[str, Any]]:
    """Liste les clusters depuis theme-canon.json.

    Args:
        multi_only: Si True, ne renvoie que les clusters avec ≥ 2 raw_members
                    (les singletons sont sans intérêt pour la review).

    Returns:
        Liste de clusters triée par count_cumulative décroissant. Format :
            {canonical, members, splits, raw_members, count_cumulative}
    """
    path = _canon_path(profile)
    if not path.exists():
        return []
    try:
        canon = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    clusters = canon.get("clusters", [])
    if not isinstance(clusters, list):
        return []
    if multi_only:
        clusters = [c for c in clusters if len(c.get("raw_members", [])) >= 2]
    clusters.sort(key=lambda c: -int(c.get("count_cumulative") or 0))
    return clusters


def update_cluster(profile: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Édition manuelle d'un cluster — réécrit theme-canon.json en place.

    Le cluster est identifié par la liste `raw_members` (canonique pour la
    recherche). Le caller passe le nouveau canonical + nouveaux members +
    nouveaux splits. Le mapping est régénéré pour ce cluster.

    Args:
        profile: Profil cible.
        payload: {
            "raw_members": list[str],   # identifiant du cluster
            "canonical": str,
            "members": list[str],
            "splits": list[{"theme": str, "reason": str}]
        }

    Returns:
        Le cluster mis à jour.

    Raises:
        FileNotFoundError: theme-canon.json absent.
        ValueError: cluster introuvable ou payload invalide.
    """
    raw_members = payload.get("raw_members") or []
    if not raw_members:
        raise ValueError("raw_members is required (cluster identifier)")
    new_canonical = (payload.get("canonical") or "").strip()
    if not new_canonical:
        raise ValueError("canonical must be a non-empty string")
    new_members = list(payload.get("members") or [])
    new_splits = list(payload.get("splits") or [])

    canon_path = _canon_path(profile)
    if not canon_path.exists():
        raise FileNotFoundError(f"theme-canon.json not found for profile {profile!r}")
    canon = json.loads(canon_path.read_text(encoding="utf-8"))
    clusters = canon.get("clusters", [])

    # Trouve le cluster par signature raw_members (set, indépendant ordre)
    target_set = set(raw_members)
    idx = next(
        (i for i, c in enumerate(clusters)
         if set(c.get("raw_members", [])) == target_set),
        None,
    )
    if idx is None:
        raise ValueError(f"cluster with raw_members={raw_members!r} not found")

    # Validation : tous les members/splits doivent être dans raw_members
    raw_set = set(raw_members)
    member_set = set(new_members)
    split_set = {s.get("theme") for s in new_splits if isinstance(s, dict)}
    if not member_set.issubset(raw_set):
        raise ValueError(
            f"members not in raw_members: {member_set - raw_set!r}"
        )
    if not split_set.issubset(raw_set):
        raise ValueError(
            f"splits not in raw_members: {split_set - raw_set!r}"
        )

    # Met à jour le cluster
    updated = clusters[idx]
    updated["canonical"] = new_canonical
    updated["members"] = new_members
    updated["splits"] = new_splits

    # Régénère le mapping pour ce cluster :
    #   members → canonical
    #   splits → identité
    #   filet de sécurité : raw_member non listé → identité
    mapping = canon.setdefault("mapping", {})
    for raw in raw_members:
        if raw in member_set:
            mapping[raw] = new_canonical
        else:
            # split OU orphelin du cluster → identité
            mapping[raw] = raw

    # Met à jour le compteur canonical_count
    canon["canonical_count"] = len(set(mapping.values()))
    canon["raw_count"] = len(mapping)

    _save_canon_atomic(canon_path, canon)
    return updated


def _save_canon_atomic(path: Path, canon: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(canon, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


def start_dedupli(
    profile: str,
    *,
    threshold: int = 92,
    mode: str = "syntactic",
) -> dict[str, Any]:
    """Démarre un build de canonisation en background thread.

    Refuse si un run est déjà en cours (status running/pending non zombie).

    Args:
        profile: Profil cible (doit exister).
        threshold: Seuil de clustering fuzzy (défaut 92).
        mode: "syntactic" (Phases 1-3) ou "semantic" (C-light avec vocabulaire
              LLM). Cf. lib.theme_canon.build_canon_table pour le détail.

    Returns:
        {"profile": ..., "status": "pending", "started_at": ...}

    Raises:
        ValueError: profile vide ou mode invalide.
        FileNotFoundError: profile inexistant.
        RuntimeError: un run est déjà actif.
    """
    if not profile:
        raise ValueError("profile is required")
    if mode not in ("syntactic", "semantic", "source"):
        raise ValueError(
            f"mode must be 'syntactic', 'semantic' or 'source', got {mode!r}"
        )
    profile_path = data.get_project_root() / "profiles" / profile
    if not profile_path.is_dir():
        raise FileNotFoundError(f"profile not found: {profile}")

    _reap_zombie(profile)
    current = _read_status(profile)
    if current and current.get("status") in ("running", "pending"):
        raise RuntimeError(
            f"un run est déjà en cours (status={current.get('status')})"
        )

    payload = {
        "status": "pending",
        "phase": "extracting",
        "progress": {"done": 0, "total": 0},
        "started_at": _now_iso(),
        "completed_at": None,
        "error": None,
        "threshold": threshold,
        "mode": mode,
    }
    _write_status(profile, payload)

    thread = threading.Thread(
        target=_run_dedupli,
        args=(profile, threshold, mode),
        daemon=True,
    )
    thread.start()
    return {"profile": profile, "status": "pending", "started_at": payload["started_at"]}


def _run_dedupli(profile: str, threshold: int, mode: str = "syntactic") -> None:
    """Cœur du thread daemon : appelle build_canon_table + persiste status."""
    # Imports lazy — évite de charger langchain au démarrage du dashboard
    from agents.llm import get_agent_llm
    from lib.theme_canon import build_canon_table

    def on_progress(done: int, total: int, phase: str) -> None:
        # Mise à jour incrémentale du status (consommée par le polling UI)
        status = _read_status(profile) or {}
        # Check cancellation : si le user a demandé un cancel, on raise
        # immédiatement. La closure remonte jusqu'à _run_dedupli qui
        # transitionne le status à "cancelled" et exit.
        if status.get("cancel_requested"):
            raise _CancelledError()
        status.update({
            "status": "running",
            "phase": phase,
            "progress": {"done": done, "total": total},
        })
        _write_status(profile, status)

    try:
        llm = get_agent_llm()
        on_progress(0, 0, "extracting")
        table = build_canon_table(
            profile, llm,
            threshold=threshold,
            on_progress=on_progress,
            mode=mode,
        )
        status = _read_status(profile) or {}
        status.update({
            "status": "done",
            "phase": "done",
            "completed_at": _now_iso(),
            "raw_count": table.get("raw_count"),
            "canonical_count": table.get("canonical_count"),
            "n_clusters": len(table.get("clusters", [])),
        })
        # Nettoie le flag (au cas où il aurait été posé tardivement)
        status.pop("cancel_requested", None)
        _write_status(profile, status)
    except _CancelledError:
        status = _read_status(profile) or {}
        status.update({
            "status": "cancelled",
            "phase": "cancelled",
            "completed_at": _now_iso(),
            "error": None,
        })
        status.pop("cancel_requested", None)
        _write_status(profile, status)
    except Exception as exc:  # noqa: BLE001 — on persiste l'erreur dans status
        status = _read_status(profile) or {}
        status.update({
            "status": "error",
            "completed_at": _now_iso(),
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc()[-2000:],
        })
        status.pop("cancel_requested", None)
        _write_status(profile, status)
