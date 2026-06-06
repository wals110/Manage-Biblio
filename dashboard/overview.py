"""Cockpit Overview — agrège des métriques par profil + métriques globales.

Cache mémoire 30 s par profil (snapshot) — invalidé par `?refresh=1` côté
endpoint ou `reset_cache()` côté tests.
"""
from __future__ import annotations

import json  # noqa: F401 — used by upcoming card functions (Task 2+)
import os
import threading
import time  # noqa: F401 — used by upcoming card functions (Task 2+)
from pathlib import Path

import yaml

from dashboard import data

# ─── Cache ────────────────────────────────────────────────────────
_CACHE_TTL_SECONDS = 30
_cache_lock = threading.Lock()
_overview_cache: dict[str, tuple[float, dict]] = {}


def reset_cache(profile: str | None = None) -> None:
    """Vide le cache (un profil ou tout). Utilisé par les tests."""
    with _cache_lock:
        if profile is None:
            _overview_cache.clear()
        else:
            _overview_cache.pop(profile, None)


# ─── Helpers profil ───────────────────────────────────────────────
def _profile_yaml_path(profile: str) -> Path:
    """Chemin attendu de profiles/<profile>/profile.yaml."""
    return data.get_project_root() / "profiles" / profile / "profile.yaml"


def _load_profile_config(profile: str) -> dict | None:
    """Lit profile.yaml. None si absent ou invalide."""
    p = _profile_yaml_path(profile)
    if not p.exists():
        return None
    try:
        cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None
    return cfg if isinstance(cfg, dict) else None


# ─── Cards ────────────────────────────────────────────────────────
def card_files_count(profile: str) -> dict:
    """Compte les fichiers PDF/EPUB dans target + taille totale.

    Retourne {total, size_gb, by_ext: {pdf, epub}, error?: str} où error =
    'profile_missing' | 'target_missing' (sentinelle pour l'UI tooltip).
    """
    cfg = _load_profile_config(profile)
    if cfg is None:
        return {"total": 0, "size_gb": 0.0,
                "by_ext": {"pdf": 0, "epub": 0}, "error": "profile_missing"}
    target_str = cfg.get("target")
    if not target_str:
        return {"total": 0, "size_gb": 0.0,
                "by_ext": {"pdf": 0, "epub": 0}, "error": "target_missing"}
    target = Path(str(target_str))
    if not target.exists():
        return {"total": 0, "size_gb": 0.0,
                "by_ext": {"pdf": 0, "epub": 0}, "error": "target_missing"}
    n_pdf = n_epub = 0
    size_bytes = 0
    for root, _dirs, files in os.walk(str(target)):
        for f in files:
            ext = f.lower().rsplit(".", 1)[-1] if "." in f else ""
            if ext == "pdf":
                n_pdf += 1
            elif ext == "epub":
                n_epub += 1
            else:
                continue
            try:
                size_bytes += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return {
        "total": n_pdf + n_epub,
        "size_gb": round(size_bytes / 1024**3, 2),
        "by_ext": {"pdf": n_pdf, "epub": n_epub},
    }


def card_classified_rate(profile: str) -> dict:
    """% de fichiers rangés vs racine ou fallback. Retourne
    {classified, unclassified, rate, fallback_count}."""
    cfg = _load_profile_config(profile)
    if cfg is None:
        return {"classified": 0, "unclassified": 0, "rate": 0.0,
                "fallback_count": 0}
    target_str = cfg.get("target")
    if not target_str:
        return {"classified": 0, "unclassified": 0, "rate": 0.0,
                "fallback_count": 0}
    fallback = str(cfg.get("fallback") or "_A-TRIER")
    target = Path(str(target_str))
    if not target.exists():
        return {"classified": 0, "unclassified": 0, "rate": 0.0,
                "fallback_count": 0}
    classified = root_count = fallback_count = 0
    for root, _dirs, files in os.walk(str(target)):
        rel = Path(root).relative_to(target).as_posix()
        candidates = [f for f in files if f.lower().endswith((".pdf", ".epub"))]
        if not candidates:
            continue
        if rel == ".":
            root_count += len(candidates)
        elif rel.split("/", 1)[0] == fallback:
            fallback_count += len(candidates)
        else:
            classified += len(candidates)
    total = classified + root_count + fallback_count
    return {
        "classified": classified,
        "unclassified": root_count + fallback_count,
        "rate": round(classified / total * 100, 1) if total else 0.0,
        "fallback_count": fallback_count,
    }


def card_folders_count(profile: str) -> dict:
    """Statistiques sur tree.yaml du profil. Retourne {total, max_depth,
    recently_modified, error?}."""
    tree_path = data.get_project_root() / "profiles" / profile / "tree.yaml"
    if not tree_path.exists():
        return {"total": 0, "max_depth": 0,
                "recently_modified": [], "error": "tree_missing"}
    try:
        d = yaml.safe_load(tree_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {"total": 0, "max_depth": 0,
                "recently_modified": [], "error": "tree_invalid"}
    folders = [str(f).strip() for f in (d.get("folders") or []) if f]
    max_depth = max((f.count("/") + 1 for f in folders), default=0)
    # recently_modified : 3 folders avec mtime FS la plus récente.
    # Lookup via target. Best-effort : silent skip si target absent.
    cfg = _load_profile_config(profile)
    target_str = cfg.get("target") if cfg else None
    rec: list[tuple[float, str]] = []
    if target_str:
        target = Path(str(target_str))
        if target.exists():
            for f in folders:
                fp = target / f
                try:
                    rec.append((fp.stat().st_mtime, f))
                except OSError:
                    pass
    rec.sort(reverse=True)
    return {
        "total": len(folders),
        "max_depth": max_depth,
        "recently_modified": [name for _, name in rec[:3]],
    }
