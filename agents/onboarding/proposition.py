"""Analyse & proposition — orchestration du pipeline onboarding."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path

import yaml

from agents.llm import get_agent_llm
from agents.onboarding.taxonomy_llm import propose_taxonomy
from agents.refonte.categories_llm import propose_keywords_for_new_folders
from agents.refonte.proposition_tools import _groupe_from_path_prefix
from lib import profile as _profilelib
from lib import vision_cache
from lib.theme_canon import extract_themes_from_vision_cache
from lib.theme_normalizer import cluster_themes
from lib.vision import DEFAULT_MODEL, analyze_cover

log = logging.getLogger(__name__)
_EXTS = (".pdf", ".epub")


def _profile_dir(profile: str) -> Path:
    # Accès par attribut de module (pas `from ... import get_project_root`) pour
    # que `mock.patch("lib.profile.get_project_root")` soit effectif en test.
    return _profilelib.get_project_root() / "profiles" / profile


def _load_profile_cfg(profile: str) -> dict:
    p = _profile_dir(profile) / "profile.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def run_vision(profile: str, on_progress: Callable[[int, int], None]) -> dict:
    """Vision LLM sur TOUT le corpus du profil → peuple vision_cache.json.

    Reprenable : skippe les fichiers déjà en cache. Sauvegarde incrémentale
    (toutes les 25 analyses + une finale). `on_progress(done, total)` reçoit
    le nombre de fichiers VISITÉS (pas seulement analysés) — la progression
    reflète le débit réel, pas le travail Vision. Retourne {n_total, n_analyzed}.
    """
    cfg = _load_profile_cfg(profile)
    target = Path(str(cfg.get("target") or ""))
    model = (cfg.get("llm") or {}).get("model") or DEFAULT_MODEL
    endpoint = (cfg.get("llm") or {}).get("endpoint") or ""
    n_pages = int((cfg.get("defaults") or {}).get("pages") or 2)
    api_key = os.environ.get("SILICONFLOW_API_KEY", "")

    cache_path = _profile_dir(profile) / ".cache" / "vision_cache.json"
    cache = vision_cache.load_cache(cache_path)

    files: list[str] = []
    for root, dirs, fs in os.walk(str(target)):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in fs:
            if not f.startswith(".") and f.lower().endswith(_EXTS):
                files.append(os.path.join(root, f))

    n_total = len(files)
    n_analyzed = 0
    for i, path in enumerate(files, start=1):
        key = vision_cache.compute_cache_key(path, model=model, n_pages=n_pages)
        if key and vision_cache.lookup(cache, key) is not None:
            on_progress(i, n_total)
            continue
        result = analyze_cover(
            path,
            api_key=api_key,
            endpoint=endpoint,
            model=model,
            n_pages=n_pages,
        )
        # Un résultat None (PDF illisible / LLM en échec) n'est jamais mis en
        # cache : il sera re-tenté au prochain run reprenable.
        if key and isinstance(result, dict):
            vision_cache.store(cache, key, result, model)
            n_analyzed += 1
            if n_analyzed % 25 == 0:
                vision_cache.save_cache(cache_path, cache)
        on_progress(i, n_total)
    vision_cache.save_cache(cache_path, cache)
    return {"n_total": n_total, "n_analyzed": n_analyzed}


def cluster_corpus(profile: str) -> list[dict]:
    """Clusterise tous les thèmes du vision_cache → clusters enrichis de counts.

    Retourne [{canonical, canonical_forms, raw_members, count}], trié par
    count décroissant. `canonical` = 1ère forme canonique du cluster.
    """
    themes = extract_themes_from_vision_cache(profile)   # {raw: count}
    clusters = cluster_themes(themes.keys())             # [{canonical_forms, raw_members}]
    out: list[dict] = []
    for c in clusters:
        count = sum(int(themes.get(m, 0)) for m in c["raw_members"])
        out.append({
            "canonical": (c["canonical_forms"] or c["raw_members"] or [""])[0],
            "canonical_forms": c["canonical_forms"],
            "raw_members": c["raw_members"],
            "count": count,
        })
    out.sort(key=lambda c: c["count"], reverse=True)
    return out


def propose_categories(tree_folders: list[str]) -> dict:
    """Génère categories.yaml (P2) pour les dossiers feuilles via categories_llm,
    ancré sur le contenu. Retourne la structure {groupe: [entries]}.

    Une « feuille » = dossier de sous-niveau (contient '/'), hors résiduel
    (`_A-TRIER`, `_INBOX`). Les sections de 1er niveau sans sous-dossier
    n'obtiennent pas d'entrée P2 (routage géré par P1 theme_mapping ; fallback
    `autres` sinon) — acceptable au bootstrap, l'utilisateur affine ensuite.
    """
    # Dossiers feuilles (≥ 1 '/' = sous-dossier), hors résiduel/_INBOX
    leaves = [f for f in tree_folders
              if "/" in f and not f.startswith("_")]
    if not leaves:
        return {}
    creations = [{"path": f, "rationale": "dossier de la taxonomie d'onboarding"}
                 for f in leaves]
    # Pas de categories existantes au bootstrap → groupes inférés depuis les
    # préfixes des dossiers proposés eux-mêmes.
    existing_cats: dict = {}
    groupe_inference = {f: _groupe_from_path_prefix(f, existing_cats) for f in leaves}
    try:
        entries = propose_keywords_for_new_folders(
            llm=get_agent_llm(), creations=creations,
            existing_groupes=[], groupe_inference=groupe_inference, sample_entries={})
    except Exception as exc:  # noqa: BLE001 — frontière LLM
        log.warning("propose_categories: étape LLM échouée — categories vides: %s", exc)
        entries = []
    cats: dict[str, list[dict]] = {}
    for e in entries:
        g = e.get("groupe") or "autres"
        chemin = e.get("chemin")
        if not chemin:
            continue
        cats.setdefault(g, []).append(
            {"chemin": chemin, "priorite": e.get("priorite", 5),
             "mots_cles": list(e.get("mots_cles", []))})
    return cats


def write_proposal(profile: str, tree_folders: list[str], theme_mapping: dict[str, str],
                   categories: dict) -> dict:
    """Écrit les 3 YAMLs proposés dans le profil (après backup) puis lance le
    dry-run de couverture. Retourne {coverage, stats, by_destination}.
    """
    from agents.refonte import agent_backup
    from dashboard import taxonomy
    pdir = _profile_dir(profile)
    try:
        agent_backup.create_backup(profile, batch_id=f"onboarding-{profile}")
    except Exception:  # noqa: BLE001 — pas de YAML à snapshoter au tout 1er run
        pass
    _atomic_yaml(pdir / "tree.yaml", {"folders": sorted(set(tree_folders))})
    _atomic_yaml(pdir / "theme_mapping.yaml", dict(theme_mapping))
    _atomic_yaml(pdir / "categories.yaml", dict(categories))
    taxonomy.reset_cache(profile)
    dry = taxonomy.reclassify_dryrun(profile)
    stats = dry.get("stats", {})
    n_lib = max(1, int(stats.get("n_in_lib", 0)))
    coverage = round(100 * int(stats.get("n_with_prediction", 0)) / n_lib, 1)
    return {"coverage": coverage, "stats": stats,
            "by_destination": dry.get("by_destination", [])}


def _atomic_yaml(path: Path, payload: dict) -> None:
    import yaml as _y
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(_y.safe_dump(payload, allow_unicode=True, sort_keys=False),
                   encoding="utf-8")
    tmp.replace(path)


def build_proposal(profile: str, on_progress: Callable[[int, int], None]) -> dict:
    """Pipeline complet « Analyse & proposition » : vision → cluster → propose →
    categories → write 3 YAMLs → dry-run. Retourne le rapport de couverture."""
    run_vision(profile, on_progress)
    clusters = cluster_corpus(profile)
    tree, mapping = propose_taxonomy(get_agent_llm(), clusters)
    cats = propose_categories(tree)
    return write_proposal(profile, tree, mapping, cats)
