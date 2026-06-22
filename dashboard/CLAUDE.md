# Dashboard — Tests fonctionnels + Cockpit Biblio Klodo

FastAPI + Jinja2 + HTMX + Chart.js + SSE, dark theme. Point d'entrée : `uv run python -m dashboard` (port 8080) — exécute [`dashboard/__main__.py`](__main__.py) qui charge `.env` puis lance `uvicorn.run("dashboard.app:app", port=8080)`. **`-m dashboard.app` ne démarre PAS le serveur** (`app.py` n'a pas de bloc `__main__`). Alternative : `./klodo.sh dashboard`.

## Architecture

- **Nav-bar 7 onglets** : Overview · Tests · Curation · Baseline · Taxonomie · Logs · Admin (refactor UX juin 2026 : 12 → 7) + entrée séparée « ➕ Nouveau profil » → `/onboarding` (assistant d'onboarding)
- **2 hubs avec sub-tabs server-rendered** (`?view=X`) :
  - `/tests?view=` → exec | rapports | comparer | metriques | historique
  - `/baseline?view=` → disagreements | suggestions
- **Anciennes routes redirigées 301** (préserve les query params) : `/rapports`, `/comparer`, `/metriques`, `/historique` → `/tests?view=X` ; `/suggestions` → `/baseline?view=suggestions`
- **Routes** : [app.py](app.py)
- **Couche données** : [data.py](data.py) — fusion YAML + JSON + DuckDB + CSV + helpers viewer
- **Module Overview cockpit** : [overview.py](overview.py) — 16 cartes + 2 builders (`build_profile_snapshot`, `build_general_snapshot`) + cache mémoire 30 s thread-safe
- **Module Taxonomie** : [taxonomy.py](taxonomy.py) — agrégation `tree.yaml + theme_mapping.yaml + vision_cache.json` + écritures sécurisées + cascade rename → categories
- **Module Catégories** : [categories.py](categories.py) — KeywordClassifier YAML, suggest_target_path pour orphelins
- **Module Dédupli** : [dedupli.py](dedupli.py) — canonisation des thèmes long-tail (utilise `lib/theme_canon.py`)
- **Modules Agent Refonte** : [agent_refonte.py](agent_refonte.py) (Phases A + B) + [agent_refonte_phase_c.py](agent_refonte_phase_c.py) (Phase C dialog/mutations — **UI désactivée** depuis 2026-06-07 via `PHASE_C_UI_ENABLED = False`, code conservé pour réactivation future) + [agent_refonte_apply.py](agent_refonte_apply.py) (Apply/Execute — voir ci-dessous) — délègue à `agents/refonte/`
- **Module Baseline** : [baseline.py](baseline.py) — validation manuelle des désaccords prédiction Klodo vs placement actuel

### Apply/Execute (application d'une refonte Phase B)

- **`agent_refonte_apply.py`** — adoption de la config proposée (promotion
  des YAML `proposed/` vers la prod, snapshot `agent_backup`, mkdir des
  créations) puis exécution des déplacements physiques (projection figée,
  exclusion zone de doute via `select_move_rows`, journal
  `lib/move_journal.py`, skip stale/collision/error + rapport CSV dans
  `logs/rapport_apply_*.csv`). État par run dans
  `profiles/<p>/.cache/refonte/<run_id>/apply/{state,status}.json`.
- Gating : ① avant ② ; restore-config bloqué tant que les moves ne sont
  pas annulés ; un seul run adopté à la fois ; lock `.cache/taxonomy.lock`
  posé pendant les moves (writes mappings/tree → 423 ; categories.yaml et
  renames ne sont pas fencés — les moves concernés finissent en skip stale,
  rapportés) ; garde anti-traversal
  (`_safe_target_subdir`, validation `run_id`).
- Routes : `GET …/apply/{run_id}/preview|status`,
  `POST …/apply/adopt|restore-config|execute|undo-moves`.
- UI : bloc « Application » (stepper 2 étapes) au-dessus des onglets
  Verdict/Arbre/Plan d'un run Phase B done.

### Apply global (synchroniser la bibliothèque avec la config live)

- **`apply_engine.py`** — moteur de déplacement source-agnostique (extrait de
  l'apply refonte) : `execute_move_batch(target, moves, profile_dir, on_progress)`,
  garde-fous `safe_target_subdir`/`prune_empty_dirs`, `spawn`, `target_path`,
  `ApplyError`. Partagé par refonte ET global — un seul exemplaire du code de
  déplacement dangereux.
- **`reclassify_apply.py`** — apply global (couche B seule, pas d'adoption) :
  preview (projection live `taxonomy.build_reclassify_projection` figée en
  `projection.csv`) → execute (rejoue le CSV via apply_engine) → undo
  (`move_journal.undo_batch`). État global par profil
  `.cache/reclassify/apply/{state,status,projection.csv}`. Détection
  « en attente » via hash de config (theme_mapping+categories+tree+theme-canon,
  instantané, sans scan). Déterministe P1+P2 (P1 par défaut, P2 opt-in),
  canonicalisation appliquée.
- Routes : `GET/POST /api/taxonomy/reclassify/apply/{preview,pending,status,execute,undo}`.
- UI : modale « Voir ce qui bougerait » (toolbar Mappings) étendue en
  preview→appliquer→progression→annuler + carte « Bibliothèque » dans Overview.
- Fencing : `categories.py` vérifie désormais `.cache/taxonomy.lock` (423 pendant
  un apply).

### Apply global du RENAME (renommer toute la bibliothèque)

- **`rename_apply.py`** — symétrique de `reclassify_apply` mais pour les **noms**
  de fichiers (le basename, pas le dossier) : preview (audit complet → fige la
  liste des renommages **placeholder + divergents**, hors casse-seule et hors
  « marqués OK », dans `projection.csv`) → execute (rejoue via
  `rename.commit_rename_bulk` = UN batch journalisé, collisions/manquants
  skippés et rapportés) → undo (`rename.undo_batch_for_profile`). État par profil
  `.cache/rename/apply/{state,status,projection.csv}`. Job en thread daemon +
  polling `status.json`.
- Routes : `GET /api/rename/apply/status`, `POST /api/rename/apply/{preview,execute,undo}`.
- UI : bouton **🚀 Tout renommer** (toolbar sub-tab Rename) → confirm avec
  compteurs → application → toast → propose d'annuler le lot.
- ⚠ Asymétrie connue **classification vs rename** : ce sont deux moteurs distincts
  (`apply_engine`/`move_journal` déplacent ; `renamer`/`rename_journal` renomment).
  L'onboarding ne renomme PAS — pour appliquer les titres détectés par la Vision,
  passer par ce « Tout renommer » (ou `./klodo.sh rename --execute`).

### Agent Onboarding (bootstrap d'un profil depuis un répertoire brut)

- **`agents/onboarding/`** (pipeline, PAS un agent ReAct/LangGraph) :
  `scan.py` (scan + estimation coût, read-only), `taxonomy_llm.py` (le LLM
  assigne un domaine à CHAQUE cluster par lots de 40, matching par numéro →
  structure SECTION/Thème scalable, couverture quasi-totale ; sections issues du contenu,
  forme suivant des conventions **paramétrables** par l'utilisateur via
  `onboarding_options` : profondeur min/max (1-3), numérotation des sections
  (`01-…`), casse (`title`/`upper`/`lower` — `title` préserve les acronymes),
  séparateur des mots composés (`-`/`_`/`none`), langue (auto/fr/en),
  granularité (auto/compact/detailed). Défauts = 1-2 niveaux, Casse Titre,
  tirets, sections numérotées, `_A-TRIER` résiduel, `_INBOX` réservé.
  `_format_segment` applique casse+séparateur ; `_sanitize_folder` borne la
  profondeur. `with_structured_output(..., method="function_calling")` (compat GLM/Qwen)),
  `proposition.py` (orchestration :
  `run_vision` Vision full-corpus reprenable → `cluster_corpus` via
  `lib/theme_canon` → `propose_taxonomy` → `propose_categories` via
  `agents/refonte/categories_llm` → `write_proposal` : écrit les 3 YAMLs après
  backup + dry-run de couverture via `reclassify_dryrun`).
- **`agent_onboarding.py`** : wrapper thread+poll (scan synchrone,
  `start_onboarding` crée le profil **brouillon** + lance l'analyse en thread
  daemon, `get_status`, `finalize`). État par run dans
  `profiles/<p>/.cache/onboarding/<run_id>/status.json`. Noms de profil/run_id
  validés anti-traversal (`_is_safe_segment`).
- Profil **brouillon** : flag `onboarding_draft: true` dans `profile.yaml`
  (`lib/profile.create_draft_profile` / `set_onboarding_draft`) → badge sidebar,
  bandeau Taxonomie/Mappings et bouton **Finaliser** (retire le flag).
- Routes : `POST …/onboarding/{scan,start,finalize}`, `GET …/onboarding/status`,
  `GET …/onboarding/is-draft`, `GET /api/fs/browse` (explorateur de dossiers
  serveur — sous-dossiers + n_files, local-only 127.0.0.1, pour le sélecteur
  « 📂 Parcourir » de l'étape Configuration). Page assistant `/onboarding`
  (stepper 3 étapes : scan/estimation → progression Vision → couverture) + entrée nav
  « ➕ Nouveau profil ». À l'étape 3, bouton **🚀 Déplacer les fichiers classés**
  (frontend only) : appelle l'Apply global reclassify existant
  (`/api/taxonomy/reclassify/apply/{preview,execute,status,undo}`, keyword=true)
  sur le profil brouillon → preview/confirm → déplace les fichiers ayant une
  destination (les sans-destination restent en place), annulable. Puis handoff
  « Continuer dans Mappings » pour le raffinage.
- Réutilise : `lib.vision`, `vision_cache`, `theme_canon`, `categories_llm`,
  `reclassify_dryrun`, `init_profile`.

- **Backend tests fonctionnels** : DuckDB via [../tests/functional/db.py](../tests/functional/db.py) — tables `runs`, `series_results`, `check_results`, `manual_validations`
- **Templates** : 10 templates principaux + ~12 partials (`partials/tests_*.html`, `partials/baseline_*.html`, `partials/overview_*.html`)
- **Macros réutilisables** dans `templates/macros/widgets.html` : `kpi_card`, `kpi_card_big`, `status_badge`, `run_banner`, `pagination`, `filter_bar`, `data_table`, `mini_bar`, `activity_timeline`, `subtabs_header`
- **Static JS** : modules dans `static/js/` (`common.js`, `validation.js`, `filters.js`, `tests.js`, `admin.js`, `viewer.js`, `taxonomy.js`, `taxonomy_categories.js`, `taxonomy_rename.js`) — tout extrait des templates
- **Static CSS** : `static/style.css` (dark theme + composants viewer + treemap D3 + progress bar inline + classes `.dash-*` pour le cockpit Overview)

## Patterns clés
- **Monitoring temps réel** : thread background lit stdout du subprocess runner
- **SSE (Server-Sent Events)** : `/api/events` (tests fonctionnels) et `/api/viewer/events` (génération thumbnails) remplacent l'ancien polling
- **Runs nommés aléatoirement** : adjectif-nom ("cosmic-koala") avec branche + commit git
- **Validation manuelle** : boutons Oui/Non pour les checks `manual_check`, badge "Validation manuelle" une fois validé
- **Fusion données** : `get_merged_test_view()` fusionne tests.yaml + rapport ; `apply_db_statuses()` préserve les validations DuckDB
- **Métriques** : DuckDB `read_csv_auto()` — SQL direct sur les CSV, pas de parsing Python
- **Comparaison de runs** : check-by-check entre run courant et ancien run
- **Admin** : gestion clés API (.env), stats système, gestion des runs
- **Path traversal guard** : `_is_safe_file_path()` valide tous les chemins file servis (rapports, logs, thumbnails)

## Onglet Curation (viewer double panneau)
- **Rôle** : constituer des jeux de tests sur mesure par copie sélective de fichiers entre profils
- **Layout** : panneau **source** (tous profils, lecture seule) + panneau **destination** (test/test-local uniquement) — empilés verticalement
- **Marquage** : bouton `+` à droite de chaque fichier source, sélection persistée en `sessionStorage`
- **Copie** : bouton "Copier sélection (N) → destination" déclenche `POST /api/viewer/copy` (path traversal bloqué, profils destination bridés à `test`/`test-local`)
- **Vider destination** : supprime uniquement `.pdf`/`.epub`, **préserve** `.thumbnail-cache/`
- **Thumbnails** : générés à la demande dans `{INBOX}/.thumbnail-cache/{stem}/{1..n}.jpg` (sous-dossier par document)
- **Multi-pages** : sélecteur "Pages 1-4" dans le header destination ; côté source toujours 1 page mais affiche tout ce qui est en cache
- **Navigateur de pages** : flèches ←/→ sous chaque thumbnail (caché si N=1)
- **"Générer manquants"** : complète chaque fichier jusqu'à N pages (option B)
- **"Régénérer tout"** : vide complètement et regenere
- **Génération en parallèle** : `ProcessPoolExecutor` avec `_THUMBNAIL_BATCH_WORKERS = 4` pour vrai parallélisme CPU (contourne le GIL)
- **Progress bar inline** : intégrée sous le header de chaque panneau (bleu en cours → vert "Terminé")
- **Mockup statique** : `/viewer-mockup` reste accessible comme référence visuelle
- **Logs viewer** (`/logs`) : page séparée pour visualiser les rapports CSV utilisateur dans `logs/`

## Onglet Overview (cockpit Biblio, refactor juin 2026)

- **Rôle** : cockpit profile-aware. Moitié haute = 12 cartes par profil (sélecteur dropdown), moitié basse = 4 cartes générales tous profils
- **URL** : `/?profile=X&refresh=0|1` — fallback transparent si profil inconnu, `?refresh=1` invalide le cache 30 s
- **Layout B hiérarchique** : 4 KPI vedettes (Fichiers, Classifiés, Coût LLM, Health) + bloc Détails + grilles 2-col (charts + activité)
- **Cartes profile-aware** : `files_count`, `classified_rate`, `folders_count`, `llm_cost`, `health` (orphans + locks), `vision_cache`, `inbox`, `baseline_runs`, `agent_sessions`, `top_themes` (vision cache aggregation), `top_folders` (FS walk), `recent_activity` (mtime backups + rename-journal)
- **Cartes générales** : `llm_models`, `api_keys`, `profiles_list`, `global_cost` (pie chart Chart.js — pie segments < 1 % filtrés)
- **Cache mémoire** : `_overview_cache: dict[profile, (ts, snapshot)]` avec TTL 30 s + lock thread-safe
- **Sentinelles d'erreur** affichées dans la card : `target_missing`, `profile_missing`, `tree_missing`, `tree_invalid`
- **Bouton 🔄 refresh** : link `?refresh=1` invalide le cache du profil sélectionné

## Onglet Tests (hub avec 5 sub-tabs)

- **URL** : `/tests?view=exec|rapports|comparer|metriques|historique` — defaults à `exec`
- **Server-render Jinja** : chaque sub-tab a son partial (`partials/tests_*.html`). Rendering 100 % serveur, pas de fetch JS ni HTMX swap.
- **Macro `subtabs_header`** dans `widgets.html` génère les `<a class="dash-subtab">` selon `current_view`
- **Sub-tab Exécution** : liste des phases/series avec actions de run et monitoring SSE temps réel
- **Sub-tab Rapports** : viewer CSV avec filtres + pagination + tri (anciennement `/rapports`)
- **Sub-tab Comparer** : diff check-by-check entre 2 runs (anciennement `/comparer`)
- **Sub-tab Métriques** : agrégations DuckDB (classify, rename, refine) — anciennement `/metriques`
- **Sub-tab Historique** : timeline des runs avec deltas (anciennement `/historique`)
- **Redirects 301** sur les anciennes URLs avec préservation des query params

## Onglet Baseline (hub avec 2 sub-tabs)

- **URL** : `/baseline?view=disagreements|suggestions` — defaults à `disagreements`
- **Sub-tab Désaccords** : interface de validation manuelle pour arbitrer les divergences prédiction Klodo vs placement actuel des fichiers (1 fichier par écran avec K/A/N/S keyboard shortcuts)
- **Sub-tab Suggestions** : viewer des suggestions LLM Mapper (anciennement `/suggestions`)
- **Redirect** : `/suggestions` → `/baseline?view=suggestions`

## Onglet Taxonomie (5 sub-tabs)

- **Rôle** : cockpit pour piloter la taxonomie — visualiser l'arborescence, voir l'univers des thèmes LLM, ajouter des mappings sans éditer le YAML à la main
- **Sub-tabs** :
  - **Mappings** : la vue historique (tree + viewer + treemap + thèmes mappés/LLM)
  - **Catégories** : éditeur du `categories.yaml` (KeywordClassifier) avec badge ⚠ orphelin + bouton 💡 Suggérer
  - **Rename** : audit + commit des renommages
  - **🤖 Refonte** : agent IA — sub-tab affiche **Phases A + B** (diagnostic + proposition). La Phase C (dialog conversationnel + mutations YAML directes) existe en code dans `agents/refonte/dialog.py + mutations.py` mais son UI a été désactivée le 2026-06-07 (chat trop limité, voir PR #170) — pour réactiver : `PHASE_C_UI_ENABLED = True` dans `agent_refonte_phase_c.py`
  - **🔗 Dédupli** : canonisation des thèmes LLM long-tail (cf. `dedupli.py` + `lib/theme_canon.py`)
- **Trois sources agrégées sub-tab Mappings** : `tree.yaml` (arborescence) + `theme_mapping.yaml` (mapping thème → dossier) + `vision_cache.json` (thèmes LLM bruts, filtré `confidence ≥ 0.5`)
- **Layout 3 colonnes Mappings** : tree gauche (30%) + center stack (45%, viewer/card LLM/treemap en grid fixe 55/20/25) + thèmes droite (25%, mappés top / LLM universe bottom)
- **Routage 3-way** dans Mappings : ✓ Stables / → Entrants / ← Sortants — visualise l'impact d'un futur reclassify sur un dossier
- **Cascade rename folder → categories.yaml** : un rename de folder dans le tree propage automatiquement les `chemin:` dans `categories.yaml` (merge intelligent en cas de collision)
- **Badge ⚠ orphelin** dans Catégories : signale les entries dont la cible n'existe plus dans tree.yaml + bouton **💡 Suggérer** (matching normalisé par préfixe numérique)
- **Bulk-delete** multi-thèmes mappés sur un folder (toolbar de sélection avec checkboxes)
- **Tree** : dossiers + fichiers lazy-loaded (50 par page, bouton "Afficher N de plus"), chevron sur tout dossier expandable (sous-dossiers OU fichiers > 0)
- **Viewer PDF** : cover seule par défaut + bouton "+ Voir pages 2-N" pour multipage à la demande (thumbnails via [../lib/thumbnail.py](../lib/thumbnail.py), cap 5 pages)
- **Card Analyse LLM** : toujours visible, affiche titre / auteur / langue / thèmes détectés + destinations theme-only + emplacement actuel + prédiction `classify_by_theme` étiquetée
- **Treemap 1-niveau** : enfants directs du dossier actif (drill-down sur clic), rectangle virtuel "(directs)" pour les fichiers stockés directement dans un dossier mixte
- **Toggle Lissé/Réel** : sqrt + floor 8% par défaut pour gérer les disparités extrêmes (Autres 4500 vs autres 5), bascule en linéaire fidèle aux proportions
- **Highlight sélection** : quand un fichier est sélectionné, le rectangle de son dossier parent est mis en évidence dans le treemap
- **Ajout mapping** : **drag-drop** thème LLM → dossier (arbre ou treemap) OU bouton **"+ Mapper"** au hover avec popover autocomplete sur les dossiers
- **Affectation en lot** (colonne Thèmes LLM) : checkboxes de multi-sélection + toolbar avec 2 actions :
  - **Mapper la sélection → dossier** : choisir un dossier (autocomplete) → tous les thèmes cochés y sont affectés via `POST /api/taxonomy/mappings/bulk-add` (`add_mappings_bulk` : backup unique, skip rapporté des déjà-mappés, lock 423)
  - **💡 Suggérer + Mapper** : `POST /api/taxonomy/mappings/suggest` (`suggest_mappings`) → moteur : (1) passe **déterministe fiable** = matching exact nom de thème ↔ dernier segment de dossier (haute précision, le KeywordClassifier a été retiré car imprécis — il matchait des mots-clés incidents, ex. « Colloid Science » → Géométrie via « surface »), (2) passe **LLM batché** = `LLMMapper.resolve_batch` (N thèmes en 1 appel chunké de 40, borné `max_llm`, no-op sans clé API) pour les ambigus → panneau de revue (badge source déterministe/LLM/non-résolu, confiance, dossier éditable inline, non-résolus décochés) → Appliquer les acceptés en bulk-add

### Arbre Mappings : reflet du disque (config ↔ disque)

- `get_snapshot` construit l'arbre depuis l'**union** de `tree.yaml` (config) +
  du **scan disque** (`_scan_disk` : un seul `os.walk` du target, dossiers cachés
  exclus) + des **parents implicites** (pour `a/b/c`, on recrée `a` et `a/b`).
  Chaque nœud porte `in_config`/`on_disk` → l'UI marque 3 états : normal,
  **« hors config »** (sur disque, absent de tree.yaml), **« non créé »** (dans
  tree.yaml, absent du disque).
- `snap["folders"]` reste **config-only** (= cibles de mapping valides) ; l'union
  ne sert qu'à l'affichage de l'arbre. Un dossier hors-config n'est mappable
  qu'après **adoption**.
- `adopt_folder(profile, path)` (+ `POST /api/taxonomy/folder/adopt`) ajoute un
  dossier disque hors-config (+ parents implicites) à `tree.yaml` (lock + backup),
  via le bouton « Adopter » sur le nœud. Corrige le bug : un `tree.yaml`
  « leaf-only » (enfants sans parents déclarés) masquait des sections pourtant
  présentes sur le disque.

- **Toast d'impact** : `✓ "X" → /Y · N fichiers au prochain reclassify` (N pris dans `themes_llm[theme].count`)
- **Backup auto** : avant chaque write, `theme_mapping.yaml` copié dans `profiles/<p>/.cache/taxonomy-backups/theme_mapping-YYYYMMDD-HHMMSS.yaml` (rotation 20)
- **Lock concurrence** : `.taxonomy.lock` bloque les writes pendant un baseline_run ou autre tâche externe
- **Validation pré-write** : refuse les doublons (409), folders inconnus (400), thèmes vides (400)
- **Sélecteur profil** : header, change le profil actif (snapshot + cache memoire isolés par profil)

## Tests

- **63 tests** dans [../tests/auto/test_dashboard.py](../tests/auto/test_dashboard.py) (routes + data.py + hubs + redirects 301)
- **70 tests** dans [../tests/auto/test_overview.py](../tests/auto/test_overview.py) (16 cards + 2 builders + cache TTL + 3 macros)
- **233 tests** dans [../tests/auto/test_taxonomy.py](../tests/auto/test_taxonomy.py) (write safety + agrégation snapshot + endpoints HTTP + cascade rename + breakdown 3-way + bulk-delete + bulk-add + suggest hybride + disk-reflect + adopt_folder)
- **104 tests** dans [../tests/auto/test_categories.py](../tests/auto/test_categories.py) (CRUD entries + suggest_target_path + orphan detection + lock fencing)
- **9 tests** dans [../tests/auto/test_viewer_copy.py](../tests/auto/test_viewer_copy.py) (copy + clear destination, path traversal, refus de `default`)
- **31 tests** dans [../tests/auto/test_thumbnail.py](../tests/auto/test_thumbnail.py) (PDF, ePub2/3, placeholder, count_pages, clear_cache mixed format)
- **51 tests** dans [../tests/auto/test_refonte_apply.py](../tests/auto/test_refonte_apply.py) (adopt, execute, undo-moves, restore-config, preview, status, gating, anti-traversal)
- **16 tests** dans [../tests/auto/test_reclassify_apply.py](../tests/auto/test_reclassify_apply.py) (preview, execute, undo, pending, status, hash config, fencing 423)
- **9 tests** dans [../tests/auto/test_rename_apply.py](../tests/auto/test_rename_apply.py) (apply global rename : preview périmètre placeholder+divergent, execute rejoue la projection figée, undo, 409 sans preview, endpoints)
- **38 tests** dans [../tests/auto/test_agent_onboarding.py](../tests/auto/test_agent_onboarding.py) (draft profile + modèle Vision actif, scan/estimate, run_vision reprenable + no-cache des erreurs, cluster_corpus, propose_taxonomy + options de forme, propose_categories, write_proposal + dry-run, warnings Vision/proposition, onboarding_options bout-en-bout, wrapper thread/poll, routes scan/start/status/finalize, anti-traversal)
