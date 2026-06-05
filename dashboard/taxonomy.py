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

import json
import os
import re
import shutil
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
# {profile -> {theme_lower: [{rel_path, title, current_folder, confidence}]}}
_theme_files_index_cache: dict[str, dict[str, list[dict]]] = {}
# {profile -> {predicted_folder: [{rel_path, title, current_folder, top_theme, top_confidence}]}}
# Computed from the file's TOP theme (max confidence) resolved via classify_by_theme.
# Depends on `theme_mapping.yaml` → invalidated together with the rest on writes.
_folder_predicted_index_cache: dict[str, dict[str, list[dict]]] = {}
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

    Si `profiles/<p>/.cache/theme-canon.json` existe (produit par
    `lib.theme_canon.build_canon_table`), les raw_themes sont résolus
    vers leur canonical avant cumul. Cumule donc les counts des
    variantes orthographiques d'un même concept. Sinon comportement
    historique (key = lowercase raw).
    """
    cache_path = _vision_cache_path(profile)
    if not cache_path.exists():
        return [], {"total_themes_llm": 0, "orphans": 0, "mapped": 0}
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], {"total_themes_llm": 0, "orphans": 0, "mapped": 0}

    # Import lazy — lib.theme_canon dépend de lib.theme_judge qui importe
    # langchain ; on évite de charger ça au démarrage du dashboard.
    from lib.theme_canon import canonicalize, load_canon_table
    canon_table = load_canon_table(profile)

    # Aggregate: per-canonical key count + conf sum + sample titles
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
            # Résolution canonisation → cumule les variantes orthographiques
            canonical = canonicalize(t, canon_table)
            key = canonical.lower()
            slot = agg.setdefault(
                key, {"theme": canonical, "count": 0, "conf_sum": 0.0}
            )
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


# ─── Folder theme breakdown (stable / incoming / outgoing) ───────────────


def _aggregate_themes_in_folder_files(
    profile: str, path: str,
) -> dict[str, dict]:
    """Pour chaque fichier physiquement présent dans `path`, agrège les
    thèmes détectés (via vision_cache). Retourne `{theme_lower: {count, label}}`
    où `label` est la forme canonique du thème (Title Case d'origine).

    Le matching fichier ↔ vision_cache se fait via `compute_content_key`
    (MD5 des head bytes) — survit aux renames. Filtre par
    `_MIN_CONFIDENCE` pour cohérence avec _aggregate_themes_llm.
    """
    from lib.thumbnail import compute_content_key
    target = _profile_target_path(profile)
    if target is None or not target.exists():
        return {}
    folder_path = (target / path) if path else target
    if not folder_path.is_dir():
        return {}

    cache_path = _vision_cache_path(profile)
    if not cache_path.exists():
        return {}
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    # Optionnel : canonisation Phase dédupli si theme-canon.json présent
    try:
        from lib.theme_canon import canonicalize, load_canon_table
        canon_table = load_canon_table(profile)
    except Exception:  # noqa: BLE001
        canon_table = None

        def canonicalize(t, _ct):  # type: ignore[no-redef]
            return t

    counts: dict[str, dict] = {}
    # Pas de récursion : seulement fichiers DIRECTEMENT dans le folder
    # (cohérent avec _scan_folder_counts qui sert au file_count du tree).
    for f in folder_path.iterdir():
        if not f.is_file():
            continue
        if f.suffix.lower() not in (".pdf", ".epub"):
            continue
        key = compute_content_key(f)
        if not key:
            continue
        entry = cache.get(key)
        if not entry:
            continue
        for theme_str, conf in _iter_themes({"x": entry}):
            if conf < _MIN_CONFIDENCE:
                continue
            canonical = canonicalize(theme_str, canon_table)
            kl = canonical.lower()
            slot = counts.setdefault(
                kl, {"label": canonical, "count": 0},
            )
            slot["count"] += 1
    return counts


def compute_folder_theme_breakdown(
    profile: str, path: str,
) -> dict[str, list[dict]]:
    """Décomposition des thèmes d'un dossier pour visualiser l'impact d'un
    futur reclassify.

    Renvoie `{stable, incoming, outgoing, summary}` où :

      - **stable** : thèmes mappés vers `path` ET détectés sur les fichiers
        actuels du dossier. Au reclassify, leurs fichiers RESTENT.
      - **incoming** : thèmes mappés vers `path` mais PAS détectés sur les
        fichiers actuels. Au reclassify, des fichiers ARRIVENT depuis ailleurs.
      - **outgoing** : thèmes détectés sur les fichiers actuels mais mappés
        ailleurs (ou orphelins). Au reclassify, ces fichiers PARTENT.

    Le résultat est utile pour répondre "que se passera-t-il au prochain
    reclassify sur ce dossier ?" — sans avoir à lancer un dry-run complet.
    """
    mapping = _load_mapping(profile)
    mapping_lower = {k.lower(): v for k, v in mapping.items()}

    # 1. Thèmes mappés VERS ce path (avec casse originale + count global)
    mapped_to_here_keys = [k for k, v in mapping.items() if v == path]
    mapped_to_here_lower = {k.lower() for k in mapped_to_here_keys}

    # 2. Thèmes effectivement présents dans les fichiers du dossier
    in_folder = _aggregate_themes_in_folder_files(profile, path)

    # 3. Univers global pour les counts d'incoming
    themes_llm, _stats = _aggregate_themes_llm(profile, mapping)
    counts_global = {t["theme"].lower(): t["count"] for t in themes_llm}

    stable: list[dict] = []
    outgoing: list[dict] = []
    for theme_l, slot in in_folder.items():
        target_folder = mapping_lower.get(theme_l)
        entry = {
            "theme": slot["label"],
            "count_in_folder": slot["count"],
        }
        if target_folder == path:
            stable.append(entry)
        elif target_folder is None:
            outgoing.append({**entry, "target": None, "reason": "orphan"})
        else:
            outgoing.append({
                **entry, "target": target_folder, "reason": "mapped_elsewhere",
            })

    incoming: list[dict] = []
    for theme in mapped_to_here_keys:
        if theme.lower() in in_folder:
            continue
        total = counts_global.get(theme.lower(), 0)
        incoming.append({
            "theme": theme,
            "count_expected": total,  # pas dans le folder = arrivera entièrement
        })

    stable.sort(key=lambda x: -x["count_in_folder"])
    incoming.sort(key=lambda x: -x["count_expected"])
    outgoing.sort(key=lambda x: -x["count_in_folder"])

    summary = {
        "path": path,
        "n_stable": len(stable),
        "n_incoming": len(incoming),
        "n_outgoing": len(outgoing),
        "files_stable": sum(s["count_in_folder"] for s in stable),
        "files_incoming": sum(s["count_expected"] for s in incoming),
        "files_outgoing": sum(s["count_in_folder"] for s in outgoing),
        "n_mapped_rules": len(mapped_to_here_lower),
    }
    return {
        "stable": stable,
        "incoming": incoming,
        "outgoing": outgoing,
        "summary": summary,
    }


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
            _theme_files_index_cache.clear()
            _folder_predicted_index_cache.clear()
        else:
            _snapshot_cache.pop(profile, None)
            _theme_files_index_cache.pop(profile, None)
            _folder_predicted_index_cache.pop(profile, None)
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


# ─── Theme → files reverse index ──────────────────────────────────────────
#
# vision_cache.json is keyed by an MD5 of the file's head bytes; there is
# no reverse mapping from theme → list-of-files. We build one lazily by
# walking the target filesystem, hashing each PDF/EPUB, and looking up the
# cache. The result is kept in-memory and invalidated together with the
# snapshot on writes.

_FILE_EXTS = (".pdf", ".epub")


def _build_indexes(profile: str) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """Walk the target FS, hash each file, return (theme_index, folder_index).

    - theme_index : {theme_lower: [{rel_path, title, current_folder, confidence}]}
                    one entry per (file × theme≥MIN_CONFIDENCE). Used by the
                    "Actuellement" tab (intersection theme × mapped folder).

    - folder_index: {predicted_folder: [{rel_path, title, current_folder,
                                          top_theme, top_confidence}]}
                    one entry per file, indexed by the predicted folder using
                    classify_by_theme(top_theme, mapping). Used by the
                    "Impact futur" tab — reflects what would actually happen
                    at the next reclassify (including substring matches).
    """
    target = _profile_target_path(profile)
    if target is None or not target.exists():
        return {}, {}

    cache_path = _vision_cache_path(profile)
    if not cache_path.exists():
        return {}, {}
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, {}
    if not isinstance(cache, dict):
        return {}, {}

    mapping = _load_mapping(profile)
    cfg = _load_profile_yaml(profile)
    model = (cfg.get("llm") or {}).get("model") or "Qwen/Qwen3-VL-32B-Instruct"
    n_pages = int((cfg.get("defaults") or {}).get("pages") or 2)

    # Enumerate every candidate file under target/
    file_list: list[tuple[str, str]] = []
    target_str = str(target)
    for root, dirs, files in os.walk(target_str):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.startswith(".") or not f.lower().endswith(_FILE_EXTS):
                continue
            abs_path = os.path.join(root, f)
            try:
                rel = os.path.relpath(abs_path, target_str)
            except ValueError:
                continue
            file_list.append((abs_path, rel.replace("\\", "/")))

    if not file_list:
        return {}, {}

    def _process(item: tuple[str, str]) -> tuple[str, str, list[tuple[str, float]]] | None:
        abs_path, rel = item
        key = vision_cache.compute_cache_key(abs_path, model=model, n_pages=n_pages)
        if not key:
            return None
        entry = cache.get(key)
        if not isinstance(entry, dict):
            return None
        result = entry.get("result")
        if not isinstance(result, dict):
            return None
        themes: list[tuple[str, float]] = []
        arr = result.get("themes")
        if isinstance(arr, list):
            for it in arr:
                if not isinstance(it, dict):
                    continue
                t = _normalize_theme(str(it.get("theme") or ""))
                c = float(it.get("confidence") or 0.0)
                if t and c >= _MIN_CONFIDENCE:
                    themes.append((t, c))
        legacy = _normalize_theme(str(result.get("theme") or ""))
        if legacy:
            c = float(result.get("confidence") or 0.0)
            if c >= _MIN_CONFIDENCE:
                themes.append((legacy, c))
        if not themes:
            return None
        title = str(result.get("title") or "").strip()
        return rel, title, themes

    from concurrent.futures import ThreadPoolExecutor

    theme_index: dict[str, list[dict]] = defaultdict(list)
    folder_index: dict[str, list[dict]] = defaultdict(list)
    with ThreadPoolExecutor(max_workers=8) as ex:
        for res in ex.map(_process, file_list):
            if res is None:
                continue
            rel, title, themes = res
            i = rel.rfind("/")
            current_folder = rel[:i] if i >= 0 else ""

            # Dedup themes per file (same theme can appear in `themes[]` AND
            # legacy `result.theme`) — keep the highest confidence.
            best: dict[str, float] = {}
            best_original: dict[str, str] = {}
            for theme, conf in themes:
                k = theme.lower()
                if conf > best.get(k, -1.0):
                    best[k] = conf
                    best_original[k] = theme
            for k, conf in best.items():
                theme_index[k].append({
                    "rel_path": rel,
                    "title": title,
                    "current_folder": current_folder,
                    "confidence": round(conf, 3),
                })

            # Predicted folder = classify_by_theme(top_theme, mapping).
            # Mirrors how the pipeline actually classifies a file at
            # reclassify time (top theme by confidence wins).
            top_theme_key = max(best, key=lambda k: best[k]) if best else None
            if top_theme_key:
                top_conf = best[top_theme_key]
                top_label = best_original[top_theme_key]
                predicted = classify_by_theme(top_label, mapping)
                if predicted:
                    folder_index[predicted].append({
                        "rel_path": rel,
                        "title": title,
                        "current_folder": current_folder,
                        "top_theme": top_label,
                        "top_confidence": round(top_conf, 3),
                    })

    for items in theme_index.values():
        items.sort(key=lambda r: (-r["confidence"], r["rel_path"].lower()))
    for items in folder_index.values():
        items.sort(key=lambda r: (-r["top_confidence"], r["rel_path"].lower()))
    return dict(theme_index), dict(folder_index)


def _get_indexes(profile: str) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """Return (theme_index, folder_index), built on first access and cached."""
    with _cache_lock:
        t_idx = _theme_files_index_cache.get(profile)
        f_idx = _folder_predicted_index_cache.get(profile)
    if t_idx is not None and f_idx is not None:
        return t_idx, f_idx
    # Build outside the lock (slow operation, multiple seconds on cold cache).
    t_idx, f_idx = _build_indexes(profile)
    with _cache_lock:
        _theme_files_index_cache[profile] = t_idx
        _folder_predicted_index_cache[profile] = f_idx
    return t_idx, f_idx


def theme_files(profile: str, theme: str, limit: int = 50) -> dict:
    """Return file lists for a mapped theme.

    Two views:
      - future:  files whose vision_cache contains this theme (where they
                 *would* go on the next reclassify, based on theme_mapping).
      - current: files **actually classified via this theme**, i.e. the
                 intersection of (files in the mapped folder) and (files
                 with this theme in vision_cache). A different theme also
                 mapped to the same folder will NOT pollute this list.

    Both lists are capped at `limit` items; the full per-folder breakdown
    is returned via `by_folder_future` / `by_folder_current` so the UI
    can render badges on the tree.
    """
    theme_norm = _normalize_theme(theme or "")
    theme_key = theme_norm.lower()
    empty = {
        "theme": theme,
        "n_future": 0,
        "n_current": 0,
        "future": [],
        "current": [],
        "by_folder_future": {},
        "by_folder_current": {},
        "mapped_folder": None,
        "limit": limit,
    }
    if not theme_key:
        return empty

    theme_idx, folder_idx = _get_indexes(profile)
    mapping = _load_mapping(profile)
    mapped_folder = classify_by_theme(theme_norm, mapping)

    # FUTURE = files predicted to land in `mapped_folder` at the next
    # reclassify, regardless of which theme key triggered the resolution.
    # This includes substring matches (e.g. "Optics and Light Physics" →
    # `physics` → 01-SCIENCES/PHYSIQUE) — what the pipeline actually does.
    future_all = folder_idx.get(mapped_folder, []) if mapped_folder else []
    by_folder_future: dict[str, int] = {}
    for f in future_all:
        cf = f["current_folder"] or "(racine)"
        by_folder_future[cf] = by_folder_future.get(cf, 0) + 1

    # CURRENT = files that this theme ACTUALLY contributed to classify:
    # (a) in the mapped folder right now AND (b) carry this theme in their
    # vision_cache (≥ MIN_CONFIDENCE). Strict exact-theme intersection,
    # distinguishes two themes mapped to the same folder.
    current_all: list[dict] = []
    by_folder_current: dict[str, int] = {}
    if mapped_folder:
        theme_files_list = theme_idx.get(theme_key, [])
        current_all = [f for f in theme_files_list
                       if f["current_folder"] == mapped_folder]
        current_all.sort(key=lambda r: r["rel_path"].lower())
        if current_all:
            by_folder_current[mapped_folder] = len(current_all)

    return {
        "theme": theme_norm,
        "n_future": len(future_all),
        "n_current": len(current_all),
        "future": future_all[:limit],
        "current": current_all[:limit],
        "by_folder_future": by_folder_future,
        "by_folder_current": by_folder_current,
        "mapped_folder": mapped_folder,
        "limit": limit,
    }


# ─── Reclassify dry-run (read-only impact projection) ────────────────────
#
# "If I ran klodo classify --execute now, what would move where?"
#
# Runs the real pipeline (step 1 theme_mapping + step 2 KeywordClassifier
# via categories.yaml) for every file in the lib, using the cached
# vision results. No new LLM calls, no FS writes. Step 3 (LLM Mapper,
# paid) is NOT simulated — those files are reported as no_prediction.


def reclassify_dryrun(
    profile: str,
    sample_size: int = 50,
    include_step2: bool = True,
) -> dict:
    """Project what a reclassify would move.

    Runs ``classify_combined`` (steps 1+2 of the pipeline) on every
    indexed file. ``include_step2=False`` short-circuits to step 1 only
    (cheap, useful for comparing what categories.yaml would rescue).

    Returns::

        {
          "ok": True,
          "stats": {
              "n_in_lib": int,
              "n_with_prediction": int,
              "n_no_prediction": int,
              "n_moving": int,
              "n_stable": int,
              "n_via_step1": int,        # resolved via theme_mapping
              "n_via_step2": int,        # resolved via KeywordClassifier
          },
          "by_destination": [...],
          "sample_moves": [{
              "rel_path": str, "from": str, "to": str,
              "top_theme": str, "confidence": float,
              "source": str,             # "LLM (theme)" / "Keyword" / ...
          }],
          "limits": {step2_included, no_llm_mapper, no_execute},
        }
    """
    from concurrent.futures import ThreadPoolExecutor

    from lib.classifier import classify_combined, load_keyword_classifier

    target = _profile_target_path(profile)
    if target is None or not target.exists():
        return _empty_dryrun(include_step2)

    mapping = _load_mapping(profile)

    classifier = None
    if include_step2:
        cat_path = _profile_dir(profile) / "categories.yaml"
        if cat_path.exists():
            classifier = load_keyword_classifier(str(cat_path))

    cache_path = _vision_cache_path(profile)
    cache: dict = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cache = {}
    if not isinstance(cache, dict):
        cache = {}

    cfg = _load_profile_yaml(profile)
    model = (cfg.get("llm") or {}).get("model") or "Qwen/Qwen3-VL-32B-Instruct"
    n_pages = int((cfg.get("defaults") or {}).get("pages") or 2)

    # Enumerate every candidate file
    target_str = str(target)
    file_list: list[tuple[str, str, str]] = []  # (abs, rel, basename)
    for root, dirs, files in os.walk(target_str):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.startswith(".") or not f.lower().endswith(_FILE_EXTS):
                continue
            abs_path = os.path.join(root, f)
            try:
                rel = os.path.relpath(abs_path, target_str).replace("\\", "/")
            except ValueError:
                continue
            file_list.append((abs_path, rel, f))

    def _process(item: tuple[str, str, str]) -> dict:
        abs_path, rel, filename = item
        i = rel.rfind("/")
        current_folder = rel[:i] if i >= 0 else ""
        # Lookup vision_cache
        key = vision_cache.compute_cache_key(
            abs_path, model=model, n_pages=n_pages)
        result = vision_cache.lookup(cache, key) if key else None
        if not isinstance(result, dict):
            result = {}
        # Extract top theme + score for display
        top_theme = ""
        top_conf = 0.0
        themes_arr = result.get("themes")
        if isinstance(themes_arr, list) and themes_arr:
            best = max(
                (t for t in themes_arr if isinstance(t, dict)),
                key=lambda t: float(t.get("confidence") or 0.0),
                default=None,
            )
            if best is not None:
                top_theme = str(best.get("theme") or "")
                top_conf = float(best.get("confidence") or 0.0)
        elif result.get("theme"):
            top_theme = str(result.get("theme") or "")
            top_conf = float(result.get("confidence") or 0.0)
        # Run the actual pipeline (step 1 + optional step 2; no LLM mapper)
        dest, score, source = classify_combined(
            result, filename, mapping,
            classifier=classifier,
            llm_mapper=None,
            pdf_path=abs_path,
        )
        return {
            "rel_path": rel,
            "current_folder": current_folder,
            "predicted_folder": dest,
            "source": source,
            "score": float(score) if score else 0.0,
            "top_theme": top_theme,
            "top_confidence": round(top_conf, 3),
        }

    # Parallel walk (8 workers — same as _build_indexes)
    processed: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for r in ex.map(_process, file_list):
            processed.append(r)

    # Aggregate
    n_in_lib = len(processed)
    n_moving = 0
    n_stable = 0
    n_step1 = 0
    n_step2 = 0
    n_no_pred = 0
    by_dest: dict[str, dict] = {}
    moves: list[dict] = []

    for r in processed:
        dest = r["predicted_folder"]
        src = r["source"]
        if not dest:
            n_no_pred += 1
            continue
        # Classify source family
        if src.startswith("Keyword"):
            n_step2 += 1
        else:
            # "LLM (theme)", "LLM (theme→refined)", "LLM (theme-generic)" → step 1
            n_step1 += 1

        if dest == r["current_folder"]:
            n_stable += 1
        else:
            n_moving += 1
            bucket = by_dest.setdefault(dest, {
                "folder": dest,
                "n_incoming": 0,
                "n_already_there": 0,
                "from": defaultdict(int),
            })
            bucket["n_incoming"] += 1
            bucket["from"][r["current_folder"] or "(racine)"] += 1
            if len(moves) < sample_size:
                moves.append({
                    "rel_path": r["rel_path"],
                    "from": r["current_folder"] or "(racine)",
                    "to": dest,
                    "top_theme": r["top_theme"],
                    "confidence": r["top_confidence"],
                    "source": src,
                })

    # n_already_there: stable files per destination
    for r in processed:
        dest = r["predicted_folder"]
        if dest and dest == r["current_folder"] and dest in by_dest:
            by_dest[dest]["n_already_there"] += 1

    # Top-5 sources per destination
    dests_out: list[dict] = []
    for d in by_dest.values():
        sources = sorted(d["from"].items(), key=lambda kv: -kv[1])[:5]
        dests_out.append({
            "folder": d["folder"],
            "n_incoming": d["n_incoming"],
            "n_already_there": d["n_already_there"],
            "from": dict(sources),
        })
    dests_out.sort(key=lambda r: (-r["n_incoming"], r["folder"].lower()))

    return {
        "ok": True,
        "stats": {
            "n_in_lib": n_in_lib,
            "n_with_prediction": n_step1 + n_step2,
            "n_no_prediction": n_no_pred,
            "n_moving": n_moving,
            "n_stable": n_stable,
            "n_via_step1": n_step1,
            "n_via_step2": n_step2,
        },
        "by_destination": dests_out,
        "sample_moves": moves,
        "limits": {
            "step2_included": include_step2 and classifier is not None,
            "no_llm_mapper": True,
            "no_execute": True,
        },
    }


def _empty_dryrun(include_step2: bool) -> dict:
    return {
        "ok": True,
        "stats": {"n_in_lib": 0, "n_with_prediction": 0, "n_no_prediction": 0,
                  "n_moving": 0, "n_stable": 0,
                  "n_via_step1": 0, "n_via_step2": 0},
        "by_destination": [],
        "sample_moves": [],
        "limits": {"step2_included": include_step2,
                   "no_llm_mapper": True, "no_execute": True},
    }


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

    # Best-effort explanation of the file's CURRENT location. The pipeline
    # doesn't log per-file decisions, so we reconstruct the most likely
    # reason by cross-referencing the detected themes with today's mapping.
    current_folder = out["file"]["current_folder"]
    reason: dict = {"kind": "unknown", "via_theme": None, "explanation": ""}
    if themes_out and current_folder:
        # 1. Exact: does any theme map to the current folder?
        exact_matches = [t for t in themes_out
                         if t["mapped_to"] == current_folder]
        # 2. Ancestor: does any theme map to an ancestor of current folder?
        ancestor_matches = [t for t in themes_out
                            if t["mapped_to"]
                            and current_folder.startswith(t["mapped_to"] + "/")]
        if exact_matches:
            sorted_m = sorted(exact_matches,
                              key=lambda r: -r["confidence"])
            top = sorted_m[0]
            top_overall = sorted(themes_out,
                                 key=lambda r: -r["confidence"])[0]
            if top["theme"] == top_overall["theme"]:
                reason["kind"] = "consistent"
                reason["explanation"] = (
                    f"Cohérent : le pipeline confirmerait ce dossier via le "
                    f"thème top « {top['theme']} » (conf {int(top['confidence']*100)}%)."
                )
            else:
                reason["kind"] = "secondary_theme"
                reason["explanation"] = (
                    f"Probablement classé via le thème secondaire "
                    f"« {top['theme']} » (conf {int(top['confidence']*100)}%) — "
                    f"le thème top « {top_overall['theme']} » pointe ailleurs."
                )
            reason["via_theme"] = top["theme"]
        elif ancestor_matches:
            sorted_a = sorted(ancestor_matches,
                              key=lambda r: -r["confidence"])
            top = sorted_a[0]
            reason["kind"] = "ancestor_match"
            reason["via_theme"] = top["theme"]
            reason["explanation"] = (
                f"Le thème « {top['theme']} » pointe vers « {top['mapped_to']} » — "
                f"le fichier est dans un sous-dossier, probablement raffiné par "
                f"une étape ultérieure (KeywordClassifier ou tri manuel)."
            )
        else:
            reason["kind"] = "no_theme_match"
            reason["explanation"] = (
                "Aucun thème détecté ne pointe vers ce dossier ni un ancêtre. "
                "Classement probable par KeywordClassifier (étape 2), LLM Mapper "
                "(étape 3) ou déplacement manuel."
            )
    elif not themes_out:
        reason["kind"] = "no_themes"
        reason["explanation"] = "Pas d'analyse LLM disponible pour ce fichier."
    out["classification_reason"] = reason

    # ── Cross-link to Rename audit (feature: card LLM also shows the
    # renaming diagnostic for this file so the user gets the full picture
    # in one place). Cheap: 1 template render + 1 trigram jaccard.
    out["rename_diagnostic"] = _rename_diagnostic_for(profile, rel_path, result)
    return out


def _rename_diagnostic_for(
    profile: str,
    rel_path: str,
    vision_result: dict,
) -> dict:
    """Best-effort filename audit for a single file. Returns the same
    shape as one entry from dashboard.rename.rename_audit, plus a
    ``kind`` discriminator the UI uses for icons/colors.

    Returns kind=``no_metadata`` when the title isn't usable.
    """
    from dashboard import rename as rename_mod
    from lib import rename_template as rt

    try:
        cfg = rename_mod.get_rename_config(profile)
    except Exception:
        return {"kind": "no_metadata"}

    title = str(vision_result.get("title") or "").strip()
    if not title:
        return {"kind": "no_metadata"}
    conf = float(vision_result.get("confidence") or 0.0)
    if conf < cfg["min_title_confidence"]:
        return {"kind": "low_confidence",
                "confidence": round(conf, 2),
                "min_required": cfg["min_title_confidence"]}

    filename = rel_path.split("/")[-1]
    stem, ext = filename.rsplit(".", 1) if "." in filename else (filename, "pdf")
    ext_with_dot = "." + ext.lower()

    metadata = {
        "title": title,
        "author": str(vision_result.get("author") or "").strip(),
        "year": str(vision_result.get("year") or "").strip(),
        "lang": str(vision_result.get("language")
                    or vision_result.get("lang") or "").strip(),
    }
    rendered = rt.render_with_fallback(
        template=cfg["template"], fallback=cfg["fallback"],
        metadata=metadata, extension=ext_with_dot,
        sanitize_cfg=cfg["sanitize"], max_length=cfg["max_length"],
    )
    if not rendered.is_valid:
        return {"kind": "render_failed", "issues": rendered.issues}

    similarity = rename_mod._jaccard_3grams(stem, rendered.new_stem)
    category = rename_mod._categorize(stem, similarity)
    return {
        "kind": "ok",
        "category": category,                 # placeholder | divergent | minor_case | ok
        "similarity": round(similarity, 3),
        "current_name": filename,
        "suggested_name": rendered.new_name,
        "used_fallback": rendered.used_fallback,
        "issues": rendered.issues,
    }


# ─── Thumbnails (cover + multipage lazy) ──────────────────────────────────


def _thumbnail_cache_dir(profile: str, abs_path: Path | str) -> Path | None:
    """Cache thumbnails at ``profile/.cache/thumbnails/<content_key>/``.

    ``content_key`` is derived from the file's head bytes (see
    ``lib.thumbnail.compute_content_key``), NOT from its rel_path —
    so a rename or a move doesn't invalidate the cached image.

    Returns ``None`` if the file can't be hashed (caller should
    surface "fichier introuvable" / "indisponible" rather than try to
    generate into a meaningless dir).
    """
    from lib.thumbnail import compute_content_key
    key = compute_content_key(abs_path)
    if not key:
        return None
    d = _profile_dir(profile) / ".cache" / "thumbnails" / key
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
    cache_dir = _thumbnail_cache_dir(profile, abs_path)
    if cache_dir is None:
        return None, "fichier illisible"
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


# ─── Dormant mapping audit ────────────────────────────────────────────────
#
# A "dormant" mapping is a key in theme_mapping.yaml that no file's top
# theme ever resolves through. Removing it would not change the placement
# of any file at the next reclassify. Useful for cleaning up the YAML.


def _resolve_with_key(
    theme: str,
    mapping: dict[str, str],
    keys_by_len: list[str],
) -> tuple[str | None, str | None]:
    """Like classify_by_theme but also returns the winning mapping key.

    Mirrors lib.classifier.classify_by_theme step-for-step. `keys_by_len`
    must be the mapping keys pre-sorted by len(key) DESC so the longest
    substring wins (kept outside to amortize across many calls).
    """
    if not theme or not mapping:
        return None, None
    theme_clean = re.sub(r"[\(\[][^\)\]]*[\)\]]", "", theme).strip()
    theme_lower = (theme_clean or theme).lower().strip()

    # 1. Exact (case-insensitive) — try cleaned form first, fallback raw
    for candidate in (theme_clean, theme):
        if not candidate:
            continue
        cand = candidate.lower().strip()
        for k in keys_by_len:
            if k.lower() == cand:
                return mapping[k], k

    # 2. Substring match, longest wins (keys already sorted DESC by length)
    for k in keys_by_len:
        k_lower = k.lower()
        if " " in k_lower:
            matched = k_lower in theme_lower
        else:
            pattern = r"(?<![a-zà-ÿ])" + re.escape(k_lower) + r"(?![a-zà-ÿ])"
            matched = bool(re.search(pattern, theme_lower))
        if matched:
            return mapping[k], k

    # 3. Reverse substring (theme in key) — also longest-first
    for k in keys_by_len:
        if theme_lower in k.lower():
            return mapping[k], k

    return None, None


def dormant_mappings(profile: str) -> dict:
    """Identify mapping keys whose removal wouldn't change any file's
    classification at the next reclassify.

    Returns:
        {
          dormant: [{key, folder}],         # mappings nobody uses
          active:  {key: file_count},       # mappings used by ≥1 file
          n_total, n_dormant, n_active,
        }
    """
    mapping = _load_mapping(profile)
    if not mapping:
        return {"dormant": [], "active": {}, "n_total": 0,
                "n_dormant": 0, "n_active": 0}

    keys_by_len = sorted(mapping.keys(), key=lambda k: -len(k))

    _, folder_index = _get_indexes(profile)

    keys_used: dict[str, int] = {}
    for items in folder_index.values():
        for item in items:
            top_theme = item.get("top_theme")
            if not top_theme:
                continue
            _, k = _resolve_with_key(top_theme, mapping, keys_by_len)
            if k is not None:
                keys_used[k] = keys_used.get(k, 0) + 1

    dormant: list[dict] = []
    for k, folder in mapping.items():
        if k not in keys_used:
            dormant.append({"key": k, "folder": folder})
    dormant.sort(key=lambda r: r["key"].lower())

    return {
        "dormant": dormant,
        "active": keys_used,
        "n_total": len(mapping),
        "n_dormant": len(dormant),
        "n_active": len(keys_used),
    }


def mapping_conflicts(profile: str) -> dict:
    """Extended mapping audit beyond strict dormants:

    - substring_conflicts: a key that is unused because a LONGER key
      contains its lowercased form as a substring (longest-substring
      rule from classify_by_theme always picks the longer one first).
      Helps explain *why* a key is dormant — the user can decide to
      delete the loser, or rename the winner more specifically.

    - duplicate_groups: keys pointing to the SAME destination folder.
      Not necessarily a bug (different LLM theme spellings can route
      to the same place), but worth surfacing so the user can decide
      to consolidate.

    Both lists are pure analysis (no writes). The dormant list is NOT
    returned here — use dormant_mappings() for that.
    """
    mapping = _load_mapping(profile)
    if not mapping:
        return {
            "substring_conflicts": [],
            "duplicate_groups": [],
            "stats": {"n_substring_conflicts": 0, "n_duplicate_groups": 0,
                      "n_keys_in_duplicates": 0},
        }

    keys_by_len = sorted(mapping.keys(), key=lambda k: -len(k))
    _, folder_index = _get_indexes(profile)
    keys_used: dict[str, int] = {}
    for items in folder_index.values():
        for item in items:
            top_theme = item.get("top_theme")
            if not top_theme:
                continue
            _, k = _resolve_with_key(top_theme, mapping, keys_by_len)
            if k is not None:
                keys_used[k] = keys_used.get(k, 0) + 1

    # 1. Substring conflicts — pair each dormant (loser) with the longer
    # key (winner) that eclipses it via longest-substring matching.
    keys_lower = {k: k.lower() for k in mapping}
    substring_conflicts: list[dict] = []
    for loser, loser_low in keys_lower.items():
        if loser in keys_used:
            continue  # only flag dormants — active keys are by def not eclipsed
        # Find the SHORTEST longer key that contains loser as substring AND
        # is itself active — that's the most direct culprit.
        best_winner: tuple[str, int] | None = None  # (winner, winner_len)
        for winner, winner_low in keys_lower.items():
            if winner == loser:
                continue
            if loser_low not in winner_low:
                continue
            if len(winner_low) <= len(loser_low):
                continue
            if keys_used.get(winner, 0) <= 0:
                continue
            if best_winner is None or len(winner_low) < best_winner[1]:
                best_winner = (winner, len(winner_low))
        if best_winner is not None:
            winner = best_winner[0]
            substring_conflicts.append({
                "loser": loser,
                "loser_folder": mapping[loser],
                "winner": winner,
                "winner_folder": mapping[winner],
                "winner_files": keys_used[winner],
            })
    substring_conflicts.sort(
        key=lambda r: (-r["winner_files"], r["loser"].lower()))

    # 2. Duplicate groups — keys sharing the same destination folder.
    by_folder: dict[str, list[str]] = defaultdict(list)
    for k, folder in mapping.items():
        by_folder[folder].append(k)
    duplicate_groups: list[dict] = []
    for folder, keys in by_folder.items():
        if len(keys) <= 1:
            continue
        keys_sorted = sorted(keys, key=str.lower)
        duplicate_groups.append({
            "folder": folder,
            "keys": keys_sorted,
            "n_keys": len(keys_sorted),
            "total_files": sum(keys_used.get(k, 0) for k in keys_sorted),
            "per_key": {k: keys_used.get(k, 0) for k in keys_sorted},
        })
    # Sort: groups with most keys first, then by folder name
    duplicate_groups.sort(key=lambda r: (-r["n_keys"], r["folder"].lower()))

    return {
        "substring_conflicts": substring_conflicts,
        "duplicate_groups": duplicate_groups,
        "stats": {
            "n_substring_conflicts": len(substring_conflicts),
            "n_duplicate_groups": len(duplicate_groups),
            "n_keys_in_duplicates": sum(g["n_keys"] for g in duplicate_groups),
        },
    }


def delete_mappings_bulk(profile: str, keys: list[str]) -> dict:
    """Delete multiple mapping keys in one transaction.

    A single backup is created before the batch; the lock is held for
    the whole operation. Unknown keys are reported in `not_found`
    rather than aborting (so a partial selection still proceeds).
    """
    if not isinstance(keys, list) or not keys:
        raise TaxonomyError("liste de thèmes vide", 400)
    keys = [_validate_theme(k) for k in keys]
    # Dedup while keeping order for the response
    seen = set()
    deduped = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            deduped.append(k)
    keys = deduped

    lock = _locks[profile]
    with lock:
        _check_lock_free(profile)
        mapping = _load_mapping(profile)
        deleted: list[dict] = []
        not_found: list[str] = []
        for k in keys:
            if k in mapping:
                deleted.append({"theme": k, "previous_folder": mapping.pop(k)})
            else:
                not_found.append(k)
        if not deleted:
            return {
                "ok": True,
                "deleted": [],
                "not_found": not_found,
                "n_deleted": 0,
                "backup": None,
            }
        backup = _backup_mapping(profile)
        _write_mapping(profile, mapping)
        reset_cache(profile)
        return {
            "ok": True,
            "deleted": deleted,
            "not_found": not_found,
            "n_deleted": len(deleted),
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


def delete_folder_preview(profile: str, path: str) -> dict:
    """Stats about what a `delete_folder(path)` would remove.

    Counts (read-only, no side effect):
      - n_files : files inside the folder + its descendants on disk
      - n_subfolders : tree.yaml entries under `path` (excluding path itself)
      - n_mappings : theme_mapping.yaml keys whose value points to `path`
        or any descendant of it
      - fs_size_bytes : total size on disk (informative)

    Returns {ok, path, n_files, n_subfolders, n_mappings, fs_size_bytes,
             is_empty (bool)}.
    """
    path = (path or "").strip().strip("/")
    if not path:
        raise TaxonomyError("path vide", 400)
    folders = _load_tree(profile)
    if path not in folders:
        raise TaxonomyError(f"dossier inexistant dans tree.yaml : {path}", 400)

    prefix = path + "/"
    n_subfolders = sum(1 for f in folders if f.startswith(prefix))
    mapping = _load_mapping(profile)
    n_mappings = sum(
        1 for v in mapping.values()
        if v == path or v.startswith(prefix)
    )

    target = _profile_target_path(profile)
    n_files = 0
    fs_size = 0
    if target is not None:
        fs = target / path
        if fs.is_dir():
            for root, dirs, files in os.walk(fs):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for fname in files:
                    if fname.startswith("."):
                        continue
                    n_files += 1
                    try:
                        fs_size += (Path(root) / fname).stat().st_size
                    except OSError:
                        pass

    return {
        "ok": True,
        "path": path,
        "n_files": n_files,
        "n_subfolders": n_subfolders,
        "n_mappings": n_mappings,
        "fs_size_bytes": fs_size,
        "is_empty": (n_files == 0 and n_subfolders == 0 and n_mappings == 0),
    }


def delete_folder(profile: str, path: str, force: bool = False) -> dict:
    """Delete a folder from tree.yaml + theme_mapping.yaml + filesystem.

    Two modes:
      - Safe (force=False, default): refuses if the folder is non-empty
        on disk OR contains subfolders OR has mappings pointing to it.
        Raises TaxonomyError(409) with the same stats as
        delete_folder_preview(), so the caller can show a confirmation
        modal and resubmit with force=True.
      - Force (force=True): performs shutil.rmtree() on the filesystem
        and cascades the deletion through tree.yaml (path + descendants)
        and theme_mapping.yaml (any value targeting path or descendants
        is removed — those themes become orphans).

    Backups: tree.yaml AND theme_mapping.yaml are snapshotted before
    write (rotated chain, recoverable via the Annuler button). The
    deleted files on disk are NOT recoverable through undo —
    shutil.rmtree() is irreversible.

    Refuses also when:
      - path absent from tree.yaml (400)
      - path is empty (400)
      - taxonomy.lock present (423)
    """
    import shutil

    lock = _locks[profile]
    with lock:
        _check_lock_free(profile)
        path = (path or "").strip().strip("/")
        if not path:
            raise TaxonomyError("path vide", 400)
        folders = _load_tree(profile)
        if path not in folders:
            raise TaxonomyError(f"dossier inexistant dans tree.yaml : {path}", 400)

        # Compute the same stats as the preview so we can refuse with them
        preview = delete_folder_preview(profile, path)
        if not preview["is_empty"] and not force:
            err = TaxonomyError(
                f"dossier non vide : {preview['n_files']} fichier(s), "
                f"{preview['n_subfolders']} sous-dossier(s), "
                f"{preview['n_mappings']} mapping(s) — force=True requis",
                409,
            )
            err.preview = preview  # type: ignore[attr-defined]
            raise err

        prefix = path + "/"
        new_folders = [f for f in folders if f != path and not f.startswith(prefix)]

        mapping = _load_mapping(profile)
        new_mapping = {}
        removed_mappings: list[str] = []
        for theme, folder in mapping.items():
            if folder == path or folder.startswith(prefix):
                removed_mappings.append(theme)
                continue
            new_mapping[theme] = folder

        # Backups before any write
        tree_backup = _backup_tree(profile)
        mapping_backup = _backup_mapping(profile) if removed_mappings else None

        # Filesystem first — irreversible. If it fails we abort cleanly.
        target = _profile_target_path(profile)
        fs_deleted = False
        if target is not None:
            fs = target / path
            if fs.exists():
                try:
                    if force:
                        shutil.rmtree(fs)
                    else:
                        fs.rmdir()  # raises OSError if non-empty (already excluded above)
                    fs_deleted = True
                except OSError as exc:
                    raise TaxonomyError(
                        f"erreur suppression filesystem : {exc}", 500
                    ) from exc

        try:
            _write_tree(profile, new_folders)
            if removed_mappings:
                _write_mapping(profile, new_mapping)
        except Exception:
            # We can't undelete the files (rmtree is destructive). The
            # YAMLs at least are still recoverable through their backups.
            raise

        reset_cache(profile)
        return {
            "ok": True,
            "path": path,
            "n_files_deleted": preview["n_files"] if force else 0,
            "n_subfolders_deleted": preview["n_subfolders"],
            "n_mappings_removed": len(removed_mappings),
            "fs_deleted": fs_deleted,
            "force": bool(force),
            "tree_backup": (
                str(tree_backup.relative_to(data.get_project_root()))
                if tree_backup else None
            ),
            "mapping_backup": (
                str(mapping_backup.relative_to(data.get_project_root()))
                if mapping_backup else None
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

        # Cascade categories.yaml — relaie le rename de chemin sur les
        # entries de l'étape 2 du pipeline (KeywordClassifier). Lazy import
        # pour éviter une dépendance circulaire à l'import et un échec sur
        # les profils sans categories.yaml (cascade_rename_target no-op).
        # Best-effort : si la cascade categories crash, on n'annule PAS le
        # rename — l'utilisateur verra les badges ⚠ orphelin et corrigera.
        cat_result: dict = {"n_entries_updated": 0, "n_merged": 0, "backup": None}
        try:
            from dashboard import categories as _cat
            cat_result = _cat.cascade_rename_target(profile, old_path, new_path)
        except Exception:  # noqa: BLE001
            pass

        reset_cache(profile)
        return {
            "ok": True,
            "old_path": old_path,
            "new_path": new_path,
            "n_tree_entries_renamed": n_renamed,
            "n_mappings_updated": n_mappings_updated,
            "n_categories_updated": cat_result.get("n_entries_updated", 0),
            "n_categories_merged": cat_result.get("n_merged", 0),
            "fs_renamed": fs_renamed,
            "tree_backup": (
                str(tree_backup.relative_to(data.get_project_root()))
                if tree_backup else None
            ),
            "mapping_backup": (
                str(mapping_backup.relative_to(data.get_project_root()))
                if mapping_backup else None
            ),
            "categories_backup": cat_result.get("backup"),
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


# ─── Audit log — list + targeted restore (feature I) ─────────────────────


def _human_age(seconds: float) -> str:
    """Concise relative age: '12s', '4m', '3h', '2j', '5j ago'."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}j"


