#!/usr/bin/env python3
"""
OCR Cover v3 — Migration de bibliothèque PDF via LLM Vision.
=============================================================================
Convertit la première page (couverture) d'un PDF en image, puis envoie
cette image à un modèle LLM Vision (Qwen3-VL-8B via SiliconFlow) pour :

  1. Identifier le titre et l'auteur depuis la couverture
  2. Détecter le thème / la discipline du livre
  3. Renommer le fichier vers le format "Titre - Auteur.pdf"
  4. Copier le fichier dans le bon dossier de BIBLIO_V2

Mode migration : scanne BIBLIO (source) et copie les fichiers identifiés
vers BIBLIO_V2 (cible) avec renommage + classement. L'original n'est
jamais modifié.

Usage :
    # Scanner et générer un rapport (dry-run)
    python3 ocr_cover.py /chemin/vers/BIBLIO --target /chemin/vers/BIBLIO_V2 --report

    # Limiter à N fichiers pour tester
    python3 ocr_cover.py /chemin/vers/BIBLIO --target /chemin/vers/BIBLIO_V2 --max 100 --report

    # Appliquer la migration (copie vers BIBLIO_V2)
    python3 ocr_cover.py /chemin/vers/BIBLIO --target /chemin/vers/BIBLIO_V2 --execute

    # Tester sur un seul fichier
    python3 ocr_cover.py /chemin/vers/fichier.pdf --api-key sk-xxx

Prérequis :
    brew install poppler           # macOS (pour pdf2image)
    pip3 install pdf2image Pillow pyyaml requests

Coût estimé : ~$5.20 pour 15000 images via SiliconFlow (Qwen3-VL-8B).
"""

__version__ = "3.0.0"

import os
import sys
import csv
import re
import io
import json
import base64
import time
import signal
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
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
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

try:
    import requests as req_lib
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


# ════════════════════════════════════════════════════════════════════════════
# CONFIGURATION API
# ════════════════════════════════════════════════════════════════════════════

SILICONFLOW_API_URL = "https://api.siliconflow.com/v1/chat/completions"
# Qwen2.5-VL-7B retiré de SiliconFlow le 17/03/2026 → remplacé par Qwen3-VL-8B
DEFAULT_MODEL = "Qwen/Qwen3-VL-8B-Instruct"

# Prompt envoyé au LLM Vision pour chaque couverture
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
    """Extrait la première page du PDF comme image PIL."""
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

def call_vision_api(img_base64: str, api_key: str,
                    model: str = DEFAULT_MODEL,
                    timeout: int = 30,
                    max_retries: int = 3) -> Optional[Dict]:
    """
    Envoie l'image au modèle LLM Vision via SiliconFlow et parse la réponse JSON.
    Retourne un dict avec title, author, theme, language, confidence.
    """
    if not HAS_REQUESTS:
        print("  ⚠ requests non installé (pip install requests)")
        return None

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

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
                SILICONFLOW_API_URL,
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
    """Parse la réponse JSON du LLM Vision."""
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
# RENOMMAGE
# ════════════════════════════════════════════════════════════════════════════

def sanitize_filename(text: str) -> str:
    """Nettoie un texte pour en faire un nom de fichier sûr."""
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', text)
    text = text.replace('\u201c', '').replace('\u201d', '')
    text = text.replace('\u2018', '').replace('\u2019', "'")
    text = re.sub(r'\.{2,}', '.', text)
    text = re.sub(r'\s+', ' ', text).strip()
    text = text.strip('. ')
    if len(text) > 200:
        text = text[:200].rsplit(' ', 1)[0]
    return text


def build_new_filename(title: str, author: str) -> Optional[str]:
    """
    Construit un nouveau nom de fichier au format "Titre - Auteur.pdf".
    Retourne None si le titre est trop court ou inexploitable.
    """
    title = sanitize_filename(title)
    author = sanitize_filename(author)

    if len(title) < 3:
        return None

    title = title_case_smart(title)

    if author and len(author) >= 2:
        author = title_case_smart(author)
        new_name = f"{title} - {author}.pdf"
    else:
        new_name = f"{title}.pdf"

    return new_name


def title_case_smart(text: str) -> str:
    """
    Capitalisation intelligente :
    - Premier mot toujours en majuscule
    - Mots courts (de, the, and, in, of, etc.) en minuscules sauf en début
    - Acronymes connus préservés (PDF, OCR, API, SQL, HTML, etc.)
    """
    small_words = {
        'a', 'an', 'the', 'and', 'but', 'or', 'nor', 'for', 'yet', 'so',
        'in', 'on', 'at', 'to', 'by', 'up', 'of', 'as', 'if',
        'de', 'du', 'des', 'le', 'la', 'les', 'un', 'une', 'et',
        'ou', 'en', 'au', 'aux', 'par', 'pour', 'sur', 'avec', 'dans',
    }
    acronyms = {
        'pdf', 'ocr', 'api', 'sql', 'html', 'css', 'xml', 'json',
        'http', 'https', 'url', 'tcp', 'ip', 'udp', 'dns', 'ftp',
        'gpu', 'cpu', 'ram', 'ssd', 'hdd', 'usb', 'led', 'lcd',
        'ai', 'ml', 'nlp', 'cnn', 'rnn', 'gan', 'svm', 'pca',
        'ios', 'macos', 'linux', 'unix',
        'isbn', 'doi', 'ieee', 'acm',
        'ii', 'iii', 'iv', 'vi', 'vii', 'viii', 'ix', 'xi', 'xii',
    }

    words = text.split()
    result = []
    for i, word in enumerate(words):
        lower = word.lower()
        if lower in acronyms:
            result.append(word.upper())
        elif i == 0:
            result.append(word.capitalize())
        elif lower in small_words:
            result.append(lower)
        elif word.isupper() and len(word) > 3:
            result.append(word.capitalize())
        else:
            if word[0].isupper():
                result.append(word)
            else:
                result.append(word.capitalize())

    return ' '.join(result)


def is_name_already_clean(filename: str) -> bool:
    """
    Vérifie si le nom de fichier est déjà propre (format "Titre - Auteur.pdf")
    ou s'il contient des artefacts typiques nécessitant un renommage.
    """
    name = os.path.splitext(filename)[0]

    junk_patterns = [
        r'^\d{2}[\s_]\d{4,}',        # 01_182383_ffirs
        r'^cid[\s_]?\d+',             # cid_9
        r'\.qxp',                      # fichiers QuarkXPress
        r'\.qxd',
        r'\.indd',                     # fichiers InDesign
        r'ffirs|fmatter|flast|ftoc',  # front/back matter
        r'^\d{10,}',                   # ISBN comme nom
        r'^[a-f0-9]{32}',             # hash MD5
        r'Page\s*\w+\s*$',            # "Page iii"
    ]
    for pattern in junk_patterns:
        if re.search(pattern, name, re.IGNORECASE):
            return False

    alpha_chars = re.sub(r'[^a-zA-Z\u00c0-\u017f]', '', name)
    if len(alpha_chars) < 5:
        return False

    return True


# ════════════════════════════════════════════════════════════════════════════
# CLASSIFICATION PAR THÈME LLM
# ════════════════════════════════════════════════════════════════════════════

