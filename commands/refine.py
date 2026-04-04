"""
Sous-commande refine — Raffinement récursif des sous-catégories.
"""

import os
import sys

from lib.logger import get_logger
from lib.refiner import (
    load_refinement_rules, scan_and_refine,
    save_refine_report, print_refine_summary,
    make_refine_llm_callback,
)
from commands.helpers import PROJECT_ROOT

log = get_logger()


def cmd_refine(args, profile) -> None:
    # type: (object, object)
    """Sous-commande refine : raffinement des sous-catégories (parcours récursif)."""
    base_path = args.path or profile.target
    if not base_path or not os.path.isdir(base_path):
        log.error("❌ Dossier introuvable : {}".format(base_path))
        sys.exit(1)

    rules = load_refinement_rules(profile.refinement_rules)
    use_llm = getattr(args, 'llm', False)
    use_vision = getattr(args, 'vision', False)

    # ── Build LLM callback if requested ──
    llm_callback = None
    if use_llm:
        api_key = os.environ.get('SILICONFLOW_API_KEY', '')
        if not api_key:
            log.error("❌ --llm nécessite une clé API. export SILICONFLOW_API_KEY=votre-clé")
            sys.exit(1)
        raw_callback = make_refine_llm_callback(
            api_key=api_key,
            endpoint=profile.llm_endpoint,
            model=profile.llm_model,
            min_confidence=0.6,
            verbose=args.verbose,
            vision=use_vision,
        )
        # Wrap with --max limit if specified
        max_llm = getattr(args, 'max', 0)
        if raw_callback and max_llm > 0:
            _counter = [0]
            _inner = raw_callback

            def _limited_callback(filename, current_folder, subdirs,
                                  pdf_path=None):
                if _counter[0] >= max_llm:
                    return (None, '')
                _counter[0] += 1
                return _inner(filename, current_folder, subdirs,
                              pdf_path=pdf_path)

            _limited_callback.stats = raw_callback.stats
            llm_callback = _limited_callback
        else:
            llm_callback = raw_callback

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    llm_label = " + LLM" if use_llm else ""
    if use_vision:
        llm_label += " + Vision"
    max_llm = getattr(args, 'max', 0)
    log.info("\n🔍 Raffinement récursif des sous-catégories ({}{})".format(mode, llm_label))
    log.info("📁 Base   : {}".format(base_path))
    log.info("👤 Profil : {} ({} règles YAML)".format(profile.name, len(rules)))
    llm_workers = getattr(args, 'workers', 1) or 1
    if use_llm:
        log.info("🤖 LLM    : {} ({})".format(profile.llm_model, profile.llm_endpoint))
        if use_vision:
            log.info("👁 Vision  : escalade activée (couverture PDF)")
        if max_llm > 0:
            log.info("🔢 Max LLM: {} appels".format(max_llm))
        if llm_workers > 1:
            log.info("⚡ Workers: {} threads".format(llm_workers))

    results = scan_and_refine(base_path, rules, execute=args.execute,
                              llm_callback=llm_callback, workers=llm_workers)

    if not results:
        log.info("✅ Aucun fichier à raffiner — tout est bien classé.")
        return

    # Print LLM stats if available
    if llm_callback and hasattr(llm_callback, 'stats'):
        stats = llm_callback.stats
        if stats['calls'] > 0:
            log.info("\n🤖 LLM Refine : {} appels, {} résolus, {} échecs".format(
                stats['calls'], stats['successes'], stats['failures']))

    print_refine_summary(results)

    logs_dir = os.path.join(PROJECT_ROOT, 'logs')
    os.makedirs(logs_dir, exist_ok=True)

    report = save_refine_report(results, logs_dir)
    log.info("\n📋 Rapport : {}".format(report))