def list_taxonomy_backups(profile: str) -> dict:
    """List both theme_mapping and tree backups, newest first.

    Each entry: {filename, kind, timestamp, size_bytes, age_seconds, age_human}.
    Kind is one of "mapping" / "tree" so the UI can render them distinctly.
    """
    backup_dir = _backup_dir(profile)
    if not backup_dir.exists():
        return {"backups": [], "n_total": 0}
    now = time.time()
    items: list[dict] = []
    for path in backup_dir.iterdir():
        if not path.is_file() or not path.suffix == ".yaml":
            continue
        if path.name.startswith("theme_mapping-"):
            kind = "mapping"
        elif path.name.startswith("tree-"):
            kind = "tree"
        else:
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        age = now - st.st_mtime
        items.append({
            "filename": path.name,
            "kind": kind,
            "timestamp": _backup_timestamp(path),
            "size_bytes": st.st_size,
            "age_seconds": int(age),
            "age_human": _human_age(age),
        })
    # Sort by timestamp DESC (newest first). Filename embeds it.
    items.sort(key=lambda r: r["timestamp"], reverse=True)
    return {"backups": items, "n_total": len(items)}


def restore_taxonomy_backup(profile: str, filename: str) -> dict:
    """Restore a SPECIFIC backup by its filename, regardless of position
    in the history. The current state is backed up first (under a new
    timestamp), so this operation is itself undoable via the standard
    Annuler button.

    Backups after the restored one are kept in place (no chain rewrite)
    — they remain available for further jumps.
    """
    if not filename or "/" in filename or "\\" in filename:
        raise TaxonomyError("nom de backup invalide", 400)

    lock = _locks[profile]
    with lock:
        _check_lock_free(profile)
        target = _backup_dir(profile) / filename
        if not target.exists() or not target.is_file():
            raise TaxonomyError(f"backup introuvable : {filename}", 404)

        # Snapshot the current state under a fresh timestamp BEFORE
        # restoring — so this restore can itself be undone.
        if filename.startswith("theme_mapping-"):
            pre_backup = _backup_mapping(profile)
            result = _restore_mapping_from_backup(profile, target)
        elif filename.startswith("tree-"):
            pre_backup = _backup_tree(profile)
            result = _restore_tree_from_backup(profile, target)
        else:
            raise TaxonomyError(
                f"type de backup non supporté : {filename}", 400,
            )

        reset_cache(profile)
        result["ok"] = True
        result["pre_restore_backup"] = (
            pre_backup.name if pre_backup is not None else None
        )
        return result


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


