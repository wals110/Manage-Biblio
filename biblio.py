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
    --report          Générer un rapport CSV
    --workers N       Threads parallèles (pour classify/process)
    --max N           Limiter à N fichiers
    --verbose         Afficher les détails

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


# ════════════════════════════════════════════════════════════════════════════
# PIPELINE : CLASSIFY (LLM Vision + classement)
# ════════════════════════════════════════════════════════════════════════════

def process_single_file(pdf_path, api_key, endpoint, model,
                        theme_mapping, classifier=None, llm_mapper=None,
                        verbose=False):
    # type: (str, str, str, str, Dict[str, str], object, object, bool) -> Dict
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
    if vision is None:
        # analyze_cover retourne None si extraction ou API échoue
        # Distinguer les deux cas
        result['status'] = 'erreur_extraction'
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
    if title and confidence >= 0.3 and not is_name_already_clean(filename):
        new_name = build_new_filename(title, author)
        if new_name and new_name != filename:
            result['nouveau_nom'] = new_name
            result['renommage'] = True

    # 3. Classification
    if confidence >= 0.3:
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


def scan_and_classify(source_dir, profile, api_key,
                      max_files=0, verbose=False, delay=0.2,
                      workers=1, logs_dir='', checkpoint_name='progress.json'):
    # type: (str, Profile, str, int, bool, float, int, str, str) -> List[Dict]
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
        print("\n🔄 Reprise : {}/{} déjà traités → {} restants".format(
            resumed, len(pdf_files), len(pdf_files) - resumed))

    to_process = [p for p in pdf_files if p not in progress]
    remaining = len(to_process)

    print("\n📚 {} fichiers à traiter (sur {} trouvés)".format(remaining, total_found))
    print("🤖 Modèle  : {}".format(profile.llm_model))
    if workers > 1:
        print("🧵 Workers : {} threads".format(workers))
    print("💾 Checkpoint : {}".format(cm.path))
    print()

    if remaining > 0 and not api_key:
        print("❌ Clé API requise. --api-key sk-xxx ou export SILICONFLOW_API_KEY=sk-xxx")
        sys.exit(1)

    if not to_process:
        if resumed > 0:
            print("✅ Tous les fichiers ont déjà été traités.")
            print("   --reset pour recommencer à zéro.")
        return list(progress.values())

    # ── Gestion Ctrl+C ──
    interrupted = [False]
    progress_lock = threading.Lock()
    print_lock = threading.Lock()
    processed_count = [0]
    api_errors = [0]

    def signal_handler(sig, frame):
        interrupted[0] = True
        print("\n\n⚠  Interruption (Ctrl+C) — sauvegarde en cours...")

    old_handler = signal.signal(signal.SIGINT, signal_handler)

    endpoint = profile.llm_endpoint
    model = profile.llm_model
    theme_mapping = profile.theme_mapping

    # Charger le classifieur mots-clés
    classifier = None
    cats_path = os.path.join(
        profile._profile_dir, 'categories.yaml')  # type: ignore
    if os.path.exists(str(cats_path)):
        try:
            classifier = load_keyword_classifier(str(cats_path))
            if classifier:
                print("✅ Classifieur YAML chargé (mode hybride)")
        except Exception as e:
            print("⚠ Classifieur non chargé: {}".format(e))

    # Charger le LLM Mapper (résolution thèmes inconnus)
    mapper = None
    mapper_enabled = profile.defaults.get('llm_mapper', True)
    if mapper_enabled and api_key and profile.tree:
        mapper = LLMMapper(
            folders=profile.tree,
            api_key=api_key,
            endpoint=endpoint,
            model=model,
            min_confidence=profile.defaults.get('mapper_min_confidence', 0.6),
            verbose=verbose,
        )
        print("🧠 LLM Mapper activé (résolution thèmes inconnus)")

    def _process_one(pdf_path):
        # type: (str) -> Optional[Dict]
        if interrupted[0]:
            return None

        filename = os.path.basename(pdf_path)
        result = process_single_file(
            pdf_path, api_key, endpoint, model,
            theme_mapping, classifier, mapper, verbose=False)

        with progress_lock:
            progress[pdf_path] = result
            processed_count[0] += 1
            done = resumed + processed_count[0]

            if result['status'] == 'erreur_api':
                api_errors[0] += 1
                if api_errors[0] >= 10:
                    interrupted[0] = True
            else:
                api_errors[0] = 0

            if logs_dir and (workers <= 1 or processed_count[0] % 5 == 0):
                cm.save(progress, model, len(pdf_files))

        with print_lock:
            classified = sum(1 for r in progress.values()
                           if r.get('status') == 'classifié')
            icon = '✅' if result['status'] == 'classifié' else '❌'
            print("  [{}/{}] {} {:50s} → {} ({} classifiés)".format(
                done, len(pdf_files), icon, filename[:50],
                result['status'], classified))

        return result

    # ── Exécution ──
    if workers > 1:
        print("🚀 {} requêtes avec {} threads...\n".format(len(to_process), workers))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}  # type: Dict
            for pdf_path in to_process:
                if interrupted[0]:
                    break
                future = executor.submit(_process_one, pdf_path)
                futures[future] = pdf_path
            for future in as_completed(futures):
                if interrupted[0]:
                    break
                try:
                    future.result()
                except Exception as e:
                    with print_lock:
                        print("  ⚠ Exception: {}".format(e))
    else:
        import time
        print("🚀 Traitement séquentiel de {} fichiers...\n".format(len(to_process)))
        for pdf_path in to_process:
            if interrupted[0]:
                break
            _process_one(pdf_path)
            if not interrupted[0] and delay > 0:
                time.sleep(delay)

    # Checkpoint final
    if logs_dir:
        cm.save(progress, model, len(pdf_files))

    signal.signal(signal.SIGINT, old_handler)

    if interrupted[0]:
        print("\n💾 Progression sauvegardée ({}/{}).".format(
            len(progress), len(pdf_files)))
        print("   Relancez pour reprendre.")

    # Auto-apprentissage : sauvegarder les nouveaux thèmes dans theme_mapping.yaml
    if mapper:
        if mapper.learned:
            theme_mapping_path = os.path.join(
                profile._profile_dir, 'theme_mapping.yaml')  # type: ignore
            mapper.save_learned(theme_mapping_path)
        if mapper.suggestions and logs_dir:
            mapper.save_suggestions(logs_dir)
        mapper.print_stats()

    return list(progress.values())


