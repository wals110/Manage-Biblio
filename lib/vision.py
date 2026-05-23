#!/usr/bin/env python3
"""
LLM Vision module — Extraction et analyse de couvertures PDF.
=============================================================================
Fournit les fonctions pour :

  1. Extraire la première page d'un PDF comme image
  2. Convertir l'image en base64
  3. Envoyer l'image à un modèle LLM Vision (SiliconFlow, Ollama, etc.)
  4. Parser la réponse JSON

Compatible avec Python 3.9+ (pas de syntaxe 3.10+).

Usage:
    from lib.vision import analyze_cover

    # Analyser une couverture via SiliconFlow
    result = analyze_cover(
        "/chemin/vers/livre.pdf",
        api_key="sk-xxx",
        endpoint="https://api.siliconflow.com/v1/chat/completions",
        model="Qwen/Qwen3-VL-8B-Instruct"
    )

    if result:
        log.info(f"Titre: {result['title']}")
        log.info(f"Auteur: {result['author']}")
        log.info(f"Thème: {result['theme']}")
        log.info(f"Confiance: {result['confidence']}")

    # Analyser via Ollama (local)
    result = analyze_cover(
        "/chemin/vers/livre.pdf",
        api_key="no-key",
        endpoint="http://localhost:11434/api/chat",
        model="llama2-vision"
    )
"""


import base64
import io
import json
import os

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

from lib.constants import (
    JPEG_QUALITY,
    LLM_MAX_RETRIES,
    LLM_TIMEOUT,
    LLM_VISION_MAX_TOKENS,
    PDF_DPI,
    PDF_MAX_PAGES,
)
from lib.llm_client import LLMClient
from lib.logger import get_logger

log = get_logger()


# ════════════════════════════════════════════════════════════════════════════
# CONFIGURATION API
# ════════════════════════════════════════════════════════════════════════════

SILICONFLOW_ENDPOINT = "https://api.siliconflow.com/v1/chat/completions"
DEFAULT_MODEL = "Qwen/Qwen3-VL-8B-Instruct"

VISION_PROMPT = """Analyze this book cover image. Respond ONLY with a valid JSON object, nothing else:

{
  "title": "the book title (in the original language of the book)",
  "author": "the author name(s), or empty string if not visible",
  "themes": [
    {"theme": "primary topic", "confidence": 0.0 to 1.0, "reason": "why"},
    {"theme": "alt topic if ambiguous", "confidence": 0.0 to 1.0, "reason": "why"}
  ],
  "language": "the main language of the book (fr, en, ar, de, es, etc.)",
  "confidence": 0.0 to 1.0
}

Rules for `themes`:
- Provide 1 to 3 candidate themes in English, ranked by confidence.
- Be SPECIFIC: prefer "Machine Learning" over "Computer Science",
  "Topology" over "Mathematics", "Quantum Mechanics" over "Physics".
- If the title contains TECHNICAL terms (stochastic, bayesian, markov,
  algorithm, gradient, neural, optimization, …), include the matching
  technical discipline as a candidate even when the cover style suggests
  another field (e.g. a humanities-styled cover on a math book).
- If a theme is ambiguous between fields (e.g. "Learning" can mean
  Machine Learning OR Educational Psychology), include BOTH candidates.
- For each candidate, give a 1-line `reason` from the cover/title text.

Other rules:
- If you cannot read the title clearly, set top-level confidence below 0.3.
- If the image is not a book cover, set all fields to empty strings.
- Keep the title exactly as written on the cover (original language + case).
- For author, use "Firstname Lastname" format if possible."""

VISION_PROMPT_MULTI = """You are given multiple pages from a book (cover + first pages). Respond ONLY with a valid JSON object:

{
  "title": "the SPECIFIC book title (NOT the series/collection name)",
  "author": "the author name(s), or empty string if not visible",
  "themes": [
    {"theme": "primary topic", "confidence": 0.0 to 1.0, "reason": "why (cite TOC/title)"},
    {"theme": "alt topic if ambiguous", "confidence": 0.0 to 1.0, "reason": "why"}
  ],
  "language": "main language (fr, en, ar, de, es, etc.)",
  "confidence": 0.0 to 1.0
}

Rules for `themes`:
- 1 to 3 candidates in English, ranked by confidence.
- Use the table of contents / chapter titles when visible to identify
  the technical discipline. The TOC is often more decisive than the cover.
- Be SPECIFIC: prefer "Machine Learning" over "Computer Science",
  "Topology" over "Mathematics", "Quantum Mechanics" over "Physics".
- For ambiguous books (mathematical psychology, computational linguistics,
  applied ethics, etc.), include BOTH candidate disciplines.
- Each candidate has a 1-line `reason` citing what you saw in the pages.

Other rules:
- IMPORTANT: identify the SPECIFIC title of this book, not the
  collection/series name (e.g. not "Lecture Notes in Computer Science"
  but the actual book title — usually on page 2-3).
- If you cannot read the title clearly, set top-level confidence < 0.3.
- If the images are not from a book, set all fields to empty strings.
- Preserve title exactly (original language and case)."""


