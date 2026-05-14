"""Taxonomy management — interactive viewer + mapping editor.

Aggregates three sources of truth for a profile and exposes them to the UI:

1. `tree.yaml`           — Target folder structure (~90 nodes)
2. `theme_mapping.yaml`  — Curated theme → folder mapping (~355 entries)
3. `vision_cache.json`   — Raw LLM output (~28k entries, field `result.themes`)

Phase 1 surface:
    - read snapshot (tree + mapping + LLM themes universe)
    - lazy file listing per folder (filesystem walk + 5min mem cache)
    - add a single mapping entry (theme → folder) with backup + lock

Out of scope (later phases):
    - edit/delete mapping keys
    - rename/move/delete tree folders
    - reclassify preview

Concurrency: a single write lock per profile guards `theme_mapping.yaml`
edits. A separate `.taxonomy.lock` sentinel can be set externally (e.g. by
a baseline_run) to block writes.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import yaml

from dashboard import data
from lib import vision_cache
from lib.classifier import classify_by_theme, classify_combined, load_keyword_classifier

# Filter weak LLM signals before populating the themes universe.
_MIN_CONFIDENCE = 0.5

# File listing cache TTL (seconds)
_FILES_CACHE_TTL = 300

# Snapshot cache TTL — derived data is cheap but vision_cache parsing is
# the slow part; the snapshot stays valid until a write invalidates it.
_snapshot_cache: dict[str, dict] = {}
_files_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_cache_lock = threading.Lock()


# ─── Path helpers ─────────────────────────────────────────────────────────


def _profile_dir(profile: str) -> Path:
    return data.get_project_root() / "profiles" / profile


def _tree_path(profile: str) -> Path:
    return _profile_dir(profile) / "tree.yaml"


def _mapping_path(profile: str) -> Path:
    return _profile_dir(profile) / "theme_mapping.yaml"


def _vision_cache_path(profile: str) -> Path:
    return _profile_dir(profile) / ".cache" / "vision_cache.json"


def _backup_dir(profile: str) -> Path:
    d = _profile_dir(profile) / ".cache" / "taxonomy-backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _lock_file(profile: str) -> Path:
    return _profile_dir(profile) / ".cache" / "taxonomy.lock"


# ─── Profile listing (read-only convenience for the picker) ───────────────


def list_profiles() -> list[dict]:
    """Return every profile that has both tree.yaml and theme_mapping.yaml."""
    root = data.get_project_root() / "profiles"
    if not root.exists():
        return []
    out = []
    for p in sorted(root.iterdir()):
        if not p.is_dir() or p.name.startswith("."):
            continue
        if not (p / "tree.yaml").exists() or not (p / "theme_mapping.yaml").exists():
            continue
        out.append({"name": p.name})
    return out


# ─── Tree parsing ─────────────────────────────────────────────────────────


def _load_tree(profile: str) -> list[str]:
    """Return the flat list of folder paths from tree.yaml (sorted)."""
    path = _tree_path(profile)
    if not path.exists():
        return []
    try:
        data_ = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return []
    folders = data_.get("folders") or []
    return sorted({str(f).strip() for f in folders if f})


def _tree_hierarchy(folders: list[str], counts: dict[str, int]) -> dict:
    """Build a nested {name, path, file_count, children} tree from a flat list.

    The 'file_count' is the count of files directly inside that folder
    (not aggregated from descendants — the treemap aggregates itself).
    """
    folder_set = set(folders)

    def children_of(prefix: str) -> list[str]:
        out: list[str] = []
        if not prefix:
            for f in folder_set:
                if "/" not in f:
                    out.append(f)
        else:
            pref = prefix + "/"
            for f in folder_set:
                if f.startswith(pref) and "/" not in f[len(pref):]:
                    out.append(f)
        return sorted(out)

    def build(path: str) -> dict:
        children = [build(c) for c in children_of(path)]
        return {
            "name": path.split("/")[-1] if path else "racine",
            "path": path,
            "file_count": int(counts.get(path, 0)),
            "children": children,
        }

    return build("")


# ─── Vision cache parsing — universe of LLM themes ─────────────────────────


_LABEL_RE = re.compile(r"\s+")


def _normalize_theme(s: str) -> str:
    return _LABEL_RE.sub(" ", s).strip()


def _iter_themes(cache: dict):
    """Yield (theme_str, confidence) for every theme in the cache."""
    for entry in cache.values():
        if not isinstance(entry, dict):
            continue
        result = entry.get("result")
        if not isinstance(result, dict):
            continue
        # Multi-candidate themes[]
        themes_arr = result.get("themes")
        if isinstance(themes_arr, list) and themes_arr:
            for item in themes_arr:
                if not isinstance(item, dict):
                    continue
                t = _normalize_theme(str(item.get("theme") or ""))
                if not t:
                    continue
                conf = float(item.get("confidence") or 0.0)
                yield t, conf
        # Legacy single 'theme'
        t_single = _normalize_theme(str(result.get("theme") or ""))
        if t_single:
            yield t_single, float(result.get("confidence") or 0.0)


def _aggregate_themes_llm(profile: str, mapping: dict) -> tuple[list[dict], dict]:
    """Return (themes_list, stats) — universe of LLM themes with frequencies.

    Each theme entry: {theme, count, confidence_avg, mapped_to, is_orphan,
                       sample_titles}.
    """
    cache_path = _vision_cache_path(profile)
    if not cache_path.exists():
        return [], {"total_themes_llm": 0, "orphans": 0, "mapped": 0}
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], {"total_themes_llm": 0, "orphans": 0, "mapped": 0}

    # Aggregate: per-theme (lowercased key) count + conf sum + sample titles
    agg: dict[str, dict] = {}
    sample_titles: dict[str, list[str]] = defaultdict(list)
    for entry in cache.values():
        if not isinstance(entry, dict):
            continue
        result = entry.get("result") if isinstance(entry.get("result"), dict) else {}
        title = str(result.get("title") or "").strip()
        for t, conf in _iter_themes({"x": entry}):
            if conf < _MIN_CONFIDENCE:
                continue
            key = t.lower()
            slot = agg.setdefault(key, {"theme": t, "count": 0, "conf_sum": 0.0})
            slot["count"] += 1
            slot["conf_sum"] += conf
            if title and len(sample_titles[key]) < 3 and title not in sample_titles[key]:
                sample_titles[key].append(title)

    themes_out: list[dict] = []
    n_orphans = 0
    for key, slot in agg.items():
        mapped_to = classify_by_theme(slot["theme"], mapping)
        is_orphan = mapped_to is None
        if is_orphan:
            n_orphans += 1
        themes_out.append({
            "theme": slot["theme"],
            "count": slot["count"],
            "confidence_avg": round(slot["conf_sum"] / slot["count"], 3),
            "mapped_to": mapped_to,
            "is_orphan": is_orphan,
            "sample_titles": sample_titles[key],
        })

    themes_out.sort(key=lambda r: (-r["count"], r["theme"].lower()))
    stats = {
        "total_themes_llm": len(themes_out),
        "orphans": n_orphans,
        "mapped": len(themes_out) - n_orphans,
    }
    return themes_out, stats


# ─── Live file counts per folder ──────────────────────────────────────────


def _profile_target_path(profile: str) -> Path | None:
    pf = _profile_dir(profile) / "profile.yaml"
    if not pf.exists():
        return None
    try:
        cfg = yaml.safe_load(pf.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return None
    target = cfg.get("target")
    if not target:
        return None
    return Path(str(target))


def _scan_folder_counts(target: Path, folders: list[str]) -> dict[str, int]:
    """Count files directly inside each folder (no recursion into subfolders).

    Returns {folder_relpath: count}. Missing folders → 0.
    """
    counts: dict[str, int] = {}
    if not target.exists():
        return counts
    folder_set = set(folders)
    # For each folder declared in tree.yaml, count direct files
    for f in folder_set:
        full = target / f
        if not full.is_dir():
            counts[f] = 0
            continue
        try:
            n = 0
            with __import__("os").scandir(full) as it:
                for entry in it:
                    if entry.is_file(follow_symlinks=False) and not entry.name.startswith("."):
                        n += 1
            counts[f] = n
        except OSError:
            counts[f] = 0
    return counts


# ─── Snapshot ─────────────────────────────────────────────────────────────


def _load_mapping(profile: str) -> dict[str, str]:
    path = _mapping_path(profile)
    if not path.exists():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    out: dict[str, str] = {}
    for k, v in loaded.items():
        if isinstance(v, str):
            out[str(k)] = v
    return out


def _mapping_reverse(mapping: dict[str, str]) -> dict[str, list[str]]:
    """Folder path → list of theme keys mapped to it (sorted)."""
    rev: dict[str, list[str]] = defaultdict(list)
    for theme, folder in mapping.items():
        rev[folder].append(theme)
    return {k: sorted(v, key=str.lower) for k, v in rev.items()}


def get_snapshot(profile: str, force_reload: bool = False) -> dict:
    """Return the full taxonomy snapshot for `profile`."""
    with _cache_lock:
        if not force_reload and profile in _snapshot_cache:
            return _snapshot_cache[profile]

    folders = _load_tree(profile)
    mapping = _load_mapping(profile)
    target = _profile_target_path(profile)
    counts = _scan_folder_counts(target, folders) if target else {}
    tree = _tree_hierarchy(folders, counts)
    themes_llm, themes_stats = _aggregate_themes_llm(profile, mapping)
    mapping_by_folder = _mapping_reverse(mapping)

    backup_dir = _profile_dir(profile) / ".cache" / "taxonomy-backups"
    mapping_backups: list[Path] = []
    tree_backups: list[Path] = []
    if backup_dir.exists():
        mapping_backups = sorted(backup_dir.glob("theme_mapping-*.yaml"), key=lambda p: p.name)
        tree_backups = sorted(backup_dir.glob("tree-*.yaml"), key=lambda p: p.name)
    backup_count = len(mapping_backups) + len(tree_backups)

    # Touched indicator: diff vs the OLDEST backup of each type.
    # Empty when no backups (= no pending changes).
    touched_themes: set[str] = set()
    touched_folders: set[str] = set()

    # Mapping diff
    if mapping_backups:
        oldest = mapping_backups[0]
        try:
            baseline = yaml.safe_load(oldest.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            baseline = {}
        baseline_map: dict[str, str] = {
            str(k): v for k, v in baseline.items() if isinstance(v, str)
        }
        for key in set(mapping.keys()) | set(baseline_map.keys()):
            cur = mapping.get(key)
            base = baseline_map.get(key)
            if cur != base:
                touched_themes.add(key)
                if cur:
                    touched_folders.add(cur)
                if base:
                    touched_folders.add(base)

    # Tree diff — folders added/removed since the oldest tree backup
    if tree_backups:
        oldest_tree = tree_backups[0]
        try:
            baseline_tree = yaml.safe_load(oldest_tree.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            baseline_tree = {}
        baseline_folders = set(baseline_tree.get("folders", []) or [])
        current_set = set(folders)
        for f in current_set ^ baseline_folders:   # symmetric diff
            touched_folders.add(f)

    snap = {
        "profile": profile,
        "tree": tree,
        "folders": folders,
        "mapping_by_folder": mapping_by_folder,
        "themes_llm": themes_llm,
        "stats": {
            **themes_stats,
            "tree_nodes": len(folders),
            "total_files": int(sum(counts.values())),
            "target_exists": bool(target and target.exists()),
            "backup_count": backup_count,
            "touched_folders": sorted(touched_folders),
            "touched_themes": sorted(touched_themes),
        },
    }
    with _cache_lock:
        _snapshot_cache[profile] = snap
    return snap


def reset_cache(profile: str | None = None) -> None:
    """Drop the snapshot cache (one profile or all)."""
    with _cache_lock:
        if profile is None:
            _snapshot_cache.clear()
            _files_cache.clear()
        else:
            _snapshot_cache.pop(profile, None)
            # Drop files cache entries for this profile
            keys = [k for k in _files_cache if k[0] == profile]
            for k in keys:
                _files_cache.pop(k, None)


# ─── File listing (lazy, paginated, mem-cached) ───────────────────────────


def list_files_in_folder(
    profile: str,
    folder: str,
    offset: int = 0,
    limit: int = 50,
) -> dict:
    """Return `{files, total, offset, limit}` for direct files inside `folder`.

    Subfolders are NOT included (they live in the tree structure). The
    listing is cached in memory for `_FILES_CACHE_TTL` seconds.
    """
    target = _profile_target_path(profile)
    if not target or not target.exists():
        return {"files": [], "total": 0, "offset": offset, "limit": limit}

    cache_key = (profile, folder)
    now = time.time()
    with _cache_lock:
        cached = _files_cache.get(cache_key)
    if cached and (now - cached[0]) < _FILES_CACHE_TTL:
        all_files = cached[1]
    else:
        full = target / folder if folder else target
        all_files = []
        if full.is_dir():
            try:
                import os as _os
                with _os.scandir(full) as it:
                    for entry in it:
                        if entry.is_file(follow_symlinks=False) and not entry.name.startswith("."):
                            all_files.append({"name": entry.name})
            except OSError:
                all_files = []
            all_files.sort(key=lambda r: r["name"].lower())
        with _cache_lock:
            _files_cache[cache_key] = (now, all_files)

    total = len(all_files)
    page = all_files[offset: offset + limit]
    return {"files": page, "total": total, "offset": offset, "limit": limit}


# ─── Mapping writes (add only — Phase 1) ──────────────────────────────────


# ─── File metadata (vision cache lookup + theme-only prediction) ──────────


def _load_profile_yaml(profile: str) -> dict:
    pf = _profile_dir(profile) / "profile.yaml"
    if not pf.exists():
        return {}
    try:
        return yaml.safe_load(pf.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}


def get_file_metadata_full_pipeline(profile: str, rel_path: str) -> dict:
    """Recompute the prediction using the FULL classify_combined pipeline:
      1. classify_by_theme (theme_mapping)
      2. KeywordClassifier (categories.yaml)
      3. LLM Mapper is NOT called here (we don't want to spend tokens on
         a UI preview — only theme_mapping + keywords are computed)

    Returns:
      {ok, prediction: {dest, source, score, theme_used}} or {ok, prediction: None}
    """
    target = _profile_target_path(profile)
    if target is None:
        return {"ok": False, "error": "profil sans target configuré"}
    abs_path = target / rel_path
    if not abs_path.exists():
        return {"ok": False, "error": "fichier introuvable"}

    cfg = _load_profile_yaml(profile)
    model = (cfg.get("llm") or {}).get("model") or "Qwen/Qwen3-VL-32B-Instruct"
    n_pages = int((cfg.get("defaults") or {}).get("pages") or 2)

    cache_path = _vision_cache_path(profile)
    if not cache_path.exists():
        return {"ok": True, "prediction": None}
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ok": True, "prediction": None}

    key = vision_cache.compute_cache_key(str(abs_path), model=model, n_pages=n_pages)
    if not key:
        return {"ok": True, "prediction": None}
    result = vision_cache.lookup(cache, key)
    if not isinstance(result, dict):
        return {"ok": True, "prediction": None}

    mapping = _load_mapping(profile)
    # Load the KeywordClassifier from categories.yaml (étape 2 of pipeline)
    categories_path = _profile_dir(profile) / "categories.yaml"
    classifier = load_keyword_classifier(str(categories_path)) if categories_path.exists() else None

    dest, score, source = classify_combined(
        result, abs_path.name, mapping,
        classifier=classifier,
        llm_mapper=None,             # do NOT spend LLM tokens for UI
        pdf_path=str(abs_path),
    )
    if not dest:
        return {"ok": True, "prediction": None}
    return {
        "ok": True,
        "prediction": {
            "dest": dest,
            "source": source,
            "score": float(score) if score else 0.0,
            "label": "classify_combined (étapes 1+2 du pipeline — KeywordClassifier inclus, LLM Mapper exclu)",
        },
    }


def get_file_metadata(profile: str, rel_path: str) -> dict:
    """Return LLM metadata + theme-only predictions for a file.

    Schema:
        {
          ok: bool,
          file: {rel_path, abs_path, exists, current_folder, page_count_estimate},
          vision: {title, author, language, confidence, themes:[{theme,confidence,reason,mapped_to}]} | null,
          prediction: {dest, used_theme, label} | null,  # via classify_by_theme on best theme
        }
    """
    target = _profile_target_path(profile)
    if target is None:
        return {"ok": False, "error": "profil sans target configuré"}
    abs_path = target / rel_path
    parent = "/".join(rel_path.split("/")[:-1])
    out: dict = {
        "ok": True,
        "file": {
            "rel_path": rel_path,
            "abs_path": str(abs_path),
            "exists": abs_path.exists(),
            "current_folder": parent,
            "page_count_estimate": None,
        },
        "vision": None,
        "prediction": None,
    }
    if not abs_path.exists():
        return out

    # Look up the cached vision result via the same key as the pipeline
    cfg = _load_profile_yaml(profile)
    model = (cfg.get("llm") or {}).get("model") or "Qwen/Qwen3-VL-32B-Instruct"
    n_pages = int((cfg.get("defaults") or {}).get("pages") or 2)
    cache_path = _vision_cache_path(profile)
    if not cache_path.exists():
        return out
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return out

    key = vision_cache.compute_cache_key(str(abs_path), model=model, n_pages=n_pages)
    if not key:
        return out
    result = vision_cache.lookup(cache, key)
    if not isinstance(result, dict):
        return out

    mapping = _load_mapping(profile)
    themes_raw = result.get("themes")
    themes_out: list[dict] = []
    if isinstance(themes_raw, list) and themes_raw:
        for it in themes_raw:
            if not isinstance(it, dict):
                continue
            t = str(it.get("theme") or "").strip()
            if not t:
                continue
            themes_out.append({
                "theme": t,
                "confidence": float(it.get("confidence") or 0.0),
                "reason": str(it.get("reason") or ""),
                "mapped_to": classify_by_theme(t, mapping),
            })
    else:
        t = str(result.get("theme") or "").strip()
        if t:
            themes_out.append({
                "theme": t,
                "confidence": float(result.get("confidence") or 0.0),
                "reason": "",
                "mapped_to": classify_by_theme(t, mapping),
            })

    out["vision"] = {
        "title": result.get("title"),
        "author": result.get("author"),
        "language": result.get("language"),
        "confidence": float(result.get("confidence") or 0.0),
        "themes": themes_out,
    }
    # Prediction theme-only : prendre le premier candidat qui résout vers
    # un chemin spécifique (avec un '/'), sinon le premier qui résout du tout.
    best_specific = None
    best_any = None
    for t in themes_out:
        if t["mapped_to"]:
            if "/" in t["mapped_to"] and not best_specific:
                best_specific = t
            if not best_any:
                best_any = t
    chosen = best_specific or best_any
    if chosen:
        out["prediction"] = {
            "dest": chosen["mapped_to"],
            "used_theme": chosen["theme"],
            "label": "classify_by_theme (étape 1/4 du pipeline) — KeywordClassifier/Mapper non inclus",
        }
    return out


# ─── Thumbnails (cover + multipage lazy) ──────────────────────────────────


def _thumbnail_cache_dir(profile: str, rel_path: str) -> Path:
    """Cache thumbnails at profile/.cache/thumbnails/<hash16>/."""
    h = hashlib.sha1(rel_path.encode("utf-8")).hexdigest()[:16]
    d = _profile_dir(profile) / ".cache" / "thumbnails" / h
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_thumbnail(profile: str, rel_path: str, page: int) -> tuple[Path | None, str | None]:
    """Return (image_path, mime) for the requested page; generate if absent.

    Returns (None, error_message) on failure.
    """
    page = max(1, min(5, int(page)))
    target = _profile_target_path(profile)
    if target is None:
        return None, "profil sans target"
    abs_path = target / rel_path
    if not abs_path.exists():
        return None, "fichier introuvable"
    cache_dir = _thumbnail_cache_dir(profile, rel_path)
    img = cache_dir / f"{page}.jpg"
    if img.exists():
        return img, "image/jpeg"
    # Generate the missing range — lib.thumbnail.generate_thumbnail makes pages
    # [start_page .. start_page + n_pages - 1].
    try:
        from lib.thumbnail import generate_thumbnail
        n = generate_thumbnail(abs_path, cache_dir, n_pages=1, start_page=page)
    except Exception as exc:  # pragma: no cover — defensive
        return None, f"erreur génération: {exc}"
    if n > 0 and img.exists():
        return img, "image/jpeg"
    return None, "page indisponible"


def get_file_page_count(profile: str, rel_path: str) -> int:
    """Return total page count of a PDF (best-effort, pypdf), capped to 5.

    Returns 0 if unreadable.
    """
    target = _profile_target_path(profile)
    if target is None:
        return 0
    abs_path = target / rel_path
    if not abs_path.exists() or abs_path.suffix.lower() != ".pdf":
        return 0
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(abs_path))
        return min(5, len(reader.pages))
    except Exception:
        return 0


# ─── Errors ───────────────────────────────────────────────────────────────


# ─── Impact preview — dry-run before mapping write ────────────────────────


def preview_mapping_impact(
    profile: str,
    theme: str,
    folder: str | None,
    action: str = "add",
) -> dict:
    """Simulate the effect of an add/update/delete mapping operation on the
    current vision cache, without writing anything.

    Returns:
        {
            "n_files_affected": int,
            "cross_section_changes": int,
            "examples": [{title, current_dest, new_dest}, ...],  # up to 5
            "current_dest": str | None,  # what the theme currently resolves to
            "new_dest": str | None,
        }

    action='add'    → theme expected to be new; new_dest = folder
    action='update' → theme expected to exist; new_dest = folder
    action='delete' → folder ignored; new_dest = None (theme removed)
    """
    if action not in ("add", "update", "delete"):
        raise TaxonomyError(f"action invalide : {action}", 400)
    theme = theme.strip()
    if not theme:
        raise TaxonomyError("theme vide", 400)

    mapping = _load_mapping(profile)
    new_mapping = dict(mapping)
    if action == "delete":
        new_mapping.pop(theme, None)
    else:
        new_mapping[theme] = folder

    current_dest = classify_by_theme(theme, mapping)
    new_dest = classify_by_theme(theme, new_mapping)

    cache_path = _vision_cache_path(profile)
    if not cache_path.exists():
        return {
            "n_files_affected": 0,
            "cross_section_changes": 0,
            "examples": [],
            "current_dest": current_dest,
            "new_dest": new_dest,
        }
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "n_files_affected": 0,
            "cross_section_changes": 0,
            "examples": [],
            "current_dest": current_dest,
            "new_dest": new_dest,
        }

    n_affected = 0
    cross_section = 0
    examples: list[dict] = []
    for entry in cache.values():
        if not isinstance(entry, dict):
            continue
        result = entry.get("result") if isinstance(entry.get("result"), dict) else {}
        # Build the same candidate iteration as classify_combined would.
        candidates: list[str] = []
        themes_arr = result.get("themes")
        if isinstance(themes_arr, list) and themes_arr:
            for it in themes_arr:
                if not isinstance(it, dict):
                    continue
                t = str(it.get("theme") or "").strip()
                if not t:
                    continue
                conf = float(it.get("confidence") or 0.0)
                if conf < _MIN_CONFIDENCE:
                    continue
                candidates.append(t)
        else:
            t = str(result.get("theme") or "").strip()
            if t and float(result.get("confidence") or 0.0) >= _MIN_CONFIDENCE:
                candidates.append(t)
        if not candidates:
            continue

        # Resolve to first specific (with '/') destination, else first generic.
        def _resolve(cands: list[str], m: dict[str, str]) -> str | None:
            best_specific = None
            best_generic = None
            for c in cands:
                p = classify_by_theme(c, m)
                if not p:
                    continue
                if "/" in p:
                    return p
                if best_generic is None:
                    best_generic = p
            return best_specific or best_generic

        cur = _resolve(candidates, mapping)
        new = _resolve(candidates, new_mapping)
        if cur == new:
            continue
        n_affected += 1
        cur_sect = (cur or "").split("/")[0]
        new_sect = (new or "").split("/")[0]
        if cur_sect != new_sect:
            cross_section += 1
        if len(examples) < 5:
            examples.append({
                "title": result.get("title") or "(sans titre)",
                "current_dest": cur,
                "new_dest": new,
            })

    return {
        "n_files_affected": n_affected,
        "cross_section_changes": cross_section,
        "examples": examples,
        "current_dest": current_dest,
        "new_dest": new_dest,
    }


class TaxonomyError(Exception):
    """Domain error carrying an HTTP-friendly status code."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