# Mapping thème LLM → chemin dans l'arborescence BIBLIO
# Ce mapping est utilisé quand le LLM retourne un thème mais qu'on n'a pas
# le classifieur YAML chargé, ou en complément.
THEME_TO_PATH = {
    # ══════════════════════════════════════════════════════════════════════
    # 01-SCIENCES
    # ══════════════════════════════════════════════════════════════════════
    'mathematics': '01-SCIENCES/MATHEMATIQUES/08-Mathematiques-Generales',
    'algebra': '01-SCIENCES/MATHEMATIQUES/01-Algebre',
    'linear algebra': '01-SCIENCES/MATHEMATIQUES/01-Algebre',
    'calculus': '01-SCIENCES/MATHEMATIQUES/02-Analyse',
    'analysis': '01-SCIENCES/MATHEMATIQUES/02-Analyse',
    'mathematical analysis': '01-SCIENCES/MATHEMATIQUES/02-Analyse',
    'differential equations': '01-SCIENCES/MATHEMATIQUES/02-Analyse',
    'geometry': '01-SCIENCES/MATHEMATIQUES/03-Geometrie-Topologie',
    'topology': '01-SCIENCES/MATHEMATIQUES/03-Geometrie-Topologie',
    'statistics': '01-SCIENCES/MATHEMATIQUES/04-Probabilites-Statistiques',
    'probability': '01-SCIENCES/MATHEMATIQUES/04-Probabilites-Statistiques',
    'stochastic': '01-SCIENCES/MATHEMATIQUES/04-Probabilites-Statistiques',
    'logic': '01-SCIENCES/MATHEMATIQUES/05-Logique',
    'mathematical logic': '01-SCIENCES/MATHEMATIQUES/05-Logique',
    'number theory': '01-SCIENCES/MATHEMATIQUES/06-Theorie-des-Nombres',
    'numerical methods': '01-SCIENCES/MATHEMATIQUES/07-Calcul-Numerique',
    'numerical analysis': '01-SCIENCES/MATHEMATIQUES/07-Calcul-Numerique',
    'finite element': '01-SCIENCES/MATHEMATIQUES/07-Calcul-Numerique',
    'history of mathematics': '01-SCIENCES/MATHEMATIQUES/09-Histoire-Philosophie-Maths',
    'philosophy of mathematics': '01-SCIENCES/MATHEMATIQUES/09-Histoire-Philosophie-Maths',
    'operations research': '01-SCIENCES/MATHEMATIQUES/08-Mathematiques-Generales',

    'physics': '01-SCIENCES/PHYSIQUE',
    'mechanics': '01-SCIENCES/PHYSIQUE/01-Mecanique',
    'fluid mechanics': '01-SCIENCES/PHYSIQUE/01-Mecanique',
    'classical mechanics': '01-SCIENCES/PHYSIQUE/01-Mecanique',
    'electromagnetism': '01-SCIENCES/PHYSIQUE/02-Electromagnetisme-Electronique',
    'thermodynamics': '01-SCIENCES/PHYSIQUE/03-Thermodynamique',
    'statistical mechanics': '01-SCIENCES/PHYSIQUE/03-Thermodynamique',
    'optics': '01-SCIENCES/PHYSIQUE/04-Optique',
    'quantum physics': '01-SCIENCES/PHYSIQUE/05-Relativite-Quantique',
    'quantum mechanics': '01-SCIENCES/PHYSIQUE/05-Relativite-Quantique',
    'relativity': '01-SCIENCES/PHYSIQUE/05-Relativite-Quantique',
    'quantum': '01-SCIENCES/PHYSIQUE/05-Relativite-Quantique',
    'astrophysics': '01-SCIENCES/PHYSIQUE/06-Astrophysique-Cosmologie',
    'astronomy': '01-SCIENCES/PHYSIQUE/06-Astrophysique-Cosmologie',
    'cosmology': '01-SCIENCES/PHYSIQUE/06-Astrophysique-Cosmologie',
    'nuclear physics': '01-SCIENCES/PHYSIQUE/07-Physique-Nucleaire',
    'particle physics': '01-SCIENCES/PHYSIQUE/07-Physique-Nucleaire',

    'chemistry': '01-SCIENCES/CHIMIE',
    'organic chemistry': '01-SCIENCES/CHIMIE/01-Chimie-Organique',
    'physical chemistry': '01-SCIENCES/CHIMIE/02-Chimie-Physique',
    'nanotechnology': '01-SCIENCES/CHIMIE/03-Nanotechnologie',
    'crystallography': '01-SCIENCES/CHIMIE',
    'chemometrics': '01-SCIENCES/CHIMIE',
    'chemoinformatics': '01-SCIENCES/CHIMIE',

    'biology': '01-SCIENCES/BIOLOGIE',
    'genetics': '01-SCIENCES/BIOLOGIE/Genetique',
    'neuroscience': '01-SCIENCES/BIOLOGIE/Neurosciences',
    'ecology': '01-SCIENCES/BIOLOGIE/Ecologie',
    'environmental science': '01-SCIENCES/BIOLOGIE/Ecologie',
    'ecotoxicology': '01-SCIENCES/BIOLOGIE/Ecologie',
    'biotechnology': '01-SCIENCES/BIOLOGIE/Biotechnologie',
    'bioenergetics': '01-SCIENCES/BIOLOGIE',
    'cognitive science': '01-SCIENCES/BIOLOGIE/Neurosciences',

    'astronomy': '01-SCIENCES/ASTRONOMIE',

    'epistemology': '01-SCIENCES/EPISTEMOLOGIE',
    'nanoscience': '01-SCIENCES/PHYSIQUE',
    'geology': '01-SCIENCES',
    'geography': '01-SCIENCES',
    'integrated science': '01-SCIENCES',
    'science and technology': '01-SCIENCES',

    # ══════════════════════════════════════════════════════════════════════
    # 02-INFORMATIQUE
    # ══════════════════════════════════════════════════════════════════════
    'computer science': '02-INFORMATIQUE/01-Fondamentaux-CS',
    'theory of computation': '02-INFORMATIQUE/01-Fondamentaux-CS',
    'computer architecture': '02-INFORMATIQUE/01-Fondamentaux-CS',

    'algorithms': '02-INFORMATIQUE/02-Algorithmes-Structures',
    'data structures': '02-INFORMATIQUE/02-Algorithmes-Structures',

    'programming': '02-INFORMATIQUE/03-Langages-Programmation/Autres',
    'python': '02-INFORMATIQUE/03-Langages-Programmation/Python',
    'java': '02-INFORMATIQUE/03-Langages-Programmation/Java',
    'javascript': '02-INFORMATIQUE/03-Langages-Programmation/JavaScript',
    'typescript': '02-INFORMATIQUE/03-Langages-Programmation/JavaScript',
    'c programming': '02-INFORMATIQUE/03-Langages-Programmation/C-Cpp-CSharp',
    'c++': '02-INFORMATIQUE/03-Langages-Programmation/C-Cpp-CSharp',
    'c#': '02-INFORMATIQUE/03-Langages-Programmation/C-Cpp-CSharp',
    '.net': '02-INFORMATIQUE/03-Langages-Programmation/C-Cpp-CSharp',
    'rust': '02-INFORMATIQUE/03-Langages-Programmation/Autres',
    'go': '02-INFORMATIQUE/03-Langages-Programmation/Autres',
    'ruby': '02-INFORMATIQUE/03-Langages-Programmation/Autres',
    'scala': '02-INFORMATIQUE/03-Langages-Programmation/Autres',
    'kotlin': '02-INFORMATIQUE/03-Langages-Programmation/Autres',
    'haskell': '02-INFORMATIQUE/03-Langages-Programmation/Autres',
    'r programming': '02-INFORMATIQUE/03-Langages-Programmation/Autres',
    'perl': '02-INFORMATIQUE/03-Langages-Programmation/Autres',
    'php': '02-INFORMATIQUE/03-Langages-Programmation/Autres',
    'swift': '02-INFORMATIQUE/03-Langages-Programmation/Autres',

    'software engineering': '02-INFORMATIQUE/04-Genie-Logiciel',
    'software development': '02-INFORMATIQUE/04-Genie-Logiciel',
    'software quality': '02-INFORMATIQUE/04-Genie-Logiciel',
    'software architecture': '02-INFORMATIQUE/04-Genie-Logiciel',
    'design patterns': '02-INFORMATIQUE/04-Genie-Logiciel',
    'system design': '02-INFORMATIQUE/04-Genie-Logiciel',
    'systems theory': '02-INFORMATIQUE/04-Genie-Logiciel',
    'enterprise interoperability': '02-INFORMATIQUE/04-Genie-Logiciel',

    'artificial intelligence': '02-INFORMATIQUE/05-IA-ML',
    'machine learning': '02-INFORMATIQUE/05-IA-ML/Machine-Learning',
    'deep learning': '02-INFORMATIQUE/05-IA-ML/Deep-Learning',
    'neural network': '02-INFORMATIQUE/05-IA-ML/Deep-Learning',
    'natural language processing': '02-INFORMATIQUE/05-IA-ML/NLP',
    'nlp': '02-INFORMATIQUE/05-IA-ML/NLP',
    'computer vision': '02-INFORMATIQUE/05-IA-ML/Vision-par-Ordinateur',
    'image processing': '02-INFORMATIQUE/05-IA-ML/Vision-par-Ordinateur',
    'computational intelligence': '02-INFORMATIQUE/05-IA-ML',
    'soft computing': '02-INFORMATIQUE/05-IA-ML',
    'neural information processing': '02-INFORMATIQUE/05-IA-ML/Deep-Learning',
    'neural computing': '02-INFORMATIQUE/05-IA-ML/Deep-Learning',
    'cognitive computing': '02-INFORMATIQUE/05-IA-ML',
    'intelligent systems': '02-INFORMATIQUE/05-IA-ML',
    'intelligent computing': '02-INFORMATIQUE/05-IA-ML',
    'collective intelligence': '02-INFORMATIQUE/05-IA-ML',
    'multi-agent systems': '02-INFORMATIQUE/05-IA-ML',
    'genetic and evolutionary computation': '02-INFORMATIQUE/05-IA-ML',
    'machine translation': '02-INFORMATIQUE/05-IA-ML/NLP',
    'speech recognition': '02-INFORMATIQUE/05-IA-ML/NLP',
    'speech processing': '02-INFORMATIQUE/05-IA-ML/NLP',
    'speech technology': '02-INFORMATIQUE/05-IA-ML/NLP',
    'digital speech processing': '02-INFORMATIQUE/05-IA-ML/NLP',
    'chinese spoken language processing': '02-INFORMATIQUE/05-IA-ML/NLP',
    'translation technology': '02-INFORMATIQUE/05-IA-ML/NLP',
    'machine vision': '02-INFORMATIQUE/05-IA-ML/Vision-par-Ordinateur',
    'biometrics': '02-INFORMATIQUE/05-IA-ML/Vision-par-Ordinateur',
    'decision science': '02-INFORMATIQUE/05-IA-ML',
    'decision theory': '02-INFORMATIQUE/05-IA-ML',
    'decision-making': '02-INFORMATIQUE/05-IA-ML',
    'spatial reasoning': '02-INFORMATIQUE/05-IA-ML',
    'game ai': '02-INFORMATIQUE/05-IA-ML',
    'reinforcement learning': '02-INFORMATIQUE/05-IA-ML/Machine-Learning',
    'recommender systems': '02-INFORMATIQUE/05-IA-ML/Machine-Learning',

    'data science': '02-INFORMATIQUE/06-Data-Science',
    'big data': '02-INFORMATIQUE/06-Data-Science/Big-Data',
    'data mining': '02-INFORMATIQUE/06-Data-Science/Big-Data',
    'data engineering': '02-INFORMATIQUE/06-Data-Science/Big-Data',
    'data visualization': '02-INFORMATIQUE/06-Data-Science/Visualisation',
    'data analysis': '02-INFORMATIQUE/06-Data-Science',

    'databases': '02-INFORMATIQUE/07-Bases-de-Donnees',
    'sql': '02-INFORMATIQUE/07-Bases-de-Donnees',
    'nosql': '02-INFORMATIQUE/07-Bases-de-Donnees',

    'networking': '02-INFORMATIQUE/08-Reseaux-Telecom',
    'computer networks': '02-INFORMATIQUE/08-Reseaux-Telecom',
    'tcp/ip': '02-INFORMATIQUE/08-Reseaux-Telecom',

    'operating systems': '02-INFORMATIQUE/09-Systemes-OS',
    'linux': '02-INFORMATIQUE/09-Systemes-OS/Linux-Unix',
    'unix': '02-INFORMATIQUE/09-Systemes-OS/Linux-Unix',
    'windows': '02-INFORMATIQUE/09-Systemes-OS/Windows',
    'macos': '02-INFORMATIQUE/09-Systemes-OS/MacOS-iOS',
    'ios': '02-INFORMATIQUE/09-Systemes-OS/MacOS-iOS',

    'cybersecurity': '02-INFORMATIQUE/10-Securite-Crypto',
    'security': '02-INFORMATIQUE/10-Securite-Crypto',
    'cryptography': '02-INFORMATIQUE/10-Securite-Crypto',
    'hacking': '02-INFORMATIQUE/10-Securite-Crypto',
    'blockchain': '02-INFORMATIQUE/10-Securite-Crypto',
    'cryptocurrency': '02-INFORMATIQUE/10-Securite-Crypto',
    'cryptology': '02-INFORMATIQUE/10-Securite-Crypto',

    'devops': '02-INFORMATIQUE/11-Cloud-DevOps',
    'cloud computing': '02-INFORMATIQUE/11-Cloud-DevOps',
    'docker': '02-INFORMATIQUE/11-Cloud-DevOps',
    'kubernetes': '02-INFORMATIQUE/11-Cloud-DevOps',
    'it operations': '02-INFORMATIQUE/11-Cloud-DevOps',
    'virtualization': '02-INFORMATIQUE/11-Cloud-DevOps',

    'iot': '02-INFORMATIQUE/12-IoT-Embarque',
    'embedded systems': '02-INFORMATIQUE/12-IoT-Embarque',
    'arduino': '02-INFORMATIQUE/12-IoT-Embarque',
    'raspberry pi': '02-INFORMATIQUE/12-IoT-Embarque',

    'computer graphics': '02-INFORMATIQUE/13-Infographie-3D',
    '3d graphics': '02-INFORMATIQUE/13-Infographie-3D',
    'opengl': '02-INFORMATIQUE/13-Infographie-3D',
    'game engine': '02-INFORMATIQUE/13-Infographie-3D',

    'web development': '02-INFORMATIQUE/14-Web',
    'web design': '02-INFORMATIQUE/14-Web',
    'web accessibility': '02-INFORMATIQUE/14-Web',
    'web performance': '02-INFORMATIQUE/14-Web',
    'frontend': '02-INFORMATIQUE/14-Web',
    'backend': '02-INFORMATIQUE/14-Web',
    'graphic design': '02-INFORMATIQUE/14-Web',
    'interaction design': '02-INFORMATIQUE/14-Web',
    'user interface design': '02-INFORMATIQUE/14-Web',
    'user experience design': '02-INFORMATIQUE/14-Web',
    'user experience': '02-INFORMATIQUE/14-Web',
    'user experience (ux) design': '02-INFORMATIQUE/14-Web',
    'digital interface design': '02-INFORMATIQUE/14-Web',
    'information design': '02-INFORMATIQUE/14-Web',
    'design': '02-INFORMATIQUE/14-Web',
    'motion graphics': '02-INFORMATIQUE/14-Web',
    'digital multimedia': '02-INFORMATIQUE/14-Web',
    'digital media': '02-INFORMATIQUE/14-Web',
    'epublishing': '02-INFORMATIQUE/14-Web',
    'games user research': '02-INFORMATIQUE/14-Web',
    'interactive digital narrative': '02-INFORMATIQUE/14-Web',
    'gamification': '02-INFORMATIQUE/14-Web',

    'mobile development': '02-INFORMATIQUE/15-Mobile',
    'android': '02-INFORMATIQUE/15-Mobile',
    'mobile computing': '02-INFORMATIQUE/15-Mobile',
    'react native': '02-INFORMATIQUE/15-Mobile',

    'conference proceedings': '02-INFORMATIQUE/16-Conferences',
    'proceedings': '02-INFORMATIQUE/16-Conferences',
    'symposium': '02-INFORMATIQUE/16-Conferences',

    'game design': '02-INFORMATIQUE/13-Infographie-3D',
    'game development': '02-INFORMATIQUE/13-Infographie-3D',
    'games': '02-INFORMATIQUE/13-Infographie-3D',
    'gaming': '02-INFORMATIQUE/13-Infographie-3D',
    'video game design': '02-INFORMATIQUE/13-Infographie-3D',
    'virtual reality': '02-INFORMATIQUE/13-Infographie-3D',
    'augmented reality': '02-INFORMATIQUE/13-Infographie-3D',

    'application development': '02-INFORMATIQUE/04-Genie-Logiciel',
    'information systems': '02-INFORMATIQUE',
    'information technology': '02-INFORMATIQUE',
    'information science': '02-INFORMATIQUE',
    'information processing': '02-INFORMATIQUE',
    'parallel computing': '02-INFORMATIQUE',
    'high performance computing': '02-INFORMATIQUE',
    'high-performance parallel computing': '02-INFORMATIQUE',
    'scientific computing': '02-INFORMATIQUE',
    'scientific and technical computing': '02-INFORMATIQUE',
    'data compression': '02-INFORMATIQUE',
    'image compression': '02-INFORMATIQUE',
    'digital literacy': '02-INFORMATIQUE',
    'digital technologies': '02-INFORMATIQUE',
    'applied computing': '02-INFORMATIQUE',
    'technology': '02-INFORMATIQUE',
    'itil': '02-INFORMATIQUE',
    'geographic information systems': '02-INFORMATIQUE',
    'human-computer interaction': '02-INFORMATIQUE/14-Web',
    'information retrieval': '02-INFORMATIQUE/06-Data-Science',
    'game theory': '01-SCIENCES/MATHEMATIQUES/08-Mathematiques-Generales',
    'bioinformatics': '01-SCIENCES/BIOLOGIE/Biotechnologie',
    'computational science': '02-INFORMATIQUE',
    'scientific computation': '02-INFORMATIQUE',
    'conceptual modeling': '02-INFORMATIQUE/04-Genie-Logiciel',
    'blogging': '02-INFORMATIQUE/14-Web',
    'space science': '01-SCIENCES/ASTRONOMIE',
    'e-commerce': '09-BUSINESS/MARKETING',
    'media studies': '04-SHS/SOCIOLOGIE',
    'human factors': '03-INGENIERIE/ROBOTIQUE',
    'swarm intelligence': '02-INFORMATIQUE/05-IA-ML',
    'ethics': '04-SHS/PHILOSOPHIE',
    'parallel processing': '02-INFORMATIQUE',
    'healthcare': '06-MEDECINE',
    'library science': '04-SHS/EDUCATION',
    'actuarial science': '09-BUSINESS/FINANCE',
    'computational logistics': '02-INFORMATIQUE',
    'uncertainty quantification': '01-SCIENCES/MATHEMATIQUES/04-Probabilites-Statistiques',
    'entertainment computing': '02-INFORMATIQUE/13-Infographie-3D',
    'services science': '09-BUSINESS',
    'natural computing': '02-INFORMATIQUE/05-IA-ML',
    'distributed computing': '02-INFORMATIQUE/11-Cloud-DevOps',
    'intelligent decision technologies': '02-INFORMATIQUE/05-IA-ML',
    'image and graphics technologies': '02-INFORMATIQUE/13-Infographie-3D',
    'intelligent autonomous systems': '03-INGENIERIE/ROBOTIQUE',
    'web technologies': '02-INFORMATIQUE/14-Web',
    'coding theory': '01-SCIENCES/MATHEMATIQUES/08-Mathematiques-Generales',
    'health informatics': '06-MEDECINE',
    'control systems': '03-INGENIERIE',
    'brain-inspired computing': '02-INFORMATIQUE/05-IA-ML/Deep-Learning',
    'information and communication technologies': '02-INFORMATIQUE',
    'digital heritage': '04-SHS/HISTOIRE',
    'genetic and evolutionary computing': '02-INFORMATIQUE/05-IA-ML',
    'formal methods': '02-INFORMATIQUE/01-Fondamentaux-CS',
    'interactive digital storytelling': '02-INFORMATIQUE/14-Web',
    'speech and language technologies': '02-INFORMATIQUE/05-IA-ML/NLP',
    'digital speech processing': '02-INFORMATIQUE/05-IA-ML/NLP',

    # ══════════════════════════════════════════════════════════════════════
    # 03-INGENIERIE
    # ══════════════════════════════════════════════════════════════════════
    'engineering': '03-INGENIERIE',
    'electrical engineering': '03-INGENIERIE/ELECTRONIQUE',
    'electronics': '03-INGENIERIE/ELECTRONIQUE',
    'home automation': '03-INGENIERIE/ELECTRONIQUE',
    'mechanical engineering': '03-INGENIERIE',
    'robotics': '03-INGENIERIE/ROBOTIQUE',
    'intelligent control': '03-INGENIERIE/ROBOTIQUE',
    'robot vision': '03-INGENIERIE/ROBOTIQUE',
    'human-machine interaction': '03-INGENIERIE/ROBOTIQUE',
    'signal processing': '03-INGENIERIE/TRAITEMENT-SIGNAL',
    'medical imaging': '03-INGENIERIE/TRAITEMENT-SIGNAL',
    'acoustics': '03-INGENIERIE/TRAITEMENT-SIGNAL',
    'telecommunications': '03-INGENIERIE/TELECOMMUNICATIONS',
    'control theory': '03-INGENIERIE',
    'aerospace and astronautics': '03-INGENIERIE',
    'energy': '03-INGENIERIE',

    # ══════════════════════════════════════════════════════════════════════
    # 04-SHS (Sciences Humaines et Sociales)
    # ══════════════════════════════════════════════════════════════════════
    'philosophy': '04-SHS/PHILOSOPHIE',
    'transhumanism': '04-SHS/PHILOSOPHIE',
    'psychology': '04-SHS/PSYCHOLOGIE',
    'sociology': '04-SHS/SOCIOLOGIE',
    'social sciences': '04-SHS/SOCIOLOGIE',
    'social justice': '04-SHS/SOCIOLOGIE',
    'social computing': '04-SHS/SOCIOLOGIE',
    'computational social sciences': '04-SHS/SOCIOLOGIE',
    'digital media and society': '04-SHS/SOCIOLOGIE',
    'society': '04-SHS/SOCIOLOGIE',
    'history': '04-SHS/HISTOIRE',
    'archaeology': '04-SHS/HISTOIRE',
    'economics': '04-SHS/ECONOMIE',
    'political science': '04-SHS/SCIENCES-POLITIQUES',
    'linguistics': '04-SHS/LINGUISTIQUE',
    'anthropology': '04-SHS/SOCIOLOGIE',
    'education': '04-SHS/EDUCATION',
    'e-learning': '04-SHS/EDUCATION',
    'law': '04-SHS/DROIT',
    'digital humanities': '04-SHS',

    # ══════════════════════════════════════════════════════════════════════
    # 05-RELIGIONS
    # ══════════════════════════════════════════════════════════════════════
    'religion': '05-RELIGIONS',
    'islam': '05-RELIGIONS/ISLAM',
    'christianity': '05-RELIGIONS/CHRISTIANISME',
    'judaism': '05-RELIGIONS/JUDAISME',
    'buddhism': '05-RELIGIONS/AUTRES-RELIGIONS',
    'hinduism': '05-RELIGIONS/AUTRES-RELIGIONS',
    'theology': '05-RELIGIONS',
    'spirituality': '05-RELIGIONS/AUTRES-RELIGIONS',

    # ══════════════════════════════════════════════════════════════════════
    # 06-MEDECINE
    # ══════════════════════════════════════════════════════════════════════
    'medicine': '06-MEDECINE',
    'anatomy': '06-MEDECINE/Anatomie',
    'pharmacology': '06-MEDECINE/Pharmacologie',
    'surgery': '06-MEDECINE/Chirurgie',
    'clinical laboratory sciences': '06-MEDECINE',
    'nursing': '06-MEDECINE',
    'dentistry': '06-MEDECINE',
    'public health': '06-MEDECINE',

    # ══════════════════════════════════════════════════════════════════════
    # 07-LANGUES
    # ══════════════════════════════════════════════════════════════════════
    'language learning': '07-LANGUES',
    'english language': '07-LANGUES/ANGLAIS',
    'scientific english': '07-LANGUES/ANGLAIS',
    'french language': '07-LANGUES/FRANCAIS',
    'arabic language': '07-LANGUES/ARABE',
    'german language': '07-LANGUES/AUTRES',
    'spanish language': '07-LANGUES/AUTRES',
    'writing': '07-LANGUES',
    'science writing': '07-LANGUES',

    # ══════════════════════════════════════════════════════════════════════
    # 08-LOISIRS
    # ══════════════════════════════════════════════════════════════════════
    'art': '08-LOISIRS/ART',
    'architecture': '08-LOISIRS/ART',
    'typography': '08-LOISIRS/ART',
    'product design': '08-LOISIRS/ART',
    'digital sculpting': '08-LOISIRS/ART',
    'drawing': '08-LOISIRS/DESSIN',
    'sketching': '08-LOISIRS/DESSIN',
    'painting': '08-LOISIRS/DESSIN',
    'music': '08-LOISIRS/MUSIQUE',
    'podcasting': '08-LOISIRS/MUSIQUE',
    'cooking': '08-LOISIRS/CUISINE',
    'sports': '08-LOISIRS/SPORT-FITNESS',
    'fitness': '08-LOISIRS/SPORT-FITNESS',
    'photography': '08-LOISIRS/PHOTOGRAPHIE',
    'video editing': '08-LOISIRS/PHOTOGRAPHIE',
    'literature': '08-LOISIRS/LITTERATURE',
    'fiction': '08-LOISIRS/LITTERATURE',
    'chess': '08-LOISIRS/ECHECS',
    'puzzle solving': '08-LOISIRS',
    'fashion': '08-LOISIRS',
    'woodworking': '08-LOISIRS',
    'home improvement': '08-LOISIRS',
    'general knowledge': '08-LOISIRS',
    'events and people': '08-LOISIRS',
    'travel': '08-LOISIRS',
    'agricultural technology': '08-LOISIRS',
    'agricultural sciences': '08-LOISIRS',
    'agriculture': '08-LOISIRS',

    # ══════════════════════════════════════════════════════════════════════
    # 09-BUSINESS
    # ══════════════════════════════════════════════════════════════════════
    'business': '09-BUSINESS',
    'management': '09-BUSINESS/MANAGEMENT',
    'marketing': '09-BUSINESS/MARKETING',
    'social media': '09-BUSINESS/MARKETING',
    'finance': '09-BUSINESS/FINANCE',
    'accounting': '09-BUSINESS/FINANCE',
    'entrepreneurship': '09-BUSINESS',
    'project management': '09-BUSINESS/MANAGEMENT',
    'operations': '09-BUSINESS/MANAGEMENT',
}