# ─── Per-file FS operations (feature/mapping-file-actions) ──────────────


class TaxonomyFileError(Exception):
    """User-facing error for file-level FS operations (delete / move).
    Carries an HTTP-style status code so the endpoint can map it
    directly. Calqued on dashboard.rename.RenameError."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


_TRASH_FOLDER_NAME = ".trash"
_TRASH_JOURNAL_NAME = "file-trash-journal.jsonl"


def _trash_root(profile: str) -> Path | None:
    """Trash root for a profile: ``<target>/.trash/``. Returns None if
    the profile is misconfigured."""
    target = _profile_target_path(profile)
    if target is None:
        return None
    return target / _TRASH_FOLDER_NAME


def _trash_journal_path(profile: str) -> Path:
    return _profile_dir(profile) / ".cache" / _TRASH_JOURNAL_NAME


def _is_safe_under_target(profile: str, rel_path: str) -> Path:
    """Resolve ``rel_path`` against the profile target and assert it
    stays inside. Returns the absolute Path on success, raises
    TaxonomyFileError on any failure.
    """
    if not rel_path:
        raise TaxonomyFileError("rel_path requis", 400)
    target = _profile_target_path(profile)
    if target is None or not target.exists():
        raise TaxonomyFileError("profil sans target configuré", 400)
    abs_path = (target / rel_path).resolve()
    try:
        abs_path.relative_to(target.resolve())
    except ValueError as exc:
        raise TaxonomyFileError(
            "rel_path hors du target", 400) from exc
    return abs_path


def _invalidate_post_fs_op(profile: str) -> None:
    """After a per-file FS op (delete/move), drop the taxonomy snapshot
    cache + the rename audit cache + the categories cache so the next
    reads rebuild from disk. The rename audit cache lacks a targeted
    patch path for delete (the file vanished, not just moved), and
    the taxonomy snapshot is keyed by counts — both need a full drop.
    """
    reset_cache(profile)
    try:
        from dashboard import rename as _rn
        _rn.reset_cache(profile)
    except Exception:
        pass
    try:
        from dashboard import categories as _cat
        _cat.reset_cache(profile)
    except Exception:
        pass


def soft_delete_file(profile: str, rel_path: str) -> dict:
    """Move a file from ``<target>/<rel_path>`` to
    ``<target>/.trash/<YYYYMMDD-HHMMSS>/<basename>``. Reversible via
    Finder. Appends an entry to ``.cache/file-trash-journal.jsonl``.

    Raises TaxonomyFileError on:
      400 — bad rel_path / outside target / file is itself inside .trash/
      404 — file not found on disk
      500 — FS error during move
    """
    abs_old = _is_safe_under_target(profile, rel_path)
    if not abs_old.exists():
        raise TaxonomyFileError(
            f"fichier introuvable : {rel_path}", 404)
    if not abs_old.is_file():
        raise TaxonomyFileError(
            "ce n'est pas un fichier", 400)

    # Refuse to trash items already in trash — would be an infinite-
    # nesting accident on a wrong click. Resolve both sides so the
    # comparison works on macOS where /var symlinks to /private/var.
    trash_root = _trash_root(profile)
    if trash_root is not None and trash_root.exists():
        trash_root_resolved = trash_root.resolve()
        if trash_root_resolved in abs_old.parents:
            raise TaxonomyFileError(
                "ce fichier est déjà dans la corbeille", 400)

    # Build the destination: .trash/<ts>/<basename>. Collision suffix
    # on basename (-1, -2, …) so two deletes within the same second
    # don't overwrite each other.
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    trash_dir = trash_root / ts
    trash_dir.mkdir(parents=True, exist_ok=True)

    basename = abs_old.name
    dest = trash_dir / basename
    suffix = 0
    stem, ext = os.path.splitext(basename)
    while dest.exists():
        suffix += 1
        dest = trash_dir / f"{stem}-{suffix}{ext}"

    try:
        shutil.move(str(abs_old), str(dest))
    except OSError as exc:
        raise TaxonomyFileError(
            f"échec du déplacement vers la corbeille : {exc}", 500
        ) from exc

    # Append journal entry. Best-effort: a failure here doesn't undo
    # the FS move (the file IS in .trash/, that's what matters).
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "original_rel_path": rel_path,
        "trash_rel_path": str(dest.relative_to(
            _profile_target_path(profile))).replace("\\", "/"),
    }
    try:
        journal = _trash_journal_path(profile)
        journal.parent.mkdir(parents=True, exist_ok=True)
        with open(journal, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass

    _invalidate_post_fs_op(profile)

    return {
        "ok": True,
        "original_rel_path": rel_path,
        "trash_rel_path": record["trash_rel_path"],
        "journal_entry": record,
    }


def _resolve_dest_folder(profile: str, dest_folder: str) -> Path:
    """Resolve ``dest_folder`` (a path relative to target) into an
    absolute path and assert it exists + is a directory + is inside
    target. Empty string = target root."""
    if dest_folder is None:
        raise TaxonomyFileError("dest_folder requis", 400)
    target = _profile_target_path(profile)
    if target is None or not target.exists():
        raise TaxonomyFileError("profil sans target configuré", 400)
    target_resolved = target.resolve()
    # Treat empty / "/" / "." as root
    cleaned = (dest_folder or "").strip().strip("/")
    abs_dest = (target / cleaned).resolve() if cleaned else target_resolved
    try:
        abs_dest.relative_to(target_resolved)
    except ValueError as exc:
        raise TaxonomyFileError(
            "dest_folder hors du target", 400) from exc
    if not abs_dest.exists():
        raise TaxonomyFileError(
            f"dossier destination introuvable : {dest_folder}", 404)
    if not abs_dest.is_dir():
        raise TaxonomyFileError(
            f"dest_folder n'est pas un dossier : {dest_folder}", 400)
    return abs_dest


def soft_delete_bulk(profile: str, rel_paths: list[str]) -> dict:
    """Bulk-soft-delete N files in one call. Each one goes through the
    same FS move + journal append as ``soft_delete_file``, but the
    cache invalidation runs ONCE at the end (instead of N times) and
    a per-second timestamp is reused — files trashed within the same
    call land under the same ``<target>/.trash/<ts>/`` folder, with
    -1/-2 suffixes on basename collisions.

    Best-effort: a per-item failure is captured in ``errors[]`` and
    does NOT abort the batch. Returns:

      {
        ok: True,
        n_total: int,
        n_deleted: int,
        n_errors: int,
        successes: [{rel_path, trash_rel_path}, ...],
        errors:    [{rel_path, error, status}, ...],
      }
    """
    if not isinstance(rel_paths, list) or not rel_paths:
        raise TaxonomyFileError("rel_paths vide ou invalide", 400)

    target = _profile_target_path(profile)
    if target is None or not target.exists():
        raise TaxonomyFileError("profil sans target configuré", 400)
    target_resolved = target.resolve()

    trash_root = _trash_root(profile)
    trash_root_resolved = None
    if trash_root is not None and trash_root.exists():
        trash_root_resolved = trash_root.resolve()

    # Single timestamp + journal handle for the whole batch — keeps
    # related deletions grouped on disk.
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    trash_dir = trash_root / ts
    trash_dir.mkdir(parents=True, exist_ok=True)
    journal_path = _trash_journal_path(profile)
    journal_path.parent.mkdir(parents=True, exist_ok=True)

    successes: list[dict] = []
    errors: list[dict] = []

    # De-dup the input list — the same rel_path twice in a batch is
    # a UI bug, not a user intent.
    seen: set[str] = set()

    with open(journal_path, "a", encoding="utf-8") as journal_fh:
        for raw in rel_paths:
            rel_path = str(raw or "").strip()
            if not rel_path:
                errors.append({"rel_path": rel_path,
                               "error": "rel_path manquant", "status": 400})
                continue
            if rel_path in seen:
                errors.append({"rel_path": rel_path,
                               "error": "doublon dans la batch",
                               "status": 400})
                continue
            seen.add(rel_path)

            try:
                abs_old = (target / rel_path).resolve()
                abs_old.relative_to(target_resolved)
            except (ValueError, OSError):
                errors.append({"rel_path": rel_path,
                               "error": "hors du target", "status": 400})
                continue
            if not abs_old.exists():
                errors.append({"rel_path": rel_path,
                               "error": "fichier introuvable",
                               "status": 404})
                continue
            if not abs_old.is_file():
                errors.append({"rel_path": rel_path,
                               "error": "pas un fichier", "status": 400})
                continue
            if (trash_root_resolved is not None
                    and trash_root_resolved in abs_old.parents):
                errors.append({"rel_path": rel_path,
                               "error": "déjà dans la corbeille",
                               "status": 400})
                continue

            basename = abs_old.name
            dest = trash_dir / basename
            suffix = 0
            stem, ext = os.path.splitext(basename)
            while dest.exists():
                suffix += 1
                dest = trash_dir / f"{stem}-{suffix}{ext}"

            try:
                shutil.move(str(abs_old), str(dest))
            except OSError as exc:
                errors.append({"rel_path": rel_path,
                               "error": f"échec FS : {exc}", "status": 500})
                continue

            trash_rel = str(dest.relative_to(target)).replace("\\", "/")
            record = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "original_rel_path": rel_path,
                "trash_rel_path": trash_rel,
            }
            try:
                journal_fh.write(
                    json.dumps(record, ensure_ascii=False) + "\n")
            except OSError:
                # Journal write failure doesn't undo the FS move.
                pass
            successes.append({
                "rel_path": rel_path,
                "trash_rel_path": trash_rel,
            })

    # Single cache drop for the whole batch — way cheaper than N drops.
    if successes:
        _invalidate_post_fs_op(profile)

    return {
        "ok": True,
        "n_total": len(rel_paths),
        "n_deleted": len(successes),
        "n_errors": len(errors),
        "successes": successes,
        "errors": errors,
    }


def move_file(profile: str, rel_path: str, dest_folder: str) -> dict:
    """Move a file from its current folder to ``dest_folder`` (kept
    basename). Reuses the audit cache invalidation path so the UI
    reflects the change without a full rebuild on the rename side
    (the taxonomy snapshot is rebuilt lazily — there's no targeted
    patch for it).

    Raises TaxonomyFileError on:
      400 — bad inputs / outside target / dest_folder not a dir
      404 — source file or dest folder missing
      409 — destination already has a file with that basename
      500 — FS error during move
    """
    abs_old = _is_safe_under_target(profile, rel_path)
    if not abs_old.exists():
        raise TaxonomyFileError(
            f"fichier introuvable : {rel_path}", 404)
    if not abs_old.is_file():
        raise TaxonomyFileError(
            "ce n'est pas un fichier", 400)

    abs_dest_dir = _resolve_dest_folder(profile, dest_folder)
    abs_new = abs_dest_dir / abs_old.name

    # Same-folder no-op
    if abs_new.resolve() == abs_old.resolve():
        return {
            "ok": True,
            "unchanged": True,
            "rel_path": rel_path,
            "new_rel_path": rel_path,
        }

    if abs_new.exists():
        # APFS case-insensitive guard: only refuse if it's a different
        # inode than the source.
        try:
            same = abs_new.samefile(abs_old)
        except OSError:
            same = False
        if not same:
            raise TaxonomyFileError(
                f"un fichier porte déjà ce nom dans la destination : "
                f"{abs_old.name}", 409)

    try:
        shutil.move(str(abs_old), str(abs_new))
    except OSError as exc:
        raise TaxonomyFileError(
            f"échec du déplacement : {exc}", 500) from exc

    target = _profile_target_path(profile)
    new_rel = str(abs_new.resolve().relative_to(
        target.resolve())).replace("\\", "/")

    _invalidate_post_fs_op(profile)

    return {
        "ok": True,
        "rel_path": rel_path,
        "new_rel_path": new_rel,
        "from_folder": str(abs_old.parent.relative_to(
            target.resolve())).replace("\\", "/"),
        "to_folder": str(abs_dest_dir.resolve().relative_to(
            target.resolve())).replace("\\", "/"),
    }


def compute_move_impact(
    profile: str, rel_path: str, dest_folder: str,
) -> dict:
    """Preview the impact of moving ``rel_path`` to ``dest_folder``:
    does it match what the classifier would predict at the next
    reclassify pass?

    Returns::

        {
          "current_folder": "01-SCIENCES/PHYSIQUE",
          "dest_folder":    "02-INFORMATIQUE/AI",
          "predicted_folder": "02-INFORMATIQUE/AI",  # or None if classifier failed
          "predicted_source": "LLM (theme)",
          "predicted_score":  0.87,
          "is_consistent":  true,
          "themes_used":    ["machine learning", ...]
        }

    Doesn't touch the FS. Pure preview, safe to call repeatedly.
    """
    abs_old = _is_safe_under_target(profile, rel_path)
    if not abs_old.exists():
        raise TaxonomyFileError(
            f"fichier introuvable : {rel_path}", 404)

    target = _profile_target_path(profile)
    current_folder = str(abs_old.parent.resolve().relative_to(
        target.resolve())).replace("\\", "/")

    # Validate dest_folder shape (existence is allowed to fail — the
    # user might be previewing toward a folder they'll create later;
    # but for safety we still require it to be a real directory).
    abs_dest = _resolve_dest_folder(profile, dest_folder)
    dest_rel = str(abs_dest.resolve().relative_to(
        target.resolve())).replace("\\", "/")

    # Now compute the predicted destination via the classifier — same
    # logic as get_file_metadata_full_pipeline, but we extract themes
    # too so the UI can name them in the warning message.
    cfg = _load_profile_yaml(profile)
    model = (cfg.get("llm") or {}).get("model") or "Qwen/Qwen3-VL-32B-Instruct"
    n_pages = int((cfg.get("defaults") or {}).get("pages") or 2)

    cache_path = _vision_cache_path(profile)
    vision_result: dict = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            key = vision_cache.compute_cache_key(
                str(abs_old), model=model, n_pages=n_pages)
            if key:
                looked = vision_cache.lookup(cache, key)
                if isinstance(looked, dict):
                    vision_result = looked
        except (OSError, json.JSONDecodeError):
            pass

    themes_used: list[str] = []
    raw_themes = vision_result.get("themes") if vision_result else None
    if isinstance(raw_themes, list):
        themes_used = [
            str(t.get("theme", "")).strip()
            for t in raw_themes if isinstance(t, dict) and t.get("theme")
        ]
    elif vision_result and vision_result.get("theme"):
        themes_used = [str(vision_result.get("theme", "")).strip()]

    predicted_folder: str | None = None
    predicted_source = ""
    predicted_score = 0.0
    if vision_result:
        mapping = _load_mapping(profile)
        categories_path = _profile_dir(profile) / "categories.yaml"
        classifier = (load_keyword_classifier(str(categories_path))
                      if categories_path.exists() else None)
        try:
            dest, score, source = classify_combined(
                vision_result, abs_old.name, mapping,
                classifier=classifier, llm_mapper=None,
                pdf_path=str(abs_old),
            )
            if dest:
                predicted_folder = dest
                predicted_source = source
                predicted_score = float(score) if score else 0.0
        except Exception:
            # Classifier failure is non-fatal here — we just report
            # "no prediction" and let the user decide.
            pass

    is_consistent = (predicted_folder is not None
                     and predicted_folder == dest_rel)

    return {
        "current_folder": current_folder,
        "dest_folder": dest_rel,
        "predicted_folder": predicted_folder,
        "predicted_source": predicted_source,
        "predicted_score": round(predicted_score, 2),
        "is_consistent": is_consistent,
        "themes_used": themes_used,
    }
