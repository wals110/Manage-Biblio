"""Categories management — interactive viewer + editor for categories.yaml.

`categories.yaml` is step 2 of the classification pipeline (after
`theme_mapping.yaml`). It groups entries under top-level domains
(informatique, mathematique, physique, …) where each entry maps a
target folder to a list of keywords + a priority.

Format::

    informatique:
      - chemin: "02-INFORMATIQUE/05-IA-ML/Deep-Learning"
        priorite: 2
        mots_cles: ["deep learning", "neural network", ...]

Phase A surface (read-only):
    - list profiles having categories.yaml
    - parse + aggregate snapshot for the UI
    - mem-cached, invalidated on writes (later phases)
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from pathlib import Path

import yaml

from dashboard import data

# Memory cache — invalidated whenever categories.yaml is written. Same
# pattern as taxonomy._snapshot_cache.
_snapshot_cache: dict[str, dict] = {}
_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_cache_lock = threading.Lock()


def _profile_dir(profile: str) -> Path:
    return data.get_project_root() / "profiles" / profile


def _categories_path(profile: str) -> Path:
    return _profile_dir(profile) / "categories.yaml"


def has_categories(profile: str) -> bool:
    return _categories_path(profile).exists()


def _load_raw(profile: str) -> dict:
    """Return the parsed YAML or {} on missing/invalid."""
    p = _categories_path(profile)
    if not p.exists():
        return {}
    try:
        loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _normalize_entry(raw: dict) -> dict | None:
    """Validate + clean a single entry. Returns None on bad shape."""
    if not isinstance(raw, dict):
        return None
    chemin = str(raw.get("chemin") or "").strip()
    if not chemin:
        return None
    try:
        priorite = int(raw.get("priorite") or 0)
    except (TypeError, ValueError):
        priorite = 0
    mots_cles_raw = raw.get("mots_cles") or []
    if not isinstance(mots_cles_raw, list):
        mots_cles_raw = []
    mots_cles = [str(m).strip() for m in mots_cles_raw if str(m).strip()]
    return {
        "chemin": chemin,
        "priorite": priorite,
        "mots_cles": mots_cles,
        "n_keywords": len(mots_cles),
    }


def build_snapshot(profile: str, force_reload: bool = False) -> dict:
    """Aggregate categories.yaml into a UI-friendly snapshot.

    Returns::

        {
          "ok": True,
          "profile": <name>,
          "groups": [
            {"group": "informatique", "n_entries": 26, "entries": [{...}]},
            ...
          ],
          "stats": {
            "n_groups": int,
            "n_entries": int,
            "n_keywords": int,
            "avg_keywords": float,
          },
          "exists": bool,
        }

    Entries inside each group are sorted by (priorite asc, chemin asc).
    Groups are sorted by their name (alpha).
    """
    if not force_reload:
        with _cache_lock:
            cached = _snapshot_cache.get(profile)
            if cached is not None:
                return cached

    out: dict = {
        "ok": True,
        "profile": profile,
        "groups": [],
        "stats": {"n_groups": 0, "n_entries": 0, "n_keywords": 0,
                  "avg_keywords": 0.0},
        "exists": _categories_path(profile).exists(),
    }
    if not out["exists"]:
        with _cache_lock:
            _snapshot_cache[profile] = out
        return out

    raw = _load_raw(profile)
    total_entries = 0
    total_keywords = 0
    groups = []
    for group_name, group_entries in raw.items():
        if not isinstance(group_entries, list):
            continue
        cleaned: list[dict] = []
        for raw_entry in group_entries:
            normalized = _normalize_entry(raw_entry)
            if normalized is None:
                continue
            cleaned.append(normalized)
        # Sort: lowest priority first (1 = highest), then path alpha
        cleaned.sort(key=lambda r: (r["priorite"], r["chemin"].lower()))
        for e in cleaned:
            total_keywords += e["n_keywords"]
        total_entries += len(cleaned)
        groups.append({
            "group": str(group_name),
            "n_entries": len(cleaned),
            "entries": cleaned,
        })
    groups.sort(key=lambda g: g["group"].lower())

    out["groups"] = groups
    out["stats"] = {
        "n_groups": len(groups),
        "n_entries": total_entries,
        "n_keywords": total_keywords,
        "avg_keywords": (round(total_keywords / total_entries, 1)
                         if total_entries else 0.0),
    }

    with _cache_lock:
        _snapshot_cache[profile] = out
    return out


def reset_cache(profile: str | None = None) -> None:
    """Drop the snapshot cache (one profile or all)."""
    with _cache_lock:
        if profile is None:
            _snapshot_cache.clear()
        else:
            _snapshot_cache.pop(profile, None)