def classify_by_theme(theme: str) -> Optional[str]:
    """
    Mappe un thème LLM vers un chemin dans l'arborescence BIBLIO.
    Recherche d'abord exact, puis par sous-chaîne.
    """
    if not theme:
        return None

    theme_lower = theme.lower().strip()

    # 1. Match exact
    if theme_lower in THEME_TO_PATH:
        return THEME_TO_PATH[theme_lower]

    # 2. Match par sous-chaîne (le thème LLM contient un de nos mots-clés)
    best_match = None
    best_len = 0
    for key, path in THEME_TO_PATH.items():
        if key in theme_lower and len(key) > best_len:
            best_match = path
            best_len = len(key)

    if best_match:
        return best_match

    # 3. Match inverse (un de nos mots-clés contient le thème)
    for key, path in THEME_TO_PATH.items():
        if theme_lower in key:
            return path

    return None


def load_classifier(config_path: str):
    """Charge le classifieur par mots-clés depuis le YAML."""
    if not HAS_YAML:
        return None

    sys.path.insert(0, os.path.dirname(config_path))
    from klodo_organizer import KeywordClassifier

    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    return KeywordClassifier(config)


def classify_combined(vision_result: Dict, filename: str,
                      classifier=None) -> Tuple[str, float, str]:
    """
    Classification combinée : thème LLM + classifieur par mots-clés.
    Priorité au thème LLM si la confiance est élevée.
    """
    title = vision_result.get('title', '')
    author = vision_result.get('author', '')
    theme = vision_result.get('theme', '')
    confidence = vision_result.get('confidence', 0.0)

    # 1. Classification par thème LLM (prioritaire si confiance > 0.5)
    if theme and confidence >= 0.5:
        path = classify_by_theme(theme)
        if path:
            return (path, confidence, f"LLM:{theme}")

    # 2. Fallback sur le classifieur par mots-clés
    if classifier:
        combined_text = f"{title} {author} {theme}"
        results = classifier.classify(combined_text)
        if results:
            return results[0]

    # 3. Dernier recours : thème LLM même avec faible confiance
    if theme:
        path = classify_by_theme(theme)
        if path:
            return (path, confidence, f"LLM-low:{theme}")

    return ('', 0.0, '')


