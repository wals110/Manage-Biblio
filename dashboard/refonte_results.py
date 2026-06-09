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


# Poids du risk-score (constantes nommées, ajustables). Cf. spec §1.5.
_SRC_WEIGHT = {
    "failed": 1.0,
    "fallback": 0.7,
    "keyword": 0.6,
    "p1_refined": 0.1,
    "p1_theme": 0.0,
}
_W_SRC, _W_CONF, _W_JUMP, _W_NEW = 0.5, 0.2, 0.2, 0.1


def risk_score(source_class: str, confidence: float | None,
               is_jump: bool, is_new_dest: bool) -> float:
    """Score de risque composite (0..1+), sert le tri par défaut de la
    table de drill-down. Transparent : chaque terme est nommé."""
    sw = _SRC_WEIGHT.get(source_class, 0.6)
    conf = float(confidence) if confidence is not None else 0.0
    r = (_W_SRC * sw
         + _W_CONF * (1.0 - conf)
         + _W_JUMP * (1.0 if is_jump else 0.0)
         + _W_NEW * (1.0 if is_new_dest else 0.0))
    return round(r, 4)


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


SOURCE_CLASSES = ("p1_theme", "p1_refined", "keyword", "fallback", "failed")


def build_risk_matrix(rows: list[dict]) -> dict:
    """Construit la matrice source_class × confidence_band + le budget
    (totaux par source) + n_doubt (zone de doute).

    Zone de doute = toutes les classes sauf P1 (theme/refined) à
    confiance >= 0.7.
    """
    from collections import defaultdict
    cells: dict = defaultdict(int)
    budget: dict = defaultdict(int)
    n_doubt = 0
    for r in rows:
        sc = bucket_source(r.get("source"))
        try:
            conf = float(r.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        band = confidence_band(conf)
        cells[(sc, band)] += 1
        budget[sc] += 1
        is_safe = sc in ("p1_theme", "p1_refined") and conf >= 0.7
        if not is_safe:
            n_doubt += 1
    matrix = [
        {"source_class": sc, "band": b, "count": cells.get((sc, b), 0)}
        for sc in SOURCE_CLASSES for b in CONF_BANDS
    ]
    budget_list = [
        {"source_class": sc, "count": budget.get(sc, 0)}
        for sc in SOURCE_CLASSES
    ]
    return {"matrix": matrix, "budget": budget_list, "n_doubt": n_doubt}


def enrich_row(row: dict, creations: set[str]) -> dict:
    """Enrichit une ligne CSV avec source_class, band, is_jump,
    is_new_dest, risk. Retourne un nouveau dict (n'altère pas l'entrée)."""
    try:
        conf = float(row.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    sc = bucket_source(row.get("source"))
    cur = row.get("current_folder", "")
    prop = row.get("proposed_folder", "")
    jump = is_inter_discipline_jump(cur, prop)
    new_dest = bool(prop) and prop in creations
    return {
        "rel_path": row.get("rel_path", ""),
        "current_folder": cur,
        "proposed_folder": prop,
        "source": row.get("source", ""),
        "source_class": sc,
        "top_theme": row.get("top_theme", ""),
        "confidence": conf,
        "band": confidence_band(conf),
        "is_jump": jump,
        "is_new_dest": new_dest,
        "risk": risk_score(sc, conf, jump, new_dest),
    }


def _is_doubt(enriched: dict) -> bool:
    sc = enriched["source_class"]
    return not (sc in ("p1_theme", "p1_refined") and enriched["confidence"] >= 0.7)


def select_doubt_files(rows: list[dict], creations: set[str], *,
                       page: int = 1, page_size: int = 50,
                       source_class: str | None = None,
                       band: str | None = None) -> dict:
    """Zone de doute enrichie, filtrée (source_class/band), triée par
    risque décroissant, paginée côté serveur."""
    enriched = [enrich_row(r, creations) for r in rows]
    doubt = [e for e in enriched if _is_doubt(e)]
    if source_class:
        doubt = [e for e in doubt if e["source_class"] == source_class]
    if band:
        doubt = [e for e in doubt if e["band"] == band]
    doubt.sort(key=lambda e: e["risk"], reverse=True)
    total = len(doubt)
    page = max(1, page)
    start = (page - 1) * page_size
    return {
        "rows": doubt[start:start + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
