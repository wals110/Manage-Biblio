# Cahier de tests — Biblio v4.0

## Vue d'ensemble

Ce cahier couvre l'ensemble des cas de figure pour le projet Biblio.
Les tests sont organisés en deux catégories :

- **Tests automatiques** (`./tests/run_all.sh`) — exécutables sans SSD ni clé API
- **Tests manuels** — nécessitent le SSD externe et une clé API SiliconFlow

---

## A. Tests automatiques

### A1. Compilation (test_compile.py)

Vérifie que tous les fichiers Python du projet sont syntaxiquement corrects via `py_compile`. Détecte les parenthèses manquantes, les indentations cassées, les imports mal formés, etc. Ne détecte pas les erreurs d'exécution.

| # | Cas | Description | Vérifie |
|---|---|---|---|
| A1.1 | biblio.py compile | Compile le point d'entrée principal du CLI | Pas d'erreur de syntaxe dans biblio.py |
| A1.2 | Tous les lib/*.py compilent | Compile chacun des 8 modules du dossier lib/ (logger, profile, checkpoint, vision, utils, classifier, refiner, llm_mapper) | Aucun module cassé par un refactoring |
| A1.3 | organiser/*.py compilent | Compile les modules legacy du classifieur original (ocr_cover, biblio_organizer) | Les anciens modules restent valides |
| A1.4 | renommage/*.py compilent | Compile le moteur de renommage biblio_renamer.py | Le renamer est syntaxiquement correct |

### A2. Imports croisés (test_imports.py)

Vérifie que chaque module s'importe correctement et que les dépendances croisées entre modules fonctionnent. Va plus loin que la compilation : exécute réellement les `import` et vérifie que les fonctions attendues existent.

| # | Cas | Description | Vérifie |
|---|---|---|---|
| A2.1 | lib.logger | Importe setup_logger et get_logger depuis lib/logger.py | Le système de logging est chargeable |
| A2.2 | lib.profile | Importe Profile, list_profiles, init_profile depuis lib/profile.py | La gestion des profils YAML fonctionne |
| A2.3 | lib.checkpoint | Importe CheckpointManager depuis lib/checkpoint.py | Le système de reprise est disponible |
| A2.4 | lib.vision | Importe analyze_cover depuis lib/vision.py | L'analyse de couverture LLM est chargeable |
| A2.5 | lib.utils | Importe 6 fonctions utilitaires (sanitize_filename, build_new_filename, is_name_already_clean, collect_pdf_files, save_report, print_summary) | Toutes les fonctions utilitaires sont exportées |
| A2.6 | lib.classifier | Importe classify_by_theme, classify_combined, make_classify_fn, load_keyword_classifier | Le classifieur hybride est opérationnel |
| A2.7 | lib.refiner | Importe load_refinement_rules, scan_and_refine, save_refine_report, print_refine_summary | Le raffinement sous-catégories est chargeable |
| A2.8 | lib.llm_mapper | Importe LLMMapper, load_suggestions, apply_suggestions, save_suggestions_file | Le mapper LLM et les suggestions fonctionnent |
| A2.9 | biblio.py — fonctions existent | Vérifie avec hasattr() que les 19 fonctions attendues (process_single_file, scan_and_classify, _load_classifiers, _run_processing, etc.) existent dans le module biblio | Aucune fonction supprimée ou renommée par erreur lors d'un refactoring |
| A2.10 | biblio.py — fonctions callable | Vérifie avec callable() que chaque fonction est bien une fonction et pas un autre objet | Pas de variable écrasant une fonction par erreur |

### A3. Sécurité inbox (test_safety.py)

Teste la fonction `_check_inbox_safety()` qui empêche les configurations dangereuses où l'inbox pointe vers le même dossier que la cible ou le fallback. Sans cette protection, les fichiers seraient supprimés après "copie" vers eux-mêmes (bug critique découvert en session précédente).

| # | Cas | Description | Résultat attendu |
|---|---|---|---|
| A3.1 | inbox != target != fallback | Crée 3 dossiers temporaires distincts (INBOX, BIBLIO, BIBLIO/_A-TRIER) et appelle _check_inbox_safety | Passe sans erreur — configuration normale |
| A3.2 | inbox == target | Passe le même dossier comme inbox et target | sys.exit(1) — bloque car copier dans le même dossier puis supprimer = perte de données |
| A3.3 | inbox == fallback (_A-TRIER) | Passe le dossier _A-TRIER comme inbox | sys.exit(1) — bloque car les non-classifiés iraient dans l'inbox et seraient supprimés |
| A3.4 | Symlink inbox → target | Crée un symlink qui pointe vers target et le passe comme inbox | sys.exit(1) — détecte via os.path.realpath() que c'est le même dossier physique |
| A3.5 | Symlink inbox → fallback | Crée un symlink qui pointe vers le fallback | sys.exit(1) — même détection par realpath |
| A3.6 | Fallback custom (NON_CLASSE) | Teste avec un nom de fallback personnalisé au lieu de _A-TRIER | Détection correcte : inbox NON_CLASSE = fallback NON_CLASSE bloque, inbox INBOX ≠ fallback NON_CLASSE passe |

### A4. Copie et suppression (test_copy.py)

Teste les deux fonctions critiques du pipeline d'exécution : `_safe_remove_source()` (suppression sécurisée d'un fichier source après copie vérifiée) et `_copy_files()` (copie d'une liste de résultats vers la bibliothèque cible). Tous les tests utilisent des fichiers temporaires réels.

#### _safe_remove_source

Cette fonction ne supprime le fichier source QUE si : (1) source et destination sont des fichiers différents, (2) la destination existe, et (3) la taille de la destination correspond à celle de la source.

| # | Cas | Description | Résultat attendu |
|---|---|---|---|
| A4.1 | Copie vérifiée (même taille) | Crée src et dest avec le même contenu, appelle _safe_remove_source | Source supprimée, destination intacte — cas nominal |
| A4.2 | src == dest (même chemin) | Passe le même chemin comme source et destination | Pas de suppression — protège contre le cas inbox=target |
| A4.3 | Tailles différentes | Crée src (5 octets) et dest (24 octets) | Pas de suppression — la copie n'a pas été vérifiée |
| A4.4 | Destination absente | Crée src mais pas de dest | Pas de suppression — rien ne prouve que la copie a eu lieu |
| A4.5 | Source absente | Crée dest mais pas de src | Pas de suppression — rien à supprimer |
| A4.6 | Symlink vers même fichier | Crée un fichier réel et un symlink vers lui | Pas de suppression — realpath identique, supprimer le lien supprimerait l'original |

#### _copy_files

Cette fonction copie une liste de fichiers (résultats du pipeline) vers la bibliothèque, puis supprime les sources via _safe_remove_source. Elle gère aussi l'anti-collision (fichiers homonymes) et le renommage.

| # | Cas | Description | Résultat attendu |
|---|---|---|---|
| A4.7 | Fichier classifié → sous-dossier | Crée un fichier "algo.pdf" dans inbox, le copie avec destination "02-INFORMATIQUE/ALGO" | Fichier copié dans le bon sous-dossier, source supprimée de l'inbox |
| A4.8 | Renommage pendant copie | Fichier "old_name.pdf" avec renommage=True et nouveau_nom="Physique Quantique - Feynman.pdf" | Le fichier arrive à destination sous son nouveau nom |
| A4.9 | classify_only=True | Fichier avec renommage=True mais classify_only activé | Garde le nom original malgré renommage=True — utile pour la commande classify (pas de renommage) |
| A4.10 | Non-classifié → fallback | Fichier sans destination, copié avec dest_subdir="_A-TRIER" | Copié dans le dossier fallback _A-TRIER |
| A4.11 | Anti-collision (taille diff) | Un fichier "ml.pdf" existe déjà dans la destination avec un contenu différent | Crée "ml (2).pdf" au lieu d'écraser — fichiers différents avec le même nom |
| A4.12 | Anti-collision (même taille) | Un fichier "dup.pdf" existe déjà dans la destination avec exactement le même contenu | Skip "déjà présent" (done=0) — évite la duplication inutile |
| A4.13 | Source manquante | Le chemin source pointe vers un fichier qui n'existe pas | Skip gracieux sans erreur (done=0, cleaned=0) |
| A4.14 | Copie de 5 fichiers | Crée 5 fichiers dans inbox et les copie tous vers "08-LOISIRS/LITTERATURE" | 5 copiés, 5 sources supprimées — vérifie le comportement en batch |

### A5. Parser CLI (test_parser.py)

Teste que `_build_parser()` construit correctement le parser argparse avec toutes les sous-commandes, options, valeurs par défaut et raccourcis. Vérifie aussi que les options LLM ne "fuient" pas vers les commandes qui n'en ont pas besoin (ex: --workers sur rename).

| # | Cas | Description | Vérifie |
|---|---|---|---|
| A5.1 | Pas de commande | `parse_args([])` sans sous-commande | command = None — permet d'afficher l'aide |
| A5.2 | --version | `parse_args(['--version'])` | Affiche la version et quitte avec code 0 |
| A5.3 | process avec path | `parse_args(['process', '/tmp/inbox'])` | command='process', path='/tmp/inbox' |
| A5.4 | process sans path | `parse_args(['process'])` | path=None — utilisera l'inbox du profil par défaut |
| A5.5 | process toutes options | `parse_args(['process', '/tmp', '--profile', 'custom', '--execute', '--verbose', '--api-key', 'sk-test', '--workers', '8', '--max', '100', '--delay', '0.5', '--reset', '--retry-errors', '--reclassify', '--progress-file', 'custom.json'])` | Les 11 options sont correctement parsées avec les bonnes valeurs |
| A5.6 | classify basique | `parse_args(['classify'])` | Défauts corrects : path=None, execute=False, workers=0 |
| A5.7 | classify --execute | `parse_args(['classify', '--execute'])` | Flag execute activé |
| A5.8 | rename basique | `parse_args(['rename', '/tmp/src'])` | command='rename', path='/tmp/src' |
| A5.9 | rename --no-online --no-pdf | `parse_args(['rename', '/tmp', '--no-online', '--no-pdf'])` | Les deux flags de désactivation sont activés |
| A5.10 | rename n'a pas --workers | `parse_args(['rename', '--workers', '5'])` | SystemExit — rename n'hérite pas des options LLM |
| A5.11 | refine basique | `parse_args(['refine'])` | path=None (utilisera target du profil) |
| A5.12 | refine --execute --verbose | `parse_args(['refine', '--execute', '--verbose'])` | Les deux flags activés |
| A5.13 | profiles | `parse_args(['profiles'])` | command='profiles' — commande simple sans options |
| A5.14 | suggest review | `parse_args(['suggest'])` | apply=False — mode review par défaut |
| A5.15 | suggest --apply --execute | `parse_args(['suggest', '--apply', '--execute'])` | Les deux flags activés pour appliquer + reclassifier |
| A5.16 | init nom --target | `parse_args(['init', 'mon-profil', '--target', '/Volumes/SSD/LIB'])` | name='mon-profil', target='/Volumes/SSD/LIB' |
| A5.17 | init sans --target | `parse_args(['init', 'test'])` | SystemExit — --target est requis |
| A5.18 | --profile défaut | `parse_args(['classify'])` | profile='default' |
| A5.19 | --delay défaut | `parse_args(['classify'])` | delay=0.2 |
| A5.20 | --workers défaut | `parse_args(['classify'])` | workers=0 (0 = utiliser le défaut du profil) |
| A5.21 | -v raccourci | `parse_args(['classify', '-v'])` | verbose=True — raccourci de --verbose |
| A5.22 | -w raccourci | `parse_args(['classify', '-w', '4'])` | workers=4 — raccourci de --workers |

### A6. Rename LLM (test_rename_llm.py)

Teste l'intégration du LLM Vision comme fallback dans le pipeline de renommage. Le LLM n'est appelé que quand les 3 méthodes offline (nettoyage nom, ISBN, métadonnées PDF) échouent. Utilise des callbacks simulés (mock) au lieu de vrais appels API.

| # | Cas | Description | Résultat attendu |
|---|---|---|---|
| A6.1 | Parser accepte --llm | `parse_args(['rename', '/tmp', '--llm'])` | llm=True — le flag est reconnu par le parser |
| A6.2 | Parser accepte --llm --api-key | `parse_args(['rename', '/tmp', '--llm', '--api-key', 'sk-test'])` | api_key='sk-test' — nécessaire pour les appels LLM |
| A6.3 | Parser sans --llm | `parse_args(['rename', '/tmp'])` | llm=False, api_key=None — comportement par défaut inchangé |
| A6.4 | LLM retourne titre+auteur | Mock callback retourne `{title: 'Deep Learning Foundations', author: 'Ian Goodfellow'}`, fichier nommé '4829473829.pdf' | action=EXTRAIRE_LLM, nouveau nom contient "Deep Learning" et "Goodfellow" |
| A6.5 | LLM titre seul sans auteur | Mock callback retourne `{title: 'Introduction to Algorithms', author: ''}` | action=EXTRAIRE_LLM — un titre seul suffit pour renommer |
| A6.6 | LLM erreur API | Mock callback retourne `{error: 'api'}` | Fallback vers ECHEC — pas de crash, l'erreur est gérée |
| A6.7 | LLM erreur extraction | Mock callback retourne `{error: 'extraction'}` (couverture non extractible) | Fallback vers ECHEC |
| A6.8 | LLM exception (ConnectionError) | Mock callback lève `ConnectionError('API down')` | Pas de crash, fallback vers ECHEC — l'exception est attrapée par le try/except |
| A6.9 | LLM titre vide | Mock callback retourne `{title: '', author: ''}` | Fallback vers ECHEC — un titre vide ne peut pas servir au renommage |
| A6.10 | Sans callback (None) | Appel avec llm_callback=None | Comportement identique à avant l'ajout du LLM — rétrocompatibilité |
| A6.11 | Nom nettoyable → NORMALISER prioritaire | Fichier avec URL encoding dans le nom (ex: 'Introduction%20to%20ML.pdf'), mock LLM installé | action=NORMALISER, LLM pas appelé — les méthodes gratuites ont priorité |
| A6.12 | Parser accepte --force | `parse_args(['rename', '/tmp', '--force'])` | force=True — le flag est reconnu par le parser |
| A6.13 | Parser sans --force → défaut | `parse_args(['rename', '/tmp'])` | force=False — pas de force par défaut |
| A6.14 | Parser --llm --force combinés | `parse_args(['rename', '/tmp', '--llm', '--force'])` | Les deux flags activés — LLM en priorité + bypass is_name_clean |
| A6.15 | Parser accepte --max | `parse_args(['rename', '/tmp', '--max', '10'])` | max=10 — limite le nombre de fichiers |
| A6.16 | Parser accepte --pages | `parse_args(['rename', '/tmp', '--pages', '3'])` | pages=3 — nombre de pages à analyser par LLM |
| A6.17 | Parser --pages défaut | `parse_args(['rename', '/tmp'])` | pages=1 — une seule page par défaut |
| A6.18 | Sans --force, noms propres ignorés | Fichier "Titre - Auteur.pdf" analysé par scan() sans force | Fichier absent du rapport (INCHANGE) |
| A6.19 | Avec --force, noms propres analysés | Même fichier avec scan(force=True) | Fichier présent dans le rapport, re-analysé |
| A6.20 | --force + --llm renomme propre | Fichier propre avec mock LLM retournant un meilleur titre | action=EXTRAIRE_LLM, nouveau nom contient le titre du LLM |

---

## B. Tests manuels (Mac + SSD)

### Prérequis

- SSD monté sur `/Volumes/ExtSSD/BIBLIO`
- Dossier `_INBOX` créé avec des PDFs de test
- Variable `SILICONFLOW_API_KEY` exportée
- Se placer dans le répertoire du projet

### B1. Commande classify

Teste la classification LLM Vision avec différents modes (dry-run, exécution, reprise, parallélisme).

| # | Commande | Description | Vérifie |
|---|---|---|---|
| B1.1 | `./biblio.sh classify --max 5` | Lance la classification sur 5 fichiers en mode dry-run : envoie les couvertures au LLM, génère un rapport CSV mais ne copie rien | Rapport CSV généré dans logs/, pas de fichier déplacé |
| B1.2 | `./biblio.sh classify --max 5 --verbose` | Comme B1.1 mais avec les détails de chaque appel LLM (thème détecté, score, destination) | Détails LLM affichés pour chaque fichier |
| B1.3 | `./biblio.sh classify --max 5 --execute --reset` | Vide le checkpoint, reclassifie 5 fichiers, puis copie les classifiés vers BIBLIO et les non-classifiés vers _A-TRIER. Supprime les sources de _INBOX après copie vérifiée | Fichiers copiés, sources supprimées, _INBOX a 5 fichiers de moins |
| B1.4 | `./biblio.sh classify --max 5 --execute` (relance) | Relance sans --reset : le checkpoint contient les 5 fichiers déjà traités | Message "Tous les fichiers ont déjà été traités", rien à faire |
| B1.5 | `./biblio.sh classify --reset` | Supprime le fichier de checkpoint progress.json | Checkpoint vidé, la prochaine exécution repart de zéro |
| B1.6 | `./biblio.sh classify --retry-errors` | Remet en file d'attente uniquement les fichiers dont le status est erreur_api ou erreur_extraction | Seuls les fichiers en erreur sont retraités, les classifiés restent |
| B1.7 | `./biblio.sh classify --reclassify --execute` | Reclassifie les fichiers dans le checkpoint en utilisant le theme_mapping mis à jour, sans rappeler le LLM Vision (gratuit et instantané) | Fichiers re-mappés avec les nouveaux thèmes, pas d'appel API |
| B1.8 | `./biblio.sh classify --workers 5 --max 20` | Lance 5 threads parallèles pour traiter 20 fichiers simultanément | Pas de crash, pas de corruption du checkpoint, résultats cohérents |
| B1.9 | Ctrl+C pendant classify | Interrompre le traitement avec Ctrl+C pendant que les appels LLM sont en cours | Checkpoint sauvegardé avec la progression actuelle, relance reprend là où on s'est arrêté |

#### Vérifications post-exécution B1.3 :
- [ ] Fichiers classifiés présents dans les bons sous-dossiers de BIBLIO
- [ ] Fichiers non-classifiés dans `_A-TRIER`
- [ ] Fichiers source supprimés de `_INBOX`
- [ ] `ls _INBOX/ | wc -l` montre 5 fichiers de moins
- [ ] Rapport CSV lisible et cohérent

### B2. Commande process (pipeline complet)

Teste le pipeline complet qui enchaîne : classification LLM Vision → copie vers bibliothèque → raffinement sous-catégories.

| # | Commande | Description | Vérifie |
|---|---|---|---|
| B2.1 | `./biblio.sh process --max 3` | Pipeline complet en dry-run sur 3 fichiers : classification LLM, rapport, mais pas de copie ni raffinement réel | Les 3 étapes s'affichent (1/3, 2/3, 3/3), rapport généré |
| B2.2 | `./biblio.sh process --max 3 --execute` | Pipeline complet avec exécution réelle. Étape 1 : LLM Vision + classification. Étape 2 : copie vers BIBLIO + suppression inbox. Étape 3 : raffinement sous-catégories | Fichiers classifiés, copiés, sources supprimées, raffinement appliqué |
| B2.3 | `./biblio.sh process --max 3 --execute -y` | Comme B2.2 mais avec -y (--yes) qui saute les deux confirmations interactives (avant LLM et avant copie) | Aucune question posée, exécution directe |

#### Vérifications post-exécution B2.2 :
- [ ] Étapes 1/3, 2/3, 3/3 affichées
- [ ] Fichiers copiés et source supprimée
- [ ] Raffinement exécuté (ou "pas nécessaire")

### B3. Commande rename

Teste le renommage intelligent avec ses différents modes : offline (ISBN, métadonnées PDF), LLM Vision en fallback, et les options de désactivation.

| # | Commande | Description | Vérifie |
|---|---|---|---|
| B3.1 | `./biblio.sh rename /chemin` | Scan les PDFs du dossier, propose des renommages via nettoyage nom + ISBN + métadonnées PDF. Génère un rapport sans rien renommer | Rapport avec colonnes NORMALISER/EXTRAIRE_ISBN/EXTRAIRE_PDF/ECHEC |
| B3.2 | `./biblio.sh rename /chemin --execute` | Comme B3.1 puis applique les renommages proposés. Crée un log de renommage pour pouvoir annuler (--undo) | Fichiers renommés au format "Titre - Auteur.pdf" |
| B3.3 | `./biblio.sh rename /chemin --no-online` | Désactive la recherche ISBN via Google Books / Open Library. Seuls le nettoyage du nom et l'extraction PDF sont utilisés | Aucune requête HTTP vers les APIs ISBN |
| B3.4 | `./biblio.sh rename /chemin --no-pdf` | Désactive l'ouverture et l'extraction des métadonnées depuis le PDF. Utile pour les fichiers corrompus ou très lents | Seuls le nettoyage du nom et la recherche ISBN sont utilisés |
| B3.5 | `./biblio.sh rename /chemin --llm` | Active le LLM Vision comme étape de fallback. Pour chaque fichier où ISBN et métadonnées échouent, la couverture est envoyée au LLM pour identifier titre et auteur | Le résumé montre "Via LLM Vision : N" avec N > 0 |
| B3.6 | `./biblio.sh rename /chemin --llm --execute` | Comme B3.5 puis applique les renommages, y compris ceux trouvés par le LLM | Fichiers précédemment en ECHEC sont maintenant renommés grâce au LLM |
| B3.7 | `./biblio.sh rename /chemin --llm` sans API key | Lance --llm sans avoir exporté SILICONFLOW_API_KEY et sans --api-key | Message d'erreur clair demandant la clé API |
| B3.8 | `./biblio.sh rename /chemin --llm --force --verbose` | Active --force pour que le LLM soit appelé en priorité, même pour les fichiers "propres". Le verbose affiche le détail de chaque détection | Les fichiers propres sont re-analysés, résultat LLM affiché pour chaque fichier |
| B3.9 | `./biblio.sh rename /chemin --llm --pages 2 --max 6 --verbose` | Envoie les 2 premières pages (couverture + page titre) au LLM. Teste sur 6 fichiers | Le LLM détecte le vrai titre (pas le nom de la collection/série) |
| B3.10 | `./biblio.sh rename /chemin --llm --force --pages 3 --max 6` | Combine force + 3 pages. Idéal pour les collections Springer LNCS dont le vrai titre est sur la page intérieure | 6/6 fichiers détectés avec titres spécifiques au lieu du nom de collection |
| B3.11 | `./biblio.sh rename /chemin --max 10 --verbose` | Test limité à 10 fichiers sans LLM, mode verbose | Verbose affiche le résultat de chaque fichier (✅/❌/⏭) |

#### Vérifications post-exécution B3.5 :
- [ ] Le résumé montre une ligne "Via LLM Vision : N"
- [ ] Les fichiers ECHEC du mode normal sont récupérés par le LLM
- [ ] Les fichiers déjà nommables par NORMALISER/ISBN ne passent pas par le LLM

#### Vérifications post-exécution B3.9 (multi-pages) :
- [ ] Le log verbose affiche "pages 1-2" (et non "page 1")
- [ ] Les livres de collection (LNCS, etc.) obtiennent leur titre spécifique
- [ ] Le prompt multi-pages est utilisé (vérifiable via verbose)

### B4. Commande refine

Teste le raffinement qui déplace les fichiers depuis les catégories parentes vers les bonnes sous-catégories en analysant les mots-clés dans les noms de fichiers (basé sur refinement.yaml).

| # | Commande | Description | Vérifie |
|---|---|---|---|
| B4.1 | `./biblio.sh refine` | Scan toute la bibliothèque, identifie les fichiers qui sont dans une catégorie parente mais devraient être dans une sous-catégorie plus précise | Liste des fichiers à déplacer avec source → destination |
| B4.2 | `./biblio.sh refine --execute` | Comme B4.1 puis déplace réellement les fichiers vers les sous-catégories | Fichiers déplacés, rapport généré |

### B5. Commande suggest

Teste la gestion des suggestions de nouveaux dossiers, générées automatiquement quand le LLM Mapper ne trouve aucun dossier existant pour un thème.

| # | Commande | Description | Vérifie |
|---|---|---|---|
| B5.1 | `./biblio.sh suggest` | Affiche les suggestions en attente (stockées dans logs/suggestions.yaml) avec le thème, le dossier proposé et la raison | Liste formatée des suggestions, instructions pour modifier/appliquer |
| B5.2 | `./biblio.sh suggest --apply` | Crée les dossiers physiques sur le SSD et met à jour tree.yaml et theme_mapping.yaml avec les nouvelles entrées | Dossiers créés, fichiers YAML mis à jour, suggestions marquées "applied" |
| B5.3 | `./biblio.sh suggest --apply --execute` | Comme B5.2 puis reclassifie automatiquement les fichiers du checkpoint qui étaient non_classifié, en utilisant les nouveaux mappings | Fichiers reclassifiés avec les nouveaux dossiers |

### B6. Commandes utilitaires

Teste les commandes de gestion des profils.

| # | Commande | Description | Vérifie |
|---|---|---|---|
| B6.1 | `./biblio.sh profiles` | Liste tous les profils disponibles dans le dossier profiles/ avec leur nom, description et chemin cible | Affichage formaté : "default — Bibliothèque principale (cible: /Volumes/ExtSSD/BIBLIO)" |
| B6.2 | `./biblio.sh init test --target /tmp/test` | Crée un nouveau profil "test" avec les fichiers YAML squelettes (profile.yaml, tree.yaml, theme_mapping.yaml, categories.yaml, refinement.yaml) | Dossier profiles/test/ créé avec les 5 fichiers |
| B6.3 | `./biblio.sh init test --target /tmp/test` (doublon) | Tente de créer un profil qui existe déjà | Message d'erreur "Le profil 'test' existe déjà" |

### B7. Sécurité et cas limites

Teste les cas d'erreur, les fichiers problématiques et les limites du système. Ces tests vérifient que le programme ne crash pas et donne des messages d'erreur clairs.

| # | Cas | Description | Résultat attendu |
|---|---|---|---|
| B7.1 | classify avec inbox == target | Modifier temporairement profile.yaml pour que inbox pointe vers le même dossier que target | Bloqué par _check_inbox_safety avec message "SÉCURITÉ : inbox et target pointent vers le même dossier" |
| B7.2 | classify sans clé API | Lancer classify sans SILICONFLOW_API_KEY exportée et sans --api-key | Message "Clé API requise" |
| B7.3 | classify sur dossier vide | Créer un dossier vide et lancer classify dessus | "0 fichiers à traiter" |
| B7.4 | classify sur dossier inexistant | Passer un chemin qui n'existe pas | "Dossier introuvable" |
| B7.5 | Profil inexistant (--profile nope) | Utiliser un nom de profil qui n'existe pas dans profiles/ | "Profil 'nope' introuvable" avec la liste des profils disponibles |
| B7.6 | SSD non monté | Lancer une commande alors que le SSD externe n'est pas branché | Erreur de validation du profil (target n'existe pas) |
| B7.7 | PDF corrompu / protégé | Mettre un PDF protégé par mot de passe ou un fichier corrompu dans _INBOX | Status erreur_extraction, pas de crash du programme |
| B7.8 | Image PNG renommée en .pdf | Renommer une image PNG en .pdf et la mettre dans _INBOX | Status erreur_extraction ou non_identifié — pdf2image ne peut pas l'ouvrir |
| B7.9 | Fichier 0 octets | Créer un fichier vide nommé test.pdf | Status erreur_extraction, pas de crash |
| B7.10 | Nom avec caractères spéciaux (é, ñ, 日本) | Mettre un PDF nommé "Résumé — café日本語.pdf" dans _INBOX | Copie correcte vers la destination, pas d'erreur d'encodage |
| B7.11 | Nom très long (> 180 chars) | Mettre un PDF avec un nom de plus de 180 caractères | Nom tronqué à MAX_FILENAME_LEN sans crash (macOS limite à 255) |
| B7.12 | Deux fichiers identiques (même nom, même taille) | Copier un fichier qui existe déjà à destination avec exactement la même taille | Skip "déjà présent", source supprimée de l'inbox |
| B7.13 | Deux fichiers même nom, taille différente | Copier un fichier dont le nom existe à destination mais avec un contenu différent | Anti-collision : crée "fichier (2).pdf" à destination |

### B8. Confirmations

Teste le système de double confirmation qui protège contre les opérations coûteuses (appels LLM) et irréversibles (copie + suppression).

| # | Cas | Description | Résultat attendu |
|---|---|---|---|
| B8.1 | classify --execute sans -y | Lancer classify --execute en mode normal (sans --yes) | Deux confirmations : "Lancer le traitement ? (o/N)" avant les appels LLM, puis "Continuer ? (o/N)" avant la copie |
| B8.2 | classify --execute -y | Lancer classify --execute -y (ou --yes) | Aucune confirmation, exécution directe — utile pour les scripts automatisés |
| B8.3 | Répondre "N" à la 1ère confirmation | Lancer classify --execute, répondre "N" à "Lancer le traitement ?" | Annulé immédiatement, aucun appel LLM effectué, pas de coût API |
| B8.4 | Répondre "N" à la 2ème confirmation | Lancer classify --execute, répondre "o" à la 1ère confirmation, "N" à "Continuer ?" | Classification LLM effectuée + rapport CSV généré, mais aucun fichier copié ni supprimé |

---

## Lancement des tests automatiques

```bash
# Tous les tests (74+ tests)
./tests/run_all.sh

# Mode verbeux (détail de chaque test)
./tests/run_all.sh -v

# Un seul module
./tests/run_all.sh test_compile
./tests/run_all.sh test_imports
./tests/run_all.sh test_safety
./tests/run_all.sh test_copy
./tests/run_all.sh test_parser
./tests/run_all.sh test_rename_llm
```
