"""
Theme and keyword classification module.

Combines two approaches:
1. Theme mapping (from LLM Vision results) -> path lookup
2. Keyword classifier (YAML-based + TF-IDF) -> fallback classification

Usage:
    from lib.classifier import (
        classify_by_theme,
        load_keyword_classifier,
        classify_combined,
        make_classify_fn,
    )

    # Load theme mapping from profile
    theme_mapping = {...}  # from profile YAML
    classifier = load_keyword_classifier('path/to/categories.yaml')

    # Classify by vision theme
    path = classify_by_theme('Machine Learning', theme_mapping)

    # Combined classification
    path, score, keyword_info = classify_combined(
        vision_result={'theme': 'Machine Learning', 'confidence': 0.95},
        filename='example.pdf',
        theme_mapping=theme_mapping,
        classifier=classifier,
    )

    # For reclassify() workflows
    classify_fn = make_classify_fn(theme_mapping)
"""

import os
from collections.abc import Callable

from lib.constants import CONFIDENCE_THRESHOLD, KEYWORD_DEFAULT_SCORE, MAPPER_PENALTY
from lib.logger import get_logger

log = get_logger()


def classify_by_theme(
    theme: str,
    theme_mapping: dict[str, str],
) -> str | None:
    """
    Map a theme string to a BIBLIO_V2 path using the provided theme mapping.

    Search order:
    1. Exact match (case-insensitive)
    2. Substring match (longest match wins)
    3. Reverse substring match (theme appears in mapping key)

    Args:
        theme: Theme string from LLM Vision (e.g., 'Machine Learning', 'Python')
        theme_mapping: Dict mapping theme strings to BIBLIO_V2 paths.
                      Loaded from profile YAML, NOT hardcoded.

    Returns:
        Path string (e.g., '02-INFORMATIQUE/05-IA-ML/Machine-Learning') if found.
        None if no match found.

    Example:
        >>> theme_mapping = {
        ...     'Machine Learning': '02-INFORMATIQUE/05-IA-ML/Machine-Learning',
        ...     'Python': '02-INFORMATIQUE/03-Langages-Programmation/Python',
        ... }
        >>> classify_by_theme('machine learning', theme_mapping)
        '02-INFORMATIQUE/05-IA-ML/Machine-Learning'
        >>> classify_by_theme('Python Programming', theme_mapping)
        '02-INFORMATIQUE/03-Langages-Programmation/Python'
    """
    if not theme or not theme_mapping:
        return None

    theme_lower = theme.lower().strip()

    # 1. Exact match (case-insensitive)
    for key, path in theme_mapping.items():
        if key.lower() == theme_lower:
            return path

    # 2. Substring match (longest wins)
    best_match = None
    best_length = 0
    for key, path in theme_mapping.items():
        key_lower = key.lower()
        if key_lower in theme_lower and len(key_lower) > best_length:
            best_match = path
            best_length = len(key_lower)

    if best_match:
        return best_match

    # 3. Reverse substring match (theme appears in mapping key)
    best_match = None
    best_length = 0
    for key, path in theme_mapping.items():
        key_lower = key.lower()
        if theme_lower in key_lower and len(theme_lower) > best_length:
            best_match = path
            best_length = len(theme_lower)

    return best_match


def load_keyword_classifier(categories_yaml_path: str) -> object | None:
    """
    Load the KeywordClassifier from lib/keyword_classifier.py.

    Args:
        categories_yaml_path: Path to categories.yaml

    Returns:
        KeywordClassifier instance if successful.
        None if import fails or YAML file not found.

    Example:
        >>> classifier = load_keyword_classifier('profiles/default/categories.yaml')
        >>> if classifier:
        ...     result = classifier.classify('machine learning text', 'example.pdf')
        ...     log.info(result)
    """
    if not os.path.exists(categories_yaml_path):
        log.warning(f"[WARNING] categories.yaml not found: {categories_yaml_path}")
        return None

    try:
        import yaml

        from lib.keyword_classifier import KeywordClassifier

        with open(categories_yaml_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)

        classifier = KeywordClassifier(config)
        return classifier
    except ImportError as e:
        log.error(f"[ERROR] Failed to import KeywordClassifier: {e}")
        return None
    except Exception as e:
        log.error(f"[ERROR] Failed to instantiate KeywordClassifier: {e}")
        return None


