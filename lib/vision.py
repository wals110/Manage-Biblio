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
        print(f"Titre: {result['title']}")
        print(f"Auteur: {result['author']}")
        print(f"Thème: {result['theme']}")
        print(f"Confiance: {result['confidence']}")

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


# ════════════════════════════════════════════════════════════════════════════
# EXTRACTION COUVERTURE
# ════════════════════════════════════════════════════════════════════════════

def extract_cover_image(pdf_path: str, dpi: int = 150) -> Optional['Image.Image']:
    """
    Extrait la première page du PDF comme image PIL.

    Args:
        pdf_path: Chemin vers le fichier PDF
        dpi: Résolution de l'extraction (par défaut 150 dpi)

    Returns:
        Image PIL ou None si extraction échoue
    """
    if not HAS_PDF2IMAGE:
        print("  ⚠ pdf2image non installé (pip install pdf2image)")
        return None
    try:
        images = convert_from_path(
            pdf_path,
            first_page=1,
            last_page=1,
            dpi=dpi,
            fmt='png',
            thread_count=2,
        )
        return images[0] if images else None
    except Exception as e:
        print(f"  ⚠ Erreur extraction couverture: {e}")
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

def call_vision_api(img_base64: str, api_key: str, endpoint: str,
                    model: str = DEFAULT_MODEL,
                    timeout: int = 30,
                    max_retries: int = 3) -> Optional[Dict]:
    """
    Envoie l'image au modèle LLM Vision et parse la réponse JSON.

    Compatible avec plusieurs endpoints (SiliconFlow, Ollama, etc.).

    Args:
        img_base64: Image encodée en base64
        api_key: Clé API pour l'authentification (peut être vide pour Ollama local)
        endpoint: URL de l'API LLM Vision (e.g., https://api.siliconflow.com/v1/chat/completions)
        model: Nom du modèle à utiliser (par défaut Qwen3-VL-8B-Instruct)
        timeout: Timeout en secondes (par défaut 30)
        max_retries: Nombre de tentatives en cas d'erreur (par défaut 3)

    Returns:
        Dict avec title, author, theme, language, confidence ou None en cas d'erreur
    """
    if not HAS_REQUESTS:
        print("  ⚠ requests non installé (pip install requests)")
        return None

    headers = {
        "Content-Type": "application/json",
    }

    # Ajouter Authorization seulement si la clé est fournie (Ollama local n'en a pas besoin)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{img_base64}",
                        },
                    },
                    {
                        "type": "text",
                        "text": VISION_PROMPT,
                    },
                ],
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
                print(f"  ⏳ Rate limit, attente {wait}s...")
                time.sleep(wait)
                continue

            if resp.status_code != 200:
                print(f"  ⚠ API erreur {resp.status_code}: {resp.text[:200]}")
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                return None

            data = resp.json()
            content = data['choices'][0]['message']['content'].strip()

            # Parser le JSON de la réponse
            return parse_vision_response(content)

        except req_lib.exceptions.Timeout:
            print(f"  ⏳ Timeout (tentative {attempt + 1}/{max_retries})")
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
            return None
        except Exception as e:
            print(f"  ⚠ Erreur API: {e}")
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
        print(f"  ⚠ Pas de JSON dans la réponse: {content[:100]}")
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
        print(f"  ⚠ JSON invalide: {e} — contenu: {content[:150]}")
        return None


# ════════════════════════════════════════════════════════════════════════════
# FONCTION CONVENIANCE
# ════════════════════════════════════════════════════════════════════════════

def analyze_cover(pdf_path: str, api_key: str, endpoint: str,
                  model: str = DEFAULT_MODEL,
                  dpi: int = 150,
                  verbose: bool = False) -> Optional[Dict]:
    """
    Analyse complète d'une couverture de livre.

    Enchaîne les étapes : extraction image → conversion base64 → appel API → parsing.

    Args:
        pdf_path: Chemin vers le fichier PDF
        api_key: Clé API pour l'authentification
        endpoint: URL de l'API LLM Vision
        model: Nom du modèle à utiliser (par défaut Qwen3-VL-8B-Instruct)
        dpi: Résolution d'extraction (par défaut 150 dpi)
        verbose: Afficher les détails du traitement

    Returns:
        Dict avec title, author, theme, language, confidence ou None en cas d'erreur

    Example:
        result = analyze_cover(
            "/path/to/book.pdf",
            api_key="sk-xxx",
            endpoint="https://api.siliconflow.com/v1/chat/completions",
            model="Qwen/Qwen3-VL-8B-Instruct",
            verbose=True
        )
        if result and result['confidence'] > 0.5:
            print(f"Title: {result['title']}")
    """
    if verbose:
        print(f"📖 Analysing cover: {os.path.basename(pdf_path)}")

    # Étape 1: Extraire la couverture
    if verbose:
        print("  → Extracting cover image...")
    img = extract_cover_image(pdf_path, dpi=dpi)
    if not img:
        if verbose:
            print("  ✗ Failed to extract cover image")
        return None

    # Étape 2: Convertir en base64
    if verbose:
        print("  → Converting to base64...")
    img_base64 = image_to_base64(img)

    # Étape 3: Appeler l'API
    if verbose:
        print(f"  → Calling Vision API ({model})...")
    result = call_vision_api(img_base64, api_key, endpoint, model)

    if verbose:
        if result:
            print(f"  ✓ Success (confidence: {result['confidence']:.2f})")
        else:
            print("  ✗ API call failed")

    return result
