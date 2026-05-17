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


def _invalidate_dependent_caches(profile: str) -> None:
    """After any FS rename (apply OR undo), drop the rename / taxonomy /
    categories caches so subsequent reads rebuild from the current state.
    Failures to import a sibling module are swallowed: the rename
    succeeded, a missing cache is at worst a stale read.
    """
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
    _invalidate_dependent_caches(profile)

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


# ─── Journal viewing + undo (PR4) ────────────────────────────────────────


def _path_under(target_resolved: Path, abs_path: str) -> bool:
    """True if abs_path resolves under target_resolved. Used to refuse
    journal entries that point outside the current profile (e.g. a
    crafted payload trying to undo a rename in a different profile)."""
    try:
        Path(abs_path).resolve().relative_to(target_resolved)
        return True
    except (ValueError, OSError):
        return False


def get_journal(profile: str, limit: int = 200) -> dict:
    """Return the rename journal for the UI, newest first, with the
    per-record `is_undo` / `is_undone` flags + relative paths against
    the profile's target so the modal can show short names.

    Also returns ``batches``: the list_batches() grouping (used by PR4
    bulk undo). Does NOT touch the filesystem.
    """
    from lib import rename_journal
    profile_dir = _profile_dir(profile)
    target = _profile_target(profile)
    target_resolved = (target.resolve()
                       if (target and target.exists()) else None)

    all_records = rename_journal.read_journal(profile_dir)
    # A record is "already undone" if a later inverse op (batch starting
    # with "undo") has its `old` field == this record's `new` field.
    inverse_olds = {
        r["old"] for r in all_records
        if (r.get("batch") or "").startswith("undo")
    }

    def _rel(abs_path: str) -> str:
        if not target_resolved:
            return abs_path
        try:
            return str(Path(abs_path).resolve().relative_to(
                target_resolved)).replace("\\", "/")
        except (ValueError, OSError):
            return abs_path

    # Walk newest first
    enriched: list[dict] = []
    for r in reversed(all_records):
        batch = r.get("batch") or ""
        is_undo = batch.startswith("undo")
        is_undone = r.get("new") in inverse_olds
        enriched.append({
            **r,
            "old_rel": _rel(r.get("old", "")),
            "new_rel": _rel(r.get("new", "")),
            "is_undo": is_undo,
            "is_undone": is_undone,
        })

    # Cap *after* enrichment so the freshest entries are kept
    enriched = enriched[:limit]

    batches = rename_journal.list_batches(profile_dir)

    return {
        "records": enriched,
        "batches": batches,
        "n_total": len(all_records),
        "n_active": sum(1 for r in enriched
                        if not r["is_undo"] and not r["is_undone"]),
    }


def commit_rename_bulk(
    profile: str,
    items: list[dict],
    batch_id: str = "",
) -> dict:
    """Apply N renames as a single batch.

    ``items`` is ``[{"rel_path": str, "new_name": str}, ...]``. A shared
    ``batch_id`` (auto-generated if not provided) tags every record in
    the journal so the whole batch can be undone in one call.

    Failure semantics: best-effort. Each item is attempted independently;
    a per-item failure does NOT abort the batch. The result lists
    successes and per-item errors so the UI can show what happened.

    Cache invalidation happens once at the end (instead of once per
    item) — small but meaningful for the 18k-file lib.
    """
    if not isinstance(items, list) or not items:
        raise RenameError("items vide ou invalide", 400)

    from lib import rename_journal
    if not batch_id:
        batch_id = rename_journal.generate_batch_id()

    target = _profile_target(profile)
    if target is None or not target.exists():
        raise RenameError("profil sans target configuré", 400)

    # We bypass the full commit_rename() to skip its per-call cache
    # invalidation; everything else (path traversal, name validation,
    # case-insensitive collision, journal append) we replicate inline.
    target_resolved = target.resolve()
    profile_dir = _profile_dir(profile)

    successes: list[dict] = []
    errors: list[dict] = []

    for item in items:
        rel_path = (item or {}).get("rel_path") or ""
        raw_new = (item or {}).get("new_name") or ""
        if not rel_path:
            errors.append({"rel_path": rel_path, "error": "rel_path manquant",
                           "status": 400})
            continue
        try:
            new_name = _validate_new_basename(raw_new)
        except RenameError as exc:
            errors.append({"rel_path": rel_path, "error": str(exc),
                           "status": exc.status})
            continue

        abs_old = (target / rel_path).resolve()
        try:
            abs_old.relative_to(target_resolved)
        except ValueError:
            errors.append({"rel_path": rel_path,
                           "error": "rel_path hors du target", "status": 400})
            continue
        if not abs_old.exists():
            errors.append({"rel_path": rel_path,
                           "error": "fichier introuvable", "status": 404})
            continue

        abs_new = abs_old.parent / new_name
        if abs_new.name == abs_old.name:
            successes.append({
                "rel_path": rel_path,
                "new_rel_path": rel_path,
                "new_name": new_name,
                "unchanged": True,
            })
            continue

        if abs_new.exists():
            try:
                same = abs_new.samefile(abs_old)
            except OSError:
                same = False
            if not same:
                errors.append({"rel_path": rel_path,
                               "error": f"collision avec {new_name}",
                               "status": 409})
                continue

        try:
            os.rename(abs_old, abs_new)
        except OSError as exc:
            errors.append({"rel_path": rel_path,
                           "error": f"échec rename : {exc}", "status": 500})
            continue

        record = rename_journal.append_rename(
            profile_dir,
            old_abs=str(abs_old),
            new_abs=str(abs_new),
            batch_id=batch_id,
        )
        new_rel = str(abs_new.relative_to(target_resolved)).replace("\\", "/")
        successes.append({
            "rel_path": rel_path,
            "new_rel_path": new_rel,
            "new_name": new_name,
            "journal_entry": record,
        })

    # Single cache invalidation for the whole batch
    if successes:
        _invalidate_dependent_caches(profile)

    return {
        "ok": True,
        "batch_id": batch_id,
        "n_total": len(items),
        "n_renamed": len(successes),
        "n_errors": len(errors),
        "successes": successes,
        "errors": errors,
    }


