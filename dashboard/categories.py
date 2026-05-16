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
import os
import re
import shutil
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import yaml

from dashboard import data


# ─── Exceptions ───────────────────────────────────────────────────────────


class CategoriesError(Exception):
    """User-facing error from a categories.yaml write attempt.

    `status` mirrors HTTP semantics so endpoints can pass it through.
    """

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ─── Validation ───────────────────────────────────────────────────────────

_PATH_MAX = 200
_KEYWORD_MAX = 100
_GROUP_MAX = 60
# Path: letters (incl. accents), digits, separators commonly used in
# existing tree.yaml + slash for subfolders. Strict to avoid YAML/FS breakage.
_PATH_RE = re.compile(r"^[A-Za-zÀ-ÿ0-9 _.\-&()/]+$")
_GROUP_RE = re.compile(r"^[A-Za-zà-ÿ0-9 _-]+$", re.IGNORECASE)


def _validate_path(path: str) -> str:
    p = (path or "").strip()
    if not p:
        raise CategoriesError("chemin vide", 400)
    if len(p) > _PATH_MAX:
        raise CategoriesError(f"chemin trop long (> {_PATH_MAX} chars)", 400)
    if not _PATH_RE.match(p):
        raise CategoriesError(
            "chemin invalide (autorisés : lettres, chiffres, espaces, "
            "-_.&()/)",
            400,
        )
    if "//" in p or p.startswith("/") or p.endswith("/"):
        raise CategoriesError("chemin invalide (slashes en double ou bordants)", 400)
    return p


def _validate_keyword(kw: str) -> str:
    k = (kw or "").strip()
    if not k:
        raise CategoriesError("mot-clé vide", 400)
    if len(k) > _KEYWORD_MAX:
        raise CategoriesError(f"mot-clé trop long (> {_KEYWORD_MAX} chars)", 400)
    return k


def _validate_priority(p) -> int:
    try:
        n = int(p)
    except (TypeError, ValueError) as exc:
        raise CategoriesError("priorité invalide (entier requis)", 400) from exc
    if not (1 <= n <= 99):
        raise CategoriesError("priorité hors plage (1-99)", 400)
    return n


def _validate_group(name: str) -> str:
    n = (name or "").strip()
    if not n:
        raise CategoriesError("nom de groupe vide", 400)
    if len(n) > _GROUP_MAX:
        raise CategoriesError(f"nom de groupe trop long (> {_GROUP_MAX} chars)", 400)
    if not _GROUP_RE.match(n):
        raise CategoriesError(
            "nom de groupe invalide (autorisés : lettres, chiffres, espaces, -_)",
            400,
        )
    return n

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


# ─── Backup + write helpers ───────────────────────────────────────────────
#
# Same pattern as taxonomy theme_mapping: every write creates a timestamped
# copy of the YAML in .cache/categories-backups/, rotated to keep the last
# 20. Undo restores the most recent one.

_BACKUP_ROTATION = 20


def _backup_dir(profile: str) -> Path:
    d = _profile_dir(profile) / ".cache" / "categories-backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _backup_file(profile: str) -> Path | None:
    """Copy categories.yaml to a timestamped backup. Returns None if the
    source file doesn't exist (e.g. first write to a fresh profile)."""
    src = _categories_path(profile)
    if not src.exists():
        return None
    # Microsecond precision avoids name collisions when two writes happen
    # in rapid succession (e.g. restore = backup current + overwrite, both
    # within the same millisecond).
    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dst = _backup_dir(profile) / f"categories-{ts}.yaml"
    shutil.copy2(src, dst)
    # Rotate
    backups = sorted(_backup_dir(profile).glob("categories-*.yaml"))
    if len(backups) > _BACKUP_ROTATION:
        for old in backups[:-_BACKUP_ROTATION]:
            try:
                old.unlink()
            except OSError:
                pass
    return dst


