"""Cockpit Overview — agrège des métriques par profil + métriques globales.

Cache mémoire 30 s par profil (snapshot) — invalidé par `?refresh=1` côté
endpoint ou `reset_cache()` côté tests.
"""
from __future__ import annotations

import json
import os
import threading
import time
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


def _vision_cache_path(profile: str) -> Path:
    """Chemin attendu de profiles/<profile>/.cache/vision_cache.json."""
    return (data.get_project_root() / "profiles" / profile
            / ".cache" / "vision_cache.json")


def _load_vision_cache(profile: str) -> dict | None:
    """Lit vision_cache.json. None si absent, corrompu ou non-dict."""
    p = _vision_cache_path(profile)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def card_llm_cost(profile: str) -> dict:
    """Coût LLM estimé = n_entries × cost_per_call (profile.yaml)."""
    cfg = _load_profile_config(profile)
    cpc = float(((cfg or {}).get("defaults") or {}).get("cost_per_call") or 0)
    cache = _load_vision_cache(profile)
    n = len(cache) if cache else 0
    return {"cost_usd": round(n * cpc, 4), "n_calls": n, "cost_per_call": cpc}


def card_vision_cache(profile: str) -> dict:
    """Stats sur vision_cache.json. n_successful = entries avec result non vide."""
    p = _vision_cache_path(profile)
    cache = _load_vision_cache(profile)
    if cache is None:
        return {"n_entries": 0, "n_successful": 0, "size_kb": 0,
                "last_modified": None}
    n_ok = sum(1 for v in cache.values()
               if isinstance(v, dict) and v.get("result"))
    size_kb = round(p.stat().st_size / 1024, 1)
    mtime_iso = time.strftime(
        "%Y-%m-%dT%H:%M:%S", time.localtime(p.stat().st_mtime),
    )
    return {
        "n_entries": len(cache),
        "n_successful": n_ok,
        "size_kb": size_kb,
        "last_modified": mtime_iso,
    }


def card_inbox(profile: str) -> dict:
    """Stats sur le dossier inbox du profil. Retourne
    {n_files, size_mb, oldest_iso, exists}."""
    cfg = _load_profile_config(profile)
    inbox_path = (cfg or {}).get("inbox")
    if not inbox_path:
        return {"n_files": 0, "size_mb": 0.0,
                "oldest_iso": None, "exists": False}
    inbox = Path(str(inbox_path))
    if not inbox.exists() or not inbox.is_dir():
        return {"n_files": 0, "size_mb": 0.0,
                "oldest_iso": None, "exists": False}
    n = 0
    size_bytes = 0
    oldest_mtime: float | None = None
    for f in inbox.iterdir():
        if not f.is_file():
            continue
        if f.suffix.lower() not in (".pdf", ".epub"):
            continue
        n += 1
        try:
            st = f.stat()
            size_bytes += st.st_size
            if oldest_mtime is None or st.st_mtime < oldest_mtime:
                oldest_mtime = st.st_mtime
        except OSError:
            pass
    return {
        "n_files": n,
        "size_mb": round(size_bytes / 1024**2, 4),
        "oldest_iso": (time.strftime("%Y-%m-%dT%H:%M:%S",
                                     time.localtime(oldest_mtime))
                       if oldest_mtime else None),
        "exists": True,
    }


def card_baseline_runs(profile: str) -> dict:
    """Délègue à baseline.list_runs. Retourne {n_runs, last_run_iso}."""
    from dashboard import baseline
    try:
        runs = baseline.list_runs(profile)
    except Exception:  # noqa: BLE001 — baseline peut crasher sur profil incomplet
        runs = []
    if not runs:
        return {"n_runs": 0, "last_run_iso": None}
    # baseline.list_runs trie déjà du plus récent au plus ancien
    return {"n_runs": len(runs), "last_run_iso": runs[0].get("created_at")}


def _agent_info(profile: str, agent: str) -> dict:
    """Lit .cache/<agent>/batches/ + status.json. status ∈ {idle, running,
    error, missing}."""
    base = data.get_project_root() / "profiles" / profile / ".cache" / agent
    if not base.exists():
        return {"n_batches": 0, "status": "missing"}
    batches_dir = base / "batches"
    n = 0
    if batches_dir.exists():
        n = sum(1 for d in batches_dir.iterdir() if d.is_dir())
    status_file = base / "status.json"
    status = "missing"
    if status_file.exists():
        try:
            data_ = json.loads(status_file.read_text(encoding="utf-8"))
            status = str(data_.get("status") or "idle")
        except (json.JSONDecodeError, OSError):
            status = "error"
    return {"n_batches": n, "status": status}


