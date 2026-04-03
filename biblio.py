#!/usr/bin/env python3
"""
Biblio — Outil unifié de gestion de bibliothèque PDF.
======================================================
Pipeline complet : renommage → identification LLM → classification → raffinement.

Sous-commandes :
    process   Pipeline complet (renommage + LLM + classement + raffinement)
    classify  LLM Vision + classement thématique
    rename    Renommage "Titre - Auteur.pdf" (ISBN / métadonnées)
    refine    Raffinement des sous-catégories par mots-clés
    profiles  Lister les profils disponibles
    init      Créer un nouveau profil

Options globales :
    --profile NAME    Profil à utiliser (défaut: default)
    --execute         Appliquer les modifications (sinon dry-run)
    --verbose         Mode détaillé
    --workers N       Threads parallèles (pour classify/process)
    --max N           Limiter à N fichiers

Exemples :
    ./biblio.sh process /chemin/vers/nouveaux_pdfs
    ./biblio.sh classify /chemin/vers/dossier --workers 10
    ./biblio.sh rename /chemin/vers/dossier
    ./biblio.sh refine
    ./biblio.sh profiles
    ./biblio.sh init mon-profil --target /Volumes/MonDisque/BIBLIO

Prérequis :
    brew install poppler
    pip3 install pdf2image Pillow pyyaml requests
"""

__version__ = "4.0.0"

import os
import sys
import time
import shutil
import signal
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional, List, Tuple, Dict

# ── Ajout du répertoire projet au path ──
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from lib.profile import Profile, list_profiles, init_profile
from lib.checkpoint import CheckpointManager
from lib.vision import analyze_cover
from lib.logger import setup_logger, get_logger
from lib.utils import (
    sanitize_filename, build_new_filename, is_name_already_clean,
    collect_pdf_files, save_report, print_summary,
)
from lib.classifier import (
    classify_by_theme, classify_combined, make_classify_fn,
    load_keyword_classifier,
)
from lib.refiner import (
    load_refinement_rules, scan_and_refine,
    save_refine_report, print_refine_summary,
)
from lib.llm_mapper import (
    LLMMapper, load_suggestions, apply_suggestions, save_suggestions_file,
)

log = get_logger()


# ════════════════════════════════════════════════════════════════════════════
# PIPELINE : CLASSIFY (LLM Vision + classement)
# ════════════════════════════════════════════════════════════════════════════

