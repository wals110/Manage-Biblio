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
            return {"_no_metadata": True}

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
    }


def reset_cache(profile: str | None = None) -> None:
    """Drop the audit cache (one profile or all)."""
    with _cache_lock:
        if profile is None:
            _audit_cache.clear()
        else:
            _audit_cache.pop(profile, None)