def card_agent_sessions(profile: str) -> dict:
    """Sessions des agents refonte + dedupli."""
    return {
        "refonte": _agent_info(profile, "refonte"),
        "dedupli": _agent_info(profile, "dedupli"),
    }


_STALE_LOCK_SECONDS = 3600


def card_health(profile: str) -> dict:
    """Compte les orphelins (mappings vers folder absent, categories orphelins)
    + locks actifs. {n_orphans_mappings, n_orphans_categories,
    n_orphans_total, locks_active: [{name, age_seconds, stale}]}."""
    from dashboard import taxonomy as _tax

    # Orphans côté theme_mapping : mapping val absente de tree
    tree_set = set()
    try:
        tree_set = set(_tax._load_tree(profile))
    except Exception:  # noqa: BLE001
        pass
    mapping = {}
    try:
        mapping = _tax._load_mapping(profile)
    except Exception:  # noqa: BLE001
        pass
    n_map_orphans = sum(1 for v in mapping.values() if v and v not in tree_set)

    # Orphans côté categories : snapshot.stats.n_orphans
    n_cat_orphans = 0
    try:
        from dashboard import categories as _cat
        snap = _cat.build_snapshot(profile, force_reload=False)
        n_cat_orphans = int((snap.get("stats") or {}).get("n_orphans") or 0)
    except Exception:  # noqa: BLE001
        pass

    # Locks actifs : glob .cache/.*.lock
    locks: list[dict] = []
    cache_dir = data.get_project_root() / "profiles" / profile / ".cache"
    if cache_dir.exists():
        now = time.time()
        for lock in cache_dir.glob(".*.lock"):
            try:
                age = now - lock.stat().st_mtime
            except OSError:
                continue
            locks.append({
                "name": lock.name,
                "age_seconds": int(age),
                "stale": age > _STALE_LOCK_SECONDS,
            })

    return {
        "n_orphans_mappings": n_map_orphans,
        "n_orphans_categories": n_cat_orphans,
        "n_orphans_total": n_map_orphans + n_cat_orphans,
        "locks_active": locks,
    }


_MIN_CONFIDENCE = 0.5


def card_top_themes(profile: str, limit: int = 10) -> list[dict]:
    """Top thèmes LLM (vision_cache) triés par count décroissant. Filtre
    confidence < _MIN_CONFIDENCE."""
    from dashboard import taxonomy as _tax
    cache = _load_vision_cache(profile)
    if not cache:
        return []
    counts: dict[str, int] = {}
    # Réutilise _iter_themes(cache) qui yield (theme, confidence)
    for theme, conf in _tax._iter_themes(cache):
        if conf < _MIN_CONFIDENCE:
            continue
        counts[theme] = counts.get(theme, 0) + 1
    if not counts:
        return []
    total_occ = sum(counts.values())
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [
        {"theme": t, "count": c, "pct": round(c / total_occ * 100, 1)}
        for t, c in items[:limit]
    ]


def card_top_folders(profile: str, limit: int = 10) -> list[dict]:
    """Top N folders par nombre de fichiers (PDF/EPUB). Exclut la racine
    du target (qui contient les fichiers non classifiés)."""
    cfg = _load_profile_config(profile)
    if cfg is None:
        return []
    target_str = cfg.get("target")
    if not target_str:
        return []
    target = Path(str(target_str))
    if not target.exists():
        return []
    counts: dict[str, int] = {}
    for root, _dirs, files in os.walk(str(target)):
        rel = Path(root).relative_to(target).as_posix()
        if rel == ".":
            continue  # racine = "non classifié" → exclu
        n = sum(1 for f in files if f.lower().endswith((".pdf", ".epub")))
        if n:
            counts[rel] = n
    if not counts:
        return []
    total = sum(counts.values())
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [
        {"path": p, "n_files": n, "pct": round(n / total * 100, 1)}
        for p, n in items[:limit]
    ]


