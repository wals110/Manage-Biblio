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