def execute_classify(results, target_base, classify_only=False):
    # type: (List[Dict], str, bool) -> None
    """Copie les fichiers classifiés vers la cible. Les non-classifiés vont dans _A-TRIER."""

    to_process = [r for r in results if r['status'] == 'classifié']
    if to_process:
        print("\n🚀 Copie de {} fichiers classifiés...".format(len(to_process)))
        done = 0
        skipped_src = 0
        skipped_exists = 0
        errors = 0

        for r in to_process:
            src = r['chemin']
            if not os.path.exists(src):
                skipped_src += 1
                continue

            if not classify_only and r.get('renommage') and r.get('nouveau_nom'):
                final_name = r['nouveau_nom']
            else:
                final_name = r['fichier']

            dest_dir = os.path.join(target_base, r['destination'])
            dest = os.path.join(dest_dir, final_name)

            if os.path.exists(dest):
                skipped_exists += 1
                continue

            # Anti-collision
            if os.path.exists(dest):
                base, ext = os.path.splitext(final_name)
                counter = 2
                while os.path.exists(dest):
                    dest = os.path.join(dest_dir, "{} ({}){}".format(base, counter, ext))
                    counter += 1

            os.makedirs(dest_dir, exist_ok=True)
            try:
                shutil.copy2(src, dest)
                done += 1
            except Exception as e:
                errors += 1
                print("  ⚠ Erreur {}: {}".format(r['fichier'][:40], e))

        parts = ["✅ {} fichiers copiés".format(done)]
        if skipped_exists:
            parts.append("{} déjà présents".format(skipped_exists))
        if skipped_src:
            parts.append("{} source absente".format(skipped_src))
        if errors:
            parts.append("{} erreurs".format(errors))
        print("  {}".format(" | ".join(parts)))
    else:
        print("\n⏭  Aucun fichier classifié à traiter.")

    # Fichiers non classifiés → _A-TRIER
    not_classified = [r for r in results
                      if r['status'] != 'classifié' and r.get('chemin')]
    if not_classified:
        print("\n📁 Copie de {} non-classifiés vers _A-TRIER...".format(len(not_classified)))
        atrier_done = 0
        atrier_skipped = 0
        for r in not_classified:
            src = r['chemin']
            if not os.path.exists(src):
                continue

            if not classify_only and r.get('renommage') and r.get('nouveau_nom'):
                final_name = r['nouveau_nom']
            else:
                final_name = r['fichier']

            dest_dir = os.path.join(target_base, '_A-TRIER')
            dest = os.path.join(dest_dir, final_name)

            if os.path.exists(dest):
                atrier_skipped += 1
                continue

            os.makedirs(dest_dir, exist_ok=True)
            try:
                shutil.copy2(src, dest)
                atrier_done += 1
            except Exception as e:
                print("  ⚠ Erreur {}: {}".format(final_name[:40], e))

        parts = ["✅ {} copiés vers _A-TRIER".format(atrier_done)]
        if atrier_skipped:
            parts.append("{} déjà présents".format(atrier_skipped))
        print("  {}".format(" | ".join(parts)))


