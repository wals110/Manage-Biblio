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
from lib.classifier import classify_by_theme

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
    Returns the backup file path, or None if no current mapping exists."""
    src = _mapping_path(profile)
    if not src.exists():
        return None
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
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

    Raises TaxonomyError(409) if the key already exists. Phase 2 will
    expose an explicit update endpoint.
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
