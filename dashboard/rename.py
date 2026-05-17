"""Rename audit + suggestion engine for the dashboard.

Scans the lib, compares each filename to a *suggested* one rendered
from the LLM metadata in vision_cache + the profile's template config,
and categorizes the divergence so the user can prioritize.

Read-only at this stage (PR2). PR3+ will add the apply path.
"""

from __future__ import annotations

import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from dashboard import data
from lib import rename_template, vision_cache

# Defaults applied when profile.yaml has no `rename` block
DEFAULT_TEMPLATE = "{title}{ - author}"
DEFAULT_FALLBACK = "{title}"
DEFAULT_MAX_LENGTH = 180
DEFAULT_MIN_CONFIDENCE = 0.85   # only audit files whose title LLM is solid

# Suggestions for placeholder names the old renamer used as a fallback
# ("Title Author.pdf", "Title Author (3).pdf", etc.). We flag these
# aggressively because they're the clearest fix-it candidates.
PLACEHOLDER_RE = re.compile(
    r"^title\s*author(\s*\(\d+\))?$", re.IGNORECASE,
)

# Memory cache — invalidated on demand. The scan is ~5s on 18k files so
# caching matters for repeated panel interactions.
_audit_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()


# ─── Helpers ─────────────────────────────────────────────────────────────


def _profile_dir(profile: str) -> Path:
    return data.get_project_root() / "profiles" / profile


def _vision_cache_path(profile: str) -> Path:
    return _profile_dir(profile) / ".cache" / "vision_cache.json"