def process_single_file(pdf_path, api_key, endpoint, model,
                        theme_mapping, classifier=None, llm_mapper=None,
                        verbose=False, min_confidence=0.5):
    # type: (str, str, str, str, Dict[str, str], object, object, bool, float) -> Dict
    """
    Pipeline pour un fichier :
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
    }  # type: Dict

    # 1. Analyse de la couverture via LLM Vision
    vision = analyze_cover(pdf_path, api_key, endpoint, model, verbose=verbose)

    # Gestion des erreurs (analyze_cover retourne {'error': type} en cas d'échec)
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

    # 2. Proposition de renommage
    if title and confidence >= min_confidence and not is_name_already_clean(filename):
        new_name = build_new_filename(title, author)
        if new_name and new_name != filename:
            result['nouveau_nom'] = new_name
            result['renommage'] = True

    # 3. Classification
    if confidence >= min_confidence:
        dest, score, keyword = classify_combined(
            vision, filename, theme_mapping, classifier, llm_mapper)
        if dest:
            result['destination'] = dest
            result['score'] = score
            result['mot_cle'] = keyword
            result['status'] = 'classifié'
        else:
            result['status'] = 'renommé_seul' if result['renommage'] else 'non_classifié'
    elif confidence > 0:
        result['status'] = 'confiance_basse'
    else:
        result['status'] = 'non_identifié'

    return result


def _load_classifiers(profile, api_key, verbose=False):
    # type: (Profile, str, bool) -> Tuple[object, object]
    """Charge le classifieur mots-clés et le LLM Mapper.

    Returns:
        Tuple (classifier, mapper) — chacun peut être None.
    """
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
        )
        log.info("🧠 LLM Mapper activé (résolution thèmes inconnus)")

    return classifier, mapper


def _run_processing(to_process, process_fn, workers, delay, interrupted, print_lock):
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


def _save_mapper_results(mapper, profile, logs_dir):
    # type: (object, Profile, str) -> None
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


def scan_and_classify(source_dir, profile, api_key,
                      max_files=0, verbose=False, delay=0.2,
                      workers=1, logs_dir='', checkpoint_name='progress.json',
                      skip_confirm=False):
    # type: (str, Profile, str, int, bool, float, int, str, str, bool) -> List[Dict]
    """
    Scanne un répertoire et traite chaque PDF via LLM Vision.
    Supporte la reprise automatique et le traitement parallèle.
    """
    pdf_files = collect_pdf_files(source_dir, max_files)
    total_found = len(pdf_files)

    # Checkpoint
    cm = CheckpointManager(logs_dir, checkpoint_name)
    progress = cm.load()
    resumed = sum(1 for p in pdf_files if p in progress)

    if resumed > 0:
        log.info("\n🔄 Reprise : {}/{} déjà traités → {} restants".format(
            resumed, len(pdf_files), len(pdf_files) - resumed))

    to_process = [p for p in pdf_files if p not in progress]
    remaining = len(to_process)

    log.info("\n📚 {} fichiers à traiter (sur {} trouvés)".format(remaining, total_found))
    log.info("🤖 Modèle  : {}".format(profile.llm_model))
    if workers > 1:
        log.info("🧵 Workers : {} threads".format(workers))
    log.info("💾 Checkpoint : {}".format(cm.path))

    if remaining > 0 and not api_key:
        log.error("❌ Clé API requise. --api-key sk-xxx ou export SILICONFLOW_API_KEY=sk-xxx")
        sys.exit(1)

    if not to_process:
        if resumed > 0:
            log.info("✅ Tous les fichiers ont déjà été traités.")
            log.info("   --reset pour recommencer à zéro.")
        return list(progress.values())

    # Confirmation avant lancement des appels LLM
    if not skip_confirm:
        try:
            answer = input("\n  Lancer le traitement ? (o/N) : ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            log.info("Annulé.")
            sys.exit(0)
        if answer not in ('o', 'oui', 'y', 'yes'):
            log.info("Annulé.")
            sys.exit(0)

    # ── Gestion Ctrl+C ──
    interrupted = [False]
    progress_lock = threading.Lock()
    print_lock = threading.Lock()
    processed_count = [0]
    api_errors = [0]

    def signal_handler(sig, frame):
        interrupted[0] = True
        log.warning("\n\n⚠  Interruption (Ctrl+C) — sauvegarde en cours...")

    old_handler = signal.signal(signal.SIGINT, signal_handler)

    endpoint = profile.llm_endpoint
    model = profile.llm_model
    theme_mapping = profile.theme_mapping
    min_confidence = profile.defaults.get('min_confidence', 0.5)
    max_api_errors = profile.defaults.get('max_api_errors', 10)

    classifier, mapper = _load_classifiers(profile, api_key, verbose)

    def _process_one(pdf_path):
        # type: (str) -> Optional[Dict]
        """Traite un fichier PDF et met à jour le checkpoint."""
        if interrupted[0]:
            return None

        filename = os.path.basename(pdf_path)
        result = process_single_file(
            pdf_path, api_key, endpoint, model,
            theme_mapping, classifier, mapper, verbose=verbose,
            min_confidence=min_confidence)

        with progress_lock:
            progress[pdf_path] = result
            processed_count[0] += 1
            done = resumed + processed_count[0]

            if result['status'] == 'erreur_api':
                api_errors[0] += 1
                if api_errors[0] >= max_api_errors:
                    interrupted[0] = True
            else:
                api_errors[0] = 0

            if logs_dir and (workers <= 1 or processed_count[0] % 5 == 0):
                cm.save(progress, model, len(pdf_files))

        with print_lock:
            classified = sum(1 for r in progress.values()
                           if r.get('status') == 'classifié')
            icon = '✅' if result['status'] == 'classifié' else '❌'
            log.info("  [{}/{}] {} {:50s} → {} ({} classifiés)".format(
                done, len(pdf_files), icon, filename[:50],
                result['status'], classified))

        return result

    _run_processing(to_process, _process_one, workers, delay, interrupted, print_lock)

    # Checkpoint final
    if logs_dir:
        cm.save(progress, model, len(pdf_files))

    signal.signal(signal.SIGINT, old_handler)

    if interrupted[0]:
        log.info("\n💾 Progression sauvegardée ({}/{}).".format(
            len(progress), len(pdf_files)))
        log.info("   Relancez pour reprendre.")

    _save_mapper_results(mapper, profile, logs_dir)

    return list(progress.values())


def _check_inbox_safety(source, target, fallback):
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


def _confirm_execute(results, source, target, fallback='_A-TRIER'):
    # type: (List[Dict], str, str, str) -> bool
    """Affiche un récapitulatif et demande confirmation avant exécution."""
    classified = [r for r in results if r['status'] == 'classifié']
    not_classified = [r for r in results
                      if r['status'] != 'classifié' and r.get('chemin')]
    renamed = [r for r in results if r.get('renommage')]

    log.info("\n" + "=" * 60)
    log.info("  ⚠  CONFIRMATION AVANT EXÉCUTION")
    log.info("=" * 60)
    log.info("  Source        : {}".format(source))
    log.info("  Cible         : {}".format(target))
    log.info("  Classifiés    : {} fichiers à copier".format(len(classified)))
    log.info("  Non-classifiés: {} fichiers → {}".format(len(not_classified), fallback))
    log.info("  Renommés      : {} fichiers".format(len(renamed)))
    log.info("  ⚡ Les fichiers source seront supprimés après copie vérifiée")
    log.info("=" * 60)

    try:
        answer = input("\n  Continuer ? (o/N) : ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        log.info("Annulé.")
        return False

    return answer in ('o', 'oui', 'y', 'yes')


def _safe_remove_source(src, dest):
    # type: (str, str) -> bool
    """Supprime le fichier source si la destination existe et a la même taille.
    Ne supprime JAMAIS si source et destination sont le même fichier.
    Retourne True si la suppression a été effectuée."""
    try:
        # Sécurité : ne jamais supprimer si src == dest (ex: inbox = _A-TRIER)
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


def _copy_files(file_list, target_base, dest_subdir, classify_only, label):
    # type: (List[Dict], str, Optional[str], bool, str) -> Tuple[int, int]
    """Copie une liste de fichiers vers target_base/dest_subdir.

    Pour les classifiés, dest_subdir vient de r['destination'].
    Pour les non-classifiés, dest_subdir est le fallback fixe.

    Args:
        file_list: Liste de résultats (dicts avec chemin, fichier, etc.)
        target_base: Racine de la bibliothèque cible
        dest_subdir: Sous-dossier fixe (fallback) ou None pour utiliser r['destination']
        classify_only: Si True, ne pas renommer les fichiers
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

        if not classify_only and r.get('renommage') and r.get('nouveau_nom'):
            final_name = r['nouveau_nom']
        else:
            final_name = r['fichier']

        dest_dir = os.path.join(target_base, dest_subdir or r['destination'])
        dest = os.path.join(dest_dir, final_name)

        # Anti-collision : renommer si un fichier différent existe déjà
        if os.path.exists(dest):
            if os.path.getsize(src) == os.path.getsize(dest):
                skipped_exists += 1
                if _safe_remove_source(src, dest):
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
            if _safe_remove_source(src, dest):
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


