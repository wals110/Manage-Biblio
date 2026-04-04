"""
Helpers partagés entre les sous-commandes Klodo.

Fonctions utilitaires pour la sécurité inbox, la confirmation,
la copie de fichiers et le traitement parallèle.
"""

import os
import sys
import time
import shutil
import signal
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, List, Tuple, Dict

from lib.logger import get_logger
from lib.vision import analyze_cover
from lib.checkpoint import CheckpointManager
from lib.classifier import classify_combined
from lib.llm_mapper import LLMMapper

log = get_logger()

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ════════════════════════════════════════════════════════════════════════════
# SÉCURITÉ & CONFIRMATION
# ════════════════════════════════════════════════════════════════════════════

def check_inbox_safety(source, target, fallback):
    # type: (str, str, str) -> None
    """Vérifie que l'inbox n'est pas le même dossier que target ou fallback.
    Quitte avec erreur si c'est le cas (risque de suppression de fichiers)."""
    real_source = os.path.realpath(source)
    real_target = os.path.realpath(target)
    fallback_path = os.path.join(target, fallback)
    real_fallback = os.path.realpath(fallback_path)

    if real_source == real_target:
        log.error("❌ SÉCURITÉ : inbox et target pointent vers le même dossier !")
        log.error("   inbox  = {}".format(source))
        log.error("   target = {}".format(target))
        sys.exit(1)

    if real_source == real_fallback:
        log.error("❌ SÉCURITÉ : inbox pointe vers le dossier fallback ({}) !".format(fallback))
        log.error("   inbox    = {}".format(source))
        log.error("   fallback = {}".format(fallback_path))
        sys.exit(1)


