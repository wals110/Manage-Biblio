#!/usr/bin/env python3
"""
Klodo Renamer — Renommage intelligent de bibliothèques PDF.
=============================================================
Renomme les fichiers PDF vers la nomenclature : "Titre - Auteur.pdf"

Pipeline de correction en cascade (par priorité) :
  1. Nettoyage du nom de fichier (artefacts web, underscores, IDs, etc.)
  2. Recherche ISBN en ligne (Google Books / Open Library)
  3. Extraction titre/auteur depuis les métadonnées et le texte du PDF
  4. Extraction titre depuis le nom du dossier parent

Le script ne modifie QUE les fichiers dont le nom est "pas propre".
Il peut être relancé autant de fois que nécessaire.

Usage :
    python3 klodo_renamer.py /chemin/vers/biblio              # Rapport seul
    python3 klodo_renamer.py /chemin/vers/biblio --execute     # Appliquer
    python3 klodo_renamer.py --undo log_renommage_*.csv        # Annuler

Options :
    --no-online     Désactiver la recherche ISBN en ligne
    --no-pdf        Désactiver l'extraction depuis les PDFs
    --dry-run       Synonyme du mode rapport (pas de --execute)

Dépendances : pip install pypdf pdfplumber requests
"""

try:
    from lib import __version__
except ImportError:
    __version__ = "0.0.0"

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

from lib.checkpoint import CheckpointManager

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
PDF_TIMEOUT = 15          # Timeout extraction PDF (secondes)
ISBN_CACHE_FILE = "isbn_cache.json"
MAX_FILENAME_LEN = 180    # macOS = 255, on garde de la marge

# ---------------------------------------------------------------------------
# PATTERNS
# ---------------------------------------------------------------------------
NUMERIC_ID_RE    = re.compile(r'^[\d_]+\.pdf$', re.IGNORECASE)
ISBN_GLUED_RE    = re.compile(r'^\d{7,}[A-Za-z]')
ID_SPACE_RE      = re.compile(r'^\d{7,}\s+\S')
DOI_RE           = re.compile(r'^10\.\d{4}[@/]')
ARXIV_RE         = re.compile(r'^\d{4}\.\d{4,}\.pdf$', re.IGNORECASE)
HEX_RE           = re.compile(r'^[0-9a-f]{20,}\.pdf$', re.IGNORECASE)
ISBN_IN_NAME_RE  = re.compile(r'^\d{7,13}')
GARBLED_RE       = re.compile(r'8=8AB@|0B @|8AB@0B')
PRODUCTION_RE    = re.compile(
    r'(CUP\s*BRB|CUP\s*BKH|CharCount|Page-i|prexii\.indd|'
    r'_forensics\.indb|\.pdf\.pdf$|^\d{10,13}\.pdf$|'
    r'AR\s+Engineering)', re.IGNORECASE)

GENERIC_TITLES = {
    'team-fly®', 'team-fly', 'edited by', 'summary of contents',
    'page ii', 'page iii', 'page iv', 'cid 2', 'cid 23', 'teamlrn',
    'p1 fbu', 'd p g d', 'b a y e', "' « %.^", 'untitled',
    'online access', 'title page', 'contents', 'index', 'preface',
    'introduction', 'frontmatter', 'cover', 'summary', 'copyright',
    'table of contents', 'foreword', 'acknowledgments', 'bibliography',
    'appendix', 'glossary', 'dedication', 'about the author',
    '7th edition', 'control series',
    # Placeholders PDF courants (métadonnées non remplies)
    'title', 'author', 'title - author', 'book title', 'book title - author',
    'my title', 'document', 'document title', 'no title', 'unknown',
    'unknown title', 'test', 'sample', 'example', 'template',
}

# Mots trop vagues pour constituer un bon titre à eux seuls
SINGLE_WORD_BLACKLIST = {
    'adaptive', 'advanced', 'analysis', 'application', 'applications',
    'beginning', 'companion', 'complete', 'computing', 'controlled',
    'developing', 'development', 'distributed', 'essential', 'foundations',
    'fundamental', 'fundamentals', 'getting', 'implementing', 'industrial',
    'information', 'introducing', 'learning', 'managing', 'mastering',
    'microsoft', 'migrating', 'modern', 'practical', 'processing',
    'professional', 'programming', 'revealed', 'scalable', 'scientific',
    'structures', 'understanding', 'absolute', 'collection',
}


# ===================================================================
#  PHASE 1 — DÉTECTION : le nom est-il propre ou pas ?
# ===================================================================
def _looks_like_real_title(text: str) -> bool:
    """Vérifie qu'un texte (après suppression d'un ID) est un vrai titre."""
    if not text or len(text.strip()) < 3:
        return False
    text = text.strip()
    if re.match(r'^\(\d+\)$', text):
        return False
    if re.search(r'%[0-9A-Fa-f]{2}', text):
        decoded = unquote(text)
        if decoded == text or len(decoded.strip()) < 3:
            return False
        text = decoded
    words = text.split()
    if len(words) <= 2:
        return False
    if len(words) == 1 and len(text) > 14:
        return False
    return True