# ════════════════════════════════════════════════════════════════════════════
# SOUS-COMMANDE : RENAME
# ════════════════════════════════════════════════════════════════════════════

def cmd_rename(args, profile):
    # type: (argparse.Namespace, Profile) -> None
    """Sous-commande rename : renommage 'Titre - Auteur.pdf' via biblio_renamer."""
    source = args.path or profile.inbox
    if not source or not os.path.isdir(source):
        print("❌ Dossier introuvable : {}".format(source))
        sys.exit(1)

    print("\n📚 Renommage — {}".format(source))

    # Importer biblio_renamer dynamiquement
    renamer_dir = os.path.join(PROJECT_ROOT, 'renommage')
    sys.path.insert(0, renamer_dir)
    try:
        import biblio_renamer
    except ImportError:
        print("❌ biblio_renamer.py introuvable dans renommage/")
        sys.exit(1)

    # Construire les arguments pour biblio_renamer
    rename_args = [source]
    if args.execute:
        rename_args.append('--execute')
    if hasattr(args, 'no_online') and args.no_online:
        rename_args.append('--no-online')
    if hasattr(args, 'no_pdf') and args.no_pdf:
        rename_args.append('--no-pdf')

    # Sauvegarder sys.argv et simuler l'appel
    old_argv = sys.argv
    sys.argv = ['biblio_renamer.py'] + rename_args
    try:
        biblio_renamer.main()
    finally:
        sys.argv = old_argv


# ════════════════════════════════════════════════════════════════════════════
# SOUS-COMMANDE : CLASSIFY
# ════════════════════════════════════════════════════════════════════════════

def cmd_classify(args, profile):
    # type: (argparse.Namespace, Profile) -> None
    """Sous-commande classify : LLM Vision + classement thématique."""
    source = args.path or profile.inbox
    if not source or not os.path.isdir(source):
        print("❌ Dossier introuvable : {}".format(source))
        sys.exit(1)

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
        count = cm.reclassify(classify_fn)
        if not args.execute:
            results = list(cm.load().values())
            print_summary(results)
            if args.report:
                report = save_report(results, logs_dir, 'rapport_classify')
                print("\n📋 Rapport : {}".format(report))
            return

    print("\n🔍 Classification LLM Vision")
    print("📁 Source  : {}".format(source))
    print("🎯 Cible   : {}".format(profile.target))
    print("🤖 Modèle  : {}".format(profile.llm_model))
    print("👤 Profil  : {}".format(profile.name))

    results = scan_and_classify(
        source, profile, api_key,
        max_files=args.max, verbose=args.verbose, delay=args.delay,
        workers=workers, logs_dir=logs_dir,
        checkpoint_name=args.progress_file or 'progress.json')

    print_summary(results)

    # Rapport (toujours généré)
    report = save_report(results, logs_dir, 'rapport_classify')
    print("\n📋 Rapport : {}".format(report))

    if args.execute:
        execute_classify(results, profile.target, classify_only=True)


# ════════════════════════════════════════════════════════════════════════════
# SOUS-COMMANDE : REFINE
# ════════════════════════════════════════════════════════════════════════════

def cmd_refine(args, profile):
    # type: (argparse.Namespace, Profile) -> None
    """Sous-commande refine : raffinement des sous-catégories."""
    base_path = args.path or profile.target
    if not base_path or not os.path.isdir(base_path):
        print("❌ Dossier introuvable : {}".format(base_path))
        sys.exit(1)

    rules = load_refinement_rules(profile.refinement_rules)
    if not rules:
        print("❌ Aucune règle de raffinement dans le profil '{}'".format(profile.name))
        return

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    print("\n🔍 Raffinement des sous-catégories ({})".format(mode))
    print("📁 Base   : {}".format(base_path))
    print("👤 Profil : {} ({} règles)".format(profile.name, len(rules)))
    print()

    results = scan_and_refine(base_path, rules, execute=args.execute)

    if not results:
        print("✅ Aucun fichier à raffiner — tout est bien classé.")
        return

    print_refine_summary(results)

    logs_dir = os.path.join(PROJECT_ROOT, 'logs')
    os.makedirs(logs_dir, exist_ok=True)

    if args.report or args.execute:
        report = save_refine_report(results, logs_dir)
        print("\n📋 Rapport : {}".format(report))