def execute_classify(results, target_base, classify_only=False, fallback='_A-TRIER'):
    # type: (List[Dict], str, bool, str) -> None
    """Copie les fichiers classifiés vers la cible, puis supprime la source.
    Les non-classifiés vont dans le dossier fallback. Après copie vérifiée,
    le fichier source est supprimé de l'inbox pour éviter les doublons."""
    total_cleaned = 0

    # Classifiés → sous-dossiers thématiques
    classified = [r for r in results if r['status'] == 'classifié']
    if classified:
        log.info("\n🚀 Copie de {} fichiers classifiés...".format(len(classified)))
        _, cleaned = _copy_files(
            classified, target_base, None, classify_only, "fichiers copiés")
        total_cleaned += cleaned
    else:
        log.info("\n⏭  Aucun fichier classifié à traiter.")

    # Non-classifiés → fallback
    not_classified = [r for r in results
                      if r['status'] != 'classifié' and r.get('chemin')]
    if not_classified:
        log.info("\n📁 Copie de {} non-classifiés vers {}...".format(
            len(not_classified), fallback))
        _, cleaned = _copy_files(
            not_classified, target_base, fallback, classify_only,
            "copiés vers {}".format(fallback))
        total_cleaned += cleaned

    if total_cleaned:
        log.info("\n🧹 {} fichiers supprimés de l'inbox".format(total_cleaned))