def _load_profile_yaml(profile: str) -> dict:
    pf = _profile_dir(profile) / "profile.yaml"
    if not pf.exists():
        return {}
    try:
        cfg = yaml.safe_load(pf.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _profile_target(profile: str) -> Path | None:
    cfg = _load_profile_yaml(profile)
    tgt = cfg.get("target")
    return Path(tgt) if tgt else None


def get_rename_config(profile: str) -> dict:
    """Resolve the rename config for a profile, mixing defaults + overrides
    from profile.yaml. Used by the UI to show what's in effect and by the
    audit to render suggestions.
    """
    cfg = _load_profile_yaml(profile).get("rename") or {}
    return {
        "template": cfg.get("template") or DEFAULT_TEMPLATE,
        "fallback": cfg.get("fallback") or DEFAULT_FALLBACK,
        "max_length": int(cfg.get("max_length") or DEFAULT_MAX_LENGTH),
        "sanitize": cfg.get("sanitize") or {},
        "min_title_confidence": float(
            cfg.get("min_title_confidence") or DEFAULT_MIN_CONFIDENCE),
    }


def _jaccard_3grams(a: str, b: str) -> float:
    """Trigram Jaccard similarity in [0, 1] on alphanumeric-only content.

    Lowercased, punctuation collapsed to whitespace, surrounded by
    padding spaces so prefix/suffix trigrams are real. Empty inputs
    yield 0.0 to avoid divide-by-zero.
    """
    def trigrams(s: str) -> set[str]:
        cleaned = re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
        if not cleaned:
            return set()
        norm = "  " + cleaned + "  "
        return {norm[i:i + 3] for i in range(len(norm) - 2)}
    ta, tb = trigrams(a), trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _categorize(current_stem: str, similarity: float) -> str:
    """Classify the gap between current and suggested. Order matters —
    placeholder check first (most actionable), then similarity thresholds.
    """
    if PLACEHOLDER_RE.match(current_stem.strip()):
        return "placeholder"
    if similarity < 0.4:
        return "divergent"
    if similarity < 0.7:
        return "minor_case"   # often just casing / word order / extra fluff
    return "ok"


def _extract_metadata(result: dict) -> dict:
    """Pull out the variables the template engine knows about."""
    if not isinstance(result, dict):
        return {}
    title = str(result.get("title") or "").strip()
    author = str(result.get("author") or "").strip()
    # Year + lang aren't currently in vision results but we'll thread them
    # through anyway so future enrichments don't require a code change.
    year = str(result.get("year") or "").strip()
    lang = str(result.get("language") or result.get("lang") or "").strip()
    return {
        "title": title,
        "author": author,
        "year": year,
        "lang": lang,
    }


# ─── Public API ──────────────────────────────────────────────────────────


def rename_audit(profile: str, force_reload: bool = False) -> dict:
    """Scan the lib + compute a suggested name per file + categorize.

    Returns::

        {
          "candidates": [{
              "rel_path", "current_name", "current_stem",
              "suggested_name", "suggested_stem",
              "similarity",          # 0..1 jaccard on 3-grams
              "category",            # placeholder | divergent | minor_case | ok
              "title", "author", "confidence",
              "issues": [str],       # template render warnings
              "used_fallback": bool,
          }],
          "stats": {n_total, n_with_title, n_placeholder, n_divergent,
                    n_minor_case, n_ok, n_render_failed, n_no_metadata},
          "config": {... resolved rename config ...},
        }
    """
    if not force_reload:
        with _cache_lock:
            cached = _audit_cache.get(profile)
        if cached is not None:
            return cached

    cfg = get_rename_config(profile)
    target = _profile_target(profile)
    if target is None or not target.exists():
        empty = {
            "candidates": [],
            "stats": _empty_stats(),
            "config": cfg,
        }
        with _cache_lock:
            _audit_cache[profile] = empty
        return empty

    # Load vision_cache once
    cache_path = _vision_cache_path(profile)
    cache: dict = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cache = {}
    if not isinstance(cache, dict):
        cache = {}

    # Profile-level LLM model + n_pages to derive cache keys
    profile_yaml = _load_profile_yaml(profile)
    model = (profile_yaml.get("llm") or {}).get("model") \
            or "Qwen/Qwen3-VL-32B-Instruct"
    n_pages = int((profile_yaml.get("defaults") or {}).get("pages") or 2)

    # Enumerate files
    file_list: list[tuple[str, str]] = []
    target_str = str(target)
    for root, dirs, files in os.walk(target_str):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.startswith(".") or not f.lower().endswith((".pdf", ".epub")):
                continue
            abs_path = os.path.join(root, f)
            try:
                rel = os.path.relpath(abs_path, target_str).replace("\\", "/")
            except ValueError:
                continue
            file_list.append((abs_path, rel))

    candidates: list[dict] = []
    stats = _empty_stats()
    stats["n_total"] = len(file_list)

    def _process(item: tuple[str, str]) -> dict | None:
        abs_path, rel = item
        filename = os.path.basename(abs_path)
        stem, ext = os.path.splitext(filename)
        ext = ext.lower() or ".pdf"

        # Cache lookup
        key = vision_cache.compute_cache_key(
            abs_path, model=model, n_pages=n_pages)
        result = vision_cache.lookup(cache, key) if key else None
        if not isinstance(result, dict):
            return {"_no_metadata": True}

        conf = float(result.get("confidence") or 0.0)
        if conf < cfg["min_title_confidence"]:
            return {"_low_confidence": True}

        meta = _extract_metadata(result)
        if not meta["title"]:
            return {"_no_metadata": True}

        rendered = rename_template.render_with_fallback(
            template=cfg["template"],
            fallback=cfg["fallback"],
            metadata=meta,
            extension=ext,
            sanitize_cfg=cfg["sanitize"],
            max_length=cfg["max_length"],
        )
        if not rendered.is_valid:
            return {"_render_failed": True, "issues": rendered.issues}

        similarity = _jaccard_3grams(stem, rendered.new_stem)
        category = _categorize(stem, similarity)

        return {
            "rel_path": rel,
            "current_name": filename,
            "current_stem": stem,
            "suggested_name": rendered.new_name,
            "suggested_stem": rendered.new_stem,
            "similarity": round(similarity, 3),
            "category": category,
            "title": meta["title"],
            "author": meta["author"],
            "confidence": round(conf, 2),
            "issues": rendered.issues,
            "used_fallback": rendered.used_fallback,
        }

    with ThreadPoolExecutor(max_workers=8) as ex:
        for r in ex.map(_process, file_list):
            if r is None:
                continue
            if r.get("_no_metadata"):
                stats["n_no_metadata"] += 1
                continue
            if r.get("_low_confidence"):
                stats["n_low_confidence"] += 1
                continue
            if r.get("_render_failed"):
                stats["n_render_failed"] += 1
                continue
            stats["n_with_title"] += 1
            cat = r["category"]
            stats[f"n_{cat}"] += 1
            candidates.append(r)

    # Sort: placeholder first, then divergent (worst sim), then minor, then ok
    cat_order = {"placeholder": 0, "divergent": 1, "minor_case": 2, "ok": 3}
    candidates.sort(key=lambda c: (cat_order.get(c["category"], 9),
                                    c["similarity"],
                                    c["rel_path"].lower()))

    result_dict = {
        "candidates": candidates,
        "stats": stats,
        "config": cfg,
    }
    with _cache_lock:
        _audit_cache[profile] = result_dict
    return result_dict


def _empty_stats() -> dict:
    return {
        "n_total": 0,
        "n_with_title": 0,
        "n_placeholder": 0,
        "n_divergent": 0,
        "n_minor_case": 0,
        "n_ok": 0,
        "n_render_failed": 0,
        "n_no_metadata": 0,
        # Files that DO have a cache entry but its confidence sits below
        # `min_title_confidence`. Tracked separately from `n_no_metadata`
        # so the UI can tell the user "1500 files are stuck under the
        # threshold" instead of conflating them with files that were
        # never scanned.
        "n_low_confidence": 0,
    }


def reset_cache(profile: str | None = None) -> None:
    """Drop the audit cache (one profile or all)."""
    with _cache_lock:
        if profile is None:
            _audit_cache.clear()
        else:
            _audit_cache.pop(profile, None)


# ─── Apply path (PR3) ────────────────────────────────────────────────────


class RenameError(Exception):
    """User-facing rename failure with an HTTP-style status hint."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# Refused chars in a basename — covers POSIX + Windows. Note we DO allow
# unicode chars freely; only the strictly problematic FS metachars are
# blocked. The frontend should already have run them through the same
# sanitize pipeline at preview time.
_REFUSED_BASENAME_CHARS = '/\\\x00<>:"|?*'
_MAX_BASENAME_LENGTH = 240   # POSIX is typically 255; keep margin


def _validate_new_basename(new_name: str) -> str:
    """Ensure `new_name` is a safe basename (no path parts, no FS metas).
    Raises RenameError on rejection."""
    name = (new_name or "").strip()
    if not name:
        raise RenameError("nom vide", 400)
    if any(ch in name for ch in _REFUSED_BASENAME_CHARS):
        raise RenameError(
            "nom contient des caractères interdits (/, \\, :, *, ?, \", <, >, |)",
            400)
    if name in (".", "..") or name.startswith(".."):
        raise RenameError("nom invalide (réservé : '.', '..')", 400)
    if len(name) > _MAX_BASENAME_LENGTH:
        raise RenameError(
            f"nom trop long (> {_MAX_BASENAME_LENGTH} chars)", 400)
    return name


def commit_rename(
    profile: str,
    rel_path: str,
    new_name: str,
    batch_id: str = "",
) -> dict:
    """Apply a rename on disk + record it in the journal.

    `rel_path` is the file's current relative path under target/; `new_name`
    is the new BASENAME (no directory part). The directory is preserved —
    use the tree-move flow if you want to relocate the file.

    Order of operations:
      1. Validate new_name (refused chars, length, dotfiles).
      2. Resolve abs_old + abs_new under target/. Reject if abs_new would
         be outside target (path traversal) or already exists (collision).
      3. os.rename(abs_old, abs_new).
      4. Journal append (record AFTER the FS op so a crash leaves no
         lying entry).
      5. Invalidate the rename audit cache + the dashboard's folder
         index + the categories text index (all keyed by rel_path).

    Raises RenameError(400/404/409) on the obvious failure modes;
    FileNotFoundError/FileExistsError from the OS layer are surfaced as
    well-typed errors.
    """
    new_name = _validate_new_basename(new_name)
    if not rel_path:
        raise RenameError("rel_path requis", 400)

    target = _profile_target(profile)
    if target is None or not target.exists():
        raise RenameError("profil sans target configuré", 400)

    abs_old = (target / rel_path).resolve()
    # Path traversal guard: make sure the resolved old path is still under
    # the profile's target.
    try:
        abs_old.relative_to(target.resolve())
    except ValueError as exc:
        raise RenameError("rel_path hors du target", 400) from exc
    if not abs_old.exists():
        raise RenameError(f"fichier introuvable : {rel_path}", 404)

    abs_new = abs_old.parent / new_name
    # Same-name no-op (true equality, byte-for-byte)
    if abs_new.name == abs_old.name:
        return {
            "ok": True,
            "unchanged": True,
            "old_rel_path": rel_path,
            "new_rel_path": rel_path,
            "new_name": new_name,
        }
    if abs_new.exists():
        # APFS / HFS+ are case-insensitive by default on macOS — renaming
        # ``foo.pdf`` → ``Foo.pdf`` makes ``abs_new.exists()`` True even
        # though the destination IS the source. ``samefile()`` compares
        # inodes so it tells case-only renames apart from real collisions.
        try:
            same = abs_new.samefile(abs_old)
        except OSError:
            same = False
        if not same:
            raise RenameError(
                f"un fichier porte déjà ce nom : {new_name}", 409)

    # FS rename
    try:
        os.rename(abs_old, abs_new)
    except OSError as exc:
        raise RenameError(f"échec du rename : {exc}", 500) from exc

    # Record AFTER the rename succeeded
    from lib import rename_journal
    profile_dir = _profile_dir(profile)
    record = rename_journal.append_rename(
        profile_dir,
        old_abs=str(abs_old),
        new_abs=str(abs_new),
        batch_id=batch_id,
    )

    # Invalidate dependent caches. They're all keyed by rel_path so a
    # filename change makes them stale. They rebuild lazily on next query.
    reset_cache(profile)
    try:
        from dashboard import taxonomy as _tx
        _tx.reset_cache(profile)
    except Exception:
        pass
    try:
        from dashboard import categories as _cat
        _cat.reset_cache(profile)
    except Exception:
        pass

    # ``target.resolve()`` is required because ``abs_new`` came from
    # ``abs_old.resolve().parent`` — on macOS ``/var`` is a symlink to
    # ``/private/var``, so an un-resolved ``target`` would not be a parent
    # of ``abs_new`` and ``relative_to`` would raise.
    new_rel = str(abs_new.relative_to(target.resolve())).replace("\\", "/")
    return {
        "ok": True,
        "old_rel_path": rel_path,
        "new_rel_path": new_rel,
        "new_name": new_name,
        "journal_entry": record,
    }