def _refine_to_subfolder(
    matched_path: str,
    theme_mapping: dict[str, str],
    theme: str = "",
    title: str = "",
    filename: str = "",
) -> str | None:
    """If matched_path is a generic / catchall, search title + filename for
    a more specific theme_mapping key that points to a related folder.

    Only triggers when:
    - The refinement key is STRICTLY LONGER than the original theme that
      matched (otherwise we'd risk overriding a correctly-specific theme
      with a less-specific keyword).
    - The refined path is a CHILD of matched_path (≥3 char keyword) OR a
      SIBLING under the same immediate parent (≥5 char keyword to limit
      false positives on short collisions).

    Cross-domain shifts (different top-level section) are NOT handled here
    — those go through the keyword classifier (P2) or the LLM mapper (P3).

    Examples:
        '05-RELIGIONS', theme='Religion', title="L'Islam et le Graal"
            → '05-RELIGIONS/ISLAM' (child, key='islam' (5) > 'religion' (8)?
              5 < 8 so this WOULDN'T trigger. Hmm see below.)

    Note on the strict-length rule: it's primarily for SIBLING scope, where
    we risk regressions like 'quantum mechanics' (correct) being overridden
    by 'mechanics' (sibling, shorter). For CHILD scope we keep the looser
    "any keyword in title that maps to a child" because by definition the
    child is more specific than the parent.
    """
    if not matched_path or not theme_mapping:
        return None
    text = (title + " " + filename).lower()
    if not text.strip():
        return None

    theme_len = len(theme.strip()) if theme else 0
    child_prefix = matched_path.rstrip("/") + "/"
    parts = matched_path.split("/")
    sibling_prefix = "/".join(parts[:-1]) + "/" if len(parts) > 1 else ""

    # Catchall detection: top-level section, or last segment hints at
    # being a fallback class. Catchalls allow looser sibling refinement
    # because the matched theme is admittedly generic.
    last = parts[-1].lower()
    is_catchall = (
        len(parts) == 1  # top-level (e.g. '05-RELIGIONS', '02-INFORMATIQUE')
        or "general" in last
        or "autres" in last
        or last.endswith("-general")
        or last.endswith("-other")
        or last.endswith("-misc")
    )

    best_child: tuple[str, int] | None = None
    best_sibling: tuple[str, int] | None = None
    for key, mapped in theme_mapping.items():
        if not isinstance(mapped, str):
            continue
        if mapped == matched_path:
            continue
        kl = key.lower().strip()
        if not kl or kl not in text:
            continue
        # CHILD scope: refine to a strict subfolder. The child is more
        # specific by definition, so we don't require the key to be longer
        # than the original theme.
        if mapped.startswith(child_prefix) and len(kl) >= 3:
            if best_child is None or len(kl) > best_child[1]:
                best_child = (mapped, len(kl))
            continue
        # SIBLING scope: when matched_path is a catchall (top-level section
        # or *-Generales/*-Autres) we allow short keys, since the original
        # theme is admittedly generic. Otherwise we require the new key to
        # be strictly longer than the theme to avoid regressions like
        # 'mechanics' (sibling) overriding the correctly-specific
        # 'quantum mechanics'.
        if (sibling_prefix and mapped.startswith(sibling_prefix)
                and len(kl) >= 5
                and (is_catchall or len(kl) > theme_len)):
            if best_sibling is None or len(kl) > best_sibling[1]:
                best_sibling = (mapped, len(kl))

    if best_child:
        return best_child[0]
    if best_sibling:
        return best_sibling[0]
    return None


