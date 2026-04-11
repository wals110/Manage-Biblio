#!/usr/bin/env python3
"""
Klodo — Outil unifié de gestion de bibliothèque PDF.
=====================================================
Pipeline complet : renommage → identification LLM → classification → raffinement.

Sous-commandes :
    process   Pipeline complet (renommage + LLM + classement + raffinement)
    classify  LLM Vision + classement thématique
    rename    Renommage "Titre - Auteur.pdf" (ISBN / métadonnées)
    refine    Raffinement des sous-catégories par mots-clés
    clean     Nettoyer le cache (progress, isbn, logs, all)
    profiles  Lister les profils disponibles
    init      Créer un nouveau profil

Options globales :
    --profile NAME    Profil à utiliser (défaut: default)
    --execute         Appliquer les modifications (sinon dry-run)
    --verbose         Mode détaillé
    --workers N       Threads parallèles (pour classify/process)
    --max N           Limiter à N fichiers

Exemples :
    ./klodo.sh process /chemin/vers/nouveaux_pdfs
    ./klodo.sh classify /chemin/vers/dossier --workers 10
    ./klodo.sh rename /chemin/vers/dossier
    ./klodo.sh refine
    ./klodo.sh profiles
    ./klodo.sh init mon-profil --target /Volumes/MonDisque/BIBLIO

Prérequis :
    brew install poppler
    pip3 install pdf2image Pillow pyyaml requests
"""

import argparse
import os
import sys

from lib import __version__

# ── Ajout du répertoire projet au path ──
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from commands import (
    cmd_classify,
    cmd_clean,
    cmd_detect,
    cmd_init,
    cmd_process,
    cmd_profiles,
    cmd_refine,
    cmd_rename,
    cmd_suggest,
)
from lib.exceptions import ConfigError
from lib.logger import get_logger, setup_logger
from lib.profile import Profile, list_profiles

log = get_logger()


# ════════════════════════════════════════════════════════════════════════════
# CLI PARSER
# ════════════════════════════════════════════════════════════════════════════