_THEME_MAX = 200


def _validate_theme(theme: str) -> str:
    t = theme.strip()
    if not t:
        raise TaxonomyError("theme vide", 400)
    if len(t) > _THEME_MAX:
        raise TaxonomyError(f"theme trop long (>{_THEME_MAX})", 400)
    if any(ord(c) < 32 for c in t):
        raise TaxonomyError("caractères de contrôle interdits", 400)
    return t


def _validate_folder(profile: str, folder: str) -> str:
    f = folder.strip().strip("/")
    if not f:
        raise TaxonomyError("folder vide", 400)
    folders = set(_load_tree(profile))
    if f not in folders:
        raise TaxonomyError(f"dossier inexistant dans tree.yaml : {f}", 400)
    return f


def _check_lock_free(profile: str) -> None:
    lock = _lock_file(profile)
    if lock.exists():
        raise TaxonomyError(
            "modifications verrouillées (taxonomy.lock présent — run en cours ?)",
            423,
        )


def _backup_mapping(profile: str) -> Path | None:
    """Copy current theme_mapping.yaml to taxonomy-backups/<ts>.yaml.
    Returns the backup file path, or None if no current mapping exists.

    Microsecond precision in the timestamp avoids name collisions when
    multiple writes happen within the same second (notably undo, which
    backs up + restores in rapid succession).
    """
    src = _mapping_path(profile)
    if not src.exists():
        return None
    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dst = _backup_dir(profile) / f"theme_mapping-{ts}.yaml"
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    # Rotation: keep 20 most recent
    backups = sorted(
        _backup_dir(profile).glob("theme_mapping-*.yaml"),
        key=lambda p: p.name,
        reverse=True,
    )
    for old in backups[20:]:
        try:
            old.unlink()
        except OSError:
            pass
    return dst