def is_name_clean(filename: str, name_patterns: list[str] | None = None) -> bool:
    """Retourne True si le nom n'a pas besoin de correction.

    Args:
        filename: Nom du fichier (avec extension).
        name_patterns: Liste de regex que le stem doit matcher (au moins un).
                       Si vide ou None, pas de vérification de format.
    """
    stem = Path(filename).stem

    # Décoder URL
    if '%' in stem:
        stem_decoded = unquote(stem)
    else:
        stem_decoded = stem

    # --- Signaux négatifs : fichiers clairement à corriger ---

    # Double extension .pdf.pdf
    if stem.lower().endswith('.pdf'):
        real_stem = stem[:-4]
        if re.match(r'^[\d_]+$', real_stem):
            return False

    # IDs numériques purs (ex: 2738119042.pdf)
    if NUMERIC_ID_RE.match(filename):
        return False
    # Court et cryptique sans espaces (ex: AbCd12.pdf, B01BGV25GS.pdf)
    # Mais PAS les vrais mots comme Algorithms.pdf, Blockchain.pdf
    if re.match(r'^[A-Za-z0-9_\-]{1,20}\.pdf$', filename, re.IGNORECASE) \
            and ' ' not in stem and len(stem) < 15:
        # Si c'est un mot pur (que des lettres, ≥ 5 chars) → probablement un vrai mot
        if not (re.match(r'^[A-Za-zÀ-ÿ\-\']+$', stem) and len(stem) >= 5):
            return False
    # DOI / arXiv / Hash hexadécimal
    if DOI_RE.match(stem_decoded) or ARXIV_RE.match(filename) or HEX_RE.match(filename):
        return False
    # URL-encodé pur (ex: %28%29%2F.pdf)
    if re.match(r'^(%[0-9A-Fa-f]{2})+\.pdf$', filename, re.IGNORECASE):
        return False
    # ID numérique + texte trop court
    if ID_SPACE_RE.match(stem_decoded):
        text_part = re.sub(r'^\d+\s*', '', stem_decoded).strip()
        if not _looks_like_real_title(text_part):
            return False
    # ISBN collé à du texte court
    if ISBN_GLUED_RE.match(stem_decoded):
        text_part = re.sub(r'^\d+', '', stem_decoded)
        if not _looks_like_real_title(text_part):
            return False
    # Codes URL dans le nom
    if re.search(r'%[0-9A-Fa-f]{2}', stem):
        decoded = unquote(stem)
        cleaned = re.sub(r'[(){}\[\]\d\s]', '', decoded).strip()
        if len(cleaned) < 3:
            return False
    # Encodage corrompu (Windows-1251, etc.)
    if GARBLED_RE.search(stem_decoded):
        return False
    # Code de production éditeur
    if PRODUCTION_RE.search(filename):
        return False
    # Commence par un ISBN
    if ISBN_IN_NAME_RE.match(stem_decoded):
        return False

    # Commence par des chiffres + tiret/underscore + texte (ex: 13-info-extract.pdf)
    if re.match(r'^\d+[-_]', stem_decoded):
        return False

    # --- Signal positif : le nom est-il déjà lisible ? ---
    # Le nom doit contenir de vrais mots (dictionnaire EN/FR + termes techniques)
    from lib.wordcheck import contains_real_words

    words = re.findall(r'[A-Za-zÀ-ÿ]{2,}', stem_decoded)

    # Vérification wordcheck
    wordcheck_ok = False
    # 3+ mots et ≥ 10 chars : vérifier que ce sont de vrais mots
    if len(words) >= 3 and len(stem_decoded) >= 10:
        wordcheck_ok = contains_real_words(stem_decoded)
    # 1-2 mots, ≥ 5 chars, pas d'IDs collés : vérifier dans le dico
    elif len(words) >= 1 and len(stem) >= 5 and re.search(r'[A-Za-zÀ-ÿ]{3,}', stem):
        if not re.search(r'\d{5,}', stem_decoded):
            wordcheck_ok = contains_real_words(stem_decoded)
    # Nom avec espace et ≥ 5 chars : vérifier dans le dico
    elif len(stem) >= 5 and ' ' in stem:
        wordcheck_ok = contains_real_words(stem_decoded)

    if not wordcheck_ok:
        return False

    # Vérification des patterns de nommage (si configurés)
    if name_patterns:
        for p in name_patterns:
            try:
                if re.search(p, stem_decoded):
                    return True
            except re.error:
                continue
        return False

    return True


# ===================================================================
#  PHASE 1 — NETTOYAGE DU NOM DE FICHIER
# ===================================================================
def extract_first_author(author_str: str) -> str:
    """Extrait uniquement le premier auteur."""
    if not author_str:
        return None
    author_str = re.sub(r',?\s*etc\.?\s*$', '', author_str, flags=re.IGNORECASE).strip()
    author_str = re.sub(r',?\s*et\s+al\.?\s*$', '', author_str, flags=re.IGNORECASE).strip()
    author_str = re.sub(r'\s*\((editor|ed|dir|arabe)[s.]?\s*[^)]*\)', '', author_str, flags=re.IGNORECASE).strip()
    parts = [p.strip() for p in author_str.split(',')]
    if len(parts) == 2 and len(parts[0].split()) == 1 and 1 <= len(parts[1].split()) <= 2:
        return f"{parts[1]} {parts[0]}".strip()
    authors = re.split(r',\s+(?=[A-ZÀ-Ö])', author_str)
    first = authors[0].strip() if authors else author_str.strip()
    first = first.strip(' -.,;')
    return first if len(first) > 1 else None


