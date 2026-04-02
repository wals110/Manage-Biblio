"""
Sub-category refinement module for Manage-Biblio.

Moves files from parent categories to correct sub-categories based on filename
keyword analysis. Rules are loaded from profile YAML configuration.

Provides:
  - load_refinement_rules(): Convert YAML rules to internal format
  - match_keywords(): Case-insensitive keyword matching in filenames
  - scan_and_refine(): Scan library and propose/apply refinements
  - save_refine_report(): Export results as timestamped CSV
  - print_refine_summary(): Print human-readable summary
"""

import csv
import os
import shutil
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from lib.logger import get_logger

log = get_logger()


def load_refinement_rules(rules_data: List[Dict]) -> List[Tuple[str, str, List[str]]]:
    """
    Convert YAML refinement rules to internal format.

    Transforms a list of rule dicts with keys (parent, target, keywords)
    into a list of (parent_rel_path, sub_dir, keywords) tuples.

    Args:
        rules_data: List of dicts, each with keys:
            - 'parent': parent category path (e.g., '01-SCIENCES/INFORMATIQUE')
            - 'target': target sub-category dir name (e.g., 'Machine-Learning')
            - 'keywords': list of keywords to match in filename

    Returns:
        List of (parent_rel_path: str, sub_dir: str, keywords: list) tuples
        sorted by specificity (longest parent path first).

    Example:
        >>> rules = [
        ...     {'parent': '02-INFORMATIQUE/05-IA-ML', 'target': 'Deep-Learning',
        ...      'keywords': ['neural', 'cnn', 'rnn']},
        ...     {'parent': '01-SCIENCES/PHYSIQUE', 'target': 'Optique',
        ...      'keywords': ['light', 'laser', 'optics']}
        ... ]
        >>> result = load_refinement_rules(rules)
        >>> len(result)
        2
    """
    rules = []
    for rule in rules_data:
        parent = rule.get('parent', '')
        target = rule.get('target', '')
        keywords = rule.get('keywords', [])

        if parent and target and keywords:
            rules.append((parent, target, keywords))

    # Sort by parent path length (longest first for specificity)
    rules.sort(key=lambda x: len(x[0]), reverse=True)
    return rules


def match_keywords(filename: str, keywords: List[str]) -> Optional[str]:
    """
    Case-insensitive keyword matching in filename.

    Searches for any keyword in the filename (case-insensitive). Matches
    are done as substring search (not word-boundary).

    Args:
        filename: PDF filename (e.g., "Neural Networks - Goodfellow.pdf")
        keywords: List of keywords to search for

    Returns:
        The first matched keyword (lowercase), or None if no match.

    Example:
        >>> match_keywords("Neural Networks - Goodfellow.pdf",
        ...                ["cnn", "neural", "rnn"])
        'neural'
        >>> match_keywords("Algorithms.pdf", ["neural", "quantum"])
    """
    filename_lower = filename.lower()

    for keyword in keywords:
        if keyword.lower() in filename_lower:
            return keyword.lower()

    return None


