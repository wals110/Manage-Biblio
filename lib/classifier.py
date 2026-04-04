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

import sys
import os
from typing import Optional, List, Tuple, Dict, Any, Callable

from lib.logger import get_logger

log = get_logger()


def classify_by_theme(
    theme: str,
    theme_mapping: Dict[str, str],
) -> Optional[str]:
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


def load_keyword_classifier(categories_yaml_path: str) -> Optional[Any]:
    """
    Load the KeywordClassifier from klodo_organizer.py.

    This function dynamically imports KeywordClassifier from the organiser/
    directory (located relative to project root). The organiser/ directory is
    added to sys.path, and the YAML config is used to instantiate the classifier.

    Args:
        categories_yaml_path: Path to categories.yaml (e.g., 'organiser/categories.yaml')

    Returns:
        KeywordClassifier instance if successful.
        None if import fails or YAML file not found.

    Example:
        >>> classifier = load_keyword_classifier('organiser/categories.yaml')
        >>> if classifier:
        ...     result = classifier.classify('machine learning text', 'example.pdf')
        ...     log.info(result)
    """
    if not os.path.exists(categories_yaml_path):
        log.warning(f"[WARNING] categories.yaml not found: {categories_yaml_path}")
        return None

    # Determine the organiser/ directory (contains klodo_organizer.py)
    # klodo_organizer.py is always in <project_root>/organiser/
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    organiser_dir = os.path.join(project_root, 'organiser')

    # Add organiser/ to sys.path if not already there
    if organiser_dir not in sys.path:
        sys.path.insert(0, organiser_dir)

    try:
        import yaml
        # Dynamic import of KeywordClassifier from klodo_organizer.py
        from klodo_organizer import KeywordClassifier

        # KeywordClassifier expects a parsed dict, not a file path
        with open(categories_yaml_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)

        classifier = KeywordClassifier(config)
        return classifier
    except ImportError as e:
        log.error(f"[ERROR] Failed to import KeywordClassifier from {organiser_dir}: {e}")
        return None
    except Exception as e:
        log.error(f"[ERROR] Failed to instantiate KeywordClassifier: {e}")
        return None


def classify_combined(
    vision_result: Dict[str, Any],
    filename: str,
    theme_mapping: Dict[str, str],
    classifier: Optional[Any] = None,
    llm_mapper: Optional[Any] = None,
    pdf_path: Optional[str] = None,
) -> Tuple[str, float, str]:
    """
    Combine LLM Vision theme classification, keyword classification,
    et LLM Mapper pour résolution intelligente des thèmes inconnus.

    Priorité :
    1. LLM theme si confiance >= 0.5 et thème trouvé dans mapping
    2. Keyword classifier (titre LLM + nom de fichier combinés)
    3. LLM Mapper — appel LLM texte pour résoudre un thème inconnu
    4. LLM theme avec confiance basse (< 0.5) si le thème matche
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

    # Priorité 1 : LLM theme si confiance >= 0.5
    if confidence >= 0.5:
        path = classify_by_theme(theme, theme_mapping)
        if path:
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
                        best[1] if len(best) > 1 else 0.5,  # score
                        "Keyword ({})".format(best[2] if len(best) > 2 else ''),
                    )
        except Exception as e:
            log.error("[WARNING] Keyword classifier error for {}: {}".format(filename, e))

    # Priorité 3 : LLM Mapper — résolution intelligente du thème inconnu
    # (avec escalade vision si activée dans le mapper)
    if llm_mapper and theme and confidence >= 0.5:
        mapped_path = llm_mapper.resolve(
            theme, title=title, filename=filename, pdf_path=pdf_path)
        if mapped_path:
            return (mapped_path, confidence * 0.9, "LLM (mapper)")

    # Priorité 4 : LLM theme avec confiance basse
    if theme:
        path = classify_by_theme(theme, theme_mapping)
        if path:
            return (path, confidence, "LLM (fallback)")

    # Priorité 5 : Échec total
    return (None, 0.0, "FAILED")


def make_classify_fn(
    theme_mapping: Dict[str, str],
) -> Callable[[str, float], Optional[str]]:
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

    def classify_fn(theme: str, confidence: float) -> Optional[str]:
        """
        Classify by theme using the provided theme_mapping.

        Args:
            theme: Theme string from LLM Vision.
            confidence: Confidence score (0.0 to 1.0).

        Returns:
            Path if found, None otherwise.
        """
        if confidence < 0.5:
            # Skip low-confidence results
            return None
        return classify_by_theme(theme, theme_mapping)

    return classify_fn