def classify_combined(
    vision_result: dict[str, object],
    filename: str,
    theme_mapping: dict[str, str],
    classifier: object | None = None,
    llm_mapper: object | None = None,
    pdf_path: str | None = None,
) -> tuple[str | None, float, str]:
    """
    Combine LLM Vision theme classification, keyword classification,
    et LLM Mapper pour résolution intelligente des thèmes inconnus.

    Priorité :
    1. LLM theme si confiance >= CONFIDENCE_THRESHOLD et thème trouvé dans mapping
    2. Keyword classifier (titre LLM + nom de fichier combinés)
    3. LLM Mapper — appel LLM texte pour résoudre un thème inconnu
    4. LLM theme avec confiance basse (< CONFIDENCE_THRESHOLD) si le thème matche
    5. Échec total

    Args:
        vision_result: Dict avec 'theme' (str), 'confidence' (float),
                      optionnel 'title' (str).
        filename: Nom du fichier PDF.
        theme_mapping: Dict mapping thème → chemin cible.
        classifier: KeywordClassifier (optionnel).
        llm_mapper: LLMMapper instance (optionnel). Si fourni, sera appelé
                   quand le thème n'est pas dans le mapping.

    Returns:
        Tuple (path, score, source) :
        - path: chemin cible ou None
        - score: confiance (0.0 à 1.0)
        - source: "LLM (theme)", "Keyword", "LLM (mapper)", "LLM (fallback)", "FAILED"
    """
    theme = vision_result.get('theme', '')
    confidence = vision_result.get('confidence', 0.0)
    title = vision_result.get('title', '')

    # Priorité 1 : LLM theme si confiance >= CONFIDENCE_THRESHOLD
    if confidence >= CONFIDENCE_THRESHOLD:
        path = classify_by_theme(theme, theme_mapping)
        if path:
            # Sub-folder refinement: when the matched path is a top-level
            # parent (e.g. "05-RELIGIONS" with subfolders like /ISLAM,
            # /CHRISTIANISME), search title + filename for a more specific
            # theme_mapping entry that points INTO that parent. This catches
            # the very common case where Vision LLM returns "Religion" for
            # an Islam-specific book, or "Mathematics" for a Logic book.
            refined = _refine_to_subfolder(
                path, theme_mapping, theme=theme, title=title, filename=filename)
            if refined:
                return (refined, confidence, "LLM (theme→refined)")
            return (path, confidence, "LLM (theme)")

    # Priorité 2 : Keyword classifier (titre LLM + thème + nom de fichier)
    # On combine toutes les infos textuelles disponibles pour maximiser
    # les chances de matcher un mot-clé pertinent.
    if classifier:
        try:
            # Construire un texte enrichi : titre + thème + filename
            text_parts = []
            if title:
                text_parts.append(title)
            if theme:
                text_parts.append(theme)
            text_parts.append(filename)
            enriched_text = ' '.join(text_parts)

            keyword_results = classifier.classify(enriched_text, '')
            # classify() retourne une liste de (chemin, score, mot_clé)
            if keyword_results and len(keyword_results) > 0:
                best = keyword_results[0]  # Meilleur résultat
                if best[0]:  # chemin non vide
                    return (
                        best[0],                    # chemin
                        best[1] if len(best) > 1 else KEYWORD_DEFAULT_SCORE,  # score
                        "Keyword ({})".format(best[2] if len(best) > 2 else ''),
                    )
        except Exception as e:
            log.error("[WARNING] Keyword classifier error for {}: {}".format(filename, e))

    # Priorité 3 : LLM Mapper — résolution intelligente du thème inconnu
    # (avec escalade vision si activée dans le mapper)
    if llm_mapper and theme and confidence >= CONFIDENCE_THRESHOLD:
        mapped_path = llm_mapper.resolve(
            theme, title=title, filename=filename, pdf_path=pdf_path)
        if mapped_path:
            return (mapped_path, confidence * MAPPER_PENALTY, "LLM (mapper)")

    # Priorité 4 : LLM theme avec confiance basse
    if theme:
        path = classify_by_theme(theme, theme_mapping)
        if path:
            return (path, confidence, "LLM (fallback)")

    # Priorité 5 : Échec total
    return (None, 0.0, "FAILED")


def make_classify_fn(
    theme_mapping: dict[str, str],
) -> Callable[[str, float], str | None]:
    """
    Create a classification function suitable for CheckpointManager.reclassify().

    This returns a callable that maps (theme, confidence) -> path, using the
    provided theme_mapping. Useful for re-mapping vision results without
    re-running the LLM (via CheckpointManager.reclassify()).

    Args:
        theme_mapping: Dict mapping theme strings to BIBLIO_V2 paths.

    Returns:
        Callable with signature (theme: str, confidence: float) -> Optional[str]

    Example:
        >>> classify_fn = make_classify_fn(theme_mapping)
        >>> path = classify_fn('Machine Learning', 0.92)
        >>> log.info(path)
        '02-INFORMATIQUE/05-IA-ML/Machine-Learning'
    """

    def classify_fn(theme: str, confidence: float) -> str | None:
        """
        Classify by theme using the provided theme_mapping.

        Args:
            theme: Theme string from LLM Vision.
            confidence: Confidence score (0.0 to 1.0).

        Returns:
            Path if found, None otherwise.
        """
        if confidence < CONFIDENCE_THRESHOLD:
            # Skip low-confidence results
            return None
        return classify_by_theme(theme, theme_mapping)

    return classify_fn