# ════════════════════════════════════════════════════════════════════════════
# EXTRACTION COUVERTURE
# ════════════════════════════════════════════════════════════════════════════

def extract_cover_image(
    pdf_path: str,
    dpi: int = PDF_DPI,
    n_pages: int = 1,
    n_candidates: int = 0,
) -> list['Image.Image'] | None:
    """
    Extrait les pages les plus informatives du PDF comme images PIL.

    Modes:
    - n_candidates <= n_pages : extraction contiguë des n_pages premières
      (comportement legacy).
    - n_candidates > n_pages : smart page selection — score les
      n_candidates premières pages via pypdf (text density), retient les
      n_pages les plus informatives. Evite d'envoyer des pages blanches
      ou de garde au LLM, capte mieux la TOC.

    Passe par le cache mémoire intra-run `lib.pdf_cover.get_cover_image()`
    pour mutualiser l'appel Poppler entre rename, classify et viewer Curation.

    Args:
        pdf_path: Chemin vers le fichier PDF
        dpi: Résolution de l'extraction (par défaut 150 dpi)
        n_pages: Nombre de pages à retourner pour le LLM (par défaut 1)
        n_candidates: Nombre de pages à scanner pour la sélection
                      (>= n_pages active la smart selection ; 0/legacy = contiguë)

    Returns:
        Liste d'images PIL ou None si extraction échoue
    """
    from lib.pdf_cover import get_cover_image

    if n_candidates and n_candidates > n_pages:
        from lib.page_selector import select_top_pages
        indices = select_top_pages(pdf_path, n_candidates=n_candidates,
                                   n_keep=n_pages)
        if indices:
            return get_cover_image(pdf_path, dpi=dpi,
                                   page_indices=tuple(indices))
        # fallthrough: select_top_pages returned empty → legacy behavior

    return get_cover_image(pdf_path, dpi=dpi, n_pages=n_pages)


def image_to_base64(img: 'Image.Image', max_size: int = 1024) -> str:
    """
    Convertit une image PIL en base64 (JPEG) en la redimensionnant si besoin.
    On réduit la taille pour minimiser les tokens/coûts API.

    Args:
        img: Image PIL à convertir
        max_size: Dimension maximale (par défaut 1024 px)

    Returns:
        String base64 JPEG encodée
    """
    # Redimensionner si trop grande
    w, h = img.size
    if max(w, h) > max_size:
        ratio = max_size / max(w, h)
        new_w = int(w * ratio)
        new_h = int(h * ratio)
        img = img.resize((new_w, new_h), Image.LANCZOS)

    # Convertir en JPEG (plus léger que PNG)
    if img.mode == 'RGBA':
        img = img.convert('RGB')

    buffer = io.BytesIO()
    img.save(buffer, format='JPEG', quality=JPEG_QUALITY)
    return base64.b64encode(buffer.getvalue()).decode('utf-8')


# ════════════════════════════════════════════════════════════════════════════
# APPEL API LLM VISION
# ════════════════════════════════════════════════════════════════════════════

def call_vision_api(images_base64: object, api_key: str, endpoint: str,
                    model: str = DEFAULT_MODEL,
                    timeout: int = LLM_TIMEOUT, max_retries: int = LLM_MAX_RETRIES,
                    client: 'LLMClient | None' = None) -> dict | None:
    """
    Envoie une ou plusieurs images au modèle LLM Vision et parse la réponse JSON.

    Compatible avec plusieurs endpoints (SiliconFlow, Ollama, etc.).
    Utilise LLMClient pour le transport HTTP (retry, backoff, logging).

    Args:
        images_base64: String base64 (une image) ou liste de strings base64 (multi-pages)
        api_key: Clé API pour l'authentification (peut être vide pour Ollama local)
        endpoint: URL de l'API LLM Vision
        model: Nom du modèle à utiliser (par défaut Qwen3-VL-8B-Instruct)
        timeout: Timeout en secondes (par défaut 30)
        max_retries: Nombre de tentatives en cas d'erreur (par défaut 3)
        client: Instance LLMClient pré-configurée (optionnel, créée si absente)

    Returns:
        Dict avec title, author, theme, language, confidence ou None en cas d'erreur
    """
    # Normaliser : accepte une string ou une liste
    if isinstance(images_base64, str):
        images_base64 = [images_base64]

    # Choisir le prompt adapté (mono ou multi-pages)
    prompt = VISION_PROMPT_MULTI if len(images_base64) > 1 else VISION_PROMPT

    # Créer un client si non fourni (rétrocompatibilité)
    if client is None:
        client = LLMClient(
            api_key=api_key, endpoint=endpoint, model=model,
            timeout=timeout, max_retries=max_retries, verbose=True)

    content = client.call(
        prompt=prompt,
        images_b64=images_base64,
        max_tokens=LLM_VISION_MAX_TOKENS,
        timeout=timeout,
        max_retries=max_retries,
    )

    if content is None:
        return None

    return parse_vision_response(content)


