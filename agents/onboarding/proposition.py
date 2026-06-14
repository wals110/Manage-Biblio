"""Analyse & proposition — orchestration du pipeline onboarding."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import yaml

from lib import profile as _profilelib
from lib import vision_cache
from lib.theme_canon import extract_themes_from_vision_cache
from lib.theme_normalizer import cluster_themes
from lib.vision import DEFAULT_MODEL, analyze_cover

_EXTS = (".pdf", ".epub")


def _profile_dir(profile: str) -> Path:
    # Accès par attribut de module (pas `from ... import get_project_root`) pour
    # que `mock.patch("lib.profile.get_project_root")` soit effectif en test.
    return _profilelib.get_project_root() / "profiles" / profile


def _load_profile_cfg(profile: str) -> dict:
    p = _profile_dir(profile) / "profile.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def run_vision(profile: str, on_progress: Callable[[int, int], None]) -> dict:
    """Vision LLM sur TOUT le corpus du profil → peuple vision_cache.json.

    Reprenable : skippe les fichiers déjà en cache. Sauvegarde incrémentale
    (toutes les 25 analyses + une finale). `on_progress(done, total)` reçoit
    le nombre de fichiers VISITÉS (pas seulement analysés) — la progression
    reflète le débit réel, pas le travail Vision. Retourne {n_total, n_analyzed}.
    """
    cfg = _load_profile_cfg(profile)
    target = Path(str(cfg.get("target") or ""))
    model = (cfg.get("llm") or {}).get("model") or DEFAULT_MODEL
    endpoint = (cfg.get("llm") or {}).get("endpoint") or ""
    n_pages = int((cfg.get("defaults") or {}).get("pages") or 2)
    api_key = os.environ.get("SILICONFLOW_API_KEY", "")

    cache_path = _profile_dir(profile) / ".cache" / "vision_cache.json"
    cache = vision_cache.load_cache(cache_path)

    files: list[str] = []
    for root, dirs, fs in os.walk(str(target)):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in fs:
            if not f.startswith(".") and f.lower().endswith(_EXTS):
                files.append(os.path.join(root, f))

    n_total = len(files)
    n_analyzed = 0
    for i, path in enumerate(files, start=1):
        key = vision_cache.compute_cache_key(path, model=model, n_pages=n_pages)
        if key and vision_cache.lookup(cache, key) is not None:
            on_progress(i, n_total)
            continue
        result = analyze_cover(
            path,
            api_key=api_key,
            endpoint=endpoint,
            model=model,
            n_pages=n_pages,
        )
        # Un résultat None (PDF illisible / LLM en échec) n'est jamais mis en
        # cache : il sera re-tenté au prochain run reprenable.
        if key and isinstance(result, dict):
            vision_cache.store(cache, key, result, model)
            n_analyzed += 1
            if n_analyzed % 25 == 0:
                vision_cache.save_cache(cache_path, cache)
        on_progress(i, n_total)
    vision_cache.save_cache(cache_path, cache)
    return {"n_total": n_total, "n_analyzed": n_analyzed}


def cluster_corpus(profile: str) -> list[dict]:
    """Clusterise tous les thèmes du vision_cache → clusters enrichis de counts.

    Retourne [{canonical, canonical_forms, raw_members, count}], trié par
    count décroissant. `canonical` = 1ère forme canonique du cluster.
    """
    themes = extract_themes_from_vision_cache(profile)   # {raw: count}
    clusters = cluster_themes(themes.keys())             # [{canonical_forms, raw_members}]
    out: list[dict] = []
    for c in clusters:
        count = sum(int(themes.get(m, 0)) for m in c["raw_members"])
        out.append({
            "canonical": (c["canonical_forms"] or c["raw_members"] or [""])[0],
            "canonical_forms": c["canonical_forms"],
            "raw_members": c["raw_members"],
            "count": count,
        })
    out.sort(key=lambda c: c["count"], reverse=True)
    return out
