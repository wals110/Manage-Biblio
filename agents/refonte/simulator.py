"""Simulateur reclassify pour Phase B.

À partir d'un dossier `proposed/` (tree-proposed.yaml + theme_mapping-proposed.yaml),
re-applique `lib.classifier.classify_combined` sur tous les fichiers déjà
analysés (i.e. présents dans `vision_cache.json`) en utilisant le **mapping
proposé** au lieu du mapping courant.

Produit 2 artefacts dans `<proposal_dir>/../simulation/` :
  - reclassify-projection.csv : 1 ligne par fichier (rel_path, current_folder,
                                proposed_folder, changed, source, top_theme,
                                confidence)
  - simulation-summary.json   : stats agrégées (n_files, n_moving, n_stable,
                                n_no_prediction, distribution_before, after)

Module Python pur, **aucun appel LLM** — coût zéro, durée ~5-15s sur 18k
fichiers (parallélisme via ThreadPoolExecutor).

Convention : ne lit que les artefacts dans `proposed/`. Ne touche pas aux
YAMLs de production. C'est Phase C qui appliquera après validation user.
"""

from __future__ import annotations

import csv
import json
import os
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import yaml

from dashboard import taxonomy as tax

# Extensions de fichiers à considérer (aligné avec dashboard/taxonomy.py)
_FILE_EXTS = (".pdf", ".epub")


def simulate_reclassify(
    profile: str,
    proposal_dir: Path | str,
    *,
    max_workers: int = 8,
) -> dict[str, Any]:
    """Simule un reclassify complet avec le mapping proposé.

    Args:
        profile: Nom du profil cible.
        proposal_dir: Chemin du dossier `proposed/` produit par propose_changes
                      (contient `tree-proposed.yaml` + `theme_mapping-proposed.yaml`).
        max_workers: Threads parallèles pour classifier les fichiers.

    Returns:
        Dict summary :
            {
                "csv_path": str,
                "summary_path": str,
                "n_files": int,
                "n_moving": int,
                "n_stable": int,
                "n_no_prediction": int,
                "n_by_source": {"LLM (theme)": int, "Keyword": int, ...},
                "top_destinations": [{"folder": str, "n_incoming": int}, ...],
                "top_origins": [{"folder": str, "n_outgoing": int}, ...],
            }

    Raises:
        FileNotFoundError: si proposal_dir ou son theme_mapping-proposed.yaml manque.
    """
    from lib.classifier import classify_combined, load_keyword_classifier

    proposal_dir = Path(proposal_dir)
    mapping_path = proposal_dir / "theme_mapping-proposed.yaml"
    if not mapping_path.exists():
        raise FileNotFoundError(f"theme_mapping-proposed.yaml manquant dans {proposal_dir}")

    # Lit le mapping proposé
    try:
        proposed_mapping_raw = yaml.safe_load(mapping_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"theme_mapping-proposed.yaml invalide : {exc}") from exc
    proposed_mapping: dict[str, str] = {
        str(k): str(v) for k, v in proposed_mapping_raw.items() if isinstance(v, str)
    }

    # KeywordClassifier (fallback Niveau 2) — réutilise categories.yaml courant du
    # profil. Phase B ne propose pas de changements à categories.yaml.
    target = tax._profile_target_path(profile)
    if target is None or not target.exists():
        return _empty_summary(proposal_dir)

    cat_path = tax._profile_dir(profile) / "categories.yaml"
    classifier = load_keyword_classifier(str(cat_path)) if cat_path.exists() else None

    # Vision cache
    cache_path = tax._vision_cache_path(profile)
    cache: dict = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cache = {}
    if not isinstance(cache, dict):
        cache = {}

    # Config LLM (pour calculer la clé de cache)
    cfg = tax._load_profile_yaml(profile)
    model = (cfg.get("llm") or {}).get("model") or "Qwen/Qwen3-VL-32B-Instruct"
    n_pages = int((cfg.get("defaults") or {}).get("pages") or 2)

    # Enumeration des fichiers
    target_str = str(target)
    file_list: list[tuple[str, str, str]] = []
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

    # Worker : récupère result vision + appelle classify_combined
    from lib import vision_cache as vc

    def _process(item: tuple[str, str, str]) -> dict[str, Any]:
        abs_path, rel, filename = item
        i = rel.rfind("/")
        current_folder = rel[:i] if i >= 0 else ""
        # Lookup vision_cache
        key = vc.compute_cache_key(abs_path, model=model, n_pages=n_pages)
        result = vc.lookup(cache, key) if key else None
        if not isinstance(result, dict):
            result = {}
        # Top theme pour le CSV (lecture seule)
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
        # Classification avec le mapping PROPOSÉ
        dest, score, source = classify_combined(
            result, filename, proposed_mapping,
            classifier=classifier,
            llm_mapper=None,
            pdf_path=abs_path,
        )
        return {
            "rel_path": rel,
            "current_folder": current_folder,
            "proposed_folder": dest or "",
            "changed": bool(dest and dest != current_folder),
            "source": source or "FAILED",
            "top_theme": top_theme,
            "confidence": round(top_conf, 3),
            "score": float(score) if score else 0.0,
        }

    # Walk parallel
    processed: list[dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for r in ex.map(_process, file_list):
            processed.append(r)

    # Écriture CSV
    sim_dir = proposal_dir.parent / "simulation"
    sim_dir.mkdir(parents=True, exist_ok=True)
    csv_path = sim_dir / "reclassify-projection.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "rel_path", "current_folder", "proposed_folder", "changed",
            "source", "top_theme", "confidence", "score",
        ])
        writer.writeheader()
        for r in processed:
            writer.writerow(r)

    # Agrégation summary
    n_files = len(processed)
    n_moving = sum(1 for r in processed if r["changed"])
    n_stable = sum(1 for r in processed if r["proposed_folder"] and not r["changed"])
    n_no_pred = sum(1 for r in processed if not r["proposed_folder"])
    n_by_source = Counter(r["source"] for r in processed if r["proposed_folder"])

    # Top destinations (dossiers qui reçoivent le plus de fichiers en mouvement)
    incoming = defaultdict(int)
    outgoing = defaultdict(int)
    for r in processed:
        if r["changed"] and r["proposed_folder"]:
            incoming[r["proposed_folder"]] += 1
            outgoing[r["current_folder"] or "(racine)"] += 1
    top_destinations = [
        {"folder": k, "n_incoming": v}
        for k, v in sorted(incoming.items(), key=lambda x: -x[1])[:20]
    ]
    top_origins = [
        {"folder": k, "n_outgoing": v}
        for k, v in sorted(outgoing.items(), key=lambda x: -x[1])[:20]
    ]

    summary: dict[str, Any] = {
        "csv_path": str(csv_path),
        "summary_path": str(sim_dir / "simulation-summary.json"),
        "n_files": n_files,
        "n_moving": n_moving,
        "n_stable": n_stable,
        "n_no_prediction": n_no_pred,
        "n_by_source": dict(n_by_source),
        "top_destinations": top_destinations,
        "top_origins": top_origins,
    }
    (sim_dir / "simulation-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return summary


def _empty_summary(proposal_dir: Path) -> dict[str, Any]:
    """Retour minimal si le profil n'a pas de target FS valide."""
    sim_dir = proposal_dir.parent / "simulation"
    sim_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "csv_path": "",
        "summary_path": str(sim_dir / "simulation-summary.json"),
        "n_files": 0,
        "n_moving": 0,
        "n_stable": 0,
        "n_no_prediction": 0,
        "n_by_source": {},
        "top_destinations": [],
        "top_origins": [],
    }
    (sim_dir / "simulation-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary
