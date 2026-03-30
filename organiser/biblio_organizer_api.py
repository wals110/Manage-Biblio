#!/usr/bin/env python3
"""
Biblio Organizer API — Classement thématique via API web.
=========================================================
Classe les fichiers PDF en interrogeant Google Books et Open Library
pour récupérer les catégories/sujets officiels de chaque livre.

Pipeline :
  1. Extraire l'ISBN du nom de fichier ou du cache existant
  2. Requêter Google Books API (par ISBN, puis par titre)
  3. Fallback sur Open Library API
  4. Mapper la catégorie retournée vers l'arborescence locale
  5. Générer un rapport ou exécuter les déplacements

Usage :
    python3 biblio_organizer_api.py /chemin/vers/biblio                 # Rapport
    python3 biblio_organizer_api.py /chemin/vers/biblio --execute        # Appliquer
    python3 biblio_organizer_api.py /chemin/vers/biblio --no-pdf         # Sans extraction
    python3 biblio_organizer_api.py --undo log_classement_api_*.csv      # Annuler

Options :
    --config FILE       Fichier de config (défaut: categories.yaml)
    --isbn-cache FILE   Cache ISBN existant (défaut: ../renommage/isbn_cache.json)
    --no-pdf            Pas d'extraction PDF pour les titres manquants
    --delay N           Délai entre requêtes API en secondes (défaut: 0.5)
    --verbose           Afficher les détails
    --max-api N         Nombre max de requêtes API (défaut: illimité)

Dépendances : pip install requests pyyaml pypdf pdfplumber
"""

__version__ = "1.0.0"

import os
import sys
import re
import csv
import json
import time
import yaml
import signal
import argparse
import unicodedata
from pathlib import Path
from datetime import datetime
from collections import Counter
from typing import Optional, Tuple, List, Dict

# ── Optionnels ──────────────────────────────────────────────────────────────
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    import pypdf
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False

try:
    import pdfplumber
    HAS_PDFPLUMBER = True
except ImportError:
    HAS_PDFPLUMBER = False


# ════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ════════════════════════════════════════════════════════════════════════════

GOOGLE_BOOKS_URL = "https://www.googleapis.com/books/v1/volumes"
OPEN_LIBRARY_SEARCH_URL = "https://openlibrary.org/search.json"
OPEN_LIBRARY_ISBN_URL = "https://openlibrary.org/isbn/{isbn}.json"
OPEN_LIBRARY_WORKS_URL = "https://openlibrary.org{works_key}.json"

PDF_TIMEOUT = 10

# ════════════════════════════════════════════════════════════════════════════
# EXTRACTION ISBN
# ════════════════════════════════════════════════════════════════════════════

ISBN_10_RE = re.compile(r'\b(\d{9}[\dXx])\b')
ISBN_13_RE = re.compile(r'\b(97[89]\d{10})\b')
ISBN_PAREN_RE = re.compile(r'\((\d{10,13}),?\s*\d{4}\)')  # (9781234567890, 2017)


def extract_isbn_from_text(text: str) -> List[str]:
    """Extrait tous les ISBN d'un texte."""
    isbns = []
    # ISBN-13 d'abord (plus fiable)
    isbns.extend(ISBN_13_RE.findall(text))
    # ISBN dans les parenthèses (format des dossiers Springer)
    isbns.extend(ISBN_PAREN_RE.findall(text))
    # ISBN-10
    for m in ISBN_10_RE.findall(text):
        if m not in isbns:
            isbns.append(m)
    return list(dict.fromkeys(isbns))  # dédupliqué, ordre préservé


def extract_isbn_from_pdf(filepath: str) -> List[str]:
    """Extrait les ISBN des métadonnées et premières pages d'un PDF."""
    text = ""
    if HAS_PYPDF:
        try:
            old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
            signal.alarm(PDF_TIMEOUT)
            try:
                reader = pypdf.PdfReader(filepath)
                # Métadonnées
                if reader.metadata:
                    for val in reader.metadata.values():
                        if val:
                            text += str(val) + " "
                # Premières pages
                for page in reader.pages[:3]:
                    text += (page.extract_text() or "") + " "
            finally:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, old_handler)
        except Exception:
            pass
    return extract_isbn_from_text(text)


class TimeoutError(Exception):
    pass

def _timeout_handler(signum, frame):
    raise TimeoutError("timeout")


# ════════════════════════════════════════════════════════════════════════════
# EXTRACTION TITRE DEPUIS PDF
# ════════════════════════════════════════════════════════════════════════════

def extract_title_from_pdf(filepath: str) -> str:
    """Extrait le meilleur titre possible depuis un PDF."""
    title = ""

    if HAS_PYPDF:
        try:
            old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
            signal.alarm(PDF_TIMEOUT)
            try:
                reader = pypdf.PdfReader(filepath)
                if reader.metadata:
                    meta_title = str(reader.metadata.get('/Title', '') or '')
                    if len(meta_title) > 5 and not _is_garbage(meta_title):
                        title = meta_title
            finally:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, old_handler)
        except Exception:
            pass

    if not title and HAS_PDFPLUMBER:
        try:
            old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
            signal.alarm(PDF_TIMEOUT)
            try:
                with pdfplumber.open(filepath) as pdf:
                    if pdf.pages:
                        text = pdf.pages[0].extract_text() or ""
                        # Prendre la première ligne non vide significative
                        for line in text.split('\n'):
                            line = line.strip()
                            if len(line) > 5 and not _is_garbage(line):
                                title = line
                                break
            finally:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, old_handler)
        except Exception:
            pass

    return title


GARBAGE_PATTERNS = re.compile(
    r'(page\s+\d|copyright|isbn|table of contents|contents|'
    r'^\d+$|^[ivxlc]+$|frontmatter|preface)', re.IGNORECASE)

def _is_garbage(text: str) -> bool:
    return bool(GARBAGE_PATTERNS.search(text)) or len(text) < 4


# ════════════════════════════════════════════════════════════════════════════
# API CLIENTS
# ════════════════════════════════════════════════════════════════════════════

class BookAPIClient:
    """Client pour interroger Google Books et Open Library."""

    def __init__(self, delay: float = 0.5, max_api: int = 0, verbose: bool = False):
        self.delay = delay
        self.max_api = max_api  # 0 = illimité
        self.api_calls = 0
        self.verbose = verbose
        # Cache des résultats de cette session
        self.cache = {}

    def _can_call(self) -> bool:
        if self.max_api > 0 and self.api_calls >= self.max_api:
            return False
        return True

    def _rate_limit(self):
        if self.delay > 0:
            time.sleep(self.delay)

    def lookup_by_isbn(self, isbn: str) -> Optional[dict]:
        """Cherche un livre par ISBN. Retourne {title, authors, categories, source}."""
        cache_key = f"isbn:{isbn}"
        if cache_key in self.cache:
            return self.cache[cache_key]

        result = self._google_books_isbn(isbn)
        if not result:
            result = self._open_library_isbn(isbn)

        self.cache[cache_key] = result
        return result

    def lookup_by_title(self, title: str) -> Optional[dict]:
        """Cherche un livre par titre. Retourne {title, authors, categories, source}."""
        # Nettoyer le titre
        title_clean = re.sub(r'\.(pdf|djvu)$', '', title, flags=re.IGNORECASE)
        title_clean = re.sub(r'\s*-\s*[^-]+$', '', title_clean)  # Retirer l'auteur
        title_clean = re.sub(r'\([^)]*\)', '', title_clean).strip()
        title_clean = title_clean[:100]  # Limiter la longueur

        if len(title_clean) < 4:
            return None

        cache_key = f"title:{title_clean.lower()}"
        if cache_key in self.cache:
            return self.cache[cache_key]

        result = self._google_books_title(title_clean)
        if not result:
            result = self._open_library_title(title_clean)

        self.cache[cache_key] = result
        return result

    def _google_books_isbn(self, isbn: str) -> Optional[dict]:
        if not self._can_call():
            return None
        try:
            self.api_calls += 1
            self._rate_limit()
            resp = requests.get(GOOGLE_BOOKS_URL,
                                params={'q': f'isbn:{isbn}', 'maxResults': 1},
                                timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get('totalItems', 0) > 0 and 'items' in data:
                return self._parse_google_result(data['items'][0])
        except Exception as e:
            if self.verbose:
                print(f"    [Google ISBN] Erreur: {e}")
        return None

    def _google_books_title(self, title: str) -> Optional[dict]:
        if not self._can_call():
            return None
        try:
            self.api_calls += 1
            self._rate_limit()
            resp = requests.get(GOOGLE_BOOKS_URL,
                                params={'q': title, 'maxResults': 1},
                                timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get('totalItems', 0) > 0 and 'items' in data:
                return self._parse_google_result(data['items'][0])
        except Exception as e:
            if self.verbose:
                print(f"    [Google Title] Erreur: {e}")
        return None

    def _parse_google_result(self, item: dict) -> Optional[dict]:
        info = item.get('volumeInfo', {})
        categories = info.get('categories', [])
        if not categories:
            # Essayer la description pour extraire des indices
            desc = info.get('description', '')
            return {
                'title': info.get('title', ''),
                'authors': info.get('authors', []),
                'categories': [],
                'description': desc[:300],
                'source': 'google_books',
            }
        return {
            'title': info.get('title', ''),
            'authors': info.get('authors', []),
            'categories': categories,
            'description': info.get('description', '')[:300],
            'source': 'google_books',
        }

    def _open_library_isbn(self, isbn: str) -> Optional[dict]:
        if not self._can_call():
            return None
        try:
            self.api_calls += 1
            self._rate_limit()
            resp = requests.get(OPEN_LIBRARY_ISBN_URL.format(isbn=isbn),
                                timeout=10, allow_redirects=True)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()

            subjects = data.get('subjects', [])
            # Si pas de subjects, essayer via works
            if not subjects and data.get('works'):
                works_key = data['works'][0].get('key', '')
                if works_key:
                    self.api_calls += 1
                    self._rate_limit()
                    resp2 = requests.get(
                        OPEN_LIBRARY_WORKS_URL.format(works_key=works_key),
                        timeout=10)
                    if resp2.ok:
                        works_data = resp2.json()
                        subjects = works_data.get('subjects', [])

            return {
                'title': data.get('title', ''),
                'authors': [],  # Nécessiterait une requête supplémentaire
                'categories': subjects[:10] if subjects else [],
                'description': '',
                'source': 'open_library',
            }
        except Exception as e:
            if self.verbose:
                print(f"    [OpenLib ISBN] Erreur: {e}")
        return None

    def _open_library_title(self, title: str) -> Optional[dict]:
        if not self._can_call():
            return None
        try:
            self.api_calls += 1
            self._rate_limit()
            resp = requests.get(OPEN_LIBRARY_SEARCH_URL,
                                params={'title': title, 'limit': 1},
                                timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get('docs'):
                doc = data['docs'][0]
                return {
                    'title': doc.get('title', ''),
                    'authors': doc.get('author_name', []),
                    'categories': doc.get('subject', [])[:10],
                    'description': '',
                    'source': 'open_library',
                }
        except Exception as e:
            if self.verbose:
                print(f"    [OpenLib Title] Erreur: {e}")
        return None


# ════════════════════════════════════════════════════════════════════════════
# MAPPING CATÉGORIES API → ARBORESCENCE LOCALE
# ════════════════════════════════════════════════════════════════════════════

# Google Books retourne des catégories standardisées type BISAC :
#   "Computers / Machine Learning"
#   "Mathematics / Probability & Statistics / General"
#   "Science / Physics / Quantum Theory"
#
# Open Library retourne des sujets libres :
#   "Computer science", "Artificial intelligence", "Python (Computer program language)"

CATEGORY_MAP = {
    # ── INFORMATIQUE ──────────────────────────────────────────────────────
    # IA & ML
    "artificial intelligence":     "01-SCIENCES/INFORMATIQUE/05-Intelligence-Artificielle/Machine-Learning",
    "machine learning":            "01-SCIENCES/INFORMATIQUE/05-Intelligence-Artificielle/Machine-Learning",
    "deep learning":               "01-SCIENCES/INFORMATIQUE/05-Intelligence-Artificielle/Deep-Learning",
    "neural networks":             "01-SCIENCES/INFORMATIQUE/05-Intelligence-Artificielle/Deep-Learning",
    "natural language processing":  "01-SCIENCES/INFORMATIQUE/05-Intelligence-Artificielle/NLP",
    "computational linguistics":   "01-SCIENCES/INFORMATIQUE/05-Intelligence-Artificielle/NLP",
    "computer vision":             "01-SCIENCES/INFORMATIQUE/05-Intelligence-Artificielle/Vision-par-Ordinateur",
    "image processing":            "01-SCIENCES/INFORMATIQUE/05-Intelligence-Artificielle/Vision-par-Ordinateur",
    "pattern recognition":         "01-SCIENCES/INFORMATIQUE/05-Intelligence-Artificielle/Vision-par-Ordinateur",

    # Data Science
    "data mining":                 "01-SCIENCES/INFORMATIQUE/06-Data-Science/Big-Data",
    "big data":                    "01-SCIENCES/INFORMATIQUE/06-Data-Science/Big-Data",
    "data processing":             "01-SCIENCES/INFORMATIQUE/06-Data-Science/Big-Data",
    "data visualization":          "01-SCIENCES/INFORMATIQUE/06-Data-Science/Visualisation",
    "information visualization":   "01-SCIENCES/INFORMATIQUE/06-Data-Science/Visualisation",

    # Langages
    "python":                      "01-SCIENCES/INFORMATIQUE/03-Langages-Programmation/Python",
    "java":                        "01-SCIENCES/INFORMATIQUE/03-Langages-Programmation/Java",
    "c++":                         "01-SCIENCES/INFORMATIQUE/03-Langages-Programmation/C-Cpp-CSharp",
    "c#":                          "01-SCIENCES/INFORMATIQUE/03-Langages-Programmation/C-Cpp-CSharp",
    "c (computer program language)": "01-SCIENCES/INFORMATIQUE/03-Langages-Programmation/C-Cpp-CSharp",
    "javascript":                  "01-SCIENCES/INFORMATIQUE/03-Langages-Programmation/JavaScript",

    # Sécurité
    "computer security":           "01-SCIENCES/INFORMATIQUE/10-Securite-Crypto",
    "cryptography":                "01-SCIENCES/INFORMATIQUE/10-Securite-Crypto",
    "data encryption":             "01-SCIENCES/INFORMATIQUE/10-Securite-Crypto",
    "computer crimes":             "01-SCIENCES/INFORMATIQUE/10-Securite-Crypto",
    "hacking":                     "01-SCIENCES/INFORMATIQUE/10-Securite-Crypto",

    # Réseaux
    "computer networks":           "01-SCIENCES/INFORMATIQUE/08-Reseaux-Telecom",
    "computer network protocols":  "01-SCIENCES/INFORMATIQUE/08-Reseaux-Telecom",
    "telecommunications":          "01-SCIENCES/INFORMATIQUE/08-Reseaux-Telecom",
    "wireless":                    "01-SCIENCES/INFORMATIQUE/08-Reseaux-Telecom",
    "internet":                    "01-SCIENCES/INFORMATIQUE/08-Reseaux-Telecom",

    # BDD
    "databases":                   "01-SCIENCES/INFORMATIQUE/07-Bases-de-Donnees",
    "database management":         "01-SCIENCES/INFORMATIQUE/07-Bases-de-Donnees",
    "sql":                         "01-SCIENCES/INFORMATIQUE/07-Bases-de-Donnees",

    # OS
    "operating systems":           "01-SCIENCES/INFORMATIQUE/09-Systemes-OS/Linux-Unix",
    "linux":                       "01-SCIENCES/INFORMATIQUE/09-Systemes-OS/Linux-Unix",
    "unix":                        "01-SCIENCES/INFORMATIQUE/09-Systemes-OS/Linux-Unix",
    "microsoft windows":           "01-SCIENCES/INFORMATIQUE/09-Systemes-OS/Windows",

    # Web
    "web development":             "01-SCIENCES/INFORMATIQUE/14-Developpement-Web",
    "web sites":                   "01-SCIENCES/INFORMATIQUE/14-Developpement-Web",
    "web site development":        "01-SCIENCES/INFORMATIQUE/14-Developpement-Web",
    "internet programming":        "01-SCIENCES/INFORMATIQUE/14-Developpement-Web",

    # Mobile
    "mobile computing":            "01-SCIENCES/INFORMATIQUE/15-Developpement-Mobile",
    "mobile apps":                 "01-SCIENCES/INFORMATIQUE/15-Developpement-Mobile",
    "android":                     "01-SCIENCES/INFORMATIQUE/15-Developpement-Mobile",
    "iphone":                      "01-SCIENCES/INFORMATIQUE/15-Developpement-Mobile",
    "ios":                         "01-SCIENCES/INFORMATIQUE/15-Developpement-Mobile",

    # Cloud
    "cloud computing":             "01-SCIENCES/INFORMATIQUE/11-Cloud-DevOps",
    "virtualization":              "01-SCIENCES/INFORMATIQUE/11-Cloud-DevOps",

    # IoT
    "internet of things":          "01-SCIENCES/INFORMATIQUE/12-IoT-Embarque",
    "embedded computer systems":   "01-SCIENCES/INFORMATIQUE/12-IoT-Embarque",
    "microcontrollers":            "01-SCIENCES/INFORMATIQUE/12-IoT-Embarque",
    "arduino":                     "01-SCIENCES/INFORMATIQUE/12-IoT-Embarque",
    "raspberry pi":                "01-SCIENCES/INFORMATIQUE/12-IoT-Embarque",

    # Graphique
    "computer graphics":           "01-SCIENCES/INFORMATIQUE/13-Infographie-3D",
    "three-dimensional display":   "01-SCIENCES/INFORMATIQUE/13-Infographie-3D",

    # Algorithmes
    "algorithms":                  "01-SCIENCES/INFORMATIQUE/02-Algorithmes-Structures-Donnees",
    "data structures":             "01-SCIENCES/INFORMATIQUE/02-Algorithmes-Structures-Donnees",

    # Génie logiciel
    "software engineering":        "01-SCIENCES/INFORMATIQUE/04-Genie-Logiciel",
    "software development":        "01-SCIENCES/INFORMATIQUE/04-Genie-Logiciel",
    "software testing":            "01-SCIENCES/INFORMATIQUE/04-Genie-Logiciel",

    # Fondamentaux
    "computer science":            "01-SCIENCES/INFORMATIQUE/01-Fondamentaux-CS",
    "computers":                   "01-SCIENCES/INFORMATIQUE/01-Fondamentaux-CS",
    "information theory":          "01-SCIENCES/INFORMATIQUE/01-Fondamentaux-CS",

    # Catch-all Google Books "Computers / ..."
    "computers / general":                        "01-SCIENCES/INFORMATIQUE/01-Fondamentaux-CS",
    "computers / intelligence (ai) & semantics":  "01-SCIENCES/INFORMATIQUE/05-Intelligence-Artificielle/Machine-Learning",
    "computers / security":                       "01-SCIENCES/INFORMATIQUE/10-Securite-Crypto",
    "computers / networking":                     "01-SCIENCES/INFORMATIQUE/08-Reseaux-Telecom",
    "computers / programming":                    "01-SCIENCES/INFORMATIQUE/04-Genie-Logiciel",
    "computers / databases":                      "01-SCIENCES/INFORMATIQUE/07-Bases-de-Donnees",
    "computers / operating systems":              "01-SCIENCES/INFORMATIQUE/09-Systemes-OS/Linux-Unix",
    "computers / web":                            "01-SCIENCES/INFORMATIQUE/14-Developpement-Web",
    "computers / hardware":                       "01-SCIENCES/INFORMATIQUE/12-IoT-Embarque",
    "computers / software development & engineering": "01-SCIENCES/INFORMATIQUE/04-Genie-Logiciel",

    # ── MATHEMATIQUE ──────────────────────────────────────────────────────
    "algebra":                     "01-SCIENCES/MATHEMATIQUE/01-Algebre",
    "linear algebra":              "01-SCIENCES/MATHEMATIQUE/01-Algebre",
    "group theory":                "01-SCIENCES/MATHEMATIQUE/01-Algebre",
    "mathematical analysis":       "01-SCIENCES/MATHEMATIQUE/02-Analyse",
    "calculus":                    "01-SCIENCES/MATHEMATIQUE/02-Analyse",
    "differential equations":      "01-SCIENCES/MATHEMATIQUE/02-Analyse",
    "functional analysis":         "01-SCIENCES/MATHEMATIQUE/02-Analyse",
    "geometry":                    "01-SCIENCES/MATHEMATIQUE/03-Geometrie-Topologie",
    "topology":                    "01-SCIENCES/MATHEMATIQUE/03-Geometrie-Topologie",
    "probabilities":               "01-SCIENCES/MATHEMATIQUE/04-Probabilites-Statistiques",
    "statistics":                  "01-SCIENCES/MATHEMATIQUE/04-Probabilites-Statistiques",
    "mathematical statistics":     "01-SCIENCES/MATHEMATIQUE/04-Probabilites-Statistiques",
    "stochastic processes":        "01-SCIENCES/MATHEMATIQUE/04-Probabilites-Statistiques",
    "logic":                       "01-SCIENCES/MATHEMATIQUE/05-Logique",
    "mathematical logic":          "01-SCIENCES/MATHEMATIQUE/05-Logique",
    "set theory":                  "01-SCIENCES/MATHEMATIQUE/05-Logique",
    "number theory":               "01-SCIENCES/MATHEMATIQUE/06-Theorie-des-Nombres",
    "numerical analysis":          "01-SCIENCES/MATHEMATIQUE/07-Calcul-Numerique",
    "mathematics":                 "01-SCIENCES/MATHEMATIQUE/08-Mathematiques-Generales",
    "mathematics / general":       "01-SCIENCES/MATHEMATIQUE/08-Mathematiques-Generales",
    "mathematics / algebra":       "01-SCIENCES/MATHEMATIQUE/01-Algebre",
    "mathematics / mathematical analysis":  "01-SCIENCES/MATHEMATIQUE/02-Analyse",
    "mathematics / calculus":      "01-SCIENCES/MATHEMATIQUE/02-Analyse",
    "mathematics / geometry":      "01-SCIENCES/MATHEMATIQUE/03-Geometrie-Topologie",
    "mathematics / probability & statistics": "01-SCIENCES/MATHEMATIQUE/04-Probabilites-Statistiques",
    "mathematics / logic":         "01-SCIENCES/MATHEMATIQUE/05-Logique",

    # ── PHYSIQUE ──────────────────────────────────────────────────────────
    "mechanics":                   "01-SCIENCES/PHYSIQUE/01-Mecanique",
    "fluid mechanics":             "01-SCIENCES/PHYSIQUE/01-Mecanique",
    "electromagnetism":            "01-SCIENCES/PHYSIQUE/02-Electromagnetisme-Electronique",
    "electronics":                 "01-SCIENCES/PHYSIQUE/02-Electromagnetisme-Electronique",
    "thermodynamics":              "01-SCIENCES/PHYSIQUE/03-Thermodynamique",
    "optics":                      "01-SCIENCES/PHYSIQUE/04-Optique",
    "quantum theory":              "01-SCIENCES/PHYSIQUE/05-Relativite-Quantique",
    "quantum physics":             "01-SCIENCES/PHYSIQUE/05-Relativite-Quantique",
    "relativity":                  "01-SCIENCES/PHYSIQUE/05-Relativite-Quantique",
    "astrophysics":                "01-SCIENCES/PHYSIQUE/06-Astrophysique-Cosmologie",
    "astronomy":                   "01-SCIENCES/PHYSIQUE/06-Astrophysique-Cosmologie",
    "cosmology":                   "01-SCIENCES/PHYSIQUE/06-Astrophysique-Cosmologie",
    "nuclear physics":             "01-SCIENCES/PHYSIQUE/07-Physique-Nucleaire",
    "physics":                     "01-SCIENCES/PHYSIQUE/01-Mecanique",  # défaut physique
    "science / physics":           "01-SCIENCES/PHYSIQUE/01-Mecanique",

    # ── CHIMIE ────────────────────────────────────────────────────────────
    "chemistry":                   "01-SCIENCES/CHIMIE/02-Chimie-Physique",
    "organic chemistry":           "01-SCIENCES/CHIMIE/01-Chimie-Organique",
    "nanotechnology":              "01-SCIENCES/CHIMIE/03-Nanotechnologie",

    # ── BIOLOGIE ──────────────────────────────────────────────────────────
    "biology":                     "01-SCIENCES/BIOLOGIE",
    "genetics":                    "01-SCIENCES/BIOLOGIE",
    "evolution":                   "01-SCIENCES/BIOLOGIE",

    # ── LOISIRS ───────────────────────────────────────────────────────────
    "games & activities":          "08-LOISIRS/JEUX-VIDEO",
    "video games":                 "08-LOISIRS/JEUX-VIDEO",
    "games":                       "08-LOISIRS/JEUX-VIDEO",
    "drawing":                     "08-LOISIRS/DESSIN/Fondamentaux",
    "art":                         "08-LOISIRS/DESSIN/Fondamentaux",
    "chess":                       "08-LOISIRS/ECHECS",
}


def map_categories_to_local(categories: List[str]) -> Tuple[str, str]:
    """
    Mappe une liste de catégories API vers le chemin local.
    Retourne (chemin_local, catégorie_source).

    Stratégie : chercher la correspondance la plus spécifique d'abord.
    """
    if not categories:
        return ('', '')

    for cat in categories:
        cat_lower = cat.lower().strip()

        # Match exact
        if cat_lower in CATEGORY_MAP:
            return (CATEGORY_MAP[cat_lower], cat)

        # Match partiel — chercher si une clé du map est contenue dans la catégorie
        for key, path in CATEGORY_MAP.items():
            if key in cat_lower:
                return (path, cat)

    # Aucun match — essayer mot par mot sur la première catégorie
    for cat in categories:
        words = cat.lower().replace('/', ' ').replace(',', ' ').split()
        for word in words:
            word = word.strip()
            if word in CATEGORY_MAP:
                return (CATEGORY_MAP[word], cat)

    return ('', categories[0] if categories else '')


# ════════════════════════════════════════════════════════════════════════════
# MOTEUR PRINCIPAL
# ════════════════════════════════════════════════════════════════════════════

class BiblioOrganizerAPI:
    """Moteur de classification par API web."""

    def __init__(self, base_path: str, isbn_cache_path: str = None,
                 use_pdf: bool = True, delay: float = 0.5,
                 max_api: int = 0, verbose: bool = False):
        self.base_path = os.path.abspath(base_path)
        self.use_pdf = use_pdf
        self.verbose = verbose

        # Charger le cache ISBN existant
        self.isbn_cache = {}
        if isbn_cache_path and os.path.isfile(isbn_cache_path):
            with open(isbn_cache_path, 'r', encoding='utf-8') as f:
                self.isbn_cache = json.load(f)
            print(f"  [Cache] {len(self.isbn_cache)} ISBN chargés depuis {isbn_cache_path}")

        # Cache des catégories trouvées par l'API (persiste entre exécutions)
        self.categories_cache_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'categories_cache.json')
        self.categories_cache = {}
        if os.path.isfile(self.categories_cache_path):
            with open(self.categories_cache_path, 'r', encoding='utf-8') as f:
                self.categories_cache = json.load(f)
            print(f"  [Cache] {len(self.categories_cache)} catégories en cache")

        # Client API
        self.api = BookAPIClient(delay=delay, max_api=max_api, verbose=verbose)

    def _save_categories_cache(self):
        with open(self.categories_cache_path, 'w', encoding='utf-8') as f:
            json.dump(self.categories_cache, f, ensure_ascii=False, indent=2)

    def classify_file(self, filepath: str) -> dict:
        """
        Classe un fichier en interrogeant les API.
        Retourne {destination, score, method, details, api_categories}.
        """
        filename = os.path.basename(filepath)
        parent_dir = os.path.basename(os.path.dirname(filepath))

        # ── 1. Vérifier le cache de catégories ──
        cache_key = filename.lower()
        if cache_key in self.categories_cache:
            cached = self.categories_cache[cache_key]
            return {
                'destination': cached['destination'],
                'score': 0.9,
                'method': 'cache',
                'details': f"cache: {cached['api_category']}",
                'api_categories': [cached['api_category']],
            }

        # ── 2. Extraire/chercher l'ISBN ──
        isbns = extract_isbn_from_text(filename + " " + parent_dir)

        if not isbns and self.use_pdf and filepath.lower().endswith('.pdf'):
            isbns = extract_isbn_from_pdf(filepath)

        # ── 3. Chercher via ISBN ──
        book_info = None
        used_isbn = None

        for isbn in isbns:
            # D'abord le cache ISBN existant
            if isbn in self.isbn_cache:
                cached = self.isbn_cache[isbn]
                title = cached.get('title', '')
                if title:
                    # On a le titre, chercher les catégories via l'API
                    book_info = self.api.lookup_by_isbn(isbn)
                    if book_info and book_info.get('categories'):
                        used_isbn = isbn
                        break
                    # Essayer par titre si pas de catégories via ISBN
                    book_info = self.api.lookup_by_title(title)
                    if book_info and book_info.get('categories'):
                        used_isbn = isbn
                        break

            # Sinon requête directe
            book_info = self.api.lookup_by_isbn(isbn)
            if book_info and book_info.get('categories'):
                used_isbn = isbn
                break

        # ── 4. Si pas trouvé par ISBN, chercher par titre ──
        if not book_info or not book_info.get('categories'):
            # Nettoyer le nom de fichier pour en faire un titre de recherche
            title = re.sub(r'\.(pdf|djvu)$', '', filename, flags=re.IGNORECASE)
            title = re.sub(r'\s*\(\d+\)\s*$', '', title)  # Retirer (2), (3)...

            # Si le titre est trop court/vague, essayer l'extraction PDF
            if len(title) < 8 and self.use_pdf and filepath.lower().endswith('.pdf'):
                pdf_title = extract_title_from_pdf(filepath)
                if pdf_title and len(pdf_title) > len(title):
                    title = pdf_title

            if len(title) >= 5:
                result = self.api.lookup_by_title(title)
                if result and result.get('categories'):
                    book_info = result

        # ── 5. Mapper les catégories vers l'arborescence locale ──
        if book_info and book_info.get('categories'):
            dest, matched_cat = map_categories_to_local(book_info['categories'])
            if dest:
                # Sauvegarder en cache
                self.categories_cache[cache_key] = {
                    'destination': dest,
                    'api_category': matched_cat,
                    'source': book_info.get('source', '?'),
                    'isbn': used_isbn or '',
                }
                return {
                    'destination': dest,
                    'score': 0.85,
                    'method': f"api_{book_info['source']}",
                    'details': f"{matched_cat}",
                    'api_categories': book_info['categories'],
                }
            else:
                # Catégories trouvées mais pas de mapping
                return {
                    'destination': '_A-TRIER',
                    'score': 0.3,
                    'method': f"api_{book_info['source']}_unmapped",
                    'details': f"catégories non mappées: {book_info['categories'][:3]}",
                    'api_categories': book_info['categories'],
                }

        # ── 6. Rien trouvé ──
        return {
            'destination': '_A-TRIER',
            'score': 0,
            'method': 'not_found',
            'details': 'aucun résultat API',
            'api_categories': [],
        }

    def scan(self) -> List[dict]:
        """Parcourt la bibliothèque et classe chaque fichier."""
        proposals = []

        print(f"\n{'='*70}")
        print(f"  BIBLIO ORGANIZER API v{__version__}")
        print(f"  Racine : {self.base_path}")
        print(f"{'='*70}\n")

        # Collecter les fichiers
        print("  [Scan] Parcours des fichiers...")
        all_files = []
        for root, dirs, files in os.walk(self.base_path):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d != '_A-TRIER']
            for fname in files:
                if fname.lower().endswith('.pdf'):
                    all_files.append(os.path.join(root, fname))

        print(f"  [Scan] {len(all_files)} fichiers PDF trouvés.")
        print(f"  [API] Délai entre requêtes : {self.api.delay}s")
        if self.api.max_api > 0:
            print(f"  [API] Limite : {self.api.max_api} requêtes max")
        print()

        stats = Counter()

        for i, filepath in enumerate(all_files, 1):
            rel_path = os.path.relpath(filepath, self.base_path)
            current_dir = os.path.dirname(rel_path)

            result = self.classify_file(filepath)
            dest = result['destination']

            # Vérifier si déjà au bon endroit
            if dest != '_A-TRIER' and (current_dir == dest or current_dir.startswith(dest + '/')):
                stats['deja_classé'] += 1
                continue

            result['source'] = filepath
            result['rel_source'] = rel_path
            result['filename'] = os.path.basename(filepath)
            proposals.append(result)
            stats[result['method']] += 1

            if self.verbose:
                marker = '✓' if dest != '_A-TRIER' else '?'
                print(f"  {marker} {result['filename'][:50]:50s} → {dest}")
                print(f"    [{result['method']}] {result['details']}")

            if i % 50 == 0:
                print(f"  ... {i}/{len(all_files)} fichiers analysés "
                      f"({self.api.api_calls} requêtes API)")

        # Sauvegarder le cache
        self._save_categories_cache()

        # Résumé
        classified = [p for p in proposals if p['destination'] != '_A-TRIER']
        unclassified = [p for p in proposals if p['destination'] == '_A-TRIER']

        print(f"\n{'─'*70}")
        print(f"  RÉSUMÉ")
        print(f"{'─'*70}")
        print(f"  Fichiers analysés        : {len(all_files)}")
        print(f"  Déjà bien classés        : {stats['deja_classé']}")
        print(f"  À déplacer (API)         : {len(classified)}")
        print(f"    dont Google Books      : {stats.get('api_google_books', 0)}")
        print(f"    dont Open Library      : {stats.get('api_open_library', 0)}")
        print(f"    dont cache             : {stats.get('cache', 0)}")
        print(f"  Non classés (→ A-TRIER)  : {len(unclassified)}")
        print(f"    dont non trouvé        : {stats.get('not_found', 0)}")
        print(f"    dont catégorie inconnue: {stats.get('api_google_books_unmapped', 0) + stats.get('api_open_library_unmapped', 0)}")
        print(f"  Requêtes API effectuées  : {self.api.api_calls}")
        print(f"  Cache catégories         : {len(self.categories_cache)} entrées")
        print(f"{'─'*70}\n")

        return proposals

    def execute(self, proposals: list[dict]) -> str:
        """Exécute les déplacements."""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        log_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            '..', 'logs', f"log_classement_api_{timestamp}.csv")
        os.makedirs(os.path.dirname(log_file), exist_ok=True)

        moved = 0
        errors = 0

        with open(log_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                'ancien_chemin', 'nouveau_chemin', 'methode',
                'score', 'details', 'api_categories', 'status'
            ])

            for prop in proposals:
                if prop['destination'] == '_A-TRIER':
                    continue  # Ne pas déplacer les non-classés sauf dans A-TRIER

                src = prop['source']
                dest_dir = os.path.join(self.base_path, prop['destination'])
                dest = os.path.join(dest_dir, prop['filename'])

                if os.path.exists(dest):
                    base, ext = os.path.splitext(prop['filename'])
                    counter = 2
                    while os.path.exists(dest):
                        dest = os.path.join(dest_dir, f"{base} ({counter}){ext}")
                        counter += 1

                try:
                    os.makedirs(dest_dir, exist_ok=True)
                    os.rename(src, dest)
                    writer.writerow([
                        src, dest, prop['method'],
                        f"{prop['score']:.3f}", prop['details'],
                        "|".join(prop.get('api_categories', [])), 'OK'
                    ])
                    moved += 1
                except Exception as e:
                    writer.writerow([
                        src, dest, prop['method'],
                        f"{prop['score']:.3f}", prop['details'],
                        "|".join(prop.get('api_categories', [])), f'ERREUR: {e}'
                    ])
                    errors += 1

        print(f"  Fichiers déplacés : {moved}")
        print(f"  Erreurs           : {errors}")
        print(f"  Log               : {log_file}")
        return log_file

    def generate_report(self, proposals: list[dict]) -> str:
        """Génère un rapport CSV."""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        report_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            '..', 'logs', f"rapport_classement_api_{timestamp}.csv")
        os.makedirs(os.path.dirname(report_file), exist_ok=True)

        with open(report_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                'fichier', 'emplacement_actuel', 'destination_proposée',
                'methode', 'score', 'details', 'categories_api'
            ])
            for prop in proposals:
                writer.writerow([
                    prop['filename'],
                    os.path.dirname(prop['rel_source']),
                    prop['destination'],
                    prop['method'],
                    f"{prop['score']:.3f}",
                    prop['details'],
                    " | ".join(prop.get('api_categories', [])),
                ])

        print(f"  Rapport sauvegardé : {report_file}")
        return report_file