# ════════════════════════════════════════════════════════════════════════════
# SOUS-COMMANDE : RENAME
# ════════════════════════════════════════════════════════════════════════════

def _make_llm_rename_callback(api_key, endpoint, model, verbose=False, n_pages=1):
    # type: (str, str, str, bool, int) -> object
    """Crée un callback LLM Vision pour le renommage.

    Returns:
        Fonction(pdf_path) → dict avec 'title', 'author', ou {'error': type}.
    """
    def callback(pdf_path):
        # type: (str) -> Dict
        return analyze_cover(pdf_path, api_key, endpoint, model,
                             verbose=verbose, n_pages=n_pages)
    return callback


def cmd_rename(args, profile):
    # type: (argparse.Namespace, Profile) -> None
    """Sous-commande rename : renommage 'Titre - Auteur.pdf' via biblio_renamer."""
    source = args.path or profile.inbox
    if not source or not os.path.isdir(source):
        log.error("❌ Dossier introuvable : {}".format(source))
        sys.exit(1)

    # Importer biblio_renamer dynamiquement
    renamer_dir = os.path.join(PROJECT_ROOT, 'renommage')
    sys.path.insert(0, renamer_dir)
    try:
        import biblio_renamer
    except ImportError:
        log.error("❌ biblio_renamer.py introuvable dans renommage/")
        sys.exit(1)

    enable_online = not (hasattr(args, 'no_online') and args.no_online)
    enable_pdf = not (hasattr(args, 'no_pdf') and args.no_pdf)
    use_llm = getattr(args, 'llm', False)

    # Construire le callback LLM si demandé
    llm_callback = None
    if use_llm:
        api_key = args.api_key or os.environ.get('SILICONFLOW_API_KEY', '')
        if not api_key:
            log.error("❌ --llm nécessite une clé API. --api-key sk-xxx ou export SILICONFLOW_API_KEY=sk-xxx")
            sys.exit(1)
        n_pages = getattr(args, 'pages', 1)
        llm_callback = _make_llm_rename_callback(
            api_key, profile.llm_endpoint, profile.llm_model,
            verbose=args.verbose, n_pages=n_pages)

    max_files = getattr(args, 'max', 0)
    force = getattr(args, 'force', False)

    if use_llm:
        n_pages = getattr(args, 'pages', 1)
        mode = "LLM Vision ({} page{}) + ".format(n_pages, "s" if n_pages > 1 else "")
    else:
        mode = ""
    force_label = " (FORCE)" if force else ""
    log.info("\n📚 Renommage — {}".format(source))
    log.info("🔧 Mode    : {}{}{}{}".format(
        mode,
        "ISBN + " if enable_online else "",
        "PDF" if enable_pdf else "nom seul",
        force_label))

    # Scan (dry-run)
    report_path = biblio_renamer.scan(
        source, enable_online=enable_online, enable_pdf=enable_pdf,
        llm_callback=llm_callback, max_files=max_files, force=force,
        verbose=args.verbose)

    # Exécution si demandée
    if args.execute:
        biblio_renamer.execute(source, report_path)


# ════════════════════════════════════════════════════════════════════════════
# SOUS-COMMANDE : CLASSIFY
# ════════════════════════════════════════════════════════════════════════════

