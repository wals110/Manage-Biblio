"""
Sub-category refinement module for Klodo.

Recursively scans the library tree. At every non-leaf folder (a folder that
contains both PDF files and sub-folders), PDF files are considered misplaced
and are candidates for refinement.

Three-level matching per file:
  1. YAML rules (refinement.yaml) — explicit keyword → target sub-folder
  2. Implicit sub-folder name matching — folder names used as keywords
  3. LLM fallback (optional) — asks LLM to pick the best sub-folder

Files that match no level are reported as 'non_classé' (no move).

Provides:
  - load_refinement_rules(): Convert YAML rules to internal format
  - match_keywords(): Case-insensitive keyword matching in filenames
  - match_subdirs(): Implicit matching against sub-folder names
  - scan_and_refine(): Recursive scan + propose/apply refinements
  - make_refine_llm_callback(): Build LLM callback for refine
  - save_refine_report(): Export results as timestamped CSV
  - print_refine_summary(): Print human-readable summary
"""

import csv
import json
import os
import re
import shutil
import time
from datetime import datetime

from lib.constants import (
    LLM_MAX_RETRIES,
    LLM_MAX_TOKENS,
    LLM_TIMEOUT,
    MAPPER_MIN_CONFIDENCE,
    PDF_DPI,
)
from lib.logger import get_logger
from lib.utils import sanitize_for_prompt

log = get_logger()


def load_refinement_rules(rules_data):
    # type: (list[dict]) -> list[tuple[str, str, list[str]]]
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


def match_keywords(filename, keywords):
    # type: (str, list[str]) -> str | None
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


def _normalize_dirname(dirname):
    # type: (str) -> list[str]
    """
    Generate matching variants from a directory name.

    Transforms a directory name like 'Deep-Learning' into multiple
    lowercase variants for matching against filenames:
      - 'deep-learning' (original lowercase)
      - 'deep learning' (hyphens → spaces)
      - 'deeplearning' (hyphens removed)

    Strips leading numeric prefixes (e.g., '05-IA-ML' → 'ia-ml', 'ia ml').

    Args:
        dirname: Directory name (e.g., 'Deep-Learning', '05-IA-ML')

    Returns:
        List of lowercase variant strings for matching.

    Example:
        >>> _normalize_dirname('Deep-Learning')
        ['deep-learning', 'deep learning', 'deeplearning']
        >>> _normalize_dirname('05-IA-ML')
        ['ia-ml', 'ia ml', 'iaml']
    """
    # Strip leading numeric prefix (e.g., '05-' from '05-IA-ML')
    stripped = re.sub(r'^\d+-', '', dirname)
    lower = stripped.lower()

    variants = [lower]
    # Hyphens → spaces
    with_spaces = lower.replace('-', ' ')
    if with_spaces != lower:
        variants.append(with_spaces)
    # Hyphens removed
    no_hyphens = lower.replace('-', '')
    if no_hyphens != lower and no_hyphens != with_spaces:
        variants.append(no_hyphens)

    return variants


def match_subdirs(filename, subdirs):
    # type: (str, list[str]) -> str | None
    """
    Implicit matching: use sub-folder names as keywords.

    For each sub-folder, generates normalized variants (lowercase,
    hyphens replaced, etc.) and checks if any variant appears in
    the filename.

    Args:
        filename: PDF filename (e.g., "Deep Learning with Python.pdf")
        subdirs: List of sub-folder names at the current level
                 (e.g., ['Deep-Learning', 'NLP', 'Vision-par-Ordinateur'])

    Returns:
        The name of the matched sub-folder (original case), or None.

    Example:
        >>> match_subdirs("Deep Learning with Python.pdf",
        ...               ['Deep-Learning', 'NLP', 'Vision-par-Ordinateur'])
        'Deep-Learning'
        >>> match_subdirs("Algorithms.pdf",
        ...               ['Deep-Learning', 'NLP'])
    """
    filename_lower = filename.lower()

    for subdir in subdirs:
        # Skip hidden/system folders
        if subdir.startswith('.') or subdir.startswith('_'):
            continue

        variants = _normalize_dirname(subdir)
        for variant in variants:
            # Skip very short variants (≤2 chars) to avoid false positives
            if len(variant) <= 2:
                continue
            if variant in filename_lower:
                return subdir

    return None