# ════════════════════════════════════════════════════════════════════════════
# ANNULATION
# ════════════════════════════════════════════════════════════════════════════

def undo_from_log(log_file: str):
    """Annule les déplacements depuis un fichier de log."""
    print(f"\n  [Undo] Annulation depuis {log_file}...")
    restored = 0
    errors = 0

    with open(log_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    for row in reversed(rows):
        if row.get('status') != 'OK':
            continue
        src = row['nouveau_chemin']
        dest = row['ancien_chemin']
        try:
            dest_dir = os.path.dirname(dest)
            os.makedirs(dest_dir, exist_ok=True)
            os.rename(src, dest)
            restored += 1
        except Exception as e:
            print(f"  ERREUR: {e}")
            errors += 1

    print(f"  Fichiers restaurés : {restored}")
    print(f"  Erreurs            : {errors}")


# ════════════════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Biblio Organizer API — Classement thématique via API web",
    )
    parser.add_argument('path',
                        help="Chemin vers la bibliothèque ou log CSV (avec --undo)")
    parser.add_argument('--execute', action='store_true',
                        help="Appliquer les déplacements")
    parser.add_argument('--undo', action='store_true',
                        help="Annuler depuis un log CSV")
    parser.add_argument('--isbn-cache',
                        default=None,
                        help="Chemin vers isbn_cache.json")
    parser.add_argument('--no-pdf', action='store_true',
                        help="Désactiver l'extraction PDF")
    parser.add_argument('--delay', type=float, default=0.5,
                        help="Délai entre requêtes API en sec (défaut: 0.5)")
    parser.add_argument('--max-api', type=int, default=0,
                        help="Nombre max de requêtes API (0=illimité)")
    parser.add_argument('--verbose', '-v', action='store_true')

    args = parser.parse_args()

    if args.undo:
        undo_from_log(args.path)
        return

    if not os.path.isdir(args.path):
        print(f"ERREUR: {args.path} n'est pas un dossier valide.")
        sys.exit(1)

    if not HAS_REQUESTS:
        print("ERREUR: 'requests' non installé. pip install requests")
        sys.exit(1)

    # Auto-détecter le cache ISBN
    isbn_cache = args.isbn_cache
    if not isbn_cache:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        candidate = os.path.join(script_dir, '..', 'renommage', 'isbn_cache.json')
        if os.path.isfile(candidate):
            isbn_cache = candidate

    organizer = BiblioOrganizerAPI(
        base_path=args.path,
        isbn_cache_path=isbn_cache,
        use_pdf=not args.no_pdf,
        delay=args.delay,
        max_api=args.max_api,
        verbose=args.verbose,
    )

    proposals = organizer.scan()

    if not proposals:
        print("  Rien à déplacer !")
        return

    if args.execute:
        print("  [Exécution] Déplacement des fichiers...\n")
        organizer.execute(proposals)
    else:
        organizer.generate_report(proposals)
        print("\n  ℹ Pour appliquer : relancez avec --execute")
        print("  ℹ Pour annuler   : --undo logs/log_classement_api_*.csv")


if __name__ == '__main__':
    main()