def _load_mutable(profile: str) -> dict:
    """Parse categories.yaml for editing. Returns {} when missing.
    Raises CategoriesError on YAML syntax error (write would corrupt)."""
    p = _categories_path(profile)
    if not p.exists():
        return {}
    try:
        loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CategoriesError(f"categories.yaml invalide : {exc}", 500) from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise CategoriesError(
            "categories.yaml mal formé (racine doit être un dict)", 500,
        )
    return loaded


def _save_mutable(profile: str, payload: dict) -> None:
    """Atomic write: tmp file + rename."""
    p = _categories_path(profile)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True,
                       default_flow_style=False),
        encoding="utf-8",
    )
    tmp.replace(p)


def _find_entry(payload: dict, group: str, chemin: str) -> tuple[list, int] | None:
    """Locate (entries_list, index) of an entry by group+path. None if absent."""
    entries = payload.get(group)
    if not isinstance(entries, list):
        return None
    for i, e in enumerate(entries):
        if isinstance(e, dict) and str(e.get("chemin") or "").strip() == chemin:
            return entries, i
    return None


# ─── Write operations (Phase B) ───────────────────────────────────────────


def add_entry(
    profile: str,
    group: str,
    chemin: str,
    priorite: int = 5,
    mots_cles: list[str] | None = None,
) -> dict:
    """Create a new entry under `group`. Refuses if the path is already
    registered in that group (use update_entry to change priority/path)."""
    group = _validate_group(group)
    chemin = _validate_path(chemin)
    priorite = _validate_priority(priorite)
    cleaned_kw: list[str] = []
    seen: set[str] = set()
    for k in (mots_cles or []):
        kk = _validate_keyword(k)
        if kk.lower() in seen:
            continue
        seen.add(kk.lower())
        cleaned_kw.append(kk)

    with _locks[profile]:
        payload = _load_mutable(profile)
        entries = payload.get(group)
        if entries is None:
            entries = []
            payload[group] = entries
        elif not isinstance(entries, list):
            raise CategoriesError(
                f"groupe '{group}' déjà présent mais mal formé", 500,
            )
        for e in entries:
            if isinstance(e, dict) and str(e.get("chemin") or "").strip() == chemin:
                raise CategoriesError(
                    f"chemin '{chemin}' déjà présent dans '{group}'", 409,
                )
        backup = _backup_file(profile)
        entries.append({
            "chemin": chemin,
            "priorite": priorite,
            "mots_cles": cleaned_kw,
        })
        _save_mutable(profile, payload)
        reset_cache(profile)

    return {
        "ok": True,
        "group": group,
        "chemin": chemin,
        "priorite": priorite,
        "n_keywords": len(cleaned_kw),
        "backup": str(backup.relative_to(data.get_project_root())) if backup else None,
    }


def update_entry(
    profile: str,
    group: str,
    chemin: str,
    new_chemin: str | None = None,
    new_priorite: int | None = None,
) -> dict:
    """Modify an existing entry's path and/or priority. At least one of
    `new_chemin` / `new_priorite` must be provided."""
    group = _validate_group(group)
    chemin = _validate_path(chemin)
    if new_chemin is None and new_priorite is None:
        raise CategoriesError("rien à modifier", 400)
    if new_chemin is not None:
        new_chemin = _validate_path(new_chemin)
    if new_priorite is not None:
        new_priorite = _validate_priority(new_priorite)

    with _locks[profile]:
        payload = _load_mutable(profile)
        located = _find_entry(payload, group, chemin)
        if located is None:
            raise CategoriesError(
                f"entry '{chemin}' introuvable dans '{group}'", 404,
            )
        entries, idx = located
        entry = entries[idx]

        # Detect no-op
        if (new_chemin in (None, entry.get("chemin"))
                and new_priorite in (None, entry.get("priorite"))):
            return {"ok": True, "unchanged": True}

        # Refuse path collision when renaming
        if new_chemin is not None and new_chemin != entry.get("chemin"):
            for i, e in enumerate(entries):
                if i == idx:
                    continue
                if (isinstance(e, dict)
                        and str(e.get("chemin") or "").strip() == new_chemin):
                    raise CategoriesError(
                        f"chemin '{new_chemin}' déjà présent dans '{group}'", 409,
                    )

        backup = _backup_file(profile)
        previous = {"chemin": entry.get("chemin"), "priorite": entry.get("priorite")}
        if new_chemin is not None:
            entry["chemin"] = new_chemin
        if new_priorite is not None:
            entry["priorite"] = new_priorite
        _save_mutable(profile, payload)
        reset_cache(profile)

    return {
        "ok": True,
        "group": group,
        "previous": previous,
        "new_chemin": entry["chemin"],
        "new_priorite": entry["priorite"],
        "backup": str(backup.relative_to(data.get_project_root())) if backup else None,
    }