# ════════════════════════════════════════════════════════════════════════════
# PIPELINE COMPLET
# ════════════════════════════════════════════════════════════════════════════

def process_single_file(pdf_path: str, api_key: str,
                        classifier=None, model: str = DEFAULT_MODEL,
                        verbose: bool = False) -> Dict:
    """
    Pipeline complet pour un fichier :
    1. Extraction couverture → image
    2. Envoi au LLM Vision → titre, auteur, thème
    3. Proposition de renommage
    4. Classification
    """
    filename = os.path.basename(pdf_path)
    result = {
        'fichier': filename,
        'chemin': pdf_path,
        'titre_detecte': '',
        'auteur_detecte': '',
        'theme_detecte': '',
        'langue': '',
        'confiance': 0.0,
        'nouveau_nom': '',
        'renommage': False,
        'destination': '',
        'score': 0.0,
        'mot_cle': '',
        'status': 'pending',
    }

    # 1. Extraction couverture
    if verbose:
        print(f"  📄 Extraction couverture...")
    img = extract_cover_image(pdf_path, dpi=150)
    if img is None:
        result['status'] = 'erreur_extraction'
        return result

    # 2. Conversion en base64 et appel API
    if verbose:
        print(f"  🤖 Appel LLM Vision...")
    img_b64 = image_to_base64(img, max_size=800)

    vision = call_vision_api(img_b64, api_key, model=model)
    if vision is None:
        result['status'] = 'erreur_api'
        return result

    title = vision.get('title', '')
    author = vision.get('author', '')
    theme = vision.get('theme', '')
    language = vision.get('language', '')
    confidence = vision.get('confidence', 0.0)

    result['titre_detecte'] = title
    result['auteur_detecte'] = author
    result['theme_detecte'] = theme
    result['langue'] = language
    result['confiance'] = confidence

    if verbose:
        print(f"  📝 Titre  : {title[:60]}")
        print(f"  👤 Auteur : {author[:40]}")
        print(f"  🏷  Thème  : {theme}")
        print(f"  🌐 Langue : {language}")
        print(f"  📊 Conf.  : {confidence:.2f}")

    # 3. Proposition de renommage
    if title and confidence >= 0.3 and not is_name_already_clean(filename):
        new_name = build_new_filename(title, author)
        if new_name and new_name != filename:
            result['nouveau_nom'] = new_name
            result['renommage'] = True
            if verbose:
                print(f"  ✏️  Renommage : {filename[:40]} → {new_name[:40]}")
    elif verbose and title:
        print(f"  ⏭  Nom déjà propre ou confiance trop basse")

    # 4. Classification
    if confidence >= 0.3:
        dest, score, keyword = classify_combined(vision, filename, classifier)
        if dest:
            result['destination'] = dest
            result['score'] = score
            result['mot_cle'] = keyword
            result['status'] = 'classifié'
            if verbose:
                print(f"  ✅ → {dest} (score: {score:.2f}, clé: {keyword})")
        else:
            result['status'] = 'renommé_seul' if result['renommage'] else 'non_classifié'
            if verbose:
                if result['renommage']:
                    print(f"  📝 Renommé mais pas classifié (thème inconnu: {theme})")
                else:
                    print(f"  ❌ Aucune catégorie trouvée")
    elif confidence > 0:
        result['status'] = 'confiance_basse'
        if verbose:
            print(f"  ⚠ Confiance trop basse ({confidence:.2f})")
    else:
        result['status'] = 'non_identifié'
        if verbose:
            print(f"  ❌ Image non identifiable")

    return result


