"""
Sous-commande classify — LLM Vision + classement thématique.
"""

import os
import signal
import sys
import threading

from commands.helpers import (
    PROJECT_ROOT,
    check_inbox_safety,
    confirm_execute,
    execute_classify,
    load_classifiers,
    process_single_file,
    run_processing,
    save_mapper_results,
)
from lib.checkpoint import CheckpointManager
from lib.classifier import make_classify_fn
from lib.logger import get_logger
from lib.utils import collect_pdf_files, print_summary, save_report

log = get_logger()


def scan_and_classify(source_dir, profile, api_key,
                      max_files=0, verbose=False, delay=0.2,
                      workers=1, logs_dir='', checkpoint_dir='',
                      checkpoint_name='progress.json',
                      skip_confirm=False, vision=False, n_pages=1):
    # type: (str, object, str, int, bool, float, int, str, str, str, bool, bool, int) -> list[dict]
    """
    Scanne un répertoire et traite chaque PDF via LLM Vision.
    Supporte la reprise automatique et le traitement parallèle.
    """
    pdf_files = collect_pdf_files(source_dir, max_files)
    total_found = len(pdf_files)

    # Checkpoint (profile dir if provided, else logs_dir for backward compat)
    cm = CheckpointManager(checkpoint_dir or logs_dir, checkpoint_name)
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
        log.error("❌ Clé API requise. export SILICONFLOW_API_KEY=votre-clé")
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
    vision_cache_path = os.path.join(profile.cache_dir, 'vision_cache.json')
    # Capitalise on the cover extraction: dump the PIL images into the
    # dashboard's thumbnail cache so it doesn't have to re-extract on
    # first viewer open. Same key the dashboard uses (MD5 of head bytes).
    thumbnails_base_dir = os.path.join(profile.cache_dir, 'thumbnails')

    classifier, mapper = load_classifiers(profile, api_key, verbose, vision=vision)

    def _process_one(pdf_path):
        # type: (str) -> dict | None
        """Traite un fichier PDF et met à jour le checkpoint."""
        if interrupted[0]:
            return None

        filename = os.path.basename(pdf_path)
        result = process_single_file(
            pdf_path, api_key, endpoint, model,
            theme_mapping, classifier, mapper, verbose=verbose,
            min_confidence=min_confidence, n_pages=n_pages,
            vision_cache_path=vision_cache_path,
            thumbnails_base_dir=thumbnails_base_dir)

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

    run_processing(to_process, _process_one, workers, delay, interrupted, print_lock)

    # Checkpoint final
    if logs_dir:
        cm.save(progress, model, len(pdf_files))

    signal.signal(signal.SIGINT, old_handler)

    if interrupted[0]:
        log.info("\n💾 Progression sauvegardée ({}/{}).".format(
            len(progress), len(pdf_files)))
        log.info("   Relancez pour reprendre.")

    save_mapper_results(mapper, profile, logs_dir)

    return list(progress.values())


def cmd_classify(args, profile) -> None:
    # type: (object, object)
    """Sous-commande classify : LLM Vision + classement thématique."""
    source = args.path or profile.inbox
    if not source or not os.path.isdir(source):
        log.error("❌ Dossier introuvable : {}".format(source))
        sys.exit(1)

    check_inbox_safety(source, profile.target, profile.fallback)

    api_key = os.environ.get('SILICONFLOW_API_KEY', '')
    workers = args.workers or profile.defaults.get('workers', 1)
    logs_dir = os.path.join(PROJECT_ROOT, 'logs')
    os.makedirs(logs_dir, exist_ok=True)

    # Checkpoint (stored in profile directory)
    checkpoint_dir = profile.cache_dir
    cm = CheckpointManager(checkpoint_dir, args.progress_file or 'progress.json')

    if args.reset:
        cm.clear()

    if args.retry_errors:
        cm.retry_errors()

    if args.reclassify:
        classify_fn = make_classify_fn(profile.theme_mapping)
        min_confidence = profile.defaults.get('min_confidence', 0.5)
        cm.reclassify(classify_fn, min_confidence=min_confidence)
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

    use_vision = getattr(args, 'vision', False)
    n_pages = getattr(args, 'pages', 1)
    if use_vision:
        log.info("👁 Vision : escalade activée dans le mapper")
    if n_pages > 1:
        log.info("📄 Pages  : {} pages analysées par LLM Vision".format(n_pages))
    results = scan_and_classify(
        source, profile, api_key,
        max_files=args.max, verbose=args.verbose, delay=args.delay,
        workers=workers, logs_dir=logs_dir,
        checkpoint_dir=checkpoint_dir,
        checkpoint_name=args.progress_file or 'progress.json',
        skip_confirm=getattr(args, 'yes', False),
        vision=use_vision, n_pages=n_pages)

    cost_per_call = profile.defaults.get('cost_per_call', 0.00034)
    print_summary(results, cost_per_call=cost_per_call)

    report = save_report(results, logs_dir, 'rapport_classify')
    log.info("\n📋 Rapport : {}".format(report))

    if args.execute:
        if not getattr(args, 'yes', False) and not confirm_execute(
                results, source, profile.target, profile.fallback):
            log.info("\n⏭  Exécution annulée. Le rapport est disponible pour review.")
            return
        execute_classify(results, profile.target,
                         fallback=profile.fallback)
