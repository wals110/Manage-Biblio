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

__version__ = "1.0.0"

import os
import io
import re
import json
import base64
import time
from typing import Optional, List, Tuple, Dict

try:
    from pdf2image import convert_from_path
    HAS_PDF2IMAGE = True
except ImportError:
    HAS_PDF2IMAGE = False

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import requests as req_lib
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

from lib.logger import get_logger

log = get_logger()


# ════════════════════════════════════════════════════════════════════════════
# CONFIGURATION API
# ════════════════════════════════════════════════════════════════════════════

SILICONFLOW_ENDPOINT = "https://api.siliconflow.com/v1/chat/completions"
DEFAULT_MODEL = "Qwen/Qwen3-VL-8B-Instruct"

VISION_PROMPT = """Analyze this book cover image. Extract the following information and respond ONLY with a valid JSON object, nothing else:

{
  "title": "the book title (in the original language of the book)",
  "author": "the author name(s), or empty string if not visible",
  "theme": "the main topic/discipline in English (e.g. Mathematics, Computer Science, Physics, Chemistry, Biology, Medicine, Philosophy, History, Economics, Law, Psychology, Religion, Literature, Art, Music, Cooking, Sports, Photography, Engineering, Electronics, Networking, Programming, Machine Learning, Data Science, etc.)",
  "language": "the main language of the book (fr, en, ar, de, es, etc.)",
  "confidence": 0.0 to 1.0
}

Rules:
- If you cannot read the title clearly, set confidence below 0.3
- If the image is not a book cover, set all fields to empty strings and confidence to 0
- Keep the title exactly as written on the cover (preserve original language and case)
- For author, use "Firstname Lastname" format if possible
- Be specific with the theme (e.g. "Machine Learning" not just "Computer Science")"""

VISION_PROMPT_MULTI = """You are given multiple pages from a book (cover and first pages). Extract the following information and respond ONLY with a valid JSON object, nothing else:

{
  "title": "the specific book title (NOT the series/collection name)",
  "author": "the author name(s), or empty string if not visible",
  "theme": "the main topic/discipline in English (e.g. Mathematics, Computer Science, Physics, Chemistry, Biology, Medicine, Philosophy, History, Economics, Law, Psychology, Religion, Literature, Art, Music, Cooking, Sports, Photography, Engineering, Electronics, Networking, Programming, Machine Learning, Data Science, etc.)",
  "language": "the main language of the book (fr, en, ar, de, es, etc.)",
  "confidence": 0.0 to 1.0
}

Rules:
- IMPORTANT: Look for the SPECIFIC title of this book, not the collection/series name (e.g. not "Lecture Notes in Computer Science" but the actual book title)
- The title page is often on the 2nd or 3rd page, not the cover
- If you cannot read the title clearly, set confidence below 0.3
- If the images are not from a book, set all fields to empty strings and confidence to 0
- Keep the title exactly as written (preserve original language and case)
- For author, use "Firstname Lastname" format if possible
- Be specific with the theme (e.g. "Machine Learning" not just "Computer Science")"""


# ════════════════════════════════════════════════════════════════════════════
# EXTRACTION COUVERTURE
# ════════════════════════════════════════════════════════════════════════════

def extract_cover_image(pdf_path: str, dpi: int = 150,
                        n_pages: int = 1) -> Optional[List['Image.Image']]:
    """
    Extrait les N premières pages du PDF comme images PIL.

    Args:
        pdf_path: Chemin vers le fichier PDF
        dpi: Résolution de l'extraction (par défaut 150 dpi)
        n_pages: Nombre de pages à extraire (par défaut 1)

    Returns:
        Liste d'images PIL ou None si extraction échoue
    """
    if not HAS_PDF2IMAGE:
        log.warning("  ⚠ pdf2image non installé (pip install pdf2image)")
        return None
    try:
        images = convert_from_path(
            pdf_path,
            first_page=1,
            last_page=n_pages,
            dpi=dpi,
            fmt='png',
            thread_count=2,
        )
        return images if images else None
    except Exception as e:
        log.error(f"  ⚠ Erreur extraction couverture: {e}")
        return None


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
    img.save(buffer, format='JPEG', quality=80)
    return base64.b64encode(buffer.getvalue()).decode('utf-8')


# ════════════════════════════════════════════════════════════════════════════
# APPEL API LLM VISION
# ════════════════════════════════════════════════════════════════════════════