# ═══════════════════════════════════════════════════════════════════
# LLM Fallback pour le raffinement
# ═══════════════════════════════════════════════════════════════════

REFINE_LLM_PROMPT = """Tu es un bibliothécaire expert. Un fichier PDF est dans un dossier trop générique et doit être déplacé vers un sous-dossier plus spécifique.

NOM DU FICHIER : "{filename}"
DOSSIER ACTUEL : {current_folder}

Voici les sous-dossiers disponibles à ce niveau :
{subdirs_list}

Ta tâche : choisir LE MEILLEUR sous-dossier pour ce fichier, en te basant sur le titre/sujet apparent dans le nom du fichier.

Réponds UNIQUEMENT avec un objet JSON (pas de texte avant/après) :
{{"folder": "Nom-Exact-Du-Sous-Dossier", "confidence": 0.85, "reason": "explication courte"}}

Règles :
- Le "folder" DOIT être un des sous-dossiers listés ci-dessus (copie exacte)
- Si aucun sous-dossier ne convient vraiment, mets "folder": "_AUCUN"
- "confidence" entre 0.0 et 1.0
- Sois précis et conservateur : en cas de doute, réponds "_AUCUN"
"""

REFINE_VISION_PROMPT = """Tu es un bibliothécaire expert. Voici la couverture d'un livre PDF qui est dans un dossier trop générique. Tu dois le classer dans le bon sous-dossier.

NOM DU FICHIER : "{filename}"
DOSSIER ACTUEL : {current_folder}

Voici les sous-dossiers disponibles à ce niveau :
{subdirs_list}

En te basant sur la couverture du livre (titre, auteur, sujet visible), choisis LE MEILLEUR sous-dossier.

Réponds UNIQUEMENT avec un objet JSON (pas de texte avant/après) :
{{"folder": "Nom-Exact-Du-Sous-Dossier", "confidence": 0.85, "reason": "explication courte"}}

Règles :
- Le "folder" DOIT être un des sous-dossiers listés ci-dessus (copie exacte)
- Si aucun sous-dossier ne convient vraiment, mets "folder": "_AUCUN"
- "confidence" entre 0.0 et 1.0
- Sois précis et conservateur : en cas de doute, réponds "_AUCUN"
"""