# ════════════════════════════════════════════════════════════════════════════
# CHECKPOINT / REPRISE
# ════════════════════════════════════════════════════════════════════════════

_PROGRESS_FILENAME = 'progress_ocr.json'


def get_progress_path(logs_dir: str) -> str:
    """Retourne le chemin du fichier de progression."""
    return os.path.join(logs_dir, _PROGRESS_FILENAME)


def load_progress(logs_dir: str) -> Dict[str, Dict]:
    """
    Charge la progression depuis le fichier JSON.
    Retourne un dict {chemin_fichier: résultat}.
    """
    progress_path = get_progress_path(logs_dir)
    if not os.path.exists(progress_path):
        return {}
    try:
        with open(progress_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data.get('results', {})
    except (json.JSONDecodeError, KeyError):
        print("  ⚠ Fichier de progression corrompu — redémarrage à zéro")
        return {}


def save_progress(logs_dir: str, progress: Dict[str, Dict],
                  model: str = "", total_files: int = 0):
    """Sauvegarde la progression dans le fichier JSON."""
    progress_path = get_progress_path(logs_dir)
    data = {
        'version': __version__,
        'model': model,
        'last_update': datetime.now().isoformat(),
        'total_files': total_files,
        'processed': len(progress),
        'results': progress,
    }
    # Écriture atomique : écrire dans un fichier temp puis renommer
    tmp_path = progress_path + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp_path, progress_path)