def _build_parser():
    # type: () -> argparse.ArgumentParser
    """Construit le parser argparse avec toutes les sous-commandes."""
    parser = argparse.ArgumentParser(
        prog='klodo',
        description='Klodo v{} — Outil unifié de gestion de bibliothèque PDF'.format(__version__),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--version', action='version',
                        version='klodo {}'.format(__version__))

    subparsers = parser.add_subparsers(dest='command', help='Sous-commande')

    # ── Options communes ──
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--profile', default='default',
                        help='Profil à utiliser (défaut: default)')
    common.add_argument('--execute', action='store_true',
                        help='Appliquer (sinon dry-run)')
    common.add_argument('--verbose', '-v', action='store_true',
                        help='Mode verbeux')
    common.add_argument('--yes', '-y', action='store_true',
                        help='Répondre oui à toutes les confirmations')

    # ── Options LLM ──
    llm_common = argparse.ArgumentParser(add_help=False)
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
    p_process.add_argument('--no-rename', action='store_true',
                           help='Désactiver l\'étape de renommage (classify + refine uniquement)')
    p_process.add_argument('--llm', action='store_true',
                           help='Activer LLM Vision pour le renommage (fallback)')
    p_process.add_argument('--force', action='store_true',
                           help='Re-analyser tous les fichiers lors du renommage')
    p_process.add_argument('--no-online', action='store_true',
                           help='Désactiver la recherche ISBN en ligne (renommage)')
    p_process.add_argument('--no-pdf', action='store_true',
                           help='Désactiver l\'extraction PDF (renommage)')
    p_process.add_argument('--vision', action='store_true',
                           help='Escalade vision dans le LLM Mapper quand le texte échoue')
    p_process.add_argument('--pages', type=int, default=1,
                           help='Nombre de pages à analyser par LLM Vision (défaut: 1, max: 5)')

    # ── classify ──
    p_classify = subparsers.add_parser(
        'classify', parents=[common, llm_common],
        help='LLM Vision + classement thématique')
    p_classify.add_argument('path', nargs='?', default=None,
                            help='Dossier source (défaut: inbox du profil)')
    p_classify.add_argument('--vision', action='store_true',
                            help='Escalade vision dans le LLM Mapper quand le texte échoue')
    p_classify.add_argument('--pages', type=int, default=1,
                            help='Nombre de pages à analyser par LLM Vision (défaut: 1, max: 5)')

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
    p_rename.add_argument('--max', type=int, default=0,
                          help='Limiter à N fichiers')
    p_rename.add_argument('--force', action='store_true',
                          help='Re-analyser tous les fichiers (ignore is_name_clean)')
    p_rename.add_argument('--pages', type=int, default=1,
                          help='Nombre de pages à analyser par LLM Vision (défaut: 1, max: 5)')

    # ── refine ──
    p_refine = subparsers.add_parser(
        'refine', parents=[common],
        help='Raffinement récursif des sous-catégories')
    p_refine.add_argument('path', nargs='?', default=None,
                          help='Dossier bibliothèque (défaut: target du profil)')
    p_refine.add_argument('--llm', action='store_true',
                          help='Activer le fallback LLM pour les fichiers non matchés')
    p_refine.add_argument('--vision', action='store_true',
                          help='Escalade vision : envoyer la couverture PDF si le texte échoue (requiert --llm)')
    p_refine.add_argument('--max', type=int, default=0,
                          help='Limiter les appels LLM à N fichiers')
    p_refine.add_argument('-w', '--workers', type=int, default=1,
                          help='Nombre de threads parallèles pour les appels LLM (défaut: 1)')

    # ── profiles ──
    subparsers.add_parser('profiles', help='Lister les profils disponibles')

    # ── suggest ──
    p_suggest = subparsers.add_parser(
        'suggest', parents=[common],
        help='Review / appliquer les suggestions de nouveaux dossiers')
    p_suggest.add_argument('--apply', action='store_true',
                           help='Appliquer les suggestions validées')

    # ── clean ──
    p_clean = subparsers.add_parser(
        'clean', parents=[common],
        help='Nettoyer le cache du profil (classify, rename, progress, isbn, logs, all)')
    p_clean.add_argument('target',
                         choices=['classify', 'rename', 'progress', 'isbn', 'logs', 'all'],
                         help='Quoi nettoyer : classify, rename, progress (les deux), isbn, logs ou all')

    # ── init ��─
    p_init = subparsers.add_parser(
        'init', help='Créer un nouveau profil')
    p_init.add_argument('name', help='Nom du profil')
    p_init.add_argument('--target', required=True,
                        help='Chemin de la bibliothèque cible')

    # ── detect ──
    p_detect = subparsers.add_parser(
        'detect', parents=[common],
        help='Détecter un pattern de nommage depuis un échantillon de fichiers')
    p_detect.add_argument('--files', nargs='+', metavar='FICHIER',
                          help='Liste de fichiers PDF à analyser')
    p_detect.add_argument('--dir', metavar='DOSSIER',
                          help='Dossier ou pattern glob pour sélectionner les fichiers')
    p_detect.add_argument('--library', metavar='CHEMIN',
                          help='Bibliothèque pour tester la couverture (défaut: target du profil)')

    # ── dashboard ──
    p_dashboard = subparsers.add_parser(
        'dashboard', help='Dashboard tests fonctionnels')
    p_dashboard.add_argument('--port', type=int, default=8080,
                             help='Port (défaut: 8080)')

    return parser


def main():
    """Main CLI entry point."""
    parser = _build_parser()
    args = parser.parse_args()
    setup_logger(verbose=getattr(args, 'verbose', False), log_file='logs/klodo.log')

    if not args.command:
        parser.print_help()
        sys.exit(0)

    # Commandes sans profil
    if args.command == 'dashboard':
        import uvicorn
        uvicorn.run("dashboard.app:app", host="127.0.0.1", port=args.port)
        return
    if args.command == 'profiles':
        cmd_profiles(args)
        return
    if args.command == 'init':
        cmd_init(args)
        return

    # Charger le profil
    try:
        profile = Profile(args.profile)
    except (FileNotFoundError, ConfigError):
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
        'clean': cmd_clean,
        'detect': cmd_detect,
    }
    handler = dispatch.get(args.command)
    if handler:
        handler(args, profile)


if __name__ == '__main__':
    main()