def _write_mapping(profile: str, mapping: dict[str, str]) -> None:
    """Atomically write theme_mapping.yaml from a dict.

    Preserves insertion order via PyYAML default_flow_style=False.
    Note: comments in the original file are NOT preserved (Phase 1 trade-off).
    """
    path = _mapping_path(profile)
    payload = yaml.safe_dump(
        mapping,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def add_mapping(profile: str, theme: str, folder: str) -> dict:
    """Append a new theme→folder entry to theme_mapping.yaml.

    Raises TaxonomyError(409) if the key already exists. Use
    update_mapping() to change the target of an existing key.
    """
    lock = _locks[profile]
    with lock:
        _check_lock_free(profile)
        theme = _validate_theme(theme)
        folder = _validate_folder(profile, folder)

        mapping = _load_mapping(profile)
        if theme in mapping:
            raise TaxonomyError(
                f"mapping déjà présent pour '{theme}' (→ {mapping[theme]})",
                409,
            )

        backup = _backup_mapping(profile)
        mapping[theme] = folder
        _write_mapping(profile, mapping)
        reset_cache(profile)

        return {
            "ok": True,
            "theme": theme,
            "folder": folder,
            "backup": str(backup.relative_to(data.get_project_root())) if backup else None,
        }


def update_mapping(profile: str, theme: str, folder: str) -> dict:
    """Change the target folder of an existing theme key.

    Raises TaxonomyError(404) if the theme is not currently mapped.
    """
    lock = _locks[profile]
    with lock:
        _check_lock_free(profile)
        theme = _validate_theme(theme)
        folder = _validate_folder(profile, folder)

        mapping = _load_mapping(profile)
        if theme not in mapping:
            raise TaxonomyError(f"aucun mapping pour '{theme}'", 404)
        if mapping[theme] == folder:
            return {"ok": True, "theme": theme, "folder": folder, "unchanged": True, "backup": None}

        backup = _backup_mapping(profile)
        old_folder = mapping[theme]
        mapping[theme] = folder
        _write_mapping(profile, mapping)
        reset_cache(profile)

        return {
            "ok": True,
            "theme": theme,
            "folder": folder,
            "previous_folder": old_folder,
            "backup": str(backup.relative_to(data.get_project_root())) if backup else None,
        }


def delete_mapping(profile: str, theme: str) -> dict:
    """Remove an existing theme key from theme_mapping.yaml.

    Raises TaxonomyError(404) if the theme is not currently mapped.
    """
    lock = _locks[profile]
    with lock:
        _check_lock_free(profile)
        theme = _validate_theme(theme)

        mapping = _load_mapping(profile)
        if theme not in mapping:
            raise TaxonomyError(f"aucun mapping pour '{theme}'", 404)

        backup = _backup_mapping(profile)
        previous_folder = mapping.pop(theme)
        _write_mapping(profile, mapping)
        reset_cache(profile)

        return {
            "ok": True,
            "theme": theme,
            "previous_folder": previous_folder,
            "backup": str(backup.relative_to(data.get_project_root())) if backup else None,
        }


# ─── Tree editing (Phase 3 Étape A — create folder only) ─────────────────


_FOLDER_NAME_MAX = 100
# Allow letters (incl. accents), digits, spaces, and basic separators commonly
# used in the existing tree.yaml (- _ ( ) . &). Refuse everything that could
# break the filesystem or YAML parsing.
_FOLDER_NAME_RE = re.compile(r"^[A-Za-zÀ-ÿ0-9 _.\-&()]+$")


def _validate_folder_name(name: str) -> str:
    n = name.strip()
    if not n:
        raise TaxonomyError("nom de dossier vide", 400)
    if len(n) > _FOLDER_NAME_MAX:
        raise TaxonomyError(f"nom trop long (> {_FOLDER_NAME_MAX} chars)", 400)
    if "/" in n or "\\" in n:
        raise TaxonomyError("le nom ne peut pas contenir / ou \\", 400)
    if n in (".", ".."):
        raise TaxonomyError("nom de dossier invalide", 400)
    if n.startswith("."):
        raise TaxonomyError("le nom ne peut pas commencer par '.' (réservé aux dossiers cachés)", 400)
    if not _FOLDER_NAME_RE.match(n):
        raise TaxonomyError(
            "le nom contient des caractères non autorisés (autorisés : lettres, chiffres, espaces, -_.&())",
            400,
        )
    return n


def _backup_tree(profile: str) -> Path | None:
    """Snapshot tree.yaml before any edit. Same backup dir as theme_mapping
    but with a different filename prefix to keep the chains separate."""
    src = _tree_path(profile)
    if not src.exists():
        return None
    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dst = _backup_dir(profile) / f"tree-{ts}.yaml"
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    # Rotation: keep 20 most recent tree backups (separate from mapping)
    backups = sorted(
        _backup_dir(profile).glob("tree-*.yaml"),
        key=lambda p: p.name,
        reverse=True,
    )
    for old in backups[20:]:
        try:
            old.unlink()
        except OSError:
            pass
    return dst


def _write_tree(profile: str, folders: list[str]) -> None:
    """Atomically write tree.yaml from a sorted folder list."""
    path = _tree_path(profile)
    payload = yaml.safe_dump(
        {"folders": sorted(set(folders))},
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def create_folder(profile: str, parent: str, name: str) -> dict:
    """Add a new sub-folder to tree.yaml and create it on the filesystem.

    parent="" means top-level (e.g. a new section).
    Refuses if:
      - parent doesn't exist in tree.yaml (and parent != "")
      - name is invalid (empty, contains /, etc.)
      - new path already exists in tree.yaml (409)
      - filesystem path already exists (409)
      - taxonomy.lock present (423)
    """
    lock = _locks[profile]
    with lock:
        _check_lock_free(profile)
        name = _validate_folder_name(name)
        parent = (parent or "").strip().strip("/")

        folders = _load_tree(profile)
        if parent and parent not in folders:
            raise TaxonomyError(f"dossier parent inexistant : {parent}", 400)
        new_path = f"{parent}/{name}" if parent else name
        if new_path in folders:
            raise TaxonomyError(
                f"le dossier '{new_path}' existe déjà dans tree.yaml", 409,
            )

        target = _profile_target_path(profile)
        if target is None:
            raise TaxonomyError("profil sans target configuré", 400)
        fs_path = target / new_path
        if fs_path.exists():
            raise TaxonomyError(
                f"le dossier existe déjà sur le disque : {fs_path}", 409,
            )

        # Backup tree.yaml, create filesystem dir, update tree.yaml.
        backup = _backup_tree(profile)
        try:
            fs_path.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            raise TaxonomyError(f"erreur création dossier : {exc}", 500) from exc
        folders.append(new_path)
        _write_tree(profile, folders)
        reset_cache(profile)

        return {
            "ok": True,
            "path": new_path,
            "fs_path": str(fs_path),
            "backup": (
                str(backup.relative_to(data.get_project_root()))
                if backup else None
            ),
        }


def _change_folder_path(profile: str, old_path: str, new_path: str) -> dict:
    """Internal: change a folder's path from old_path to new_path.

    Cascades through tree.yaml + theme_mapping.yaml + filesystem.
    Shared engine for rename_folder (basename-only change) and
    move_folder (parent change). Caller is responsible for
    name-format validation before calling.

    Enforces structural rules only:
      - old_path exists in tree.yaml (400)
      - new_path != old_path (no-op short-circuit)
      - new_path is not a descendant of old_path (cycle, 400)
      - new_path absent from tree.yaml (409)
      - new filesystem path absent (409)
      - taxonomy.lock free (423)
    """
    lock = _locks[profile]
    with lock:
        _check_lock_free(profile)
        old_path = (old_path or "").strip().strip("/")
        new_path = (new_path or "").strip().strip("/")
        if not old_path or not new_path:
            raise TaxonomyError("path vide", 400)
        if old_path == new_path:
            return {"ok": True, "unchanged": True,
                    "old_path": old_path, "new_path": new_path}

        folders = _load_tree(profile)
        if old_path not in folders:
            raise TaxonomyError(f"dossier inexistant dans tree.yaml : {old_path}", 400)

        # Cycle: new_path must not be inside old_path
        if new_path.startswith(old_path + "/"):
            raise TaxonomyError(
                f"cycle interdit : '{new_path}' est dans '{old_path}'", 400,
            )
        if new_path in folders:
            raise TaxonomyError(
                f"le dossier '{new_path}' existe déjà dans tree.yaml", 409,
            )

        target = _profile_target_path(profile)
        if target is None:
            raise TaxonomyError("profil sans target configuré", 400)
        old_fs = target / old_path
        new_fs = target / new_path
        if new_fs.exists():
            raise TaxonomyError(
                f"chemin déjà présent sur le disque : {new_fs}", 409,
            )
        # Ensure destination parent dir exists on disk (move may target a
        # different parent — idempotent if already there)
        new_fs.parent.mkdir(parents=True, exist_ok=True)

        # Compute new tree (cascade prefix on old_path)
        prefix = old_path + "/"
        new_folders: list[str] = []
        n_renamed = 0
        for f in folders:
            if f == old_path:
                new_folders.append(new_path)
                n_renamed += 1
            elif f.startswith(prefix):
                new_folders.append(new_path + "/" + f[len(prefix):])
                n_renamed += 1
            else:
                new_folders.append(f)

        # Compute new mapping (cascade prefix on values)
        mapping = _load_mapping(profile)
        new_mapping: dict[str, str] = {}
        n_mappings_updated = 0
        for theme, folder in mapping.items():
            if folder == old_path:
                new_mapping[theme] = new_path
                n_mappings_updated += 1
            elif folder.startswith(prefix):
                new_mapping[theme] = new_path + "/" + folder[len(prefix):]
                n_mappings_updated += 1
            else:
                new_mapping[theme] = folder

        # Backups before any write
        tree_backup = _backup_tree(profile)
        mapping_backup = _backup_mapping(profile) if n_mappings_updated > 0 else None

        # FS rename first — abort cleanly on failure. Best-effort revert
        # if a subsequent YAML write blows up.
        fs_renamed = False
        if old_fs.exists():
            try:
                old_fs.rename(new_fs)
                fs_renamed = True
            except OSError as exc:
                raise TaxonomyError(f"erreur rename filesystem : {exc}", 500) from exc

        try:
            _write_tree(profile, new_folders)
            if n_mappings_updated > 0:
                _write_mapping(profile, new_mapping)
        except Exception:
            if fs_renamed:
                try:
                    new_fs.rename(old_fs)
                except OSError:
                    pass
            raise

        reset_cache(profile)
        return {
            "ok": True,
            "old_path": old_path,
            "new_path": new_path,
            "n_tree_entries_renamed": n_renamed,
            "n_mappings_updated": n_mappings_updated,
            "fs_renamed": fs_renamed,
            "tree_backup": (
                str(tree_backup.relative_to(data.get_project_root()))
                if tree_backup else None
            ),
            "mapping_backup": (
                str(mapping_backup.relative_to(data.get_project_root()))
                if mapping_backup else None
            ),
        }


def rename_folder(profile: str, old_path: str, new_name: str) -> dict:
    """Rename a folder (change basename, parent unchanged).

    Refuses when:
      - new_name is invalid (slash, '..', control chars) (400)
      - + all structural rules of _change_folder_path
    """
    old_path = (old_path or "").strip().strip("/")
    if not old_path:
        raise TaxonomyError("old_path vide", 400)
    new_name = _validate_folder_name(new_name)
    if "/" in new_name:
        raise TaxonomyError("le nouveau nom ne peut pas contenir /", 400)
    parent = "/".join(old_path.split("/")[:-1])
    new_path = f"{parent}/{new_name}" if parent else new_name
    return _change_folder_path(profile, old_path, new_path)


def move_folder(profile: str, old_path: str, new_parent: str) -> dict:
    """Move a folder under a different parent (basename unchanged).

    new_parent="" means the move target is the root (top-level).

    Refuses (in addition to the structural rules of _change_folder_path):
      - new_parent doesn't exist in tree.yaml (and isn't "") (400)
      - moving into own descendant (cycle — caught by _change_folder_path)
    """
    old_path = (old_path or "").strip().strip("/")
    if not old_path:
        raise TaxonomyError("old_path vide", 400)
    new_parent = (new_parent or "").strip().strip("/")
    if new_parent:
        folders = _load_tree(profile)
        if new_parent not in folders:
            raise TaxonomyError(f"dossier parent inexistant : {new_parent}", 400)
    basename = old_path.split("/")[-1]
    new_path = f"{new_parent}/{basename}" if new_parent else basename
    return _change_folder_path(profile, old_path, new_path)


def _backup_timestamp(path: Path) -> str:
    """Extract the timestamp portion of a backup filename.

    Works for both 'theme_mapping-YYYYMMDD-HHMMSS-FFFFFF.yaml' and
    'tree-YYYYMMDD-HHMMSS-FFFFFF.yaml' shapes.
    """
    stem = path.stem
    if "-" not in stem:
        return ""
    return stem.split("-", 1)[1]


def _restore_tree_from_backup(profile: str, backup_path: Path) -> dict:
    """Restore tree.yaml from backup_path, with filesystem reconciliation.

    Three classes of differences between current and backup get handled:
      1. Renamed folders (path-in-backup absent from current AND a same-
         parent path-in-current absent from backup) → filesystem renamed
         in reverse so tree.yaml and disk stay coherent.
      2. Folders only in current (added since backup) AND now empty on
         disk → rmdir.
      3. Folders only in current but non-empty → left as orphan on disk,
         reported in `kept_non_empty` for the caller.
    """
    backup_content = backup_path.read_text(encoding="utf-8")
    try:
        backup_data = yaml.safe_load(backup_content) or {}
    except yaml.YAMLError:
        backup_data = {}
    backup_folders = set(backup_data.get("folders", []) or [])
    current_folders = set(_load_tree(profile))

    added = current_folders - backup_folders        # in current, not backup
    removed = backup_folders - current_folders      # in backup, not current
    target = _profile_target_path(profile)

    deleted: list[str] = []
    kept_non_empty: list[str] = []
    fs_renamed: list[dict] = []

    if target is not None:
        # 1) Detect rename pairs (process shallowest paths first so a
        #    top-level rename absorbs all its descendants).
        added_remaining = set(added)
        removed_remaining = set(removed)
        for old in sorted(removed_remaining, key=lambda p: (p.count("/"), p)):
            if old not in removed_remaining:
                continue
            parent = "/".join(old.split("/")[:-1])
            candidates = [
                a for a in added_remaining
                if "/".join(a.split("/")[:-1]) == parent
            ]
            if len(candidates) != 1:
                continue  # ambiguous or no match — not a rename pair
            new = candidates[0]
            old_fs = target / old
            new_fs = target / new
            # Only perform the FS rename if it makes sense
            if new_fs.exists() and not old_fs.exists():
                try:
                    new_fs.rename(old_fs)
                    fs_renamed.append({"from": new, "to": old})
                except OSError:
                    pass  # leave it, will surface as orphan
            # Remove this pair + descendants from both sets
            old_prefix = old + "/"
            new_prefix = new + "/"
            removed_remaining -= {f for f in removed_remaining
                                  if f == old or f.startswith(old_prefix)}
            added_remaining -= {f for f in added_remaining
                                if f == new or f.startswith(new_prefix)}

        # 2-3) Remaining `added_remaining` are real adds — rmdir if empty
        for folder in sorted(added_remaining, reverse=True):
            fs = target / folder
            if not fs.exists():
                continue
            try:
                fs.rmdir()
                deleted.append(folder)
            except OSError:
                kept_non_empty.append(folder)

    # Restore tree.yaml content
    _tree_path(profile).write_text(backup_content, encoding="utf-8")
    return {
        "type": "tree",
        "restored_from": backup_path.name,
        "deleted_folders": deleted,
        "kept_non_empty": kept_non_empty,
        "fs_renamed": fs_renamed,
    }


def _restore_mapping_from_backup(profile: str, backup_path: Path) -> dict:
    content = backup_path.read_text(encoding="utf-8")
    _mapping_path(profile).write_text(content, encoding="utf-8")
    return {
        "type": "mapping",
        "restored_from": backup_path.name,
    }


def restore_last_backup(profile: str) -> dict:
    """Restore the most recent backup of EITHER theme_mapping.yaml or
    tree.yaml, picking by timestamp regardless of type.

    When restoring a tree backup, empty filesystem folders that were
    added since the backup are also removed (non-empty ones are kept
    with a clear notice).

    The restored backup file is deleted so that a second undo goes one
    step further back. No "redo" — the chain is consumed.

    Raises TaxonomyError(404) if no backup is available.
    """
    lock = _locks[profile]
    with lock:
        _check_lock_free(profile)
        backup_dir = _backup_dir(profile)
        all_backups: list[Path] = (
            list(backup_dir.glob("theme_mapping-*.yaml"))
            + list(backup_dir.glob("tree-*.yaml"))
        )
        if not all_backups:
            raise TaxonomyError("aucun backup disponible", 404)
        all_backups.sort(key=_backup_timestamp, reverse=True)
        last = all_backups[0]

        if last.name.startswith("theme_mapping-"):
            result = _restore_mapping_from_backup(profile, last)
        elif last.name.startswith("tree-"):
            result = _restore_tree_from_backup(profile, last)
        else:
            raise TaxonomyError(f"backup type inconnu : {last.name}", 500)

        try:
            last.unlink()
        except OSError:
            pass
        reset_cache(profile)
        result["ok"] = True
        return result