def cmd_classify(args, profile):
    # type: (argparse.Namespace, Profile) -> None
    """Sous-commande classify : LLM Vision + classement thématique."""
    source = args.path or profile.inbox
    if not source or not os.path.isdir(source):
        log.error("❌ Dossier introuvable : {}".format(source))
        sys.exit(1)

    _check_inbox_safety(source, profile.target, profile.fallback)

    api_key = args.api_key or os.environ.get('SILICONFLOW_API_KEY', '')
    workers = args.workers or profile.defaults.get('workers', 1)
    logs_dir = os.path.join(PROJECT_ROOT, 'logs')
    os.makedirs(logs_dir, exist_ok=True)

    # Checkpoint
    cm = CheckpointManager(logs_dir, args.progress_file or 'progress.json')

    if args.reset:
        cm.clear()

    if args.retry_errors:
        cm.retry_errors()

    if args.reclassify:
        classify_fn = make_classify_fn(profile.theme_mapping)
        min_confidence = profile.defaults.get('min_confidence', 0.5)
        count = cm.reclassify(classify_fn, min_confidence=min_confidence)
        if not args.execute:
            results = list(cm.load().values())
            cost_per_call = profile.defaults.get('cost_per_call', 0.00034)
            print_summary(results, cost_per_call=cost_per_call)
            report = save_report(results, logs_dir, 'rapport_classify')
            log.info("\n📋 Rapport : {}".format(report))
            return

    log.info("\n🔍 Classification LLM Vision")
    log.info("📁 Source  : {}".format(source))
    log.info("🎯 Cible   : {}".format(profile.target))
    log.info("🤖 Modèle  : {}".format(profile.llm_model))
    log.info("👤 Profil  : {}".format(profile.name))

    results = scan_and_classify(
        source, profile, api_key,
        max_files=args.max, verbose=args.verbose, delay=args.delay,
        workers=workers, logs_dir=logs_dir,
        checkpoint_name=args.progress_file or 'progress.json',
        skip_confirm=getattr(args, 'yes', False))

    cost_per_call = profile.defaults.get('cost_per_call', 0.00034)
    print_summary(results, cost_per_call=cost_per_call)

    # Rapport (toujours généré)
    report = save_report(results, logs_dir, 'rapport_classify')
    log.info("\n📋 Rapport : {}".format(report))

    if args.execute:
        if not getattr(args, 'yes', False) and not _confirm_execute(
                results, source, profile.target, profile.fallback):
            log.info("\n⏭  Exécution annulée. Le rapport est disponible pour review.")
            return
        execute_classify(results, profile.target, classify_only=True,
                         fallback=profile.fallback)


# ════════════════════════════════════════════════════════════════════════════
# SOUS-COMMANDE : REFINE
# ════════════════════════════════════════════════════════════════════════════

def cmd_refine(args, profile):
    # type: (argparse.Namespace, Profile) -> None
    """Sous-commande refine : raffinement des sous-catégories."""
    base_path = args.path or profile.target
    if not base_path or not os.path.isdir(base_path):
        log.error("❌ Dossier introuvable : {}".format(base_path))
        sys.exit(1)

    rules = load_refinement_rules(profile.refinement_rules)
    if not rules:
        log.error("❌ Aucune règle de raffinement dans le profil '{}'".format(profile.name))
        return

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    log.info("\n🔍 Raffinement des sous-catégories ({})".format(mode))
    log.info("📁 Base   : {}".format(base_path))
    log.info("👤 Profil : {} ({} règles)".format(profile.name, len(rules)))

    results = scan_and_refine(base_path, rules, execute=args.execute)

    if not results:
        log.info("✅ Aucun fichier à raffiner — tout est bien classé.")
        return

    print_refine_summary(results)

    logs_dir = os.path.join(PROJECT_ROOT, 'logs')
    os.makedirs(logs_dir, exist_ok=True)

    report = save_refine_report(results, logs_dir)
    log.info("\n📋 Rapport : {}".format(report))


# ════════════════════════════════════════════════════════════════════════════
# SOUS-COMMANDE : PROCESS (pipeline complet)
# ════════════════════════════════════════════════════════════════════════════