def sanitize_text(text: str) -> str:
    """Nettoie un texte : garde accents, apostrophes, tirets, points.
    Supprime parenthèses, crochets, virgules, points-virgules, deux-points."""
    if not text:
        return text
    text = text.replace('\u2018', "'").replace('\u2019', "'")
    text = text.replace('\u2013', '-').replace('\u2014', '-')
    text = re.sub(r'[<>:"/\\|?*]', ' ', text)
    text = re.sub(r'[(){}\[\],;:]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text.strip(' -.')


def parse_filename(filename: str) -> tuple:
    """Analyse un nom de fichier → (titre, auteur)."""
    stem = Path(filename).stem
    title = None
    author = None

    # Décodage URL
    if '%' in stem:
        stem = unquote(stem)

    # Phase 1 : supprimer les artefacts
    stem = re.sub(r'^EBOOK\s+', '', stem, flags=re.IGNORECASE)
    stem = re.sub(r'^pdfcoffee\.com[_\-]', '', stem, flags=re.IGNORECASE)
    stem = re.sub(r'^Microsoft Word\s*-\s*', '', stem, flags=re.IGNORECASE)
    stem = re.sub(r'\s*-\s*pdfMachine.*$', '', stem, flags=re.IGNORECASE)
    stem = re.sub(r'\s*\(Bookos\.org\)\s*', '', stem, flags=re.IGNORECASE)
    stem = re.sub(r'\s*\(Z-Library\)\s*', '', stem, flags=re.IGNORECASE)
    stem = re.sub(r'\s*\([Uu]nknown\)\s*', '', stem)
    stem = re.sub(r'\.pdf$', '', stem, flags=re.IGNORECASE)
    stem = re.sub(r'\.(doc|docx|txt|rtf|dvi|indb|indd)\b', '', stem, flags=re.IGNORECASE)
    stem = re.sub(r'^\d{5,}\s*-\s*', '', stem)
    isbn_glued = re.match(r'^(\d{7,})([A-Za-z].{10,})$', stem)
    if isbn_glued:
        stem = isbn_glued.group(2)
    stem = re.sub(r'^\d{7,}\s+', '', stem)
    stem = re.sub(r'\s*www\.[a-z0-9]+\.[a-z]+\s*', ' ', stem, flags=re.IGNORECASE)
    stem = re.sub(r'\s*https?://\S+\s*', ' ', stem, flags=re.IGNORECASE)
    stem = re.sub(r'\s*[a-z0-9]+\.com[/_\-]\s*', ' ', stem, flags=re.IGNORECASE)
    stem = re.sub(r'-pdf-free$', '', stem, flags=re.IGNORECASE)
    stem = re.sub(r'-pdf$', '', stem, flags=re.IGNORECASE)
    stem = re.sub(r'^\d{3}\s+', '', stem)
    stem = re.sub(r'^\[?eBook\]?\s*', '', stem, flags=re.IGNORECASE)
    stem = re.sub(r'\s*\[eBook\]\s*', ' ', stem, flags=re.IGNORECASE)
    stem = re.sub(r'\s*www\.AvxHome\.\w+\s*', ' ', stem, flags=re.IGNORECASE)
    stem = stem.replace('_', ' ')
    stem = re.sub(r'\.(?=[A-Z0-9])', ' ', stem)
    stem = stem.replace('@', ' ')
    stem = stem.replace('&', ' et ')
    stem = re.sub(r'\.(compressed|optimized|reduced|lite)\b', '', stem, flags=re.IGNORECASE)

    # Phase 2 : extraire Titre / Auteur
    # Format Bookos : "[Auteur] Titre"
    bookos = re.match(r'^\[([^\]]+)\]\s*(.+)$', stem)
    if bookos:
        author = bookos.group(1).strip()
        title = bookos.group(2).strip()
    else:
        # Format Z-Library : "Titre (Auteur)"
        last_paren = re.search(r'^(.+)\s+\(([^()]*(?:\([^()]*\)[^()]*)*)\)\s*$', stem)
        if last_paren:
            ct, ca = last_paren.group(1).strip(), last_paren.group(2).strip()
            if len(ca) < 150 and re.search(r'[A-ZÀ-Ö]', ca):
                title, author = ct, ca
        # Format "by Auteur"
        if not title:
            by_m = re.match(r'^(.+?)\s+by\s+(.+)$', stem, re.IGNORECASE)
            if by_m:
                title, author = by_m.group(1).strip(), by_m.group(2).strip()
        # Format "X - Y"
        if not title:
            comma_dash = re.match(r'^([A-ZÀ-Ö][^,]+,\s*[^-]+?)\s+-\s+(.+)$', stem)
            if comma_dash:
                author, title = comma_dash.group(1).strip(), comma_dash.group(2).strip()
            else:
                dash = re.match(r'^(.+?)\s+-\s+(.+)$', stem)
                if dash:
                    title, author = dash.group(1).strip(), dash.group(2).strip()

    if not title:
        title = stem

    title = sanitize_text(title)
    if author:
        author = extract_first_author(author)
        if author:
            author = sanitize_text(author)

    return (title, author)


def build_final_name(title: str, author: str = None) -> str:
    """Construit 'Titre - Auteur.pdf' ou 'Titre.pdf'."""
    if not title or len(title.strip()) < 2:
        return None
    if author and len(author.strip()) > 1:
        result = f"{title} - {author}"
    else:
        result = title
    result = re.sub(r'\s+', ' ', result).strip().strip(' -.')
    if len(result) > MAX_FILENAME_LEN:
        result = result[:MAX_FILENAME_LEN - 3] + '...'
    return result + '.pdf' if result else None


# ===================================================================
#  PHASE 2 — RECHERCHE ISBN EN LIGNE
# ===================================================================
def _load_isbn_cache(cache_path: str) -> dict:
    if os.path.exists(cache_path):
        try:
            with open(cache_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_isbn_cache(cache: dict, cache_path: str):
    try:
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _extract_isbn(filename: str) -> str:
    """Extrait un ISBN (10 ou 13 chiffres) d'un nom de fichier."""
    stem = Path(filename).stem.replace('_', '')
    m = re.match(r'^(\d{10}|\d{13})', stem)
    return m.group(1) if m else None


def lookup_isbn(isbn: str, cache: dict) -> dict:
    """Cherche un ISBN en ligne (Google Books puis Open Library)."""
    import requests

    if isbn in cache:
        return cache[isbn] if cache[isbn].get('title') else None

    # Google Books
    try:
        resp = requests.get(
            f"https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn}",
            timeout=10)
        if resp.status_code == 200:
            items = resp.json().get('items', [])
            if items:
                vol = items[0].get('volumeInfo', {})
                title = vol.get('title', '')
                subtitle = vol.get('subtitle', '')
                if subtitle and len(title) < 60:
                    title = f"{title} - {subtitle}"
                authors = vol.get('authors', [])
                if title:
                    result = {'title': title, 'author': authors[0] if authors else None}
                    cache[isbn] = result
                    return result
    except Exception:
        pass

    time.sleep(0.2)

    # Open Library
    try:
        resp = requests.get(f"https://openlibrary.org/isbn/{isbn}.json", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            title = data.get('title', '')
            authors = []
            for ref in data.get('authors', []):
                key = ref.get('key', '')
                if key:
                    try:
                        aresp = requests.get(f"https://openlibrary.org{key}.json", timeout=5)
                        if aresp.status_code == 200:
                            n = aresp.json().get('name', '')
                            if n:
                                authors.append(n)
                    except Exception:
                        pass
            if title:
                result = {'title': title, 'author': authors[0] if authors else None}
                cache[isbn] = result
                return result
    except Exception:
        pass

    cache[isbn] = {'title': None, 'author': None}
    return None


# ===================================================================
#  PHASE 3 — EXTRACTION DEPUIS LE PDF
# ===================================================================
def _pdf_worker(pdf_path: str, result_dict):
    """Worker isolé pour extraction PDF (lancé dans un sous-processus)."""
    import re as _re

    # Métadonnées
    try:
        from pypdf import PdfReader
        reader = PdfReader(pdf_path)
        meta = reader.metadata
        if meta and meta.title:
            t = meta.title.strip()
            bad = (len(t) < 5
                   or _re.match(r'^\d+', t)
                   or t.lower().endswith('.pdf')
                   or t.lower().endswith('.indb')
                   or t.startswith('Microsoft')
                   or 'untitled' in t.lower()
                   or 'Team-Fly' in t
                   or '8=8AB@' in t)
            if not bad:
                result_dict['title'] = t
                if meta.author and len(meta.author.strip()) > 1:
                    a = meta.author.strip()
                    if not _re.match(r'^\d+$', a) and '8=8AB@' not in a:
                        result_dict['author'] = a
                return
    except Exception:
        pass

    # Texte des premières pages
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            if not pdf.pages:
                return
            text = ""
            for page in pdf.pages[:5]:
                try:
                    t = page.extract_text()
                    if t:
                        text += t + "\n"
                except Exception:
                    continue
                if len(text) > 5000:
                    break
            if not text.strip():
                return

            lines = [ln.strip() for ln in text.split('\n') if ln.strip()]
            skip = _re.compile(
                r'^(page\s*\d|copyright|©|isbn|table\s*(des|of)|all\s*rights|'
                r'printed|www\.|http|edition|preface|avant-propos|sommaire|'
                r'introduction|\d+$|team.fly|edited\s*by$|springer|'
                r'lecture\s*notes|[0-9a-f]{8,}|\.{3,}|^\d{7,})', _re.IGNORECASE)
            garble = _re.compile(r'[^\x20-\x7E\u00C0-\u024F\u0400-\u04FF\u0600-\u06FF\u4E00-\u9FFF]')

            for line in lines[:30]:
                if skip.search(line):
                    continue
                if len(line) < 8 or len(line) > 200:
                    continue
                if len(garble.findall(line)) / max(len(line), 1) > 0.2:
                    continue
                clean = _re.sub(r'\s+\d+\s*$', '', line).strip()
                if len(clean) >= 8:
                    result_dict['title'] = clean
                    return
    except Exception:
        pass


def _pdf_worker_queue(pdf_path, queue):
    """Worker qui envoie le résultat via une Queue au lieu d'un Manager."""
    result = {'title': None, 'author': None}
    try:
        _pdf_worker(pdf_path, result)
    except Exception:
        pass
    queue.put(result)


def extract_from_pdf(pdf_path: str) -> dict:
    """Extrait titre/auteur depuis un PDF (sous-processus isolé + timeout)."""
    from multiprocessing import Process, Queue

    info = {'title': None, 'author': None}

    try:
        fsize = os.path.getsize(pdf_path)
        if fsize > 200 * 1024 * 1024:
            return info
    except OSError:
        return info

    try:
        queue = Queue()
        proc = Process(target=_pdf_worker_queue, args=(pdf_path, queue))
        proc.start()
        proc.join(timeout=PDF_TIMEOUT)
        if proc.is_alive():
            proc.terminate()
            proc.join(2)
            if proc.is_alive():
                proc.kill()
            return info
        if proc.exitcode != 0:
            return info
        if not queue.empty():
            result = queue.get_nowait()
            info['title'] = result.get('title')
            info['author'] = result.get('author')
    except Exception:
        pass

    return info


# ===================================================================
#  PHASE 4 — EXTRACTION DEPUIS LE NOM DU DOSSIER PARENT
# ===================================================================
def extract_title_from_folder(pdf_path: str) -> str:
    """Essaie d'extraire le titre depuis le dossier parent.
    Ex: 'My Book Title (9781234567890, 2016)/' → 'My Book Title'"""
    folder = Path(pdf_path).parent.name
    m = re.match(r'^(.+?)\s*\(\d{10,13}(?:,\s*\d{4})?\)\s*$', folder)
    if m:
        title = m.group(1).strip()
        if len(title) >= 3:
            return title
    return None


def _is_good_title(name: str) -> bool:
    """Vérifie qu'un nom proposé est de bonne qualité (pas trop vague/court)."""
    if not name:
        return False
    stem = Path(name).stem.strip()
    stem_low = stem.lower()

    # Rejeté si c'est un titre générique connu
    if stem_low in GENERIC_TITLES:
        return False
    # Rejeté si un seul mot trop vague
    words = stem.split()
    if len(words) == 1 and stem_low.rstrip('®™') in SINGLE_WORD_BLACKLIST:
        return False
    # Rejeté si c'est trop court (< 5 chars) sans auteur
    if len(stem) < 5 and ' - ' not in stem:
        return False
    # Rejeté si ça commence par un ISBN
    if ISBN_IN_NAME_RE.match(stem):
        return False
    # Rejeté si encodage corrompu
    if GARBLED_RE.search(stem):
        return False
    # Rejeté si code de production
    if PRODUCTION_RE.search(name):
        return False
    # Rejeté si c'est tout en majuscules ET un seul mot (ex: ADAPTIVE, COMPUTATIONAL)
    if stem.isupper() and len(words) <= 2 and len(stem) < 25:
        return False
    # Rejeté si le titre ne contient pas de vrais mots
    from lib.wordcheck import contains_real_words
    if not contains_real_words(stem):
        return False
    return True


def _is_better_than(new_name: str, old_name: str) -> bool:
    """Le nouveau nom est-il meilleur que l'ancien ?"""
    if not new_name or new_name == old_name:
        return False
    new_stem = Path(new_name).stem.strip()
    old_stem = Path(old_name).stem.strip()

    # L'ancien est clairement un ID/hash → tout vrai titre est mieux
    old_decoded = unquote(old_stem) if '%' in old_stem else old_stem
    if (NUMERIC_ID_RE.match(old_name) or HEX_RE.match(old_name)
            or DOI_RE.match(old_decoded) or ARXIV_RE.match(old_name)
            or re.match(r'^[\d_]+$', old_stem)
            or re.match(r'^[0-9a-f]{20,}$', old_stem, re.IGNORECASE)):
        return True

    # Si le nouveau nom a un auteur et l'ancien non → mieux
    if ' - ' in new_stem and ' - ' not in old_stem:
        return True

    new_words = re.findall(r'[A-Za-zÀ-ÿ]{2,}', new_stem)
    old_words = re.findall(r'[A-Za-zÀ-ÿ]{2,}', old_stem)

    # L'ancien commence par un ISBN suivi de texte → le nouveau sans ISBN est mieux
    if ISBN_IN_NAME_RE.match(old_stem) and not ISBN_IN_NAME_RE.match(new_stem):
        if len(new_words) >= 2:
            return True

    # Si l'ancien a déjà un nom lisible avec 3+ mots et le nouveau en a moins → garder l'ancien
    if len(old_words) >= 3 and len(new_words) < len(old_words):
        return False
    # Si le nouveau est juste un seul mot et l'ancien en contenait déjà un → pas mieux
    if len(new_words) <= 1 and len(old_words) >= 1:
        return False
    # Si le nouveau a plus de mots lisibles → mieux
    if len(new_words) > len(old_words):
        return True
    # Si pareil → mieux seulement si plus d'espaces (plus lisible)
    if new_stem.count(' ') > old_stem.count(' '):
        return True

    return False


# ===================================================================
#  PIPELINE PRINCIPAL : déterminer le nouveau nom
# ===================================================================
def compute_new_name(pdf_path: str, filename: str,
                     isbn_cache: dict = None,
                     enable_online: bool = True,
                     enable_pdf: bool = True,
                     llm_callback=None,
                     force: bool = False) -> tuple:
    """
    Retourne (nouveau_nom, action, source).
    action: NORMALISER | EXTRAIRE_ISBN | EXTRAIRE_PDF | EXTRAIRE_LLM | EXTRAIRE_DOSSIER | INCHANGE | ECHEC

    Args:
        llm_callback: Optionnel. Fonction(pdf_path) → dict avec 'title', 'author'.
                      Appelée en fallback quand ISBN et métadonnées PDF échouent.
        force: Si True et llm_callback fourni, le LLM est appelé en priorité
               (avant ISBN et PDF metadata) pour forcer une analyse fraîche.
    """
    # Étape 0 (force) : LLM Vision en priorité si --force + --llm
    if force and llm_callback and os.path.exists(pdf_path):
        try:
            vision = llm_callback(pdf_path)
            if vision and not vision.get('error') and vision.get('title'):
                t = sanitize_text(vision['title'])
                a = None
                if vision.get('author'):
                    a = extract_first_author(vision['author'])
                    if a:
                        a = sanitize_text(a)
                name = build_final_name(t, a)
                if name and _is_good_title(name):
                    return (name, 'EXTRAIRE_LLM', 'llm_vision')
                else:
                    print("    ⚠ LLM a retourné '{}' mais rejeté par filtre qualité".format(name))
            elif vision and vision.get('error'):
                print("    ⚠ LLM erreur : {}".format(vision.get('error')))
            elif vision and not vision.get('title'):
                print("    ⚠ LLM n'a pas retourné de titre (clés: {})".format(list(vision.keys())))
            else:
                print("    ⚠ LLM a retourné None/vide")
        except Exception as e:
            print("    ⚠ LLM exception : {}".format(e))

    # Étape 1 : nettoyage du nom de fichier
    title, author = parse_filename(filename)
    new_name = build_final_name(title, author)

    if new_name and new_name != filename and _is_good_title(new_name) and _is_better_than(new_name, filename):
        return (new_name, 'NORMALISER', 'filename')

    # Étape 2 : recherche ISBN en ligne
    isbn = _extract_isbn(filename)
    if isbn and enable_online and isbn_cache is not None:
        result = lookup_isbn(isbn, isbn_cache)
        if result and result.get('title'):
            t = sanitize_text(result['title'])
            a = None
            if result.get('author'):
                a = extract_first_author(result['author'])
                if a:
                    a = sanitize_text(a)
            name = build_final_name(t, a)
            if name and _is_good_title(name) and _is_better_than(name, filename):
                return (name, 'EXTRAIRE_ISBN', 'isbn_lookup')
        time.sleep(0.15)

    # Étape 3 : extraction depuis le PDF
    if enable_pdf and os.path.exists(pdf_path):
        info = extract_from_pdf(pdf_path)
        if info.get('title'):
            t = sanitize_text(info['title'])
            a = None
            if info.get('author'):
                a = extract_first_author(info['author'])
                if a:
                    a = sanitize_text(a)
            name = build_final_name(t, a)
            if name and _is_good_title(name) and _is_better_than(name, filename):
                return (name, 'EXTRAIRE_PDF', 'pdf_extraction')

    # Étape 4 : LLM Vision (analyse de couverture, optionnel)
    if llm_callback and os.path.exists(pdf_path):
        try:
            vision = llm_callback(pdf_path)
            if vision and not vision.get('error') and vision.get('title'):
                t = sanitize_text(vision['title'])
                a = None
                if vision.get('author'):
                    a = extract_first_author(vision['author'])
                    if a:
                        a = sanitize_text(a)
                name = build_final_name(t, a)
                if name and _is_good_title(name) and _is_better_than(name, filename):
                    return (name, 'EXTRAIRE_LLM', 'llm_vision')
        except Exception:
            pass  # Erreur LLM → on continue avec les méthodes suivantes

    # Étape 5 : titre depuis le dossier parent
    folder_title = extract_title_from_folder(pdf_path)
    if folder_title:
        name = build_final_name(sanitize_text(folder_title))
        if name and _is_good_title(name) and _is_better_than(name, filename):
            return (name, 'EXTRAIRE_DOSSIER', 'folder_name')

    # Étape 6 : le nettoyage partiel est-il au moins une amélioration ?
    if new_name and new_name != filename and _is_better_than(new_name, filename):
        return (new_name, 'NORMALISER', 'filename_partial')

    return (filename, 'ECHEC', 'aucune_info')


# ===================================================================
#  GESTION DES DOUBLONS
# ===================================================================
def resolve_duplicate(new_path: Path, seen: set) -> Path:
    if str(new_path).lower() not in seen and not new_path.exists():
        return new_path
    stem, suffix, parent = new_path.stem, new_path.suffix, new_path.parent
    counter = 2
    while True:
        candidate = parent / f"{stem} ({counter}){suffix}"
        if str(candidate).lower() not in seen and not candidate.exists():
            return candidate
        counter += 1


# ===================================================================
#  COMMANDES PRINCIPALES
# ===================================================================
def scan(root_path: str, enable_online: bool = True, enable_pdf: bool = True,
         llm_callback=None, max_files: int = 0, force: bool = False,
         verbose: bool = False, cache_dir: str = '',
         name_patterns: list[str] | None = None):
    """Scanne la bibliothèque et génère un rapport CSV.

    Args:
        llm_callback: Optionnel. Fonction(pdf_path) → dict avec 'title', 'author'.
                      Utilisée en fallback quand les méthodes offline échouent.
        max_files: Limiter à N fichiers (0 = pas de limite).
        force: Si True, ignore is_name_clean() et re-analyse tous les fichiers.
        verbose: Si True, affiche les détails de chaque étape de détection.
    """
    root = Path(root_path)
    if not root.exists():
        print(f"❌ '{root_path}' n'existe pas.")
        sys.exit(1)

    pdf_files = sorted(set(list(root.rglob('*.pdf')) + list(root.rglob('*.PDF'))))
    total_found = len(pdf_files)
    if max_files > 0:
        pdf_files = pdf_files[:max_files]
    total = len(pdf_files)
    print(f"\n📚 Bibliothèque : {root_path}")
    print(f"📄 PDFs trouvés : {total_found}" + (f" (limité à {total})" if max_files > 0 else "") + "\n")

    # Charger le cache ISBN (dans le profil si fourni, sinon logs/)
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        cache_path = os.path.join(cache_dir, ISBN_CACHE_FILE)
    else:
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        logs_dir = os.path.join(project_root, 'logs')
        os.makedirs(logs_dir, exist_ok=True)
        cache_path = os.path.join(logs_dir, ISBN_CACHE_FILE)
    isbn_cache = _load_isbn_cache(cache_path) if enable_online else {}

    # Checkpoint pour le mode --force (appels LLM coûteux)
    use_checkpoint = force and cache_dir
    cm = None
    progress = {}
    if use_checkpoint:
        cm = CheckpointManager(cache_dir, 'progress_rename.json')
        progress = cm.load()
        resumed = sum(1 for p in pdf_files if str(p) in progress)
        if resumed > 0:
            print(f"🔄 Reprise : {resumed}/{total} déjà traités → {total - resumed} restants")

    results = []
    seen = set()
    stats = {'INCHANGE': 0, 'NORMALISER': 0, 'EXTRAIRE_ISBN': 0,
             'EXTRAIRE_PDF': 0, 'EXTRAIRE_LLM': 0, 'EXTRAIRE_DOSSIER': 0, 'ECHEC': 0}
    save_interval = 50

    for i, pdf_path in enumerate(pdf_files, 1):
        fn = pdf_path.name
        rel = pdf_path.parent.relative_to(root)
        pct = int(i / total * 100)
        print(f"\r  [{pct:3d}%] ({i}/{total}) {fn[:55]:<55}", end='', flush=True)

        # Reprise depuis checkpoint
        pdf_key = str(pdf_path)
        if use_checkpoint and pdf_key in progress:
            cached = progress[pdf_key]
            action = cached.get('action', 'INCHANGE')
            new_fn = cached.get('nouveau_nom', fn)
            stats[action] = stats.get(action, 0) + 1
            if action not in ('INCHANGE',):
                results.append({
                    'dossier': str(rel), 'ancien_nom': fn, 'nouveau_nom': new_fn,
                    'action': action, 'source': cached.get('source', ''),
                    'chemin': pdf_key,
                })
            seen.add(str(pdf_path.parent / new_fn).lower())
            continue

        # Déjà propre ?
        if not force and is_name_clean(fn, name_patterns=name_patterns):
            stats['INCHANGE'] += 1
            seen.add(str(pdf_path).lower())
            if verbose:
                print(f"\n    ⏭ {fn} → déjà propre")
            continue

        # Calculer le nouveau nom
        try:
            new_fn, action, source = compute_new_name(
                str(pdf_path), fn, isbn_cache, enable_online, enable_pdf,
                llm_callback=llm_callback, force=force)
        except KeyboardInterrupt:
            print(f"\n\n⚠️  Interrompu à {pct}%.")
            if use_checkpoint:
                cm.save(progress)
            break
        except Exception as e:
            new_fn, action, source = fn, 'ECHEC', str(e)[:60]

        # Log verbose du résultat
        if verbose:
            if action == 'ECHEC':
                print(f"\n    ❌ {fn}")
                print(f"       → ECHEC ({source})")
            elif action == 'INCHANGE':
                print(f"\n    ⏭ {fn} → inchangé")
            else:
                print(f"\n    ✅ {fn}")
                print(f"       → {new_fn}")
                print(f"       via {action} ({source})")

        # Gérer les doublons
        new_full = pdf_path.parent / new_fn
        if new_fn != fn:
            new_full = resolve_duplicate(new_full, seen)
            new_fn = new_full.name
        seen.add(str(new_full).lower())

        stats[action] = stats.get(action, 0) + 1
        results.append({
            'dossier': str(rel), 'ancien_nom': fn, 'nouveau_nom': new_fn,
            'action': action, 'source': source, 'chemin': str(pdf_path),
        })

        # Sauvegarder dans le checkpoint
        if use_checkpoint:
            progress[pdf_key] = {
                'nouveau_nom': new_fn, 'action': action, 'source': source,
            }
            if i % save_interval == 0:
                cm.save(progress)

        if enable_online and i % save_interval == 0:
            _save_isbn_cache(isbn_cache, cache_path)

    if use_checkpoint and cm:
        cm.save(progress)

    if enable_online:
        _save_isbn_cache(isbn_cache, cache_path)

    print(f"\r  [100%] Terminé !{' ' * 70}")

    # Écrire le rapport
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    logs_dir = os.path.join(project_root, 'logs')
    os.makedirs(logs_dir, exist_ok=True)
    report_path = os.path.join(logs_dir, f"rapport_rename_{ts}.csv")

    with open(report_path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=[
            'dossier', 'ancien_nom', 'nouveau_nom', 'action', 'source', 'chemin'])
        w.writeheader()
        w.writerows(results)

    to_change = [r for r in results if r['action'] != 'INCHANGE' and r['action'] != 'ECHEC']

    print(f"\n{'='*60}")
    print("📊 RÉSUMÉ")
    print(f"{'='*60}")
    print(f"  Total PDFs         : {total}")
    print(f"  Déjà propres       : {stats.get('INCHANGE', 0)}")
    print(f"  À renommer         : {len(to_change)}")
    print(f"    - Nom nettoyé    : {stats.get('NORMALISER', 0)}")
    print(f"    - Via ISBN       : {stats.get('EXTRAIRE_ISBN', 0)}")
    print(f"    - Via PDF        : {stats.get('EXTRAIRE_PDF', 0)}")
    print(f"    - Via LLM Vision : {stats.get('EXTRAIRE_LLM', 0)}")
    print(f"    - Via dossier    : {stats.get('EXTRAIRE_DOSSIER', 0)}")
    print(f"  Échecs             : {stats.get('ECHEC', 0)}")
    print(f"\n📝 Rapport : {report_path}")
    if to_change:
        print(f"🚀 Appliquer : ./klodo.sh rename {root_path} --execute")

    return report_path


def execute(root_path: str, report_path: str = None):
    """Applique les renommages depuis le rapport."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    logs_dir = os.path.join(project_root, 'logs')

    if not report_path:
        reports = sorted(Path(logs_dir).glob('rapport_rename_*.csv'), reverse=True)
        if not reports:
            print("❌ Aucun rapport trouvé. Lancez d'abord sans --execute.")
            sys.exit(1)
        report_path = str(reports[0])

    print(f"📄 Rapport : {report_path}")

    with open(report_path, 'r', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))

    to_rename = [r for r in rows if r['action'] not in ('INCHANGE', 'ECHEC')]
    print(f"🔄 {len(to_rename)} fichiers à renommer.\n")

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_path = os.path.join(logs_dir, f"log_renommage_{ts}.csv")
    success = errors = skipped = 0

    with open(log_path, 'w', newline='', encoding='utf-8') as lf:
        lw = csv.writer(lf)
        lw.writerow(['ancien_chemin', 'nouveau_chemin', 'status'])
        for r in to_rename:
            old = Path(r['chemin'])
            new = old.parent / r['nouveau_nom']
            if not old.exists():
                lw.writerow([str(old), str(new), 'NOT_FOUND'])
                skipped += 1
                continue
            if new.exists() and old != new:
                stem, suffix = new.stem, new.suffix
                counter = 2
                while new.exists():
                    new = new.parent / f"{stem} ({counter}){suffix}"
                    counter += 1
            try:
                old.rename(new)
                lw.writerow([str(old), str(new), 'OK'])
                success += 1
            except Exception as e:
                lw.writerow([str(old), str(new), f'ERROR: {e}'])
                errors += 1

    print(f"\n{'='*60}")
    print(f"  ✅ Renommés : {success}")
    print(f"  ⏭️  Ignorés  : {skipped}")
    print(f"  ❌ Erreurs  : {errors}")
    print(f"\n  📝 Log     : {log_path}")
    print(f"  ↩️  Annuler  : python3 klodo_renamer.py --undo {log_path}")


def undo(log_path: str):
    """Annule les renommages depuis un fichier log."""
    if not os.path.exists(log_path):
        print(f"❌ '{log_path}' introuvable.")
        sys.exit(1)

    with open(log_path, 'r', encoding='utf-8') as f:
        rows = [(r[0], r[1], r[2]) for r in csv.reader(f) if len(r) >= 3 and r[2] == 'OK']

    print(f"\n↩️  Annulation de {len(rows)} renommages...\n")
    ok = err = 0
    for old, new, _ in rows:
        np, op = Path(new), Path(old)
        if np.exists():
            try:
                np.rename(op)
                ok += 1
            except Exception as e:
                print(f"  ❌ {e}")
                err += 1
        else:
            print(f"  ⚠️  Non trouvé : {np.name}")
            err += 1

    print(f"\n  Restaurés : {ok} | Erreurs : {err}")


# ===================================================================
#  MAIN
# ===================================================================
def main():
    parser = argparse.ArgumentParser(
        description="Klodo Renamer — Renommage intelligent de bibliothèques PDF",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples :
  %(prog)s /Volumes/ExtSSD/BIBLIO                 # Rapport seul
  %(prog)s /Volumes/ExtSSD/BIBLIO --execute        # Appliquer
  %(prog)s /Volumes/ExtSSD/BIBLIO --no-online      # Sans recherche ISBN
  %(prog)s --undo log_renommage_20260328.csv       # Annuler
        """)
    parser.add_argument('path', nargs='?', help='Chemin de la bibliothèque')
    parser.add_argument('--execute', action='store_true', help='Appliquer les renommages')
    parser.add_argument('--undo', default=None, help='Annuler depuis un fichier log')
    parser.add_argument('--no-online', action='store_true', help='Désactiver la recherche ISBN')
    parser.add_argument('--no-pdf', action='store_true', help='Désactiver l\'extraction PDF')
    parser.add_argument('--report', default=None, help='Fichier rapport à utiliser pour --execute')
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    args = parser.parse_args()

    if args.undo:
        undo(args.undo)
        return

    if not args.path:
        parser.print_help()
        sys.exit(1)

    if args.execute:
        execute(args.path, args.report)
    else:
        scan(args.path, enable_online=not args.no_online, enable_pdf=not args.no_pdf)


if __name__ == '__main__':
    import multiprocessing
    # macOS : utiliser 'spawn' au lieu de 'fork' pour éviter les zombies
    try:
        multiprocessing.set_start_method('spawn')
    except RuntimeError:
        pass  # Déjà défini
    try:
        main()
    finally:
        # Forcer la sortie propre — tuer tout processus enfant résiduel
        for child in multiprocessing.active_children():
            child.terminate()
            child.join(1)
            if child.is_alive():
                child.kill()
        os._exit(0)
