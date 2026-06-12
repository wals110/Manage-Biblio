# Dashboard — Tests fonctionnels + Cockpit Biblio Klodo

FastAPI + Jinja2 + HTMX + Chart.js + SSE, dark theme. Point d'entrée : `uv run python -m dashboard.app` (port 8080).

## Architecture

- **Nav-bar 7 onglets** : Overview · Tests · Curation · Baseline · Taxonomie · Logs · Admin (refactor UX juin 2026 : 12 → 7)
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
- **Modules Agent Refonte** : [agent_refonte.py](agent_refonte.py) (Phases A + B) + [agent_refonte_phase_c.py](agent_refonte_phase_c.py) (Phase C dialog/mutations — **UI désactivée** depuis 2026-06-07 via `PHASE_C_UI_ENABLED = False`, code conservé pour réactivation future) — délègue à `agents/refonte/`
- **Module Baseline** : [baseline.py](baseline.py) — validation manuelle des désaccords prédiction Klodo vs placement actuel
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
- **Toast d'impact** : `✓ "X" → /Y · N fichiers au prochain reclassify` (N pris dans `themes_llm[theme].count`)
- **Backup auto** : avant chaque write, `theme_mapping.yaml` copié dans `profiles/<p>/.cache/taxonomy-backups/theme_mapping-YYYYMMDD-HHMMSS.yaml` (rotation 20)
- **Lock concurrence** : `.taxonomy.lock` bloque les writes pendant un baseline_run ou autre tâche externe
- **Validation pré-write** : refuse les doublons (409), folders inconnus (400), thèmes vides (400)
- **Sélecteur profil** : header, change le profil actif (snapshot + cache memoire isolés par profil)

## Tests

- **63 tests** dans [../tests/auto/test_dashboard.py](../tests/auto/test_dashboard.py) (routes + data.py + hubs + redirects 301)
- **70 tests** dans [../tests/auto/test_overview.py](../tests/auto/test_overview.py) (16 cards + 2 builders + cache TTL + 3 macros)
- **207 tests** dans [../tests/auto/test_taxonomy.py](../tests/auto/test_taxonomy.py) (write safety + agrégation snapshot + endpoints HTTP + cascade rename + breakdown 3-way + bulk-delete + bulk-add + suggest hybride)
- **103 tests** dans [../tests/auto/test_categories.py](../tests/auto/test_categories.py) (CRUD entries + suggest_target_path + orphan detection)
- **9 tests** dans [../tests/auto/test_viewer_copy.py](../tests/auto/test_viewer_copy.py) (copy + clear destination, path traversal, refus de `default`)
- **20 tests** dans [../tests/auto/test_thumbnail.py](../tests/auto/test_thumbnail.py) (PDF, ePub2/3, placeholder, count_pages, clear_cache mixed format)