def cmd_process(args, profile):
    # type: (argparse.Namespace, Profile) -> None
    """Pipeline complet : renommage → LLM Vision → classement → raffinement."""
    source = args.path or profile.inbox
    if not source or not os.path.isdir(source):
        log.error("❌ Dossier introuvable : {}".format(source))
        sys.exit(1)

    _check_inbox_safety(source, profile.target, profile.fallback)

    api_key = args.api_key or os.environ.get('SILICONFLOW_API_KEY', '')
    workers = args.workers or profile.defaults.get('workers', 1)
    logs_dir = os.path.join(PROJECT_ROOT, 'logs')
    os.makedirs(logs_dir, exist_ok=True)

    log.info("=" * 60)
    log.info("  📚 Biblio v{} — Pipeline complet".format(__version__))
    log.info("=" * 60)
    log.info("  Source  : {}".format(source))
    log.info("  Cible   : {}".format(profile.target))
    log.info("  Profil  : {}".format(profile.name))
    log.info("  Modèle  : {}".format(profile.llm_model))
    log.info("  Mode    : {}".format("EXECUTE" if args.execute else "DRY-RUN"))

    # ── Étape 1 : Classification LLM ──
    log.info("━" * 40)
    log.info("  Étape 1/3 : LLM Vision + Classification")
    log.info("━" * 40)

    cm = CheckpointManager(logs_dir, args.progress_file or 'progress.json')
    if args.reset:
        cm.clear()
    if args.retry_errors:
        cm.retry_errors()

    results = scan_and_classify(
        source, profile, api_key,
        max_files=args.max, verbose=args.verbose, delay=args.delay,
        workers=workers, logs_dir=logs_dir,
        checkpoint_name=args.progress_file or 'progress.json',
        skip_confirm=getattr(args, 'yes', False))

    cost_per_call = profile.defaults.get('cost_per_call', 0.00034)
    print_summary(results, cost_per_call=cost_per_call)

    # Rapport (toujours généré)
    report = save_report(results, logs_dir, 'rapport_process')
    log.info("\n📋 Rapport : {}".format(report))

    # ── Confirmation + Exécution ──
    if args.execute:
        if not getattr(args, 'yes', False) and not _confirm_execute(
                results, source, profile.target, profile.fallback):
            log.info("\n⏭  Exécution annulée. Le rapport est disponible pour review.")
            return

        # Étape 2 : Copie vers la cible
        log.info("\n" + "━" * 40)
        log.info("  Étape 2/3 : Copie vers {}".format(profile.target))
        log.info("━" * 40)
        execute_classify(results, profile.target, fallback=profile.fallback)

        # Étape 3 : Raffinement sous-catégories
        rules = load_refinement_rules(profile.refinement_rules)
        if rules:
            log.info("\n" + "━" * 40)
            log.info("  Étape 3/3 : Raffinement sous-catégories")
            log.info("━" * 40)
            refine_results = scan_and_refine(
                profile.target, rules, execute=True)
            if refine_results:
                print_refine_summary(refine_results)
            else:
                log.info("  ✅ Pas de raffinement nécessaire.")

    log.info("\n✅ Pipeline terminé.")


# ════════════════════════════════════════════════════════════════════════════
# SOUS-COMMANDE : PROFILES
# ════════════════════════════════════════════════════════════════════════════

def cmd_profiles(args):
    # type: (argparse.Namespace) -> None
    """Liste les profils disponibles."""
    profiles = list_profiles()
    if not profiles:
        log.info("Aucun profil trouvé.")
        return

    log.info("\n📂 Profils disponibles :\n")
    for name in profiles:
        try:
            p = Profile(name)
            log.info("  {} — {} (cible: {})".format(name, p.description, p.target))
        except Exception as e:
            log.error("  {} — ⚠ Erreur: {}".format(name, e))


# ════════════════════════════════════════════════════════════════════════════
# SOUS-COMMANDE : INIT
# ════════════════════════════════════════════════════════════════════════════

def cmd_init(args):
    # type: (argparse.Namespace) -> None
    """Crée un nouveau profil."""
    name = args.name
    target = args.target

    if not target:
        log.error("❌ --target requis (chemin de la bibliothèque)")
        sys.exit(1)

    try:
        p = init_profile(name, target)
        log.info("\n✅ Profil '{}' créé".format(name))
        log.info("   Répertoire : profiles/{}/".format(name))
        log.info("   Cible      : {}".format(target))
        log.info("\n   Fichiers à personnaliser :")
        log.info("   - profiles/{}/tree.yaml           (arborescence)".format(name))
        log.info("   - profiles/{}/theme_mapping.yaml   (mapping thèmes)".format(name))
        log.info("   - profiles/{}/categories.yaml      (mots-clés)".format(name))
        log.info("   - profiles/{}/refinement.yaml      (raffinement)".format(name))
    except FileExistsError:
        log.error("❌ Le profil '{}' existe déjà.".format(name))
        sys.exit(1)


# ════════════════════════════════════════════════════════════════════════════
# SOUS-COMMANDE : SUGGEST
# ════════════════════════════════════════════════════════════════════════════