def call_vision_api(images_base64, api_key: str, endpoint: str,
                    model: str = DEFAULT_MODEL,
                    timeout: int = 30,
                    max_retries: int = 3) -> Optional[Dict]:
    """
    Envoie une ou plusieurs images au modèle LLM Vision et parse la réponse JSON.

    Compatible avec plusieurs endpoints (SiliconFlow, Ollama, etc.).

    Args:
        images_base64: String base64 (une image) ou liste de strings base64 (multi-pages)
        api_key: Clé API pour l'authentification (peut être vide pour Ollama local)
        endpoint: URL de l'API LLM Vision (e.g., https://api.siliconflow.com/v1/chat/completions)
        model: Nom du modèle à utiliser (par défaut Qwen3-VL-8B-Instruct)
        timeout: Timeout en secondes (par défaut 30)
        max_retries: Nombre de tentatives en cas d'erreur (par défaut 3)

    Returns:
        Dict avec title, author, theme, language, confidence ou None en cas d'erreur
    """
    if not HAS_REQUESTS:
        log.warning("  ⚠ requests non installé (pip install requests)")
        return None

    # Normaliser : accepte une string ou une liste
    if isinstance(images_base64, str):
        images_base64 = [images_base64]

    headers = {
        "Content-Type": "application/json",
    }

    # Ajouter Authorization seulement si la clé est fournie (Ollama local n'en a pas besoin)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # Construire le contenu avec toutes les images + le prompt
    content_parts = []  # type: List[Dict]
    for i, img_b64 in enumerate(images_base64):
        content_parts.append({
            "type": "image_url",
            "image_url": {
                "url": "data:image/jpeg;base64,{}".format(img_b64),
            },
        })

    # Choisir le prompt adapté (mono ou multi-pages)
    prompt = VISION_PROMPT_MULTI if len(images_base64) > 1 else VISION_PROMPT
    content_parts.append({
        "type": "text",
        "text": prompt,
    })

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": content_parts,
            }
        ],
        "max_tokens": 300,
        "temperature": 0.1,
    }

    for attempt in range(max_retries):
        try:
            resp = req_lib.post(
                endpoint,
                headers=headers,
                json=payload,
                timeout=timeout,
            )

            if resp.status_code == 429:
                # Rate limit — attendre et réessayer
                wait = min(2 ** attempt * 2, 30)
                log.info(f"  ⏳ Rate limit, attente {wait}s...")
                time.sleep(wait)
                continue

            if resp.status_code != 200:
                log.error(f"  ⚠ API erreur {resp.status_code}: {resp.text[:200]}")
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                return None

            data = resp.json()
            content = data['choices'][0]['message']['content'].strip()

            # Parser le JSON de la réponse
            return parse_vision_response(content)

        except req_lib.exceptions.Timeout:
            log.info(f"  ⏳ Timeout (tentative {attempt + 1}/{max_retries})")
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
            return None
        except Exception as e:
            log.error(f"  ⚠ Erreur API: {e}")
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
            return None

    return None


def parse_vision_response(content: str) -> Optional[Dict]:
    """
    Parse la réponse JSON du LLM Vision.

    Extrait et valide le JSON, en gérant les cas où le LLM
    ajoute du texte avant/après le JSON.

    Args:
        content: Contenu brut de la réponse API

    Returns:
        Dict normalisé avec title, author, theme, language, confidence ou None
    """
    # Extraire le JSON de la réponse (le LLM peut ajouter du texte autour)
    json_match = re.search(r'\{[^{}]*\}', content, re.DOTALL)
    if not json_match:
        # Essayer avec des accolades imbriquées
        json_match = re.search(r'\{.*\}', content, re.DOTALL)

    if not json_match:
        log.warning(f"  ⚠ Pas de JSON dans la réponse: {content[:100]}")
        return None

    try:
        result = json.loads(json_match.group())

        # Valider et normaliser
        return {
            'title': str(result.get('title', '')).strip(),
            'author': str(result.get('author', '')).strip(),
            'theme': str(result.get('theme', '')).strip(),
            'language': str(result.get('language', '')).strip(),
            'confidence': float(result.get('confidence', 0.0)),
        }
    except (json.JSONDecodeError, ValueError) as e:
        log.warning(f"  ⚠ JSON invalide: {e} — contenu: {content[:150]}")
        return None


# ════════════════════════════════════════════════════════════════════════════
# FONCTION CONVENIANCE
# ════════════════════════════════════════════════════════════════════════════

def analyze_cover(pdf_path: str, api_key: str, endpoint: str,
                  model: str = DEFAULT_MODEL,
                  dpi: int = 150,
                  verbose: bool = False,
                  max_retries: int = 3,
                  n_pages: int = 1) -> Optional[Dict]:
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
        if result and result['confidence'] > 0.5:
            log.info(f"Title: {result['title']}")
    """
    n_pages = max(1, min(n_pages, 5))  # Borner entre 1 et 5

    if verbose:
        pages_label = "page 1" if n_pages == 1 else "pages 1-{}".format(n_pages)
        log.info("📖 Analyse : {} ({})".format(os.path.basename(pdf_path), pages_label))

    # Étape 1: Extraire les pages
    if verbose:
        log.info("  → Extraction des pages...")
    images = extract_cover_image(pdf_path, dpi=dpi, n_pages=n_pages)
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
    result = call_vision_api(images_base64, api_key, endpoint, model, max_retries=max_retries)

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