def make_refine_llm_callback(api_key, endpoint, model,
                              min_confidence=MAPPER_MIN_CONFIDENCE, verbose=False,
                              vision=False, client=None):
    # type: (str, str, str, float, bool, bool, 'LLMClient' | None) -> Callable
    """
    Build a LLM callback function for refine.

    Returns a callable with signature:
        callback(filename, current_folder, subdirs, pdf_path=None)
            -> Tuple[Optional[str], str]

    The callback sends the filename and list of available sub-folders
    to the LLM, which picks the best one.

    With vision=True, if the text-only LLM returns no match, it retries
    by sending the cover image of the PDF for better classification.

    Args:
        api_key: API key for the LLM endpoint
        endpoint: LLM API endpoint URL
        model: LLM model name
        min_confidence: Minimum confidence to accept (default 0.6)
        verbose: Print debug info
        vision: Enable vision escalation (default False)
        client: Instance LLMClient pré-configurée (optionnel)

    Returns:
        Callable that takes (filename, current_folder, subdirs, pdf_path)
        and returns (subfolder_name or None, source_tag).
        source_tag is 'llm' for text match, 'llm_vision' for vision match.
    """
    from lib.llm_client import LLMClient

    # Créer le client si non fourni
    if client is None:
        client = LLMClient(
            api_key=api_key, endpoint=endpoint, model=model,
            timeout=LLM_TIMEOUT, max_retries=LLM_MAX_RETRIES, verbose=verbose)

    stats = {
        'calls': 0, 'successes': 0, 'failures': 0,
        'vision_calls': 0, 'vision_successes': 0,
    }

    def _send_llm_request(messages):
        # type: (list[dict]) -> str | None
        """Send a request to the LLM via the unified client."""
        return client.call_messages(messages, max_tokens=LLM_MAX_TOKENS)

    def _validate_result(content, filename, subdirs):
        # type: (str | None, str, list[str]) -> str | None
        """Parse and validate LLM response. Returns validated subfolder or None."""
        if not content:
            return None

        result = _parse_llm_json(content)
        if not result:
            return None

        folder = result.get('folder', '')
        confidence = result.get('confidence', 0.0)
        reason = result.get('reason', '')

        # LLM says no match
        if folder in ('_AUCUN', '_A-TRIER'):
            if verbose:
                log.info("  🤖 LLM: aucun sous-dossier pour {}".format(filename))
            return None

        # Validate against actual subdirs (exact or case-insensitive)
        validated = None
        for s in subdirs:
            if s == folder:
                validated = s
                break
            if s.lower() == folder.lower():
                validated = s
                break

        if not validated:
            if verbose:
                log.warning("  ⚠ LLM a proposé '{}' mais ce sous-dossier n'existe pas".format(folder))
            return None

        if confidence < min_confidence:
            if verbose:
                log.info("  ⚠ LLM confiance trop basse ({:.2f}) pour {}".format(
                    confidence, filename))
            return None

        if verbose:
            log.info("  🤖 LLM: {} → {} (conf: {:.2f}, {})".format(
                filename[:50], validated, confidence, reason))
        return validated

    def _try_vision(filename, current_folder, subdirs, pdf_path):
        # type: (str, str, list[str], str) -> str | None
        """Escalade vision : envoyer la couverture du PDF au LLM."""
        try:
            from lib.vision import extract_cover_image, image_to_base64
        except ImportError:
            if verbose:
                log.warning("  ⚠ lib.vision non disponible, vision désactivée")
            return None

        images = extract_cover_image(pdf_path, dpi=PDF_DPI, n_pages=1)
        if not images:
            if verbose:
                log.info("  ⚠ Échec extraction couverture pour {}".format(filename))
            return None

        img_b64 = image_to_base64(images[0])

        subdirs_text = "\n".join("- {}".format(s) for s in subdirs)
        prompt_text = REFINE_VISION_PROMPT.format(
            filename=sanitize_for_prompt(filename),
            current_folder=current_folder or '(racine)',
            subdirs_list=subdirs_text,
        )

        messages = [{"role": "user", "content": [
            {
                "type": "image_url",
                "image_url": {
                    "url": "data:image/jpeg;base64,{}".format(img_b64),
                },
            },
            {
                "type": "text",
                "text": prompt_text,
            },
        ]}]

        stats['vision_calls'] += 1
        content = _send_llm_request(messages)
        result = _validate_result(content, filename, subdirs)
        if result:
            stats['vision_successes'] += 1
        return result

    def _call_refine_llm(filename, current_folder, subdirs, pdf_path=None):
        # type: (str, str, list[str], str | None) -> tuple[str | None, str]
        """
        Call LLM to pick the best sub-folder for a file.

        Returns:
            Tuple of (subfolder_name or None, source_tag).
            source_tag is 'llm' or 'llm_vision'.
        """
        if not subdirs:
            return (None, '')

        stats['calls'] += 1

        # ── Étape 1 : Texte seul ──
        subdirs_text = "\n".join("- {}".format(s) for s in subdirs)
        prompt = REFINE_LLM_PROMPT.format(
            filename=sanitize_for_prompt(filename),
            current_folder=current_folder or '(racine)',
            subdirs_list=subdirs_text,
        )
        messages = [{"role": "user", "content": prompt}]
        content = _send_llm_request(messages)
        text_result = _validate_result(content, filename, subdirs)

        if text_result:
            stats['successes'] += 1
            return (text_result, 'llm')

        # ── Étape 2 : Escalade vision (si activée) ──
        if vision and pdf_path and os.path.isfile(pdf_path):
            vision_result = _try_vision(
                filename, current_folder, subdirs, pdf_path)
            if vision_result:
                stats['successes'] += 1
                return (vision_result, 'llm_vision')

        stats['failures'] += 1
        return (None, '')

    # Attach stats dict for reporting
    _call_refine_llm.stats = stats
    return _call_refine_llm