def cmd_suggest(args, profile):
    # type: (argparse.Namespace, Profile) -> None
    """Sous-commande suggest : review et application des suggestions de nouveaux dossiers."""
    logs_dir = os.path.join(PROJECT_ROOT, 'logs')
    suggestions = load_suggestions(logs_dir)

    if not suggestions:
        log.info("\n✅ Aucune suggestion trouvée.")
        log.info("   Les suggestions sont générées automatiquement lors du process/classify")
        log.info("   quand un thème ne correspond à aucun dossier existant.")
        return

    pending = [s for s in suggestions if s.get('status') == 'pending']
    applied = [s for s in suggestions if s.get('status') == 'applied']

    if args.apply:
        # ── Mode --apply : créer les dossiers et mettre à jour les YAML ──
        if not pending:
            log.info("\n✅ Aucune suggestion en attente.")
            if applied:
                log.info("   ({} suggestions déjà appliquées)".format(len(applied)))
            return

        log.info("\n🚀 Application de {} suggestions...".format(len(pending)))
        log.info("   Cible : {}".format(profile.target))

        result = apply_suggestions(
            suggestions,
            str(profile.profile_dir),
            profile.target,
        )

        # Mettre à jour le fichier suggestions.yaml (marquer applied)
        save_suggestions_file(suggestions, logs_dir)

        # Reclassifier les fichiers concernés si --execute
        if args.execute and result['themes_added'] > 0:
            log.info("\n🔄 Reclassification avec les nouveaux mappings...")
            cm = CheckpointManager(logs_dir, 'progress.json')
            from lib.classifier import make_classify_fn
            # Recharger le profil pour avoir les nouveaux mappings
            updated_profile = Profile(profile.name)
            classify_fn = make_classify_fn(updated_profile.theme_mapping)
            min_confidence = updated_profile.defaults.get('min_confidence', 0.5)
            count = cm.reclassify(classify_fn, min_confidence=min_confidence)
            if count > 0:
                log.info("  ✅ {} fichiers reclassifiés".format(count))
    else:
        # ── Mode review (défaut) : afficher les suggestions ──
        log.info("\n💡 Suggestions de nouveaux dossiers ({} en attente, {} appliquées)\n".format(
            len(pending), len(applied)))

        if pending:
            log.info("  En attente :")
            log.info("  {:4s}  {:30s}  {:40s}  {}".format(
                "#", "THÈME", "DOSSIER PROPOSÉ", "RAISON"))
            log.info("  " + "─" * 100)
            for i, s in enumerate(pending, 1):
                log.info("  {:4d}  {:30s}  {:40s}  {}".format(
                    i,
                    s.get('theme', '')[:30],
                    s.get('folder', '')[:40],
                    s.get('reason', '')[:40],
                ))
                if s.get('title'):
                    log.info("        └─ {}".format(s['title'][:70]))

        if applied:
            log.info("\n  Déjà appliquées : {}".format(len(applied)))

        log.info("\n  📝 Pour modifier : éditez logs/suggestions.yaml")
        log.info("     Supprimez les lignes non voulues, ajustez les chemins")
        log.info("  ✅ Pour appliquer : ./biblio.sh suggest --apply")
        if pending:
            log.info("  🔄 Pour appliquer + reclassifier : ./biblio.sh suggest --apply --execute")


# ════════════════════════════════════════════════════════════════════════════
# MAIN — CLI PARSER
# ════════════════════════════════════════════════════════════════════════════