# ════════════════════════════════════════════════════════════════════════════
# SOUS-COMMANDE : PROCESS (pipeline complet)
# ════════════════════════════════════════════════════════════════════════════

def cmd_process(args, profile):
    # type: (argparse.Namespace, Profile) -> None
    """Pipeline complet : renommage → LLM Vision → classement → raffinement."""
    source = args.path or profile.inbox
    if not source or not os.path.isdir(source):
        print("❌ Dossier introuvable : {}".format(source))
        sys.exit(1)

    api_key = args.api_key or os.environ.get('SILICONFLOW_API_KEY', '')
    workers = args.workers or profile.defaults.get('workers', 1)
    logs_dir = os.path.join(PROJECT_ROOT, 'logs')
    os.makedirs(logs_dir, exist_ok=True)

    print("=" * 60)
    print("  📚 Biblio v{} — Pipeline complet".format(__version__))
    print("=" * 60)
    print("  Source  : {}".format(source))
    print("  Cible   : {}".format(profile.target))
    print("  Profil  : {}".format(profile.name))
    print("  Modèle  : {}".format(profile.llm_model))
    print("  Mode    : {}".format("EXECUTE" if args.execute else "DRY-RUN"))
    print()

    # ── Étape 1 : Classification LLM ──
    print("━" * 40)
    print("  Étape 1/3 : LLM Vision + Classification")
    print("━" * 40)

    cm = CheckpointManager(logs_dir, args.progress_file or 'progress.json')
    if args.reset:
        cm.clear()
    if args.retry_errors:
        cm.retry_errors()

    results = scan_and_classify(
        source, profile, api_key,
        max_files=args.max, verbose=args.verbose, delay=args.delay,
        workers=workers, logs_dir=logs_dir,
        checkpoint_name=args.progress_file or 'progress.json')

    print_summary(results)

    # ── Étape 2 : Copie vers la cible ──
    if args.execute:
        print("\n" + "━" * 40)
        print("  Étape 2/3 : Copie vers {}".format(profile.target))
        print("━" * 40)
        execute_classify(results, profile.target)

    # ── Étape 3 : Raffinement sous-catégories ──
    if args.execute:
        rules = load_refinement_rules(profile.refinement_rules)
        if rules:
            print("\n" + "━" * 40)
            print("  Étape 3/3 : Raffinement sous-catégories")
            print("━" * 40)
            refine_results = scan_and_refine(
                profile.target, rules, execute=True)
            if refine_results:
                print_refine_summary(refine_results)
            else:
                print("  ✅ Pas de raffinement nécessaire.")

    # Rapport (toujours généré)
    report = save_report(results, logs_dir, 'rapport_process')
    print("\n📋 Rapport : {}".format(report))

    print("\n✅ Pipeline terminé.")


# ════════════════════════════════════════════════════════════════════════════
# SOUS-COMMANDE : PROFILES
# ════════════════════════════════════════════════════════════════════════════

def cmd_profiles(args):
    # type: (argparse.Namespace) -> None
    """Liste les profils disponibles."""
    profiles = list_profiles()
    if not profiles:
        print("Aucun profil trouvé.")
        return

    print("\n📂 Profils disponibles :\n")
    for name in profiles:
        try:
            p = Profile(name)
            print("  {} — {} (cible: {})".format(name, p.description, p.target))
        except Exception as e:
            print("  {} — ⚠ Erreur: {}".format(name, e))


# ════════════════════════════════════════════════════════════════════════════
# SOUS-COMMANDE : INIT
# ════════════════════════════════════════════════════════════════════════════

