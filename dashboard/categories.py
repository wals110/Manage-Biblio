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
    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]  # ms precision
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
