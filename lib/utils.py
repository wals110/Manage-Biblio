#!/usr/bin/env python3
"""
Shared utility functions for Klodo project.

Contains:
  - sanitize_filename() — safe filename cleaning
  - build_new_filename() — format "Titre - Auteur.pdf"
  - title_case_smart() — intelligent capitalization
  - is_name_already_clean() — detect clean vs. junk filenames
  - collect_pdf_files() — recursively walk directory for PDFs
  - save_report() — write CSV report with timestamp
  - print_summary() — formatted results summary

Python 3.13 compatible with modern type hints.
"""

import os
import csv
import re
from datetime import datetime
from collections import Counter

from lib.logger import get_logger

log = get_logger()


# ════════════════════════════════════════════════════════════════════════════
# FILENAME SANITIZATION
# ════════════════════════════════════════════════════════════════════════════

def sanitize_filename(text: str) -> str:
    """
    Nettoie un texte pour en faire un nom de fichier sûr.

    Supprime :
      - Caractères interdits : < > : " / \\ | ? *
      - Guillemets courbes (U+201C, U+201D, U+2018, U+2019)
      - Points excessifs (..)
      - Espaces répétés
      - Espaces de début/fin
      - Points de début/fin

    Limite à 200 caractères max.
    """
    # Supprimer caractères interdits et contrôles
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', text)

    # Supprimer guillemets courbes
    text = text.replace('\u201c', '')  # "
    text = text.replace('\u201d', '')  # "
    text = text.replace('\u2018', '')  # '
    text = text.replace('\u2019', "'")  # ' → '

    # Remplacer .. par .
    text = re.sub(r'\.{2,}', '.', text)

    # Normaliser espaces
    text = re.sub(r'\s+', ' ', text).strip()

    # Supprimer . et espace en fin
    text = text.strip('. ')

    # Limiter à 200 caractères
    if len(text) > 200:
        text = text[:200].rsplit(' ', 1)[0]

    return text


def title_case_smart(text: str) -> str:
    """
    Capitalisation intelligente pour les titres.

    Règles :
      - Premier mot toujours en majuscule
      - Mots courts (a, an, the, and, de, du, le, etc.) en minuscules
        sauf en début de phrase
      - Acronymes connus préservés en MAJUSCULES
        (PDF, OCR, API, SQL, HTML, CSS, XML, JSON, HTTP, GPU, CPU, AI, ML, NLP, CNN, etc.)
      - Chiffres romains préservés (II, III, IV, etc.)
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


def build_new_filename(title: str, author: str) -> str | None:
    """
    Construit un nouveau nom de fichier au format "Titre - Auteur.pdf".

    Applique sanitize_filename et title_case_smart.
    Retourne None si le titre est trop court (< 3 caractères).

    Exemples :
      ("machine learning", "Tom Mitchell") -> "Machine Learning - Tom Mitchell.pdf"
      ("xyz", "") -> "Xyz.pdf"
      ("ab", "") -> None  (titre trop court)
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


# ════════════════════════════════════════════════════════════════════════════
# FILENAME ANALYSIS
# ════════════════════════════════════════════════════════════════════════════

def is_name_already_clean(filename: str) -> bool:
    """
    Vérifie si le nom de fichier est déjà propre (format "Titre - Auteur.pdf")
    ou s'il contient des artefacts typiques nécessitant un renommage.

    Détecte les patterns de junk :
      - IDs numériques (01_182383_ffirs)
      - cid_9
      - Fichiers QuarkXPress (.qxp, .qxd)
      - Fichiers InDesign (.indd)
      - Front/back matter (ffirs, fmatter, flast, ftoc)
      - ISBN comme nom (10+ chiffres)
      - Hashes MD5 (32 caractères hex)
      - Numérotation de pages ("Page iii")

    Retourne True si le nom semble propre, False s'il contient du junk.
    """
    name = os.path.splitext(filename)[0]

    junk_patterns = [
        r'^\d{2}[\s_]\d{4,}',        # 01_182383_ffirs
        r'^cid[\s_]?\d+',             # cid_9
        r'\.qxp',                      # QuarkXPress
        r'\.qxd',
        r'\.indd',                     # InDesign
        r'ffirs|fmatter|flast|ftoc',  # front/back matter
        r'^\d{10,}',                   # ISBN comme nom
        r'^[a-f0-9]{32}',             # hash MD5
        r'Page\s*\w+\s*$',            # "Page iii"
    ]

    for pattern in junk_patterns:
        if re.search(pattern, name, re.IGNORECASE):
            return False

    # Vérifier qu'il y a au moins 5 caractères alphabétiques
    alpha_chars = re.sub(r'[^a-zA-Z\u00c0-\u017f]', '', name)
    if len(alpha_chars) < 5:
        return False

    return True


# ════════════════════════════════════════════════════════════════════════════
# FILE COLLECTION
# ════════════════════════════════════════════════════════════════════════════

