"""Explorateur Maintenant/Après — projection lecture seule du reclassify.

Spec : docs/superpowers/specs/2026-06-25-explorer-preview-design.md
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dashboard import apply_engine, data, reclassify_apply, taxonomy


def _explorer_dir(profile: str) -> Path:
    return data.get_project_root() / "profiles" / profile / ".cache" / "explorer"


def _write_status(profile: str, payload: dict[str, Any]) -> None:
    d = _explorer_dir(profile)
    d.mkdir(parents=True, exist_ok=True)
    (d / "status.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def build_projection(profile: str) -> dict[str, Any]:
    """Projection par fichier (TOUS les fichiers), lecture seule. Réutilise le
    MÊME classifieur P1+P2 que l'apply (taxonomy._scan_and_classify, llm_mapper
    None). Coûteux → appelé en tâche de fond par _build_job (Task 3).
    """

    def _prog(done: int, total: int) -> None:
        _write_status(profile, {"status": "building", "n_done": done,
                                "n_total": total, "error": None})

    rows = taxonomy._scan_and_classify(
        profile, include_step2=taxonomy.RECLASSIFY_INCLUDE_KEYWORD, on_progress=_prog)
    files: list[dict] = []
    n_moving = n_stable = n_no_pred = n_unan = 0
    for r in rows:
        pred, cur = r["predicted_folder"], r["current_folder"]
        analyzed = bool(r.get("analyzed", True))
        signal = None
        if pred:
            signal = "p2" if str(r["source"]).startswith("Keyword") else "p1"
        if not analyzed:
            n_unan += 1
        if pred and pred != cur:
            n_moving += 1
        elif pred and pred == cur:
            n_stable += 1
        elif analyzed:
            n_no_pred += 1
        files.append({"rel_path": r["rel_path"], "current_folder": cur,
                      "predicted_folder": pred, "source": r["source"],
                      "signal": signal, "confidence": r["top_confidence"],
                      "top_theme": r["top_theme"], "analyzed": analyzed})
    return {"ok": True, "files": files,
            "summary": {"n_total": len(rows), "n_moving": n_moving,
                        "n_stable": n_stable, "n_no_prediction": n_no_pred,
                        "n_unanalyzed": n_unan},
            "flag_keyword": taxonomy.RECLASSIFY_INCLUDE_KEYWORD,
            "tree_folders": taxonomy._load_tree(profile)}


# ---------------------------------------------------------------------------
# Cache + fraîcheur + build de fond (Task 3)
# ---------------------------------------------------------------------------

_projection_cache: dict[str, dict] = {}  # profile → {"fresh_hash": str, "data": dict}


def _status_path(profile: str) -> Path:
    return _explorer_dir(profile) / "status.json"


def _read_status(profile: str) -> dict[str, Any] | None:
    p = _status_path(profile)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _vision_sig(profile: str) -> str:
    """Signature mtime du vision_cache — invalide le cache si la Vision a tourné."""
    p = data.get_project_root() / "profiles" / profile / ".cache" / "vision_cache.json"
    try:
        return str(p.stat().st_mtime)   # float (sous-seconde) → plus sûr qu'un int
    except OSError:
        return "0"


def _fresh_hash(profile: str) -> str:
    """Hash combiné config (theme_mapping/categories/tree/theme-canon) + Vision mtime."""
    return reclassify_apply._config_hash(profile) + ":" + _vision_sig(profile)


def _build_job(profile: str) -> None:
    """Tâche de fond : construit la projection et l'écrit dans le cache."""
    # Snapshot AVANT le scan : si la config change pendant le build (~27 s),
    # le hash stocké reste l'ancien → le prochain get voit « périmé » → rebuild.
    snapshot_hash = _fresh_hash(profile)
    try:
        result = build_projection(profile)
        _projection_cache[profile] = {"fresh_hash": snapshot_hash, "data": result}
        n = result["summary"]["n_total"]
        _write_status(profile, {"status": "ready", "n_done": n, "n_total": n, "error": None})
    except Exception as exc:  # noqa: BLE001 — frontière de thread
        _write_status(profile, {"status": "error", "n_done": 0, "n_total": 0, "error": str(exc)})


def _spawn_build(profile: str) -> None:
    """Écrit le statut building puis délègue à un thread daemon."""
    _write_status(profile, {"status": "building", "n_done": 0, "n_total": 0, "error": None})
    apply_engine.spawn(_build_job, (profile,), f"explorer-build-{profile}")


def get_projection(profile: str) -> dict[str, Any]:
    """Cache frais → {status: ready, ...data} ; sinon lance un build de fond et
    renvoie {status: building}. Recalcul auto si le hash config+Vision a changé."""
    cached = _projection_cache.get(profile)
    if cached and cached["fresh_hash"] == _fresh_hash(profile):
        return {"status": "ready", **cached["data"]}
    status = _read_status(profile)
    if not (status and status.get("status") == "building"):
        _spawn_build(profile)
    return {"status": "building"}


def get_build_status(profile: str) -> dict[str, Any]:
    """Statut courant du build (idle si rien n'a encore été lancé)."""
    return _read_status(profile) or {"status": "idle"}


def refresh(profile: str) -> dict[str, Any]:
    """Force un rebuild en invalidant le cache mémoire."""
    _projection_cache.pop(profile, None)
    _spawn_build(profile)
    return {"ok": True}