def _parse_llm_json(content):
    # type: (str) -> dict | None
    """Parse JSON response from LLM, with tolerance for markdown blocks."""
    # Strip markdown ```json ... ```
    if '```' in content:
        lines = content.split('\n')
        json_lines = []
        in_block = False
        for line in lines:
            if line.strip().startswith('```'):
                in_block = not in_block
                continue
            if in_block or (not in_block and line.strip().startswith('{')):
                json_lines.append(line)
        content = '\n'.join(json_lines)

    start = content.find('{')
    end = content.rfind('}')
    if start == -1 or end == -1:
        return None

    try:
        return json.loads(content[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return None


def _apply_match(base_path, result, execute):
    # type: (str, dict, bool) -> dict
    """
    Apply a matched result: check for duplicates and optionally move.

    Takes a result dict with matched_target set, verifies the target
    doesn't already contain the file, and executes the move if requested.

    Args:
        base_path: Root library path
        result: Dict with keys: fichier, source, destination, dirpath,
                matched_target, mot_cle, source_match
        execute: If True, move files. If False, dry-run only.

    Returns:
        Final result dict (without internal keys dirpath, matched_target).
    """
    pdf = result['fichier']
    dirpath = result.pop('dirpath')
    matched_target = result.pop('matched_target')

    target_dir = os.path.join(dirpath, matched_target)
    target_full = os.path.join(target_dir, pdf)

    # Check if already present in target
    if os.path.exists(target_full):
        result['status'] = 'déjà_présent'
        return result

    # Execute or dry-run
    status = 'à_déplacer'
    if execute:
        try:
            os.makedirs(target_dir, exist_ok=True)
            shutil.move(os.path.join(dirpath, pdf), target_full)
            status = 'déplacé'
        except (OSError, shutil.Error) as e:
            log.error("  ❌ Erreur déplacement {} : {}".format(pdf, e))
            status = 'erreur'

    result['status'] = status
    return result


def scan_and_refine(
    base_path,        # type: str
    rules,            # type: list[tuple[str, str, list[str]]]
    execute=False,    # type: bool
    llm_callback=None,  # type: Callable | None
    workers=1,        # type: int
):
    # type: (...) -> list[dict]
    """
    Recursively scan library and propose/apply sub-category refinements.

    Two-pass approach:
      Pass 1 (instant): Walk tree, apply YAML rules + sub-folder name matching.
      Pass 2 (optional, parallel): For non_classé files, call LLM in parallel
      using ThreadPoolExecutor with `workers` threads.

    Matching levels per file:
      1. YAML rules (keyword matching against rules for this parent)
      2. Implicit sub-folder name matching (folder names as keywords)
      3. LLM fallback (optional) — if llm_callback is provided

    Each result dict contains:
      - 'fichier': filename (basename only)
      - 'source': source path (relative to base_path)
      - 'destination': target path (relative to base_path), or '' for non_classé
      - 'mot_cle': matched keyword (or '' for non_classé)
      - 'source_match': 'règle_yaml', 'nom_dossier', 'llm', or '' for non_classé
      - 'status': 'à_déplacer' (dry-run), 'déplacé' (executed),
                  'déjà_présent' (already in target), 'non_classé', 'erreur'

    Args:
        base_path: Root library path (e.g., '/Volumes/ExtSSD/BIBLIO')
        rules: List of (parent_rel_path, target_subdir, keywords) tuples
               from load_refinement_rules()
        execute: If True, move files. If False, dry-run only.
        llm_callback: Optional callable(filename, current_folder, subdirs) -> str|None.
                      Built by make_refine_llm_callback(). Called as étape 3
                      for files that don't match YAML rules or sub-folder names.
        workers: Number of parallel threads for LLM calls (default 1).

    Returns:
        List of result dicts.

    Note:
        - Creates target directories with os.makedirs(exist_ok=True)
        - Uses shutil.move() for actual moves
        - Skips files already present in target
        - Leaf folders (no sub-directories) are skipped entirely
    """
    results = []
    pending_llm = []  # type: list[dict]

    # Pre-index rules by parent for O(1) lookup
    rules_by_parent = {}  # type: dict[str, list[tuple[str, list[str]]]]
    for parent_rel, target_subdir, keywords in rules:
        parent_key = parent_rel.replace('\\', '/')
        if parent_key not in rules_by_parent:
            rules_by_parent[parent_key] = []
        rules_by_parent[parent_key].append((target_subdir, keywords))

    # ══════════════════════════════════════════════════════════════
    # PASSE 1 : Scan keyword (instantané)
    # ══════════════════════════════════════════════════════════════

    for dirpath, dirnames, filenames in os.walk(base_path):
        # Leaf folder → skip
        if not dirnames:
            continue

        rel_dir = os.path.relpath(dirpath, base_path)
        if rel_dir == '.':
            rel_dir = ''

        rel_dir_normalized = rel_dir.replace('\\', '/')

        pdf_files = [f for f in filenames if f.lower().endswith('.pdf')]
        if not pdf_files:
            continue

        applicable_rules = rules_by_parent.get(rel_dir_normalized, [])
        subdir_names = [d for d in dirnames
                        if not d.startswith('.') and not d.startswith('_')]

        for pdf in pdf_files:
            pdf_full = os.path.join(dirpath, pdf)
            if not os.path.isfile(pdf_full):
                continue

            matched_target = None
            matched_kw = None
            source_match = ''

            # ── Étape 1 : Règles YAML ──
            for target_subdir, keywords in applicable_rules:
                kw = match_keywords(pdf, keywords)
                if kw:
                    matched_target = target_subdir
                    matched_kw = kw
                    source_match = 'règle_yaml'
                    break

            # ── Étape 2 : Matching par nom de sous-dossier ──
            if not matched_target and subdir_names:
                subdir_match = match_subdirs(pdf, subdir_names)
                if subdir_match:
                    matched_target = subdir_match
                    variants = _normalize_dirname(subdir_match)
                    filename_lower = pdf.lower()
                    matched_kw = ''
                    for v in variants:
                        if len(v) > 2 and v in filename_lower:
                            matched_kw = v
                            break
                    source_match = 'nom_dossier'

            # Build source path
            source_rel = os.path.join(rel_dir, pdf) if rel_dir else pdf

            # ── Match found → apply ──
            if matched_target:
                dest_rel = os.path.join(rel_dir, matched_target, pdf) if rel_dir \
                    else os.path.join(matched_target, pdf)
                result = {
                    'fichier': pdf,
                    'source': source_rel,
                    'destination': dest_rel,
                    'mot_cle': matched_kw or '',
                    'source_match': source_match,
                    'dirpath': dirpath,
                    'matched_target': matched_target,
                }
                results.append(_apply_match(base_path, result, execute))
                continue

            # ── No keyword match → queue for LLM or non_classé ──
            if llm_callback and subdir_names:
                pending_llm.append({
                    'fichier': pdf,
                    'source': source_rel,
                    'dirpath': dirpath,
                    'rel_dir': rel_dir,
                    'rel_dir_normalized': rel_dir_normalized,
                    'subdir_names': subdir_names,
                    'pdf_path': os.path.join(dirpath, pdf),
                })
            else:
                results.append({
                    'fichier': pdf,
                    'source': source_rel,
                    'destination': '',
                    'mot_cle': '',
                    'source_match': '',
                    'status': 'non_classé',
                })

    # ══════════════════════════════════════════════════════════════
    # PASSE 2 : LLM fallback (parallèle si workers > 1)
    # ══════════════════════════════════════════════════════════════

    if pending_llm and llm_callback:
        effective_workers = max(1, workers)
        total_llm = len(pending_llm)

        if effective_workers > 1:
            log.info("\n🤖 Passe 2 : {} fichiers à traiter par LLM ({} workers)".format(
                total_llm, effective_workers))
        else:
            log.info("\n🤖 Passe 2 : {} fichiers à traiter par LLM".format(
                total_llm))

        # Thread-safe progress bar
        import sys as _sys
        import threading
        _progress_lock = threading.Lock()
        _progress_state = {
            'done': 0, 'classified': 0, 'non_classe': 0,
            'start_time': time.time(),
        }

        def _format_eta(elapsed, done, total):
            # type: (float, int, int) -> str
            """Format estimated time remaining."""
            if done == 0 or elapsed < 1:
                return '...'
            remaining = (elapsed / done) * (total - done)
            if remaining < 60:
                return '{:.0f}s'.format(remaining)
            elif remaining < 3600:
                return '{:.0f}min'.format(remaining / 60)
            else:
                return '{:.1f}h'.format(remaining / 3600)

        def _render_progress_bar(done, total, classified, non_classe, elapsed,
                                vision_count=0):
            # type: (int, int, int, int, float, int) -> str
            """Render a nice Unicode progress bar."""
            bar_width = 25
            if total > 0:
                ratio = done / total
                filled = int(bar_width * ratio)
            else:
                ratio = 0.0
                filled = 0
            bar = '█' * filled + '░' * (bar_width - filled)
            pct = ratio * 100

            eta = _format_eta(elapsed, done, total)
            speed = '{:.1f}/s'.format(done / elapsed) if elapsed > 0 else '-'

            parts = [
                '🤖 {bar} {done}/{total} ({pct:.1f}%)'.format(
                    bar=bar, done=done, total=total, pct=pct),
                '✓ {ok} classés'.format(ok=classified),
            ]
            if vision_count > 0:
                parts.append('👁 {} vision'.format(vision_count))
            parts.append('✗ {} non_classés'.format(non_classe))
            parts.append('⏱ {}'.format(eta))
            parts.append(speed)

            return ' | '.join(parts)

        def _log_progress(pdf, llm_result, source_tag=''):
            # type: (str, str | None, str) -> None
            """Update progress bar for each file processed by LLM."""
            with _progress_lock:
                _progress_state['done'] += 1
                if llm_result:
                    _progress_state['classified'] += 1
                else:
                    _progress_state['non_classe'] += 1
                if source_tag == 'llm_vision':
                    _progress_state['vision'] = _progress_state.get('vision', 0) + 1
                done = _progress_state['done']
                classified = _progress_state['classified']
                non_classe = _progress_state['non_classe']
                vision_count = _progress_state.get('vision', 0)
                elapsed = time.time() - _progress_state['start_time']

            line = _render_progress_bar(
                done, total_llm, classified, non_classe, elapsed,
                vision_count)

            # Overwrite current line in terminal
            try:
                _sys.stdout.write('\r' + line)
                _sys.stdout.flush()
                if done >= total_llm:
                    _sys.stdout.write('\n')
                    _sys.stdout.flush()
            except (IOError, OSError):
                pass

        def _process_one_llm(item):
            # type: (dict) -> dict
            """Process a single file through LLM callback."""
            pdf = item['fichier']
            source_rel = item['source']
            dirpath = item['dirpath']
            rel_dir = item['rel_dir']
            subdir_names = item['subdir_names']
            rel_dir_normalized = item['rel_dir_normalized']
            pdf_path = item.get('pdf_path', '')

            llm_result, source_tag = llm_callback(
                pdf, rel_dir_normalized, subdir_names, pdf_path=pdf_path)

            _log_progress(pdf, llm_result, source_tag)

            if llm_result:
                dest_rel = os.path.join(rel_dir, llm_result, pdf) if rel_dir \
                    else os.path.join(llm_result, pdf)
                result = {
                    'fichier': pdf,
                    'source': source_rel,
                    'destination': dest_rel,
                    'mot_cle': source_tag,
                    'source_match': source_tag,
                    'dirpath': dirpath,
                    'matched_target': llm_result,
                }
                return _apply_match(base_path, result, execute)
            else:
                return {
                    'fichier': pdf,
                    'source': source_rel,
                    'destination': '',
                    'mot_cle': '',
                    'source_match': '',
                    'status': 'non_classé',
                }

        if effective_workers > 1:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=effective_workers) as pool:
                futures = {pool.submit(_process_one_llm, item): item
                           for item in pending_llm}
                for future in as_completed(futures):
                    try:
                        results.append(future.result())
                    except Exception as e:
                        item = futures[future]
                        log.error("  ❌ LLM thread error for {}: {}".format(
                            item['fichier'], e))
                        results.append({
                            'fichier': item['fichier'],
                            'source': item['source'],
                            'destination': '',
                            'mot_cle': '',
                            'source_match': '',
                            'status': 'non_classé',
                        })
        else:
            # Sequential processing
            for item in pending_llm:
                results.append(_process_one_llm(item))

    return results


def save_refine_report(results, logs_dir):
    # type: (list[dict], str) -> str
    """
    Save refinement results as timestamped CSV report.

    Creates a CSV file in logs_dir with columns:
      fichier, source, destination, mot_cle, source_match, status

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
    csv_path = os.path.join(logs_dir, 'refine_{}.csv'.format(timestamp))

    # Write CSV
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(
            f,
            fieldnames=['fichier', 'source', 'destination', 'mot_cle',
                        'source_match', 'status'],
        )
        writer.writeheader()
        writer.writerows(results)

    return csv_path


def print_refine_summary(results):
    # type: (list[dict]) -> None
    """
    Print human-readable summary of refinement results.

    Prints:
      1. Count of results by status
      2. Count of results by destination (top 20, excludes non_classé)
      3. Count of non_classé by source folder (top 20)
      4. First 15 examples

    Args:
        results: List of result dicts from scan_and_refine()
    """
    if not results:
        log.info('No refinement results.')
        return

    # Count by status
    status_counts = {}  # type: dict[str, int]
    for result in results:
        status = result.get('status', 'unknown')
        status_counts[status] = status_counts.get(status, 0) + 1

    total = len(results)
    non_classe = status_counts.get('non_classé', 0)
    matched = total - non_classe

    log.info('\n=== RÉSUMÉ RAFFINEMENT ===\n')
    log.info('Fichiers scannés : {}'.format(total))
    log.info('  Matchés       : {} ({:.1f}%)'.format(
        matched, matched * 100.0 / total if total else 0))
    log.info('  Non classés   : {} ({:.1f}%)'.format(
        non_classe, non_classe * 100.0 / total if total else 0))

    log.info('\nPar statut :')
    for status, count in sorted(status_counts.items()):
        log.info('  {:15s} : {:6d}'.format(status, count))

    # Count by source_match type
    match_counts = {}  # type: dict[str, int]
    for result in results:
        sm = result.get('source_match', '')
        if sm:
            match_counts[sm] = match_counts.get(sm, 0) + 1
    if match_counts:
        log.info('\nPar source de match :')
        for sm, count in sorted(match_counts.items()):
            log.info('  {:15s} : {:6d}'.format(sm, count))

    # Count by destination (top 20, excludes non_classé)
    dest_counts = {}  # type: dict[str, int]
    for result in results:
        if result.get('status') == 'non_classé':
            continue
        dest = result.get('destination', 'unknown')
        dest_counts[dest] = dest_counts.get(dest, 0) + 1

    if dest_counts:
        log.info('\nTop 20 destinations ({} uniques) :'.format(len(dest_counts)))
        top_dests = sorted(dest_counts.items(), key=lambda x: x[1], reverse=True)[:20]
        for dest, count in top_dests:
            log.info('  {:6d}  {}'.format(count, dest))

    # Count non_classé by source folder (top 20)
    if non_classe > 0:
        nc_folder_counts = {}  # type: dict[str, int]
        for result in results:
            if result.get('status') != 'non_classé':
                continue
            source = result.get('source', '')
            folder = os.path.dirname(source)
            nc_folder_counts[folder] = nc_folder_counts.get(folder, 0) + 1

        log.info('\nTop 20 dossiers avec non classés ({} dossiers) :'.format(
            len(nc_folder_counts)))
        top_nc = sorted(nc_folder_counts.items(), key=lambda x: x[1], reverse=True)[:20]
        for folder, count in top_nc:
            log.info('  {:6d}  {}'.format(count, folder))

    # First 15 examples
    log.info('\nExemples (premiers 15 / {}) :'.format(len(results)))
    for i, result in enumerate(results[:15], 1):
        if result['status'] == 'non_classé':
            log.info('  {:2d}. {:50s} ⚠ non_classé  (dans {})'.format(
                i, result['fichier'][:50], os.path.dirname(result['source'])))
        else:
            log.info('  {:2d}. {:50s} ({:15s}) -> {} [{}]'.format(
                i, result['fichier'][:50], result.get('mot_cle', ''),
                result['status'], result.get('source_match', '')))
