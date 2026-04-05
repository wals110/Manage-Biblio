"""
Sous-commandes utilitaires — profiles, init, suggest, clean.
"""

import glob
import os
import sys

from commands.helpers import PROJECT_ROOT
from lib.checkpoint import CheckpointManager
from lib.llm_mapper import apply_suggestions, load_suggestions, save_suggestions_file
from lib.logger import get_logger
from lib.profile import Profile, init_profile, list_profiles

log = get_logger()


def cmd_profiles(args) -> None:
    # type: (object,)
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


def cmd_init(args) -> None:
    # type: (object,)
    """Crée un nouveau profil."""
    name = args.name
    target = args.target

    if not target:
        log.error("❌ --target requis (chemin de la bibliothèque)")
        sys.exit(1)

    try:
        init_profile(name, target)
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


def cmd_suggest(args, profile) -> None:
    # type: (object, object)
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
            cm = CheckpointManager(profile.cache_dir, 'progress.json')
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
        log.info("  ✅ Pour appliquer : ./klodo.sh suggest --apply")
        if pending:
            log.info("  🔄 Pour appliquer + reclassifier : ./klodo.sh suggest --apply --execute")


def cmd_clean(args, profile) -> None:
    # type: (object, object)
    """Sous-commande clean : supprime les fichiers de cache du profil."""
    cache_dir = profile.cache_dir
    target = args.target

    targets = {
        "classify": ["progress.json"],
        "rename": ["progress_rename.json"],
        "progress": ["progress.json", "progress_rename.json"],
        "isbn": ["isbn_cache.json"],
        "logs": [os.path.join(PROJECT_ROOT, "logs", "rapport_*.csv")],
        "all": ["progress.json", "progress_rename.json", "isbn_cache.json",
                os.path.join(PROJECT_ROOT, "logs", "rapport_*.csv")],
    }

    if target not in targets:
        log.error("❌ Cible inconnue : '{}'. Choix : {}".format(
            target, ", ".join(targets.keys())))
        sys.exit(1)

    patterns = targets[target]
    files = []
    for pattern in patterns:
        if os.path.isabs(pattern) or os.sep in pattern:
            # Pattern absolu (logs/rapport_*.csv)
            files.extend(sorted(glob.glob(pattern)))
        else:
            # Pattern relatif au cache du profil
            files.extend(sorted(glob.glob(os.path.join(cache_dir, pattern))))

    if not files:
        log.info("\n✅ Rien à nettoyer ({} pour le profil '{}').".format(target, profile.name))
        return

    log.info("\n🧹 Nettoyage — profil '{}' ({})".format(profile.name, target))
    for f in files:
        name = os.path.basename(f)
        if args.execute:
            os.remove(f)
            log.info("  ✗ {} supprimé".format(name))
        else:
            log.info("  [DRY-RUN] {}".format(name))

    if args.execute:
        log.info("\n✅ {} fichier(s) supprimé(s).".format(len(files)))
    else:
        log.info("\n  {} fichier(s) à supprimer.".format(len(files)))
        log.info("  Relancer avec --execute pour appliquer.")