def scan_and_refine(
    base_path: str,
    rules: List[Tuple[str, str, List[str]]],
    execute: bool = False,
) -> List[Dict]:
    """
    Scan library and propose/apply sub-category refinements.

    For each rule:
      1. Find parent category folder (relative to base_path)
      2. Scan files directly in that folder (not in subfolders)
      3. Match filenames against rule keywords
      4. Propose move to target sub-category
      5. Execute or dry-run based on execute flag

    Each result dict contains:
      - 'fichier': filename (basename only)
      - 'source': source path (relative to base_path)
      - 'destination': target path (relative to base_path)
      - 'mot_cle': matched keyword
      - 'status': 'à_déplacer' (dry-run), 'déplacé' (executed),
                  'déjà_présent' (already in target), 'erreur' (error)

    Args:
        base_path: Root library path (e.g., '/Volumes/ExtSSD/BIBLIO_V2')
        rules: List of (parent_rel_path, target_subdir, keywords) tuples
               from load_refinement_rules()
        execute: If True, move files. If False, dry-run only.

    Returns:
        List of result dicts with keys: fichier, source, destination,
        mot_cle, status.

    Note:
        - Creates target directories with os.makedirs(exist_ok=True)
        - Uses shutil.move() for actual moves
        - Skips files already present in target
        - Skips if source folder doesn't exist
    """
    results = []

    for parent_rel, target_subdir, keywords in rules:
        parent_full = os.path.join(base_path, parent_rel)

        # Skip if parent folder doesn't exist
        if not os.path.isdir(parent_full):
            continue

        # List PDF files directly in parent folder (not in subfolders)
        try:
            items = os.listdir(parent_full)
        except OSError as e:
            continue

        for item in items:
            item_full = os.path.join(parent_full, item)

            # Only process regular files, not directories
            if not os.path.isfile(item_full):
                continue

            # Only process PDFs
            if not item.lower().endswith('.pdf'):
                continue

            # Try to match keywords in filename
            matched_kw = match_keywords(item, keywords)
            if not matched_kw:
                continue

            # Build target path
            target_dir = os.path.join(parent_full, target_subdir)
            target_full = os.path.join(target_dir, item)

            # Check if already present in target
            if os.path.exists(target_full):
                status = 'déjà_présent'
                results.append({
                    'fichier': item,
                    'source': os.path.join(parent_rel, item),
                    'destination': os.path.join(parent_rel, target_subdir, item),
                    'mot_cle': matched_kw,
                    'status': status,
                })
                continue

            # Execute or dry-run
            status = 'à_déplacer'
            if execute:
                try:
                    # Create target directory if needed
                    os.makedirs(target_dir, exist_ok=True)
                    # Move file
                    shutil.move(item_full, target_full)
                    status = 'déplacé'
                except (OSError, shutil.Error) as e:
                    status = 'erreur'

            results.append({
                'fichier': item,
                'source': os.path.join(parent_rel, item),
                'destination': os.path.join(parent_rel, target_subdir, item),
                'mot_cle': matched_kw,
                'status': status,
            })

    return results


def save_refine_report(results: List[Dict], logs_dir: str) -> str:
    """
    Save refinement results as timestamped CSV report.

    Creates a CSV file in logs_dir with columns:
      fichier, source, destination, mot_cle, status

    Filename format: refine_YYYYMMDD_HHMMSS.csv

    Args:
        results: List of result dicts from scan_and_refine()
        logs_dir: Directory to save CSV report

    Returns:
        Full path to the saved CSV file.

    Raises:
        OSError: If CSV cannot be written.
    """
    # Create logs dir if needed
    os.makedirs(logs_dir, exist_ok=True)

    # Generate timestamped filename
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    csv_path = os.path.join(logs_dir, f'refine_{timestamp}.csv')

    # Write CSV
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(
            f,
            fieldnames=['fichier', 'source', 'destination', 'mot_cle', 'status'],
        )
        writer.writeheader()
        writer.writerows(results)

    return csv_path


def print_refine_summary(results: List[Dict]) -> None:
    """
    Print human-readable summary of refinement results.

    Prints:
      1. Count of results by status
      2. Count of results by destination (top 20)
      3. First 15 examples

    Args:
        results: List of result dicts from scan_and_refine()
    """
    if not results:
        log.info('No refinement results.')
        return

    # Count by status
    status_counts = {}
    for result in results:
        status = result.get('status', 'unknown')
        status_counts[status] = status_counts.get(status, 0) + 1

    log.info('\n=== RÉSUMÉ RAFFINEMENT ===\n')
    log.info('Par statut :')
    for status, count in sorted(status_counts.items()):
        log.info(f'  {status:15s} : {count:6d}')

    # Count by destination (top 20)
    dest_counts = {}
    for result in results:
        dest = result.get('destination', 'unknown')
        dest_counts[dest] = dest_counts.get(dest, 0) + 1

    log.info(f'\nTop 20 destinations ({len(dest_counts)} uniques) :')
    top_dests = sorted(dest_counts.items(), key=lambda x: x[1], reverse=True)[:20]
    for dest, count in top_dests:
        log.info(f'  {count:6d}  {dest}')

    # First 15 examples
    log.info(f'\nExemples (premiers 15 / {len(results)}) :')
    for i, result in enumerate(results[:15], 1):
        log.info(f'  {i:2d}. {result["fichier"]:50s} ({result["mot_cle"]:15s}) -> {result["status"]}')
