"""Explorateur Maintenant/Après — projection lecture seule du reclassify.

Spec : docs/superpowers/specs/2026-06-25-explorer-preview-design.md
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dashboard import data, taxonomy


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
            "flag_keyword": taxonomy.RECLASSIFY_INCLUDE_KEYWORD}
