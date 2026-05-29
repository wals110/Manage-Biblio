"""C.2 — Canonisation contextuelle des thèmes via LLM texte avec titres en contexte.

Différence avec lib.theme_canonicalizer (C-light "blind") :
  - C-light : (raw_theme) → canonical, le LLM décide à l'aveugle
  - C.2 (this) : (raw_theme, N titres représentatifs) → canonical, le LLM
    désambiguïse en voyant le contexte d'usage du thème dans la bibliothèque.

Le LLM peut ainsi distinguer un thème "Logic" :
  - Si les livres taggés sont "Foundations of Computer Logic", "Logic
    Programming with Prolog" → canonical "Logic" (informatique)
  - Si "Aristotle's Metaphysics", "Philosophy of Logic" → canonical "Logic"
    aussi mais le LLM voit le contexte philosophique
  - Pas de confusion avec "Mathematics" (l'erreur principale du run C-light v1)

Pipeline (orchestré par lib.theme_canon.build_canon_table(mode="source")) :
  1. extract_themes_with_titles(profile) — collecte les 5 titres par thème
  2. resolve_themes_with_context(themes_titles, vocabulary, llm) — LLM
  3. (côté theme_canon) re-clustering via cluster_themes pour gommer les
     doublons de canoniques produits par batches différents
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from lib.theme_canon import _parse_cached_result, _vision_cache_path

# Nombre de titres représentatifs collectés par thème (décision design 2026-05-29)
TITLES_PER_THEME = 5


def extract_themes_with_titles(profile: str) -> dict[str, list[str]]:
    """Pour chaque raw_theme du vision_cache, collecte jusqu'à 5 titres
    représentatifs (les premiers rencontrés dans l'ordre du cache).

    Le contexte des titres permet au LLM (en aval) de désambiguïser le
    sens d'un thème ambigu (Logic, Statistics, Big Data, History, …).

    Args:
        profile: Nom du profil.

    Returns:
        Dict {raw_theme: [title_1, ..., title_N]} avec N ≤ 5.
        Les titres sont uniques par thème et préservent l'ordre du cache.
        Profils sans vision_cache → dict vide.
    """
    path = _vision_cache_path(profile)
    if not path.exists():
        return {}
    import json
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    by_theme: dict[str, list[str]] = defaultdict(list)
    seen: dict[str, set[str]] = defaultdict(set)

    for entry in cache.values():
        if not isinstance(entry, dict):
            continue
        result = _parse_cached_result(entry.get("result"))
        if not isinstance(result, dict):
            continue
        title = str(result.get("title") or "").strip()
        if not title:
            continue
        for t in result.get("themes", []) or []:
            name = ""
            if isinstance(t, dict):
                name = str(t.get("theme") or "").strip()
            elif isinstance(t, str):
                name = t.strip()
            if not name:
                continue
            # Skip si on a déjà 5 titres pour ce thème (premier-arrivé)
            if len(by_theme[name]) >= TITLES_PER_THEME:
                continue
            # Skip si ce titre est déjà associé au thème (dédup)
            if title in seen[name]:
                continue
            seen[name].add(title)
            by_theme[name].append(title)

    return dict(by_theme)


def count_themes(profile: str) -> dict[str, int]:
    """Compte le nombre d'occurrences de chaque raw_theme dans le vision_cache.

    Helper séparé de `extract_themes_with_titles` car les usages diffèrent :
    le count sert au vocabulary bootstrap (top-N par fréquence), les titres
    servent au contexte LLM.
    """
    path = _vision_cache_path(profile)
    if not path.exists():
        return {}
    import json
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    counts: dict[str, int] = {}
    for entry in cache.values():
        if not isinstance(entry, dict):
            continue
        result = _parse_cached_result(entry.get("result"))
        if not isinstance(result, dict):
            continue
        for t in result.get("themes", []) or []:
            name = ""
            if isinstance(t, dict):
                name = str(t.get("theme") or "").strip()
            elif isinstance(t, str):
                name = t.strip()
            if name:
                counts[name] = counts.get(name, 0) + 1
    return counts


def themes_with_titles_iter(
    themes_titles: dict[str, list[str]],
) -> Iterable[tuple[str, list[str]]]:
    """Itérateur stable (theme, titles) sur les thèmes — ordre lexico pour
    déterminisme des batches et du cache."""
    for theme in sorted(themes_titles.keys()):
        yield theme, themes_titles[theme]