def _build_parser():
    # type: () -> argparse.ArgumentParser
    """Construit le parser argparse avec toutes les sous-commandes."""
    parser = argparse.ArgumentParser(
        prog='biblio',
        description='Biblio v{} — Outil unifié de gestion de bibliothèque PDF'.format(__version__),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--version', action='version',
                        version='biblio {}'.format(__version__))

    subparsers = parser.add_subparsers(dest='command', help='Sous-commande')

    # ── Options communes ──
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--profile', default='default',
                        help='Profil à utiliser (défaut: default)')
    common.add_argument('--execute', action='store_true',
                        help='Appliquer (sinon dry-run)')
    common.add_argument('--verbose', '-v', action='store_true',
                        help='Mode verbeux')

    # ── Options LLM ──
    llm_common = argparse.ArgumentParser(add_help=False)
    llm_common.add_argument('--api-key', default=None,
                            help='Clé API (ou var SILICONFLOW_API_KEY)')
    llm_common.add_argument('--workers', '-w', type=int, default=0,
                            help='Threads parallèles (0 = défaut du profil)')
    llm_common.add_argument('--max', type=int, default=0,
                            help='Limiter à N fichiers')
    llm_common.add_argument('--delay', type=float, default=0.2,
                            help='Délai entre requêtes séquentielles (défaut: 0.2)')
    llm_common.add_argument('--reset', action='store_true',
                            help='Supprimer le checkpoint et recommencer')
    llm_common.add_argument('--retry-errors', action='store_true',
                            help='Retraiter les fichiers en erreur')
    llm_common.add_argument('--reclassify', action='store_true',
                            help='Re-mapper les thèmes sans appel LLM')
    llm_common.add_argument('--progress-file', default=None,
                            help='Nom du fichier de checkpoint')

    # ── process ──
    p_process = subparsers.add_parser(
        'process', parents=[common, llm_common],
        help='Pipeline complet (renommage + LLM + classement + raffinement)')
    p_process.add_argument('path', nargs='?', default=None,
                           help='Dossier source (défaut: inbox du profil)')

    # ── classify ──
    p_classify = subparsers.add_parser(
        'classify', parents=[common, llm_common],
        help='LLM Vision + classement thématique')
    p_classify.add_argument('path', nargs='?', default=None,
                            help='Dossier source (défaut: inbox du profil)')

    # ── rename ──
    p_rename = subparsers.add_parser(
        'rename', parents=[common],
        help='Renommage "Titre - Auteur.pdf"')
    p_rename.add_argument('path', nargs='?', default=None,
                          help='Dossier source (défaut: inbox du profil)')
    p_rename.add_argument('--no-online', action='store_true',
                          help='Désactiver la recherche ISBN en ligne')
    p_rename.add_argument('--no-pdf', action='store_true',
                          help='Désactiver l\'extraction PDF')
    p_rename.add_argument('--llm', action='store_true',
                          help='Activer LLM Vision en fallback (analyse couverture)')
    p_rename.add_argument('--api-key', default=None,
                          help='Clé API pour --llm (ou var SILICONFLOW_API_KEY)')
    p_rename.add_argument('--max', type=int, default=0,
                          help='Limiter à N fichiers')
    p_rename.add_argument('--force', action='store_true',
                          help='Re-analyser tous les fichiers (ignore is_name_clean)')
    p_rename.add_argument('--pages', type=int, default=1,
                          help='Nombre de pages à analyser par LLM Vision (défaut: 1, max: 5)')

    # ── refine ──
    p_refine = subparsers.add_parser(
        'refine', parents=[common],
        help='Raffinement des sous-catégories par mots-clés')
    p_refine.add_argument('path', nargs='?', default=None,
                          help='Dossier bibliothèque (défaut: target du profil)')

    # ── profiles ──
    subparsers.add_parser('profiles', help='Lister les profils disponibles')

    # ── suggest ──
    p_suggest = subparsers.add_parser(
        'suggest', parents=[common],
        help='Review / appliquer les suggestions de nouveaux dossiers')
    p_suggest.add_argument('--apply', action='store_true',
                           help='Appliquer les suggestions validées')

    # ── init ──
    p_init = subparsers.add_parser(
        'init', help='Créer un nouveau profil')
    p_init.add_argument('name', help='Nom du profil')
    p_init.add_argument('--target', required=True,
                        help='Chemin de la bibliothèque cible')

    return parser


def main():
    """Main CLI entry point."""
    parser = _build_parser()
    args = parser.parse_args()
    setup_logger(verbose=getattr(args, 'verbose', False), log_file='logs/biblio.log')

    if not args.command:
        parser.print_help()
        sys.exit(0)

    # Commandes sans profil
    if args.command == 'profiles':
        cmd_profiles(args)
        return
    if args.command == 'init':
        cmd_init(args)
        return

    # Charger le profil
    try:
        profile = Profile(args.profile)
    except FileNotFoundError:
        log.error("❌ Profil '{}' introuvable.".format(args.profile))
        log.info("   Profils disponibles : {}".format(', '.join(list_profiles())))
        sys.exit(1)

    # Validation du profil
    validation_errors = profile.validate()
    if validation_errors:
        log.error("❌ Profil '{}' invalide :".format(args.profile))
        for err in validation_errors:
            log.error("   - {}".format(err))
        sys.exit(1)

    # Dispatch
    dispatch = {
        'process': cmd_process,
        'classify': cmd_classify,
        'rename': cmd_rename,
        'refine': cmd_refine,
        'suggest': cmd_suggest,
    }  # type: Dict
    handler = dispatch.get(args.command)
    if handler:
        handler(args, profile)


if __name__ == '__main__':
    main()
