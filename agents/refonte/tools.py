"""Outils read-only pour l'agent Refonte — Phase A.

Six fonctions pures qui donnent à l'agent une vue complète de la taxonomy
d'un profil sans jamais écrire quoi que ce soit. Chaque outil wrappe un
helper existant de `dashboard/taxonomy.py` ou parse un artefact runtime
(vision_cache.json, classify_*.csv).

À ce stade (A.2), les outils sont des fonctions Python normales — non
encore décorées en `@tool` LangChain. Le binding se fera en A.3 quand
l'agent LLM sera wired.
"""

from __future__ import annotations

import csv
from pathlib import Path

from dashboard import taxonomy as tax

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_LOGS_DIR = PROJECT_ROOT / "logs"


def list_folders(profile: str) -> list[str]:
    """Énumère les dossiers déclarés dans tree.yaml du profil.

    Args:
        profile: Nom du profil (ex. "default", "test").

    Returns:
        Liste triée des chemins relatifs (ex. ["01-SCIENCES", "01-SCIENCES/PHYSIQUE", ...]).
        Liste vide si profil ou tree.yaml introuvable / illisible.
    """
    return tax._load_tree(profile)


def count_files_per_folder(profile: str) -> dict[str, int]:
    """Compte les fichiers directement présents dans chaque dossier (pas de récursion).

    Combine `tree.yaml` (liste des dossiers déclarés) + `os.scandir` sur le `target`
    du profil. Les dossiers déclarés mais absents du FS reçoivent un count de 0.

    Args:
        profile: Nom du profil.

    Returns:
        Dict {folder_relpath: count}. Vide si target introuvable.
    """
    folders = tax._load_tree(profile)
    target = tax._profile_target_path(profile)
    if not target:
        return {}
    return tax._scan_folder_counts(target, folders)


def read_theme_mapping(profile: str) -> dict[str, str]:
    """Charge le mapping thème → dossier de theme_mapping.yaml.

    Args:
        profile: Nom du profil.

    Returns:
        Dict {theme_str: folder_relpath}. Vide si fichier illisible / absent.
    """
    return tax._load_mapping(profile)


def list_themes_per_folder(profile: str) -> dict[str, list[str]]:
    """Renvoie l'inverse de theme_mapping : dossier → liste de thèmes qui pointent dessus.

    Version A.2 : ne regarde que les thèmes **mappés**. Une version enrichie
    (themes LLM observés sur les fichiers physiquement présents dans le folder)
    pourra être ajoutée si Phase B en a besoin.

    Args:
        profile: Nom du profil.

    Returns:
        Dict {folder_relpath: [theme_str, ...]} trié alphabétiquement par thème.
    """
    mapping = tax._load_mapping(profile)
    return tax._mapping_reverse(mapping)


def compute_folder_overlap(profile: str, folder_a: str, folder_b: str) -> dict:
    """Calcule l'indice de Jaccard sur les thèmes mappés vers deux dossiers.

    Sert à détecter des doublons sémantiques : deux dossiers qui reçoivent
    le même genre de thèmes sont candidats à fusion ou à redéfinition.

    Args:
        profile: Nom du profil.
        folder_a: Premier dossier (chemin relatif depuis le target).
        folder_b: Deuxième dossier.

    Returns:
        {
            "folder_a": str, "folder_b": str,
            "themes_a": list[str], "themes_b": list[str],
            "common": list[str], "union": list[str],
            "jaccard": float  # 0.0 (aucun thème commun) → 1.0 (mappings identiques)
        }
    """
    mapping = tax._load_mapping(profile)
    reverse = tax._mapping_reverse(mapping)
    themes_a = set(reverse.get(folder_a, []))
    themes_b = set(reverse.get(folder_b, []))

    common = themes_a & themes_b
    union = themes_a | themes_b
    jaccard = (len(common) / len(union)) if union else 0.0

    return {
        "folder_a": folder_a,
        "folder_b": folder_b,
        "themes_a": sorted(themes_a, key=str.lower),
        "themes_b": sorted(themes_b, key=str.lower),
        "common": sorted(common, key=str.lower),
        "union": sorted(union, key=str.lower),
        "jaccard": round(jaccard, 4),
    }


def get_classifier_breakdown(
    profile: str,
    logs_dir: Path | str | None = None,
) -> dict:
    """Stats sur la dernière run de classify pour le profil — répartition par source.

    Parse le CSV `classify_*.csv` le plus récent (ou contenant le nom du profil)
    dans `logs_dir`, agrège la colonne `mot_cle` (qui contient la source label
    retournée par `classify_combined` : "LLM (theme)", "Keyword", "LLM (mapper)",
    "LLM (theme→N3-refined)", "LLM (fallback)", ou "FAILED").

    Args:
        profile: Nom du profil (matché contre le nom de fichier CSV si présent).
        logs_dir: Dossier des rapports CSV. Défaut: `logs/` à la racine du projet.

    Returns:
        {
            "csv_path": str | None,
            "total": int,
            "by_source": {"LLM (theme)": N, "Keyword": N, ...},
            "by_status": {"classifié": N, "non_classifié": N, "erreur_extraction": N, ...},
        }
        Si aucun CSV trouvé : `csv_path=None`, `total=0`, dicts vides.
    """
    base = Path(logs_dir) if logs_dir else DEFAULT_LOGS_DIR
    csv_path = _find_latest_classify_csv(base, profile)

    result: dict = {
        "csv_path": str(csv_path) if csv_path else None,
        "total": 0,
        "by_source": {},
        "by_status": {},
    }

    if not csv_path or not csv_path.exists():
        return result

    try:
        with csv_path.open(encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                result["total"] += 1
                status = (row.get("status") or "").strip() or "(unknown)"
                result["by_status"][status] = result["by_status"].get(status, 0) + 1
                if status == "classifié":
                    source = (row.get("mot_cle") or "").strip() or "(unknown)"
                    result["by_source"][source] = result["by_source"].get(source, 0) + 1
    except (OSError, csv.Error):
        return result

    return result


# ─── Helpers privés ────────────────────────────────────────────────────────


def _find_latest_classify_csv(logs_dir: Path, profile: str) -> Path | None:
    """Renvoie le `classify_*.csv` le plus récent.

    Stratégie : si un fichier matche `*<profile>*`, on prend le plus récent
    parmi ceux-là. Sinon on prend le plus récent toutes profils confondus
    (la convention actuelle n'embarque pas le nom du profil dans le fichier).
    """
    if not logs_dir.exists() or not logs_dir.is_dir():
        return None
    candidates = sorted(logs_dir.glob("classify_*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return None
    preferred = [p for p in candidates if profile.lower() in p.name.lower()]
    return preferred[0] if preferred else candidates[0]