def clear_progress(logs_dir: str):
    """Supprime le fichier de progression (--reset)."""
    progress_path = get_progress_path(logs_dir)
    if os.path.exists(progress_path):
        os.remove(progress_path)
        print("🗑  Progression précédente supprimée.")


def retry_errors_in_progress(logs_dir: str) -> int:
    """
    Retire du checkpoint les fichiers en erreur (erreur_api, erreur_extraction)
    pour qu'ils soient retraités au prochain scan.
    Retourne le nombre de fichiers retirés.
    """
    progress = load_progress(logs_dir)
    if not progress:
        print("  Pas de checkpoint trouvé.")
        return 0

    error_statuses = {'erreur_api', 'erreur_extraction', 'confiance_basse'}
    to_retry = [k for k, v in progress.items() if v.get('status') in error_statuses]

    if not to_retry:
        print("  Aucun fichier en erreur dans le checkpoint.")
        return 0

    for k in to_retry:
        del progress[k]

    save_progress(logs_dir, progress)
    print(f"🔄 {len(to_retry)} fichiers retirés du checkpoint (seront retraités)")
    return len(to_retry)


def scan_directory(dir_path: str, api_key: str, classifier=None,
                   model: str = DEFAULT_MODEL,
                   max_files: int = 0, verbose: bool = False,
                   delay: float = 0.2, workers: int = 1,
                   logs_dir: str = '') -> List[Dict]:
    """Scanne un répertoire et traite chaque PDF via LLM Vision.
    Supporte la reprise automatique et le traitement parallèle."""
    pdf_files = []

    for root, dirs, files in os.walk(dir_path):
        for f in sorted(files):
            if f.lower().endswith('.pdf'):
                pdf_files.append(os.path.join(root, f))

    total = len(pdf_files)
    if max_files > 0:
        pdf_files = pdf_files[:max_files]

    # ── Charger la progression existante ──
    progress = {}  # type: Dict[str, Dict]
    resumed = 0
    if logs_dir:
        progress = load_progress(logs_dir)
        resumed = sum(1 for p in pdf_files if p in progress)
        if resumed > 0:
            print(f"\n🔄 Reprise détectée : {resumed}/{len(pdf_files)} fichiers déjà traités")
            print(f"   → {len(pdf_files) - resumed} fichiers restants")
            print(f"   (utiliser --reset pour recommencer à zéro)")

    remaining = len(pdf_files) - resumed
    print(f"\n📚 {remaining} fichiers à traiter (sur {total} trouvés)")
    print(f"🤖 Modèle  : {model}")
    if workers > 1:
        print(f"🧵 Workers : {workers} threads parallèles")
    else:
        print(f"⏱  Délai   : {delay}s entre les requêtes")
    if logs_dir:
        print(f"💾 Checkpoint : {get_progress_path(logs_dir)}")
    print()

    # Vérifier la clé API seulement s'il reste des fichiers à traiter
    if remaining > 0 and not api_key:
        print("❌ Clé API requise pour traiter de nouveaux fichiers.")
        print("   --api-key sk-xxx")
        print("   ou : export SILICONFLOW_API_KEY=sk-xxx")
        sys.exit(1)

    # Filtrer les fichiers déjà traités
    to_process = [p for p in pdf_files if p not in progress]

    if not to_process:
        if resumed > 0:
            print(f"✅ Tous les fichiers ont déjà été traités.")
            print(f"   Utilisez --reset pour recommencer à zéro.")
        return list(progress.values())

    # ── Gestion Ctrl+C ──
    interrupted = [False]

    def signal_handler(sig, frame):
        # type: (int, object) -> None
        interrupted[0] = True
        print(f"\n\n⚠  Interruption détectée (Ctrl+C)")
        print(f"   Attente de la fin des requêtes en cours...")

    old_handler = signal.signal(signal.SIGINT, signal_handler)

    # ── Verrou pour accès thread-safe au checkpoint ──
    progress_lock = threading.Lock()
    print_lock = threading.Lock()
    processed_count = [0]  # list pour mutabilité
    api_errors = [0]
    max_consecutive_errors = 10

    def _process_one(pdf_path):
        # type: (str) -> Optional[Dict]
        """Traite un fichier (appelé par chaque thread)."""
        if interrupted[0]:
            return None

        filename = os.path.basename(pdf_path)
        result = process_single_file(pdf_path, api_key, classifier, model, verbose=False)

        with progress_lock:
            progress[pdf_path] = result
            processed_count[0] += 1
            done = resumed + processed_count[0]

            # Erreurs consécutives
            if result['status'] == 'erreur_api':
                api_errors[0] += 1
                if api_errors[0] >= max_consecutive_errors:
                    interrupted[0] = True
            else:
                api_errors[0] = 0

            # Checkpoint périodique (toutes les 5 requêtes ou en mode séquentiel)
            if logs_dir and (workers <= 1 or processed_count[0] % 5 == 0):
                save_progress(logs_dir, progress, model, len(pdf_files))

        # Affichage thread-safe
        with print_lock:
            all_results = list(progress.values())
            classified = sum(1 for r in all_results if r['status'] == 'classifié')
            status_icon = '✅' if result['status'] == 'classifié' else '❌'
            if verbose:
                title = result.get('titre_detecte', '')[:40]
                theme = result.get('theme_detecte', '')
                print(f"  [{done}/{len(pdf_files)}] {status_icon} {filename[:45]:45s} "
                      f"→ {result['status']} | {theme}")
                if title:
                    print(f"            📝 {title}")
            else:
                print(f"  [{done}/{len(pdf_files)}] {status_icon} {filename[:50]:50s} "
                      f"→ {result['status']} ({classified} classifiés)")

        return result

    # ── Exécution parallèle ou séquentielle ──
    if workers > 1:
        print(f"🚀 Lancement de {len(to_process)} requêtes avec {workers} threads...\n")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}  # type: Dict
            for pdf_path in to_process:
                if interrupted[0]:
                    break
                future = executor.submit(_process_one, pdf_path)
                futures[future] = pdf_path

            # Attendre les résultats
            for future in as_completed(futures):
                if interrupted[0]:
                    break
                try:
                    future.result()
                except Exception as e:
                    with print_lock:
                        print(f"  ⚠ Exception thread: {e}")

            # En cas d'interruption, on n'annule pas les futures en cours
            # (ils termineront naturellement)
    else:
        # Mode séquentiel (original)
        print(f"🚀 Traitement séquentiel de {len(to_process)} fichiers...\n")
        for pdf_path in to_process:
            if interrupted[0]:
                break
            _process_one(pdf_path)
            if not interrupted[0] and delay > 0:
                time.sleep(delay)

    # ── Checkpoint final ──
    if logs_dir:
        save_progress(logs_dir, progress, model, len(pdf_files))

    # ── Restaurer le handler de signal ──
    signal.signal(signal.SIGINT, old_handler)

    # ── Message de fin ──
    if interrupted[0]:
        if api_errors[0] >= max_consecutive_errors:
            print(f"\n❌ {max_consecutive_errors} erreurs API consécutives — arrêt.")
        print(f"\n💾 Progression sauvegardée ({len(progress)}/{len(pdf_files)} fichiers).")
        print(f"   Relancez la même commande pour reprendre automatiquement.")

    return list(progress.values())