def collect_pdf_files(dir_path: str, max_files: int = 0) -> list[str]:
    """
    Marche récursivement dans dir_path et collecte tous les fichiers .pdf.

    Returns :
      Liste triée (alphabétiquement) des chemins absolus vers les PDFs.
      Si max_files > 0, limité à max_files premiers fichiers.

    Exemple :
      pdfs = collect_pdf_files('/Volumes/ExtSSD/BIBLIO', max_files=100)
      # retourne ['...file1.pdf', '...file2.pdf', ...]
    """
    pdf_files = []

    for root, dirs, files in os.walk(dir_path):
        for f in sorted(files):
            if f.lower().endswith('.pdf'):
                pdf_files.append(os.path.join(root, f))

    if max_files > 0:
        pdf_files = pdf_files[:max_files]

    return pdf_files


# ════════════════════════════════════════════════════════════════════════════
# REPORTING
# ════════════════════════════════════════════════════════════════════════════

def save_report(results: list[dict], output_dir: str, prefix: str = 'rapport') -> str:
    """
    Sauvegarde un rapport CSV avec timestamp dans le nom du fichier.

    Fields du CSV :
      fichier, nouveau_nom, titre_detecte, auteur_detecte,
      theme_detecte, langue, confiance,
      destination, score, mot_cle, status

    Returns : chemin complet du fichier rapport créé.

    Exemple :
      path = save_report(results, '/logs', prefix='rapport_ocr')
      # crée : /logs/rapport_ocr_20260401_143022.csv
    """
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    report_path = os.path.join(output_dir, f'{prefix}_{timestamp}.csv')

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
                'confiance': "{:.2f}".format(float(r.get('confiance', 0) or 0)),
                'destination': r.get('destination', ''),
                'score': "{:.3f}".format(float(r.get('score', 0) or 0)),
                'mot_cle': r.get('mot_cle', ''),
                'status': r['status'],
            })

    return report_path


def print_summary(results: list[dict], cost_per_call: float = 0.00034):
    """
    Affiche un résumé formaté des résultats de traitement.

    Affiche :
      - Total traités
      - Breakdown par statut (classifiés, erreurs, etc.)
      - Coût estimé API
      - Confiance moyenne
      - Top 15 thèmes détectés
      - Top 10 destinations

    Exemple :
      print_summary(results)
      # affiche un rapport formaté
    """
    total = len(results)
    if total == 0:
        log.info("\n  Aucun fichier traité.")
        return

    classified = sum(1 for r in results if r['status'] == 'classifié')
    low_conf = sum(1 for r in results if r['status'] == 'confiance_basse')
    not_id = sum(1 for r in results if r['status'] == 'non_identifié')
    errors_extract = sum(1 for r in results if r['status'] == 'erreur_extraction')
    errors_api = sum(1 for r in results if r['status'] == 'erreur_api')
    unclassified = sum(1 for r in results if r['status'] == 'non_classifié')

    # Coût estimé (basé sur Qwen3-VL-8B : ~$0.34 / 1000 images)
    api_calls = total - errors_extract
    cost_estimate = api_calls * cost_per_call  # ~1420 input + 120 output tokens par image

    log.info(f"\n{'='*60}")
    log.info(f" RÉSUMÉ — Klodo Report")
    log.info(f"{'='*60}")
    log.info(f"  Total traités      : {total}")
    log.info(f"  ✅ Classifiés       : {classified} ({classified/total*100:.1f}%)")
    log.error(f"  ❌ Non classifiés   : {unclassified} ({unclassified/total*100:.1f}%)")
    log.warning(f"  ⚠  Confiance basse : {low_conf} ({low_conf/total*100:.1f}%)")
    log.info(f"  🔇 Non identifiés  : {not_id} ({not_id/total*100:.1f}%)")
    log.error(f"  💥 Erreurs extract. : {errors_extract} ({errors_extract/total*100:.1f}%)")
    log.error(f"  💥 Erreurs API      : {errors_api} ({errors_api/total*100:.1f}%)")
    log.info(f"  💰 Coût estimé      : ${cost_estimate:.2f}")

    # Bilan
    log.info(f"\n  📊 Bilan : {classified}/{total} fichiers classifiés")

    # Confiance moyenne
    confs = [r.get('confiance', 0) for r in results if r.get('confiance', 0) > 0]
    if confs:
        avg_conf = sum(confs) / len(confs)
        log.info(f"  📊 Confiance moy.   : {avg_conf:.2f}")

    # Top thèmes détectés
    themes = Counter(r.get('theme_detecte', '') for r in results
                     if r.get('theme_detecte'))
    if themes:
        log.info(f"\n  Top thèmes détectés :")
        for theme, count in themes.most_common(15):
            log.info(f"    {count:4d}  {theme}")

    # Top destinations
    dests = Counter(r['destination'] for r in results if r['destination'])
    if dests:
        log.info(f"\n  Top destinations :")
        for dest, count in dests.most_common(10):
            log.info(f"    {count:4d}  {dest}")
