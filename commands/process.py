"""
Sous-commande process — Pipeline complet (rename → classify → copie → refine).
"""

import os
import sys

from commands.classify import scan_and_classify
from commands.helpers import (
    PROJECT_ROOT,
    check_inbox_safety,
    confirm_execute,
    execute_classify,
    make_llm_rename_callback,
)
from lib import __version__, renamer
from lib.checkpoint import CheckpointManager
from lib.logger import get_logger
from lib.refiner import load_refinement_rules, print_refine_summary, scan_and_refine
from lib.utils import print_summary, save_report

log = get_logger()


def cmd_process(args, profile) -> None:
    # type: (object, object)
    """Pipeline complet : renommage → LLM Vision → classement → raffinement."""
    source = args.path or profile.inbox
    if not source or not os.path.isdir(source):
        log.error("❌ Dossier introuvable : {}".format(source))
        sys.exit(1)

    check_inbox_safety(source, profile.target, profile.fallback)

    api_key = os.environ.get('SILICONFLOW_API_KEY', '')
    workers = args.workers or profile.defaults.get('workers', 1)
    logs_dir = os.path.join(PROJECT_ROOT, 'logs')
    os.makedirs(logs_dir, exist_ok=True)

    skip_rename = getattr(args, 'no_rename', False)
    n_steps = 3 if skip_rename else 4

    log.info("=" * 60)
    log.info("  📚 Klodo v{} — Pipeline complet".format(__version__))
    log.info("=" * 60)
    log.info("  Source  : {}".format(source))
    log.info("  Cible   : {}".format(profile.target))
    log.info("  Profil  : {}".format(profile.name))
    log.info("  Modèle  : {}".format(profile.llm_model))
    log.info("  Mode    : {}".format("EXECUTE" if args.execute else "DRY-RUN"))
    if skip_rename:
        log.info("  Rename  : désactivé (--no-rename)")

    step = 0

    # ── Étape 1 : Renommage (sauf si --no-rename) ──
    if not skip_rename:
        step += 1
        log.info("\n" + "━" * 40)
        log.info("  Étape {}/{} : Renommage".format(step, n_steps))
        log.info("━" * 40)

        enable_online = not getattr(args, 'no_online', False)
        enable_pdf = not getattr(args, 'no_pdf', False)
        use_llm_rename = getattr(args, 'llm', False)
        force = getattr(args, 'force', False)
        n_pages = getattr(args, 'pages', 1)

        # Construire le callback LLM si demandé
        llm_callback = None
        if use_llm_rename:
            if not api_key:
                log.error("❌ --llm nécessite une clé API. export SILICONFLOW_API_KEY=votre-clé")
                sys.exit(1)
            vision_cache_path = os.path.join(profile.cache_dir, 'vision_cache.json')
            thumbnails_base_dir = os.path.join(profile.cache_dir, 'thumbnails')
            llm_callback = make_llm_rename_callback(
                api_key, profile.llm_endpoint, profile.llm_model,
                verbose=args.verbose, n_pages=n_pages,
                vision_cache_path=vision_cache_path,
                thumbnails_base_dir=thumbnails_base_dir)

        report_path = renamer.scan(
            source, enable_online=enable_online, enable_pdf=enable_pdf,
            llm_callback=llm_callback, max_files=getattr(args, 'max', 0),
            force=force, verbose=args.verbose,
            cache_dir=profile.cache_dir,
            name_patterns=profile.name_patterns)

        if args.execute:
            renamer.execute(source, report_path)
            log.info("  ✅ Renommage appliqué.")
        else:
            log.info("  📋 Rapport renommage : {}".format(report_path))
            log.info("  ℹ  Dry-run : les fichiers ne sont pas renommés.")

    # ── Étape 2 : Classification LLM ──
    step += 1
    log.info("\n" + "━" * 40)
    log.info("  Étape {}/{} : LLM Vision + Classification".format(step, n_steps))
    log.info("━" * 40)

    checkpoint_dir = profile.cache_dir
    cm = CheckpointManager(checkpoint_dir, args.progress_file or 'progress.json')
    if args.reset:
        cm.clear()
    if args.retry_errors:
        cm.retry_errors()

    results = scan_and_classify(
        source, profile, api_key,
        max_files=args.max, verbose=args.verbose, delay=args.delay,
        workers=workers, logs_dir=logs_dir,
        checkpoint_dir=checkpoint_dir,
        checkpoint_name=args.progress_file or 'progress.json',
        skip_confirm=getattr(args, 'yes', False),
        vision=getattr(args, 'vision', False),
        n_pages=getattr(args, 'pages', 1))

    cost_per_call = profile.defaults.get('cost_per_call', 0.00034)
    print_summary(results, cost_per_call=cost_per_call)

    # Rapport (toujours généré)
    report = save_report(results, logs_dir, 'rapport_process')
    log.info("\n📋 Rapport : {}".format(report))

    # ── Confirmation + Exécution ──
    if args.execute:
        if not getattr(args, 'yes', False) and not confirm_execute(
                results, source, profile.target, profile.fallback):
            log.info("\n⏭  Exécution annulée. Le rapport est disponible pour review.")
            return

        # Étape 3 : Copie vers la cible
        step += 1
        log.info("\n" + "━" * 40)
        log.info("  Étape {}/{} : Copie vers {}".format(step, n_steps, profile.target))
        log.info("━" * 40)
        execute_classify(results, profile.target, fallback=profile.fallback)

        # Étape 4 : Raffinement sous-catégories
        rules = load_refinement_rules(profile.refinement_rules)
        if rules:
            step += 1
            log.info("\n" + "━" * 40)
            log.info("  Étape {}/{} : Raffinement sous-catégories".format(step, n_steps))
            log.info("━" * 40)
            refine_results = scan_and_refine(
                profile.target, rules, execute=True)
            if refine_results:
                print_refine_summary(refine_results)
            else:
                log.info("  ✅ Pas de raffinement nécessaire.")

    log.info("\n✅ Pipeline terminé.")