def save_report(results: List[Dict], output_dir: str) -> str:
    """Sauvegarde un rapport CSV."""
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    report_path = os.path.join(output_dir, f'rapport_ocr_{timestamp}.csv')

    with open(report_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=[
            'fichier', 'nouveau_nom', 'titre_detecte', 'auteur_detecte',
            'theme_detecte', 'langue', 'confiance',
            'destination', 'score', 'mot_cle', 'status',
        ])
        writer.writeheader()
        for r in results:
            writer.writerow({
                'fichier': r['fichier'],
                'nouveau_nom': r.get('nouveau_nom', ''),
                'titre_detecte': r['titre_detecte'],
                'auteur_detecte': r['auteur_detecte'],
                'theme_detecte': r.get('theme_detecte', ''),
                'langue': r.get('langue', ''),
                'confiance': f"{r.get('confiance', 0):.2f}",
                'destination': r['destination'],
                'score': f"{r['score']:.3f}",
                'mot_cle': r['mot_cle'],
                'status': r['status'],
            })

    return report_path


def print_summary(results: List[Dict]):
    """Affiche un résumé des résultats."""
    total = len(results)
    if total == 0:
        print("\n  Aucun fichier traité.")
        return

    classified = sum(1 for r in results if r['status'] == 'classifié')
    renamed_only = sum(1 for r in results if r['status'] == 'renommé_seul')
    low_conf = sum(1 for r in results if r['status'] == 'confiance_basse')
    not_id = sum(1 for r in results if r['status'] == 'non_identifié')
    errors_extract = sum(1 for r in results if r['status'] == 'erreur_extraction')
    errors_api = sum(1 for r in results if r['status'] == 'erreur_api')
    unclassified = sum(1 for r in results if r['status'] == 'non_classifié')
    to_rename = sum(1 for r in results if r.get('renommage'))

    # Coût estimé (basé sur Qwen3-VL-8B : ~$0.34 / 1000 images)
    api_calls = total - errors_extract
    cost_estimate = api_calls * 0.00034  # ~1420 input + 120 output tokens par image

    print(f"\n{'='*60}")
    print(f" RÉSUMÉ — LLM Vision Cover v{__version__}")
    print(f"{'='*60}")
    print(f"  Total traités      : {total}")
    print(f"  ✅ Classifiés       : {classified} ({classified/total*100:.1f}%)")
    print(f"  ✏️  À renommer      : {to_rename} ({to_rename/total*100:.1f}%)")
    print(f"  📝 Renommés seuls   : {renamed_only} ({renamed_only/total*100:.1f}%)")
    print(f"  ❌ Non classifiés   : {unclassified} ({unclassified/total*100:.1f}%)")
    print(f"  ⚠  Confiance basse : {low_conf} ({low_conf/total*100:.1f}%)")
    print(f"  🔇 Non identifiés  : {not_id} ({not_id/total*100:.1f}%)")
    print(f"  💥 Erreurs extract. : {errors_extract} ({errors_extract/total*100:.1f}%)")
    print(f"  💥 Erreurs API      : {errors_api} ({errors_api/total*100:.1f}%)")
    print(f"  💰 Coût estimé      : ${cost_estimate:.2f}")

    # Bilan actions
    actionable = classified + renamed_only
    print(f"\n  📊 Bilan : {actionable}/{total} fichiers avec au moins une action")
    print(f"     ({classified} classifiés + {renamed_only} renommés seuls)")

    # Confiance moyenne
    confs = [r.get('confiance', 0) for r in results if r.get('confiance', 0) > 0]
    if confs:
        avg_conf = sum(confs) / len(confs)
        print(f"  📊 Confiance moy.   : {avg_conf:.2f}")

    # Exemples de renommages
    renamed = [r for r in results if r.get('renommage')]
    if renamed:
        print(f"\n  Exemples de renommages :")
        for r in renamed[:10]:
            print(f"    {r['fichier'][:35]:35s} → {r['nouveau_nom'][:40]}")

    # Top thèmes détectés
    from collections import Counter
    themes = Counter(r.get('theme_detecte', '') for r in results
                     if r.get('theme_detecte'))
    if themes:
        print(f"\n  Top thèmes détectés :")
        for theme, count in themes.most_common(15):
            print(f"    {count:4d}  {theme}")

    # Top destinations
    dests = Counter(r['destination'] for r in results if r['destination'])
    if dests:
        print(f"\n  Top destinations :")
        for dest, count in dests.most_common(10):
            print(f"    {count:4d}  {dest}")