def delete_entry(profile: str, group: str, chemin: str) -> dict:
    """Remove an entry. The group is left in place even if it becomes empty
    (the user may want to re-add entries later)."""
    group = _validate_group(group)
    chemin = _validate_path(chemin)

    with _locks[profile]:
        payload = _load_mutable(profile)
        located = _find_entry(payload, group, chemin)
        if located is None:
            raise CategoriesError(
                f"entry '{chemin}' introuvable dans '{group}'", 404,
            )
        entries, idx = located
        backup = _backup_file(profile)
        removed = entries.pop(idx)
        _save_mutable(profile, payload)
        reset_cache(profile)

    return {
        "ok": True,
        "group": group,
        "chemin": chemin,
        "n_keywords_removed": len(removed.get("mots_cles") or []),
        "backup": str(backup.relative_to(data.get_project_root())) if backup else None,
    }


def add_keyword(profile: str, group: str, chemin: str, keyword: str) -> dict:
    """Append a keyword to an entry's `mots_cles` list. Dedups (case-insensitive
    on lowercased form) — silent no-op when already present."""
    group = _validate_group(group)
    chemin = _validate_path(chemin)
    keyword = _validate_keyword(keyword)

    with _locks[profile]:
        payload = _load_mutable(profile)
        located = _find_entry(payload, group, chemin)
        if located is None:
            raise CategoriesError(
                f"entry '{chemin}' introuvable dans '{group}'", 404,
            )
        entries, idx = located
        entry = entries[idx]
        mots = entry.get("mots_cles")
        if not isinstance(mots, list):
            mots = []
            entry["mots_cles"] = mots
        kw_lower = keyword.lower()
        existing_lower = {str(m).strip().lower() for m in mots if str(m).strip()}
        if kw_lower in existing_lower:
            return {"ok": True, "unchanged": True, "keyword": keyword}
        backup = _backup_file(profile)
        mots.append(keyword)
        _save_mutable(profile, payload)
        reset_cache(profile)

    return {
        "ok": True,
        "group": group,
        "chemin": chemin,
        "keyword": keyword,
        "n_keywords": len(mots),
        "backup": str(backup.relative_to(data.get_project_root())) if backup else None,
    }


def delete_keyword(profile: str, group: str, chemin: str, keyword: str) -> dict:
    """Remove a keyword from an entry. Match is case-insensitive on the
    trimmed form (so "  AI  " removes "ai")."""
    group = _validate_group(group)
    chemin = _validate_path(chemin)
    keyword = _validate_keyword(keyword)
    kw_lower = keyword.lower()

    with _locks[profile]:
        payload = _load_mutable(profile)
        located = _find_entry(payload, group, chemin)
        if located is None:
            raise CategoriesError(
                f"entry '{chemin}' introuvable dans '{group}'", 404,
            )
        entries, idx = located
        entry = entries[idx]
        mots = entry.get("mots_cles")
        if not isinstance(mots, list):
            raise CategoriesError("mot-clé introuvable", 404)
        new_mots = [m for m in mots
                    if str(m).strip().lower() != kw_lower]
        if len(new_mots) == len(mots):
            raise CategoriesError(f"mot-clé '{keyword}' introuvable", 404)
        backup = _backup_file(profile)
        entry["mots_cles"] = new_mots
        _save_mutable(profile, payload)
        reset_cache(profile)

    return {
        "ok": True,
        "group": group,
        "chemin": chemin,
        "keyword": keyword,
        "n_keywords": len(new_mots),
        "backup": str(backup.relative_to(data.get_project_root())) if backup else None,
    }