def confirm_execute(results, source, target, fallback='_A-TRIER'):
    # type: (List[Dict], str, str, str) -> bool
    """Affiche un récapitulatif et demande confirmation avant exécution."""
    classified = [r for r in results if r['status'] == 'classifié']
    not_classified = [r for r in results
                      if r['status'] != 'classifié' and r.get('chemin')]

    log.info("\n" + "=" * 60)
    log.info("  ⚠  CONFIRMATION AVANT EXÉCUTION")
    log.info("=" * 60)
    log.info("  Source        : {}".format(source))
    log.info("  Cible         : {}".format(target))
    log.info("  Classifiés    : {} fichiers à copier".format(len(classified)))
    log.info("  Non-classifiés: {} fichiers → {}".format(len(not_classified), fallback))
    log.info("  ⚡ Les fichiers source seront supprimés après copie vérifiée")
    log.info("=" * 60)

    try:
        answer = input("\n  Continuer ? (o/N) : ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        log.info("Annulé.")
        return False

    return answer in ('o', 'oui', 'y', 'yes')


# ════════════════════════════════════════════════════════════════════════════
# COPIE DE FICHIERS
# ════════════════════════════════════════════════════════════════════════════

def safe_remove_source(src, dest):
    # type: (str, str) -> bool
    """Supprime le fichier source si la destination existe et a la même taille.
    Ne supprime JAMAIS si source et destination sont le même fichier.
    Retourne True si la suppression a été effectuée."""
    try:
        if os.path.realpath(src) == os.path.realpath(dest):
            return False
        if not os.path.exists(src) or not os.path.exists(dest):
            return False
        if os.path.getsize(src) != os.path.getsize(dest):
            return False
        os.remove(src)
        return True
    except OSError:
        return False


def copy_files(file_list, target_base, dest_subdir, label):
    # type: (List[Dict], str, Optional[str], str) -> Tuple[int, int]
    """Copie une liste de fichiers vers target_base/dest_subdir.

    Pour les classifiés, dest_subdir vient de r['destination'].
    Pour les non-classifiés, dest_subdir est le fallback fixe.

    Args:
        file_list: Liste de résultats (dicts avec chemin, fichier, etc.)
        target_base: Racine de la bibliothèque cible
        dest_subdir: Sous-dossier fixe (fallback) ou None pour utiliser r['destination']
        label: Label pour les messages de log

    Returns:
        Tuple (done, cleaned) — fichiers copiés et fichiers source supprimés.
    """
    done = 0
    cleaned = 0
    skipped_src = 0
    skipped_exists = 0
    errors = 0

    for r in file_list:
        src = r['chemin']
        if not os.path.exists(src):
            skipped_src += 1
            continue

        final_name = r['fichier']

        dest_dir = os.path.join(target_base, dest_subdir or r['destination'])
        dest = os.path.join(dest_dir, final_name)

        # Anti-collision : renommer si un fichier différent existe déjà
        if os.path.exists(dest):
            if os.path.getsize(src) == os.path.getsize(dest):
                skipped_exists += 1
                if safe_remove_source(src, dest):
                    cleaned += 1
                continue
            base, ext = os.path.splitext(final_name)
            counter = 2
            while os.path.exists(dest):
                dest = os.path.join(dest_dir, "{} ({}){}".format(base, counter, ext))
                counter += 1

        os.makedirs(dest_dir, exist_ok=True)
        try:
            shutil.copy2(src, dest)
            if safe_remove_source(src, dest):
                cleaned += 1
            done += 1
        except Exception as e:
            errors += 1
            log.error("  ⚠ Erreur {}: {}".format(final_name[:40], e))

    parts = ["✅ {} {}".format(done, label)]
    if skipped_exists:
        parts.append("{} déjà présents".format(skipped_exists))
    if skipped_src:
        parts.append("{} source absente".format(skipped_src))
    if errors:
        parts.append("{} erreurs".format(errors))
    log.info("  {}".format(" | ".join(parts)))

    return done, cleaned


def execute_classify(results, target_base, fallback='_A-TRIER'):
    # type: (List[Dict], str, str) -> None
    """Copie les fichiers classifiés vers la cible, puis supprime la source.
    Les non-classifiés vont dans le dossier fallback."""
    total_cleaned = 0

    classified = [r for r in results if r['status'] == 'classifié']
    if classified:
        log.info("\n🚀 Copie de {} fichiers classifiés...".format(len(classified)))
        _, cleaned = copy_files(classified, target_base, None, "fichiers copiés")
        total_cleaned += cleaned
    else:
        log.info("\n⏭  Aucun fichier classifié à traiter.")

    not_classified = [r for r in results
                      if r['status'] != 'classifié' and r.get('chemin')]
    if not_classified:
        log.info("\n📁 Copie de {} non-classifiés vers {}...".format(
            len(not_classified), fallback))
        _, cleaned = copy_files(
            not_classified, target_base, fallback,
            "copiés vers {}".format(fallback))
        total_cleaned += cleaned

    if total_cleaned:
        log.info("\n🧹 {} fichiers supprimés de l'inbox".format(total_cleaned))


# ════════════════════════════════════════════════════════════════════════════
# PIPELINE DE CLASSIFICATION
# ════════════════════════════════════════════════════════════════════════════

def process_single_file(pdf_path, api_key, endpoint, model,
                        theme_mapping, classifier=None, llm_mapper=None,
                        verbose=False, min_confidence=0.5, n_pages=1):
    # type: (str, str, str, str, Dict[str, str], object, object, bool, float, int) -> Dict
    """
    Pipeline de classification pour un fichier :
    1. Extraction couverture → image
    2. Envoi au LLM Vision → titre, auteur, thème
    3. Classification thématique
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
        'destination': '',
        'score': 0.0,
        'mot_cle': '',
        'status': 'pending',
    }  # type: Dict

    vision = analyze_cover(pdf_path, api_key, endpoint, model,
                           verbose=verbose, n_pages=n_pages)

    if vision.get('error') == 'extraction':
        result['status'] = 'erreur_extraction'
        return result
    if vision.get('error') == 'api':
        result['status'] = 'erreur_api'
        return result

    if not vision.get('title'):
        result['status'] = 'non_identifié'
        result['confiance'] = vision.get('confidence', 0.0)
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

    if confidence >= min_confidence:
        dest, score, keyword = classify_combined(
            vision, filename, theme_mapping, classifier, llm_mapper,
            pdf_path=pdf_path)
        if dest:
            result['destination'] = dest
            result['score'] = score
            result['mot_cle'] = keyword
            result['status'] = 'classifié'
        else:
            result['status'] = 'non_classifié'
    elif confidence > 0:
        result['status'] = 'confiance_basse'
    else:
        result['status'] = 'non_identifié'

    return result


def load_classifiers(profile, api_key, verbose=False, vision=False):
    # type: (object, str, bool, bool) -> Tuple
    """Charge le classifieur mots-clés et le LLM Mapper."""
    from lib.classifier import load_keyword_classifier

    classifier = None
    cats_path = os.path.join(str(profile.profile_dir), 'categories.yaml')
    if os.path.exists(str(cats_path)):
        try:
            classifier = load_keyword_classifier(str(cats_path))
            if classifier:
                log.info("✅ Classifieur YAML chargé (mode hybride)")
        except Exception as e:
            log.warning("⚠ Classifieur non chargé: {}".format(e))

    mapper = None
    mapper_enabled = profile.defaults.get('llm_mapper', True)
    if mapper_enabled and api_key and profile.tree:
        mapper = LLMMapper(
            folders=profile.tree,
            api_key=api_key,
            endpoint=profile.llm_endpoint,
            model=profile.llm_model,
            min_confidence=profile.defaults.get('mapper_min_confidence', 0.6),
            verbose=verbose,
            vision=vision,
        )
        mode = "🧠 LLM Mapper activé (résolution thèmes inconnus"
        if vision:
            mode += " + 👁 escalade vision"
        mode += ")"
        log.info(mode)

    return classifier, mapper


def run_processing(to_process, process_fn, workers, delay, interrupted, print_lock):
    # type: (List[str], object, int, float, list, threading.Lock) -> None
    """Exécute le traitement en parallèle ou séquentiel."""
    if workers > 1:
        log.info("🚀 {} requêtes avec {} threads...\n".format(len(to_process), workers))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}  # type: Dict
            for pdf_path in to_process:
                if interrupted[0]:
                    break
                future = executor.submit(process_fn, pdf_path)
                futures[future] = pdf_path
            for future in as_completed(futures):
                if interrupted[0]:
                    break
                try:
                    future.result()
                except Exception as e:
                    with print_lock:
                        log.warning("  ⚠ Exception: {}".format(e))
    else:
        log.info("🚀 Traitement séquentiel de {} fichiers...\n".format(len(to_process)))
        for pdf_path in to_process:
            if interrupted[0]:
                break
            process_fn(pdf_path)
            if not interrupted[0] and delay > 0:
                time.sleep(delay)


def save_mapper_results(mapper, profile, logs_dir):
    # type: (object, object, str) -> None
    """Sauvegarde les résultats du LLM Mapper (auto-apprentissage + suggestions)."""
    if not mapper:
        return
    if mapper.learned:
        theme_mapping_path = os.path.join(
            str(profile.profile_dir), 'theme_mapping.yaml')
        mapper.save_learned(theme_mapping_path)
    if mapper.suggestions and logs_dir:
        mapper.save_suggestions(logs_dir)
    mapper.print_stats()


def make_llm_rename_callback(api_key, endpoint, model, verbose=False, n_pages=1):
    # type: (str, str, str, bool, int) -> object
    """Crée un callback LLM Vision pour le renommage."""
    def callback(pdf_path):
        # type: (str) -> Dict
        return analyze_cover(pdf_path, api_key, endpoint, model,
                             verbose=verbose, n_pages=n_pages)
    return callback