# ════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description='OCR Cover v2 — Renommage et classification PDF par LLM Vision')
    parser.add_argument('path', help='Fichier PDF ou dossier source à scanner')
    parser.add_argument('--target', default=None,
                        help='Dossier cible (ex: BIBLIO_V2). Si défini, les fichiers sont COPIÉS vers la cible.')
    parser.add_argument('--api-key', default=None,
                        help='Clé API SiliconFlow (ou var SILICONFLOW_API_KEY)')
    parser.add_argument('--model', default=DEFAULT_MODEL,
                        help=f'Modèle Vision (défaut: {DEFAULT_MODEL})')
    parser.add_argument('--config', default=None,
                        help='Fichier categories.yaml (optionnel, pour classification hybride)')
    parser.add_argument('--report', action='store_true',
                        help='Générer un rapport CSV')
    parser.add_argument('--execute', action='store_true',
                        help='Appliquer renommage + copie/déplacement')
    parser.add_argument('--rename-only', action='store_true',
                        help='Renommer uniquement (pas de classement)')
    parser.add_argument('--classify-only', action='store_true',
                        help='Classer uniquement (pas de renommage)')
    parser.add_argument('--max', type=int, default=0,
                        help='Nombre max de fichiers à traiter (0 = tous)')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Afficher les détails LLM')
    parser.add_argument('--workers', '-w', type=int, default=1,
                        help='Nombre de threads parallèles (défaut: 1, max recommandé: 10)')
    parser.add_argument('--delay', type=float, default=0.2,
                        help='Délai entre requêtes en mode séquentiel (défaut: 0.2)')
    parser.add_argument('--dpi', type=int, default=150,
                        help='Résolution d\'extraction (défaut: 150)')
    parser.add_argument('--reset', action='store_true',
                        help='Supprimer la progression et recommencer à zéro')
    parser.add_argument('--retry-errors', action='store_true',
                        help='Retirer les erreurs du checkpoint pour les retraiter')
    parser.add_argument('--reclassify', action='store_true',
                        help='Reclassifier les non_classifié avec THEME_TO_PATH mis à jour (sans appel API)')
    parser.add_argument('--progress-file', default=None,
                        help='Nom du fichier de progression (défaut: progress_ocr.json)')

    args = parser.parse_args()

    # Vérifier les dépendances
    missing = []
    if not HAS_PDF2IMAGE:
        missing.append('pdf2image')
    if not HAS_PIL:
        missing.append('Pillow')
    if not HAS_REQUESTS:
        missing.append('requests')
    if missing:
        print(f"❌ Dépendances manquantes : {', '.join(missing)}")
        print(f"   pip3 install {' '.join(missing)}")
        sys.exit(1)

    # Clé API (peut être absente si tout est déjà dans le checkpoint)
    api_key = args.api_key or os.environ.get('SILICONFLOW_API_KEY', '')

    # Fichier de progression personnalisé
    global _PROGRESS_FILENAME
    if args.progress_file:
        _PROGRESS_FILENAME = args.progress_file

    # Mode migration : utiliser un fichier de progression dédié
    if args.target and not args.progress_file:
        _PROGRESS_FILENAME = 'progress_migration.json'
        print(f"📋 Mode migration → checkpoint : {_PROGRESS_FILENAME}")

    # Auto-detect config
    config_path = args.config
    if config_path is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(script_dir, 'categories.yaml')
        if not os.path.exists(config_path):
            config_path = None

    # Charger le classifieur (optionnel, en complément du thème LLM)
    classifier = None
    if not args.rename_only:
        if config_path and os.path.exists(config_path):
            print(f"📂 Config  : {config_path}")
            try:
                classifier = load_classifier(config_path)
                print(f"   ✅ Classifieur YAML chargé (mode hybride)")
            except Exception as e:
                print(f"   ⚠ Classifieur YAML non chargé: {e}")
                print(f"   → Classification par thème LLM uniquement")
        else:
            print("ℹ  Pas de categories.yaml — classification par thème LLM uniquement")

    # Mode
    mode_parts = []
    if not args.classify_only:
        mode_parts.append('renommage')
    if not args.rename_only:
        mode_parts.append('classification')
    print(f"🔧 Mode    : {' + '.join(mode_parts)}")
    if args.target:
        print(f"🎯 Cible   : {args.target} (copie)")
    print(f"🤖 Modèle  : {args.model}")

    # Fichier unique ?
    if os.path.isfile(args.path):
        if not api_key:
            print("❌ Clé API requise pour analyser un fichier.")
            print("   --api-key sk-xxx  ou  export SILICONFLOW_API_KEY=sk-xxx")
            sys.exit(1)
        result = process_single_file(
            args.path, api_key, classifier, args.model, verbose=True)
        print(f"\n{'='*40}")
        print(f"  Titre  : {result['titre_detecte']}")
        print(f"  Auteur : {result['auteur_detecte']}")
        print(f"  Thème  : {result.get('theme_detecte', '')}")
        print(f"  Langue : {result.get('langue', '')}")
        print(f"  Conf.  : {result.get('confiance', 0):.2f}")
        if result.get('nouveau_nom'):
            print(f"  Renom. : {result['fichier']} → {result['nouveau_nom']}")
        if result['destination']:
            print(f"  Dest.  : {result['destination']} (score: {result['score']:.2f})")
        print(f"  Status : {result['status']}")
        return

    # Dossier
    if not os.path.isdir(args.path):
        print(f"❌ Chemin introuvable : {args.path}")
        sys.exit(1)

    # Préparer le dossier de logs (pour checkpoint + rapports)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    logs_dir = os.path.join(os.path.dirname(script_dir), 'logs')
    os.makedirs(logs_dir, exist_ok=True)

    # Reset si demandé
    if args.reset:
        clear_progress(logs_dir)

    # Retry errors : retirer les erreurs du checkpoint
    if args.retry_errors:
        retry_errors_in_progress(logs_dir)

    # Reclassify : re-mapper les thèmes sans appel API
    if args.reclassify:
        progress = load_progress(logs_dir)
        if progress:
            reclassified = 0
            for path, result in progress.items():
                if result.get('status') in ('non_classifié', 'renommé_seul'):
                    theme = result.get('theme_detecte', '')
                    confidence = float(result.get('confiance', 0))
                    if theme and confidence >= 0.3:
                        dest = classify_by_theme(theme)
                        if dest:
                            result['destination'] = dest
                            result['score'] = confidence
                            result['mot_cle'] = f"LLM:{theme}"
                            result['status'] = 'classifié'
                            reclassified += 1
            save_progress(logs_dir, progress)
            print(f"🔄 Reclassification : {reclassified} fichiers récupérés avec THEME_TO_PATH mis à jour")
            if not args.execute:
                # Afficher le résumé et sortir
                results = list(progress.values())
                print_summary(results)
                report_path = save_report(results, logs_dir)
                print(f"\n📋 Rapport : {report_path}")
                return
        else:
            print("  Pas de checkpoint trouvé pour reclassifier.")

    results = scan_directory(
        args.path, api_key, classifier, args.model,
        args.max, args.verbose, args.delay,
        workers=args.workers, logs_dir=logs_dir)
    print_summary(results)

    # Rapport
    if args.report or args.execute:
        report_path = save_report(results, logs_dir)
        print(f"\n📋 Rapport : {report_path}")

    # Exécution
    if args.execute:
        import shutil

        # Déterminer le mode : copie vers cible (migration) ou déplacement sur place
        use_copy = args.target is not None
        target_base = args.target if use_copy else os.path.dirname(args.path)

        if use_copy:
            if not os.path.isdir(target_base):
                print(f"❌ Dossier cible introuvable : {target_base}")
                sys.exit(1)
            print(f"\n📋 Mode MIGRATION : copie vers {target_base}")
        else:
            print(f"\n📋 Mode classique : déplacement sur place")

        # ── Copie/Déplacement avec renommage intégré ──
        to_process_exec = [r for r in results if r['status'] == 'classifié']
        if to_process_exec:
            action_verb = "Copie" if use_copy else "Déplacement"
            print(f"\n🚀 {action_verb} de {len(to_process_exec)} fichiers...")
            done_count = 0
            skipped_src = 0
            skipped_exists = 0
            errors = 0
            for r in to_process_exec:
                src = r['chemin']
                if not os.path.exists(src):
                    skipped_src += 1
                    continue

                # Nom du fichier : renommé si possible, sinon nom original
                if not args.classify_only and r.get('renommage') and r.get('nouveau_nom'):
                    final_name = r['nouveau_nom']
                else:
                    final_name = r['fichier']

                dest_dir = os.path.join(target_base, r['destination'])
                dest = os.path.join(dest_dir, final_name)

                # En mode copie : si le fichier existe déjà dans la cible, on saute
                if use_copy and os.path.exists(dest):
                    skipped_exists += 1
                    continue

                # Éviter les collisions (noms différents pour le même fichier)
                if os.path.exists(dest):
                    base, ext = os.path.splitext(final_name)
                    counter = 2
                    while os.path.exists(dest):
                        dest = os.path.join(dest_dir, f"{base} ({counter}){ext}")
                        counter += 1

                os.makedirs(dest_dir, exist_ok=True)
                try:
                    if use_copy:
                        shutil.copy2(src, dest)
                    else:
                        shutil.move(src, dest)
                    done_count += 1
                except Exception as e:
                    errors += 1
                    print(f"  ⚠ Erreur {r['fichier'][:40]}: {e}")

            parts = [f"✅ {done_count} fichiers {'copiés' if use_copy else 'déplacés'}"]
            if skipped_exists:
                parts.append(f"{skipped_exists} déjà dans la cible")
            if skipped_src:
                parts.append(f"{skipped_src} source absente")
            if errors:
                parts.append(f"{errors} erreurs")
            print(f"  {' | '.join(parts)}")
        else:
            print("\n⏭  Aucun fichier classifié à traiter.")

        # ── Fichiers non classifiés : copier vers _A-TRIER dans la cible ──
        if use_copy:
            not_classified = [r for r in results
                              if r['status'] not in ('classifié',)
                              and r.get('chemin')]
            if not_classified:
                print(f"\n📁 Copie de {len(not_classified)} fichiers non classifiés vers _A-TRIER...")
                atrier_done = 0
                atrier_skipped = 0
                for r in not_classified:
                    src = r['chemin']
                    if not os.path.exists(src):
                        continue

                    # Renommer si possible même sans classification
                    if not args.classify_only and r.get('renommage') and r.get('nouveau_nom'):
                        final_name = r['nouveau_nom']
                    else:
                        final_name = r['fichier']

                    dest_dir = os.path.join(target_base, '_A-TRIER')
                    dest = os.path.join(dest_dir, final_name)

                    # Sauter si déjà copié
                    if os.path.exists(dest):
                        atrier_skipped += 1
                        continue

                    os.makedirs(dest_dir, exist_ok=True)
                    try:
                        shutil.copy2(src, dest)
                        atrier_done += 1
                    except Exception as e:
                        print(f"  ⚠ Erreur {final_name[:40]}: {e}")
                print(f"  ✅ {atrier_done} copiés vers _A-TRIER"
                      + (f" | {atrier_skipped} déjà présents" if atrier_skipped else ""))

        # Sauvegarder le log d'exécution
        script_dir = os.path.dirname(os.path.abspath(__file__))
        logs_dir_exec = os.path.join(os.path.dirname(script_dir), 'logs')
        os.makedirs(logs_dir_exec, exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        log_path = os.path.join(logs_dir_exec, f'log_migration_{timestamp}.csv')
        with open(log_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=[
                'fichier', 'nouveau_nom', 'destination', 'status', 'chemin_source',
            ])
            writer.writeheader()
            for r in results:
                writer.writerow({
                    'fichier': r['fichier'],
                    'nouveau_nom': r.get('nouveau_nom', ''),
                    'destination': r['destination'],
                    'status': r['status'],
                    'chemin_source': r.get('chemin', ''),
                })
        print(f"📋 Log exécution : {log_path}")


if __name__ == '__main__':
    main()
