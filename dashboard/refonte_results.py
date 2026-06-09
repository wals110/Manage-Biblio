"""Agrégations pour la restitution Phase B de l'agent Refonte (outil de
décision). Lecture seule sur les artefacts d'un run de proposition
(reclassify-projection.csv, changes.json, tree-proposed.yaml).

Fonctions pures (testables sans I/O) + lecteurs fins. Pas de DuckDB :
la bucketisation de la source et le risk-score sont des fonctions Python,
donc on lit le CSV avec csv.DictReader et on agrège en Python (DRY).
"""

from __future__ import annotations


def bucket_source(source: str | None) -> str:
    """Bucketise le label `source` brut du CSV en classe de cascade.

    Classes : p1_theme, p1_refined, keyword, fallback, failed.
    Ordre important : 'refined' testé avant 'theme' (le label
    "LLM (theme→refined)" contient "theme").
    """
    s = (source or "").strip()
    if not s or s == "FAILED":
        return "failed"
    if "fallback" in s:
        return "fallback"
    if s.startswith("Keyword"):
        return "keyword"
    if "refined" in s:
        return "p1_refined"
    if s.startswith("LLM (theme"):
        return "p1_theme"
    return "p1_theme"


def _top_segment(path: str) -> str:
    return (path or "").strip("/").split("/", 1)[0]


def is_inter_discipline_jump(current_folder: str, proposed_folder: str) -> bool:
    """True si le fichier change de discipline (1er segment de chemin).

    _INBOX / racine vide ne sont pas des disciplines : un fichier qui
    vient de l'inbox est un classement frais, pas un saut. Destination
    vide (no prediction) n'est pas un saut non plus.
    """
    src = _top_segment(current_folder)
    dst = _top_segment(proposed_folder)
    if not src or not dst:
        return False
    if src in ("_INBOX", "_A-TRIER"):
        return False
    return src != dst


CONF_BANDS = ("0-0.5", "0.5-0.7", "0.7-0.9", "0.9-1.0")


def confidence_band(conf: float | None) -> str:
    """Mappe une confiance (0-1) sur une bande discrète."""
    c = float(conf) if conf is not None else 0.0
    if c < 0.5:
        return "0-0.5"
    if c < 0.7:
        return "0.5-0.7"
    if c < 0.9:
        return "0.7-0.9"
    return "0.9-1.0"