def _extract_outermost_json(content: str) -> str | None:
    """Find the first {...} block in `content` whose braces are balanced.

    The previous regex-based approach failed when the JSON contained nested
    objects (e.g. `themes: [{theme: ...}]`) — `\\{[^{}]*\\}` matched the
    INNER theme object first instead of the outer wrapping one. This
    counter-based scan returns the outermost JSON object reliably.
    """
    if not content:
        return None
    start = content.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(content)):
        c = content[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return content[start:i + 1]
    return None


def parse_vision_response(content: str) -> dict | None:
    """
    Parse la réponse JSON du LLM Vision.

    Extrait et valide le JSON, en gérant les cas où le LLM
    ajoute du texte avant/après le JSON.

    Args:
        content: Contenu brut de la réponse API

    Returns:
        Dict normalisé avec title, author, theme, language, confidence ou None
    """
    # Extraire le JSON outermost de la réponse (le LLM peut ajouter du
    # texte autour). On compte les accolades pour trouver l'objet englobant
    # — important depuis prompt v2 qui contient des objets imbriqués
    # dans `themes: [{...}, {...}]`.
    json_str = _extract_outermost_json(content)
    if not json_str:
        log.warning(f"  ⚠ Pas de JSON dans la réponse: {content[:500]}")
        return None

    try:
        result = json.loads(json_str)

        # Normalize themes[] — support both new (themes array) and legacy
        # (single 'theme' string) shapes for backward compat.
        themes_list: list[dict] = []
        raw_themes = result.get("themes")
        if isinstance(raw_themes, list):
            for item in raw_themes:
                if not isinstance(item, dict):
                    continue
                t = str(item.get("theme", "")).strip()
                if not t:
                    continue
                themes_list.append({
                    "theme": t,
                    "confidence": float(item.get("confidence", 0.0)),
                    "reason": str(item.get("reason", "")).strip(),
                })
        # Legacy fallback: a single 'theme' string
        if not themes_list:
            t = str(result.get("theme", "")).strip()
            if t:
                themes_list.append({
                    "theme": t,
                    "confidence": float(result.get("confidence", 0.0)),
                    "reason": "",
                })

        # Top theme exposed under 'theme' for backward compat with all
        # existing callers (classifier, mapper, dashboard, etc.)
        top_theme = themes_list[0]["theme"] if themes_list else ""

        return {
            "title": str(result.get("title", "")).strip(),
            "author": str(result.get("author", "")).strip(),
            "theme": top_theme,
            "themes": themes_list,
            "language": str(result.get("language", "")).strip(),
            "confidence": float(result.get("confidence", 0.0)),
        }
    except (json.JSONDecodeError, ValueError) as e:
        log.warning(f"  ⚠ JSON invalide: {e} — contenu: {content[:150]}")
        return None


# ════════════════════════════════════════════════════════════════════════════
# FONCTION CONVENIANCE
# ════════════════════════════════════════════════════════════════════════════

def analyze_cover(pdf_path: str, api_key: str = '', endpoint: str = '',
                  model: str = DEFAULT_MODEL,
                  dpi: int = 150,
                  verbose: bool = False,
                  max_retries: int = 3,
                  n_pages: int = 1,
                  n_candidates: int = 0,
                  client: 'LLMClient | None' = None) -> dict | None:
    """
    Analyse complète d'une couverture de livre (une ou plusieurs pages).

    Enchaîne les étapes : extraction image(s) → conversion base64 → appel API → parsing.

    Args:
        pdf_path: Chemin vers le fichier PDF
        api_key: Clé API pour l'authentification
        endpoint: URL de l'API LLM Vision
        model: Nom du modèle à utiliser (par défaut Qwen3-VL-8B-Instruct)
        dpi: Résolution d'extraction (par défaut 150 dpi)
        verbose: Afficher les détails du traitement
        max_retries: Nombre de tentatives en cas d'erreur (par défaut 3)
        n_pages: Nombre de pages à analyser (par défaut 1, max 5)

    Returns:
        Dict avec title, author, theme, language, confidence ou None en cas d'erreur

    Example:
        result = analyze_cover(
            "/path/to/book.pdf",
            api_key="sk-xxx",
            endpoint="https://api.siliconflow.com/v1/chat/completions",
            model="Qwen/Qwen3-VL-8B-Instruct",
            verbose=True,
            n_pages=2
        )
        if result and result['confidence'] > CONFIDENCE_THRESHOLD:
            log.info(f"Title: {result['title']}")
    """
    n_pages = max(1, min(n_pages, PDF_MAX_PAGES))  # Borner entre 1 et PDF_MAX_PAGES

    if verbose:
        pages_label = "page 1" if n_pages == 1 else "pages 1-{}".format(n_pages)
        log.info("📖 Analyse : {} ({})".format(os.path.basename(pdf_path), pages_label))

    # Étape 1: Extraire les pages
    if verbose:
        log.info("  → Extraction des pages...")
    images = extract_cover_image(pdf_path, dpi=dpi, n_pages=n_pages,
                                 n_candidates=n_candidates)
    if not images:
        if verbose:
            log.info("  ✗ Échec extraction image")
        return {'error': 'extraction'}

    if verbose:
        log.info("  → {} page(s) extraite(s)".format(len(images)))

    # Étape 2: Convertir en base64
    if verbose:
        log.info("  → Conversion base64...")
    images_base64 = [image_to_base64(img) for img in images]

    # Étape 3: Appeler l'API
    if verbose:
        log.info("  → Appel Vision API ({})...".format(model))
    result = call_vision_api(images_base64, api_key, endpoint, model,
                             max_retries=max_retries, client=client)

    if verbose:
        if result:
            log.info("  ✓ Détecté : '{}' — '{}' (confiance: {:.2f})".format(
                result.get('title', ''), result.get('author', ''),
                result.get('confidence', 0)))
        else:
            log.info("  ✗ Échec appel API")

    if result is None:
        return {'error': 'api'}

    return result


# ════════════════════════════════════════════════════════════════════════════
# VERSION CACHÉE (persistance JSON sur disque)
# ════════════════════════════════════════════════════════════════════════════

def analyze_cover_cached(pdf_path: str, cache_path: 'str | os.PathLike',
                         api_key: str = '', endpoint: str = '',
                         model: str = DEFAULT_MODEL,
                         dpi: int = 150,
                         verbose: bool = False,
                         max_retries: int = 3,
                         n_pages: int = 1,
                         n_candidates: int = 0,
                         client: 'LLMClient | None' = None) -> dict | None:
    """Version cachée de `analyze_cover()` — lookup JSON avant appel LLM.

    Cache hit → retourne le résultat direct (0 token, ~5 ms).
    Cache miss → appelle `analyze_cover()` puis stocke le résultat valide.

    Seules les réponses « utiles » sont cachées : title non vide et pas
    d'erreur. Les `{"error": ...}` sont retournés mais pas persistés (retry
    possible sur le run suivant sans avoir à vider le cache).

    Args:
        pdf_path: Chemin du PDF à analyser
        cache_path: Chemin du fichier JSON de cache (profile.cache_dir/vision_cache.json)
        Autres args : identiques à analyze_cover()

    Returns:
        Dict avec title/author/theme/language/confidence, ou {'error': ...}
    """
    from pathlib import Path as _Path

    from lib import vision_cache

    cache_path = _Path(cache_path)
    n_pages = max(1, min(n_pages, PDF_MAX_PAGES))

    key = vision_cache.compute_cache_key(
        pdf_path, model=model, n_pages=n_pages)

    if key is not None:
        cache = vision_cache.load_cache(cache_path)
        hit = vision_cache.lookup(cache, key)
        if hit is not None:
            if verbose:
                log.info("💾 Cache vision HIT : {}".format(
                    os.path.basename(pdf_path)))
            return hit
        vision_cache.note_miss()
    else:
        cache = None

    result = analyze_cover(
        pdf_path, api_key=api_key, endpoint=endpoint, model=model,
        dpi=dpi, verbose=verbose, max_retries=max_retries,
        n_pages=n_pages, n_candidates=n_candidates, client=client)

    if (
        key is not None
        and cache is not None
        and isinstance(result, dict)
        and not result.get('error')
        and result.get('title')
    ):
        vision_cache.store(cache, key, result, model=model)
        try:
            vision_cache.save_cache(cache_path, cache)
        except OSError as e:
            log.warning("  ⚠ Impossible d'écrire le cache vision: {}".format(e))

    return result