def cmd_init(args):
    # type: (argparse.Namespace) -> None
    """Crée un nouveau profil."""
    name = args.name
    target = args.target

    if not target:
        print("❌ --target requis (chemin de la bibliothèque)")
        sys.exit(1)

    try:
        p = init_profile(name, target)
        print("\n✅ Profil '{}' créé".format(name))
        print("   Répertoire : profiles/{}/".format(name))
        print("   Cible      : {}".format(target))
        print("\n   Fichiers à personnaliser :")
        print("   - profiles/{}/tree.yaml           (arborescence)".format(name))
        print("   - profiles/{}/theme_mapping.yaml   (mapping thèmes)".format(name))
        print("   - profiles/{}/categories.yaml      (mots-clés)".format(name))
        print("   - profiles/{}/refinement.yaml      (raffinement)".format(name))
    except FileExistsError:
        print("❌ Le profil '{}' existe déjà.".format(name))
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
        print("\n✅ Aucune suggestion trouvée.")
        print("   Les suggestions sont générées automatiquement lors du process/classify")
        print("   quand un thème ne correspond à aucun dossier existant.")
        return

    pending = [s for s in suggestions if s.get('status') == 'pending']
    applied = [s for s in suggestions if s.get('status') == 'applied']

    if args.apply:
        # ── Mode --apply : créer les dossiers et mettre à jour les YAML ──
        if not pending:
            print("\n✅ Aucune suggestion en attente.")
            if applied:
                print("   ({} suggestions déjà appliquées)".format(len(applied)))
            return

        print("\n🚀 Application de {} suggestions...".format(len(pending)))
        print("   Cible : {}".format(profile.target))
        print()

        result = apply_suggestions(
            suggestions,
            profile._profile_dir,  # type: ignore
            profile.target,
        )

        # Mettre à jour le fichier suggestions.yaml (marquer applied)
        save_suggestions_file(suggestions, logs_dir)

        # Reclassifier les fichiers concernés si --execute
        if args.execute and result['themes_added'] > 0:
            print("\n🔄 Reclassification avec les nouveaux mappings...")
            cm = CheckpointManager(logs_dir, 'progress.json')
            from lib.classifier import make_classify_fn
            # Recharger le profil pour avoir les nouveaux mappings
            updated_profile = Profile(profile.name)
            classify_fn = make_classify_fn(updated_profile.theme_mapping)
            count = cm.reclassify(classify_fn)
            if count > 0:
                print("  ✅ {} fichiers reclassifiés".format(count))
    else:
        # ── Mode review (défaut) : afficher les suggestions ──
        print("\n💡 Suggestions de nouveaux dossiers ({} en attente, {} appliquées)\n".format(
            len(pending), len(applied)))

        if pending:
            print("  En attente :")
            print("  {:4s}  {:30s}  {:40s}  {}".format(
                "#", "THÈME", "DOSSIER PROPOSÉ", "RAISON"))
            print("  " + "─" * 100)
            for i, s in enumerate(pending, 1):
                print("  {:4d}  {:30s}  {:40s}  {}".format(
                    i,
                    s.get('theme', '')[:30],
                    s.get('folder', '')[:40],
                    s.get('reason', '')[:40],
                ))
                if s.get('title'):
                    print("        └─ {}".format(s['title'][:70]))

        if applied:
            print("\n  Déjà appliquées : {}".format(len(applied)))

        print("\n  📝 Pour modifier : éditez logs/suggestions.yaml")
        print("     Supprimez les lignes non voulues, ajustez les chemins")
        print("  ✅ Pour appliquer : ./biblio.sh suggest --apply")
        if pending:
            print("  🔄 Pour appliquer + reclassifier : ./biblio.sh suggest --apply --execute")


# ════════════════════════════════════════════════════════════════════════════
# MAIN — CLI PARSER
# ════════════════════════════════════════════════════════════════════════════

def main():
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
    common.add_argument('--report', action='store_true',
                        help='Générer un rapport CSV')
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

    # ── refine ──
    p_refine = subparsers.add_parser(
        'refine', parents=[common],
        help='Raffinement des sous-catégories par mots-clés')
    p_refine.add_argument('path', nargs='?', default=None,
                          help='Dossier bibliothèque (défaut: target du profil)')

    # ── profiles ──
    p_profiles = subparsers.add_parser(
        'profiles', help='Lister les profils disponibles')

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

    # Parse
    args = parser.parse_args()

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
        print("❌ Profil '{}' introuvable.".format(args.profile))
        print("   Profils disponibles : {}".format(', '.join(list_profiles())))
        sys.exit(1)

    # Dispatch
    if args.command == 'process':
        cmd_process(args, profile)
    elif args.command == 'classify':
        cmd_classify(args, profile)
    elif args.command == 'rename':
        cmd_rename(args, profile)
    elif args.command == 'refine':
        cmd_refine(args, profile)
    elif args.command == 'suggest':
        cmd_suggest(args, profile)


if __name__ == '__main__':
    main()
