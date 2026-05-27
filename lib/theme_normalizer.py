"""Normalisation déterministe des thèmes pour dédupliquer les variantes
orthographiques avant l'aggrégation des counts.

Cible : variantes orthographiques pures (casse, ponctuation, singulier/pluriel,
qualifiers entre parenthèses, accents), PAS les sous-spécialisations sémantiques
(Unsupervised Machine Learning ≠ Machine Learning — laissé pour le clustering
fuzzy + LLM judge dans les phases suivantes).

Stdlib only — pas de dépendance externe. Cas particuliers gérés :
  - Faux pluriels (Statistics, Mathematics, Physics, Analysis…) → conservés
  - Acronymes courts → conservés (OS, DS, ML…)
  - Stopwords FR + EN → strippés
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

# Stopwords FR + EN courants à strip — la liste reste volontairement
# conservatrice : on ne strip que ce qui ne porte aucun sens (déterminants,
# prépositions). Pas de verbes (ils peuvent être discriminants).
_STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "of", "for", "and", "or", "in", "on", "to",
    "le", "la", "les", "de", "des", "du", "et", "ou", "en", "au", "aux",
    "by", "with",
})

# Mots à NE PAS dépluraliser. Faux pluriels (-ics, -is) et acronymes.
_KEEP_AS_IS: frozenset[str] = frozenset({
    # Faux pluriels en -ics
    "physics", "mathematics", "statistics", "ethics", "robotics",
    "linguistics", "informatics", "mechanics", "electronics", "optics",
    "phonetics", "genetics", "economics", "politics", "ceramics",
    "athletics", "graphics", "logistics", "metrics", "gymnastics",
    "aerobics", "diagnostics", "academics",
    # Faux pluriels en -is (singulier = -is, pluriel = -es)
    "analysis", "thesis", "basis", "crisis", "hypothesis", "diagnosis",
    "synthesis", "axis", "oasis",
    # Mots terminant en -s qui n'ont pas de singulier
    "news", "lens", "series", "species", "means", "physics",
    # Acronymes ≤ 3 chars
    "os", "ds", "ms", "us", "css", "iso", "api", "sql", "cli", "gui",
    "ai", "ml", "nlp", "nn", "rl", "iot", "vr", "ar", "ux", "ui",
    "rs", "go", "js", "ts",
})

# Mots dont le pluriel est irrégulier — mapping explicite singulier
_IRREGULAR_PLURALS: dict[str, str] = {
    "children": "child",
    "people": "person",
    "men": "man",
    "women": "woman",
    "feet": "foot",
    "teeth": "tooth",
    "geese": "goose",
    "mice": "mouse",
    "data": "data",  # data = pluriel de datum mais usage courant = invariant
    "media": "media",
    "criteria": "criterion",
    "phenomena": "phenomenon",
}


def _strip_accents(s: str) -> str:
    """NFKD + drop combining marks (é → e, ñ → n, ç → c…)."""
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", s)
        if not unicodedata.combining(ch)
    )


def _singularize_word(word: str) -> str:
    """Dépluralisation d'un mot en minuscules.

    Règles appliquées dans l'ordre :
      1. Conservation des mots de _KEEP_AS_IS et des mots ≤ 3 chars
      2. Pluriels irréguliers via _IRREGULAR_PLURALS
      3. -ies → -y (technologies → technology)
      4. -sses → -ss (classes → class)
      5. -shes / -ches → strip 2 chars (bushes → bush, branches → branch)
      6. -xes / -zes / -ses (≥ 4 chars, précédé d'une consonne) → strip 2
      7. -s final (sauf -ss, -us, -is, -os) → strip 1
    """
    if word in _KEEP_AS_IS or len(word) <= 3:
        return word
    if word in _IRREGULAR_PLURALS:
        return _IRREGULAR_PLURALS[word]
    if word.endswith("ies") and len(word) > 3:
        return word[:-3] + "y"
    if word.endswith("sses"):
        return word[:-2]
    if word.endswith(("shes", "ches")):
        return word[:-2]
    if word.endswith(("xes", "zes", "ses")) and len(word) >= 4:
        # boxes → box, quizzes → quiz, kisses → kiss
        # mais cases → case (e silencieux) — heuristique : si finit par "ses"
        # précédé d'une voyelle, on ne strip que le s final
        if word.endswith("ses") and len(word) >= 5 and word[-4] in "aeiouy":
            return word[:-1]
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us", "is", "os")):
        return word[:-1]
    return word


_PAREN_RE = re.compile(r"\([^)]*\)")
_PUNCT_RE = re.compile(r"[^\w\s-]")
_SPACE_RE = re.compile(r"\s+")


def normalize_theme(theme: str) -> str:
    """Normalise un thème pour dédupli syntactique.

    Transformations appliquées (dans l'ordre) :
      1. Strip accents (NFKD) — Mathématiques → Mathematiques
      2. Lowercase
      3. Strip parenthèses et leur contenu : 'Neural Nets (CS)' → 'neural nets'
      4. Strip ponctuation sauf espace + tiret
      5. Collapse les espaces multiples
      6. Strip stopwords (the, of, le, de…)
      7. Dépluralise chaque mot (Neural Networks → neural network)

    Args:
        theme: Le thème brut tel que retourné par le LLM Vision.

    Returns:
        La forme canonique normalisée (lowercase, mots espacés).
        Empty string si le thème est vide ou ne contient que du bruit.

    Exemples :
        normalize_theme("Machine Learning")         == "machine learning"
        normalize_theme("Machine learning")         == "machine learning"
        normalize_theme("Neural Networks (CS)")     == "neural network"
        normalize_theme("Mathématiques Appliquées") == "mathematique appliquee"
        normalize_theme("Statistics")               == "statistics"  # faux pluriel
    """
    if not theme or not isinstance(theme, str):
        return ""
    # 1. NFKD + lowercase
    s = _strip_accents(theme).lower()
    # 2. Strip parenthèses (et leur contenu)
    s = _PAREN_RE.sub(" ", s)
    # 3. Strip ponctuation (garde lettres/chiffres/_/espace/tiret)
    s = _PUNCT_RE.sub(" ", s)
    # 4. Tokenize + strip stopwords + dépluralisation
    words = [
        _singularize_word(w)
        for w in _SPACE_RE.split(s.strip())
        if w and w not in _STOPWORDS
    ]
    return " ".join(words)


def normalize_themes_batch(themes: Iterable[str]) -> dict[str, str]:
    """Normalise un lot de thèmes et retourne `{raw_theme: canonical_form}`.

    Préserve l'ordre d'itération de l'input. Skip les thèmes vides.
    """
    return {t: normalize_theme(t) for t in themes if t}


def group_by_canonical(themes: Iterable[str]) -> dict[str, list[str]]:
    """Regroupe les thèmes par leur forme canonique.

    Returns:
        `{canonical_form: [raw_theme, raw_theme, ...]}` — un cluster par
        forme canonique. Les thèmes qui se normalisent à "" sont skippés.
    """
    groups: dict[str, list[str]] = {}
    for raw in themes:
        if not raw:
            continue
        canon = normalize_theme(raw)
        if not canon:
            continue
        groups.setdefault(canon, []).append(raw)
    return groups
