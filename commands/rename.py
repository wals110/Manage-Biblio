"""
Sous-commande rename — Renommage 'Titre - Auteur.pdf'.
"""

import os
import sys

from commands.helpers import make_llm_rename_callback
from lib import renamer
from lib.logger import get_logger

log = get_logger()


def cmd_rename(args, profile) -> None:
    # type: (object, object)
    """Sous-commande rename : renommage 'Titre - Auteur.pdf'."""
    source = args.path or profile.inbox
    if not source or not os.path.isdir(source):
        log.error("❌ Dossier introuvable : {}".format(source))
        sys.exit(1)

    enable_online = not (hasattr(args, 'no_online') and args.no_online)
    enable_pdf = not (hasattr(args, 'no_pdf') and args.no_pdf)
    use_llm = getattr(args, 'llm', False)

    # Construire le callback LLM si demandé
    llm_callback = None
    if use_llm:
        api_key = os.environ.get('SILICONFLOW_API_KEY', '')
        if not api_key:
            log.error("❌ --llm nécessite une clé API. export SILICONFLOW_API_KEY=votre-clé")
            sys.exit(1)
        n_pages = getattr(args, 'pages', 1)
        llm_callback = make_llm_rename_callback(
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
    report_path = renamer.scan(
        source, enable_online=enable_online, enable_pdf=enable_pdf,
        llm_callback=llm_callback, max_files=max_files, force=force,
        verbose=args.verbose)

    # Exécution si demandée
    if args.execute:
        renamer.execute(source, report_path)