# ─── Dormant audit (Phase C) ──────────────────────────────────────────────
#
# An audit identifies keywords + entries that no file in the library could
# ever trigger via the KeywordClassifier (step 2). We build a single big
# lowercased "corpus" string from every file's title + filename + themes
# in vision_cache, then for each keyword check whether its lowercased
# form appears as a substring. ~2 MB corpus × 760 keywords ≈ <1 s.


def _vision_cache_path_local(profile: str) -> Path:
    """Avoid taxonomy import dependency — mirror the path used there."""
    return _profile_dir(profile) / ".cache" / "vision_cache.json"


def _profile_target_path_local(profile: str) -> Path | None:
    """Read `target:` from profile.yaml. None if profile or path is missing."""
    pf = _profile_dir(profile) / "profile.yaml"
    if not pf.exists():
        return None
    try:
        cfg = yaml.safe_load(pf.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None
    if not isinstance(cfg, dict):
        return None
    tgt = cfg.get("target")
    if not tgt:
        return None
    return Path(str(tgt))


def _build_searchable_corpus(profile: str) -> str:
    """Concatenate every file's title + filename + themes (lowercased,
    one per line) so a keyword can be detected via simple substring
    membership. Returns "" when no source is available."""
    parts: list[str] = []

    # Vision cache: titles + themes
    cache_path = _vision_cache_path_local(profile)
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cache = {}
        if isinstance(cache, dict):
            for entry in cache.values():
                if not isinstance(entry, dict):
                    continue
                result = entry.get("result")
                if not isinstance(result, dict):
                    continue
                title = str(result.get("title") or "").strip()
                if title:
                    parts.append(title.lower())
                arr = result.get("themes")
                if isinstance(arr, list):
                    for t in arr:
                        if not isinstance(t, dict):
                            continue
                        th = str(t.get("theme") or "").strip()
                        if th:
                            parts.append(th.lower())
                legacy = str(result.get("theme") or "").strip()
                if legacy:
                    parts.append(legacy.lower())

    # Filesystem: filenames (basename only — paths add false positives)
    target = _profile_target_path_local(profile)
    if target and target.exists():
        for root, dirs, files in os.walk(str(target)):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                if f.startswith(".") or not f.lower().endswith((".pdf", ".epub")):
                    continue
                parts.append(f.lower())

    return "\n".join(parts)


def dormant_audit(profile: str) -> dict:
    """Identify keywords + entries that no file's text could ever trigger.

    Returns::

        {
          "dormant_keywords": [{group, chemin, keyword}],
          "dormant_entries":  [{group, chemin, reason, n_keywords}],
          "stats": {
            "n_keywords_total":  int,
            "n_keywords_dormant": int,
            "n_entries_total":   int,
            "n_entries_dormant":  int,
            "corpus_size":       int,  # bytes
          },
        }

    `reason` is "no_keywords" when the entry literally has zero, and
    "all_keywords_dormant" when each of its keywords is itself dormant.
    """
    snap = build_snapshot(profile)
    corpus = _build_searchable_corpus(profile)

    dormant_keywords: list[dict] = []
    dormant_entries: list[dict] = []
    n_keywords_total = 0
    n_entries_total = 0

    for g in snap.get("groups", []):
        for e in g.get("entries", []):
            n_entries_total += 1
            mots = e.get("mots_cles") or []
            n_keywords_total += len(mots)
            if not mots:
                dormant_entries.append({
                    "group": g["group"],
                    "chemin": e["chemin"],
                    "reason": "no_keywords",
                    "n_keywords": 0,
                })
                continue
            active = 0
            for kw in mots:
                kw_lower = str(kw).strip().lower()
                if not kw_lower:
                    continue
                if kw_lower in corpus:
                    active += 1
                else:
                    dormant_keywords.append({
                        "group": g["group"],
                        "chemin": e["chemin"],
                        "keyword": kw,
                    })
            if active == 0:
                dormant_entries.append({
                    "group": g["group"],
                    "chemin": e["chemin"],
                    "reason": "all_keywords_dormant",
                    "n_keywords": len(mots),
                })

    dormant_keywords.sort(key=lambda r: (r["group"], r["chemin"], r["keyword"].lower()))
    dormant_entries.sort(key=lambda r: (r["group"], r["chemin"]))

    return {
        "dormant_keywords": dormant_keywords,
        "dormant_entries": dormant_entries,
        "stats": {
            "n_keywords_total": n_keywords_total,
            "n_keywords_dormant": len(dormant_keywords),
            "n_entries_total": n_entries_total,
            "n_entries_dormant": len(dormant_entries),
            "corpus_size": len(corpus),
        },
    }


# ─── Bulk delete operations (Phase C cleanup) ─────────────────────────────


def delete_keywords_bulk(profile: str, items: list[dict]) -> dict:
    """Delete multiple keywords across (possibly several) entries in a
    single backup. Each item: {group, chemin, keyword}.

    Items targeting missing entries / unknown keywords are reported in
    `not_found` rather than aborting the batch.
    """
    if not isinstance(items, list) or not items:
        raise CategoriesError("liste vide", 400)

    # Group by (group, chemin) so we touch each entry's mots_cles once
    by_entry: dict[tuple[str, str], list[str]] = defaultdict(list)
    for it in items:
        if not isinstance(it, dict):
            raise CategoriesError("élément invalide dans la liste", 400)
        group = _validate_group(it.get("group") or "")
        chemin = _validate_path(it.get("chemin") or "")
        keyword = _validate_keyword(it.get("keyword") or "")
        by_entry[(group, chemin)].append(keyword)

    with _locks[profile]:
        payload = _load_mutable(profile)
        deleted: list[dict] = []
        not_found: list[dict] = []
        for (group, chemin), kws in by_entry.items():
            located = _find_entry(payload, group, chemin)
            if located is None:
                for kw in kws:
                    not_found.append({"group": group, "chemin": chemin, "keyword": kw})
                continue
            entries, idx = located
            entry = entries[idx]
            mots = entry.get("mots_cles") or []
            kw_lower_set = {kw.lower() for kw in kws}
            new_mots = [m for m in mots
                        if str(m).strip().lower() not in kw_lower_set]
            removed = [str(m) for m in mots
                       if str(m).strip().lower() in kw_lower_set]
            entry["mots_cles"] = new_mots
            for kw in removed:
                deleted.append({"group": group, "chemin": chemin, "keyword": kw})
            # Track which requested keywords weren't found in this entry
            removed_lower = {kw.lower() for kw in removed
                             if isinstance(kw, str)}
            for kw in kws:
                if kw.lower() not in removed_lower:
                    not_found.append({"group": group, "chemin": chemin, "keyword": kw})
        if not deleted:
            return {"ok": True, "deleted": [], "not_found": not_found,
                    "n_deleted": 0, "backup": None}
        backup = _backup_file(profile)
        _save_mutable(profile, payload)
        reset_cache(profile)

    return {
        "ok": True,
        "deleted": deleted,
        "not_found": not_found,
        "n_deleted": len(deleted),
        "backup": str(backup.relative_to(data.get_project_root())) if backup else None,
    }


def delete_entries_bulk(profile: str, items: list[dict]) -> dict:
    """Delete multiple entries in a single backup. Each item: {group, chemin}."""
    if not isinstance(items, list) or not items:
        raise CategoriesError("liste vide", 400)

    by_group: dict[str, list[str]] = defaultdict(list)
    for it in items:
        if not isinstance(it, dict):
            raise CategoriesError("élément invalide dans la liste", 400)
        group = _validate_group(it.get("group") or "")
        chemin = _validate_path(it.get("chemin") or "")
        by_group[group].append(chemin)

    with _locks[profile]:
        payload = _load_mutable(profile)
        deleted: list[dict] = []
        not_found: list[dict] = []
        for group, chemins in by_group.items():
            wanted = set(chemins)
            entries = payload.get(group)
            if not isinstance(entries, list):
                for c in chemins:
                    not_found.append({"group": group, "chemin": c})
                continue
            seen: set[str] = set()
            kept: list = []
            for e in entries:
                ch = (isinstance(e, dict)
                      and str(e.get("chemin") or "").strip())
                if ch and ch in wanted:
                    seen.add(ch)
                    deleted.append({
                        "group": group,
                        "chemin": ch,
                        "n_keywords": len(e.get("mots_cles") or []),
                    })
                else:
                    kept.append(e)
            payload[group] = kept
            for c in chemins:
                if c not in seen:
                    not_found.append({"group": group, "chemin": c})
        if not deleted:
            return {"ok": True, "deleted": [], "not_found": not_found,
                    "n_deleted": 0, "backup": None}
        backup = _backup_file(profile)
        _save_mutable(profile, payload)
        reset_cache(profile)

    return {
        "ok": True,
        "deleted": deleted,
        "not_found": not_found,
        "n_deleted": len(deleted),
        "backup": str(backup.relative_to(data.get_project_root())) if backup else None,
    }


# ─── Audit log — list + targeted restore (feature I) ─────────────────────


def _human_age(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}j"


def _backup_timestamp(path: Path) -> str:
    """Extract timestamp from 'categories-YYYYMMDD-HHMMSS-FFF.yaml'."""
    stem = path.stem
    if "-" not in stem:
        return ""
    return stem.split("-", 1)[1]


def list_categories_backups(profile: str) -> dict:
    """List categories backups, newest first.

    Each entry: {filename, timestamp, size_bytes, age_seconds, age_human}.
    """
    import time as _time
    backup_dir = _backup_dir(profile)
    if not backup_dir.exists():
        return {"backups": [], "n_total": 0}
    now = _time.time()
    items: list[dict] = []
    for path in backup_dir.iterdir():
        if not path.is_file() or path.suffix != ".yaml":
            continue
        if not path.name.startswith("categories-"):
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        age = now - st.st_mtime
        items.append({
            "filename": path.name,
            "kind": "categories",
            "timestamp": _backup_timestamp(path),
            "size_bytes": st.st_size,
            "age_seconds": int(age),
            "age_human": _human_age(age),
        })
    items.sort(key=lambda r: r["timestamp"], reverse=True)
    return {"backups": items, "n_total": len(items)}


def restore_categories_backup(profile: str, filename: str) -> dict:
    """Restore a SPECIFIC categories backup by filename. The current
    state is backed up first so the operation is itself undoable."""
    if not filename or "/" in filename or "\\" in filename:
        raise CategoriesError("nom de backup invalide", 400)
    if not filename.startswith("categories-"):
        raise CategoriesError(
            "type de backup non supporté (attendu : categories-*)", 400,
        )

    with _locks[profile]:
        target = _backup_dir(profile) / filename
        if not target.exists() or not target.is_file():
            raise CategoriesError(f"backup introuvable : {filename}", 404)
        # Snapshot the current state before overwriting it
        pre_backup = _backup_file(profile)
        shutil.copy2(target, _categories_path(profile))
        reset_cache(profile)

    return {
        "ok": True,
        "restored_from": filename,
        "pre_restore_backup": pre_backup.name if pre_backup is not None else None,
    }


def undo(profile: str) -> dict:
    """Restore categories.yaml from the most recent backup. The pre-undo
    state is itself backed up so the operation is reversible by re-undoing."""
    backups = sorted(_backup_dir(profile).glob("categories-*.yaml"))
    if not backups:
        raise CategoriesError("aucune modification à annuler", 404)
    latest = backups[-1]

    with _locks[profile]:
        # Save the current state before overwriting so undo is reversible.
        pre_undo = _backup_file(profile)
        shutil.copy2(latest, _categories_path(profile))
        # The backup we restored from is no longer relevant — drop it.
        try:
            latest.unlink()
        except OSError:
            pass
        reset_cache(profile)

    return {
        "ok": True,
        "restored_from": latest.name,
        "pre_undo_backup": (str(pre_undo.relative_to(data.get_project_root()))
                            if pre_undo else None),
    }