def undo_batch_for_profile(profile: str, batch_id: str) -> dict:
    """Reverse every rename of a batch in reverse order.

    Wraps ``lib.rename_journal.undo_batch`` to add the cache-invalidation
    step and translate FS-level errors into RenameError. Returns the
    journal's per-record undone / errors summary unchanged so the UI can
    show what was reverted vs what failed.
    """
    if not batch_id:
        raise RenameError("batch_id manquant", 400)

    target = _profile_target(profile)
    if target is None or not target.exists():
        raise RenameError("profil sans target configuré", 400)

    from lib import rename_journal
    profile_dir = _profile_dir(profile)
    try:
        summary = rename_journal.undo_batch(profile_dir, batch_id)
    except FileNotFoundError as exc:
        raise RenameError(str(exc), 404) from exc

    # Even partial success warrants a cache invalidation (the FS moved)
    if summary.get("n_undone", 0) > 0:
        _invalidate_dependent_caches(profile)

    return {"ok": True, **summary}


def undo_single_rename(
    profile: str, ts: str, old_abs: str, new_abs: str,
) -> dict:
    """Reverse one rename identified by (ts, old, new). The record must
    exist in the journal AND both paths must be under the profile's
    target. Appends an "undo-*" record so the audit trail stays
    append-only.

    Raises RenameError(400/404/409/500) on the expected failure modes:
      400 — paths outside target / profile misconfigured
      404 — record not in journal, or file not found on disk
      409 — already undone, or destination is occupied
    """
    if not ts:
        raise RenameError("ts manquant", 400)
    if not old_abs or not new_abs:
        raise RenameError("old/new manquants", 400)

    target = _profile_target(profile)
    if target is None or not target.exists():
        raise RenameError("profil sans target configuré", 400)
    target_resolved = target.resolve()

    if not (_path_under(target_resolved, old_abs)
            and _path_under(target_resolved, new_abs)):
        raise RenameError(
            "Chemins hors du target du profil — undo refusé", 400)

    from lib import rename_journal
    profile_dir = _profile_dir(profile)

    # Look up the record. We match on (ts, old, new) so the caller can
    # pass back exactly what the GET endpoint returned.
    all_records = rename_journal.read_journal(profile_dir)
    match: dict | None = None
    for r in all_records:
        if (r.get("ts") == ts
                and r.get("old") == old_abs
                and r.get("new") == new_abs):
            match = r
            break
    if match is None:
        raise RenameError(
            "Entrée introuvable dans le journal", 404)

    # Already undone? An inverse op exists if a later "undo-*" record
    # has its old field equal to this record's new field.
    if any((r.get("batch") or "").startswith("undo")
           and r.get("old") == match["new"]
           for r in all_records):
        raise RenameError("Cette entrée a déjà été annulée", 409)

    try:
        inverse = rename_journal.undo_record(profile_dir, match)
    except FileNotFoundError as exc:
        raise RenameError(
            f"Fichier introuvable sur disque : {exc}", 404) from exc
    except FileExistsError as exc:
        raise RenameError(
            f"Collision pendant l'undo : {exc}", 409) from exc
    except OSError as exc:
        raise RenameError(f"Échec de l'undo : {exc}", 500) from exc

    _invalidate_dependent_caches(profile)

    return {
        "ok": True,
        "inverse_entry": inverse,
        "restored_abs": match["old"],
        "restored_rel": str(
            Path(match["old"]).resolve().relative_to(target_resolved)
        ).replace("\\", "/") if _path_under(target_resolved, match["old"])
            else match["old"],
    }