def card_recent_activity(profile: str, limit: int = 5) -> list[dict]:
    """Liste les N mutations les plus récentes : backups taxonomy + backups
    categories + 1 événement synthétique pour rename-journal (mtime du
    fichier, pas tail par ligne). Retourne [{ts, relative_time, action,
    target}]."""
    cache_dir = data.get_project_root() / "profiles" / profile / ".cache"
    events: list[tuple[float, str, str]] = []  # (mtime, action, target)
    if not cache_dir.exists():
        return []
    # Backups
    for sub, action in (
        ("taxonomy-backups", "taxonomy"),
        ("categories-backups", "categories"),
    ):
        d = cache_dir / sub
        if not d.exists():
            continue
        for f in d.glob("*.yaml"):
            try:
                events.append((f.stat().st_mtime, action, f.name))
            except OSError:
                pass
    # Rename journal : 1 événement synthétique avec la mtime du fichier
    # (pas tail par ligne — hors scope du cockpit, voir l'onglet Rename).
    rj = cache_dir / "rename-journal.jsonl"
    if rj.exists():
        try:
            events.append((rj.stat().st_mtime, "rename",
                           "rename-journal.jsonl"))
        except OSError:
            pass
    events.sort(reverse=True)
    now = time.time()
    out: list[dict] = []
    for mtime, action, target in events[:limit]:
        out.append({
            "ts": mtime,
            "relative_time": _human_relative(now - mtime),
            "action": action,
            "target": target,
        })
    return out


def card_llm_models() -> list[dict]:
    """Liste {profile, provider, model, endpoint} pour chaque profil."""
    out: list[dict] = []
    for p in data.get_available_profiles():
        name = p["name"] if isinstance(p, dict) else p
        cfg = _load_profile_config(name) or {}
        llm = cfg.get("llm") or {}
        out.append({
            "profile": name,
            "provider": llm.get("provider", ""),
            "model": llm.get("model", ""),
            "endpoint": llm.get("endpoint", ""),
        })
    return out


def card_api_keys() -> list[dict]:
    """Statut des API keys connues (SiliconFlow pour l'instant)."""
    known = ["SILICONFLOW_API_KEY"]
    return [
        {"name": k, "configured": bool(os.environ.get(k))}
        for k in known
    ]


def card_profiles_list() -> list[dict]:
    """Liste de tous les profils + état de leur target."""
    out: list[dict] = []
    for p in data.get_available_profiles():
        name = p["name"] if isinstance(p, dict) else p
        cfg = _load_profile_config(name) or {}
        target = str(cfg.get("target") or "")
        out.append({
            "name": name,
            "target_path": target,
            "target_exists": bool(target and Path(target).exists()),
        })
    return out


def card_global_cost() -> dict:
    """Somme des coûts LLM estimés tous profils. Pie segments < 1 % cachés.
    Retourne {total_usd, by_profile: [{profile, cost_usd, n_calls, pct}]}."""
    by_profile_full: list[dict] = []
    for p in data.get_available_profiles(include_all=True):
        name = p["name"] if isinstance(p, dict) else p
        cost = card_llm_cost(name)
        if cost["n_calls"] > 0:
            by_profile_full.append({
                "profile": name,
                "cost_usd": cost["cost_usd"],
                "n_calls": cost["n_calls"],
            })
    total = round(sum(x["cost_usd"] for x in by_profile_full), 2)
    if total == 0:
        return {"total_usd": 0.0, "by_profile": []}
    # Calcule pct et filtre les segments < 1 %
    by_profile: list[dict] = []
    for x in by_profile_full:
        pct = round(x["cost_usd"] / total * 100, 1)
        if pct >= 1.0:
            by_profile.append({**x, "pct": pct})
    by_profile.sort(key=lambda x: -x["pct"])
    return {"total_usd": total, "by_profile": by_profile}


def _human_relative(seconds: float) -> str:
    """Format relatif humain. Toujours préfixé 'il y a' pour cohérence
    visuelle dans la timeline."""
    s = int(seconds)
    if s < 60:
        return f"il y a {s} s"
    if s < 3600:
        return f"il y a {s // 60} min"
    if s < 86400:
        return f"il y a {s // 3600} h"
    return f"il y a {s // 86400} j"
