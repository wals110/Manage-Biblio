# Dashboard — Tests fonctionnels + Curation Klodo

FastAPI + Jinja2 + HTMX + Chart.js + SSE, dark theme. Point d'entrée : `uv run python -m dashboard.app` (port 8080).

## Architecture
- **Routes** : [app.py](app.py) — 12 pages (Overview, Tests, Rapports, Comparer, Métriques, Historique, Logs, Curation, Baseline, **Taxonomie**, Suggestions, Admin)
- **Couche données** : [data.py](data.py) — fusion YAML + JSON + DuckDB + CSV + helpers viewer
- **Module Taxonomie** : [taxonomy.py](taxonomy.py) — agrégation `tree.yaml + theme_mapping.yaml + vision_cache.json` + écritures sécurisées
- **Backend tests fonctionnels** : DuckDB via [../tests/functional/db.py](../tests/functional/db.py) — tables `runs`, `series_results`, `check_results`, `manual_validations`
- **Templates** : 13+ templates avec **macros réutilisables** dans `templates/macros/widgets.html` (kpi_card, status_badge, run_banner, pagination, filter_bar, data_table)
- **Static JS** : modules dans `static/js/` (`common.js`, `validation.js`, `filters.js`, `tests.js`, `admin.js`, `viewer.js`, `taxonomy.js`) — tout extrait des templates
- **Static CSS** : `static/style.css` (dark theme + composants viewer + treemap D3 + progress bar inline)

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

## Onglet Taxonomie (Phase 1)

- **Rôle** : cockpit pour piloter la taxonomie — visualiser l'arborescence, voir l'univers des thèmes LLM, ajouter des mappings sans éditer le YAML à la main
- **Trois sources agrégées** : `tree.yaml` (arborescence) + `theme_mapping.yaml` (mapping thème → dossier) + `vision_cache.json` (thèmes LLM bruts, filtré `confidence ≥ 0.5`)
- **Layout 3 colonnes** : tree gauche (30%) + center stack (45%, viewer/card LLM/treemap en grid fixe 55/20/25) + thèmes droite (25%, mappés top / LLM universe bottom)
- **Tree** : dossiers + fichiers lazy-loaded (50 par page, bouton "Afficher N de plus"), chevron sur tout dossier expandable (sous-dossiers OU fichiers > 0)
- **Viewer PDF** : cover seule par défaut + bouton "+ Voir pages 2-N" pour multipage à la demande (thumbnails via [../lib/thumbnail.py](../lib/thumbnail.py), cap 5 pages)
- **Card Analyse LLM** : toujours visible, affiche titre / auteur / langue / thèmes détectés + destinations theme-only + emplacement actuel + prédiction `classify_by_theme` étiquetée
- **Treemap 1-niveau** : enfants directs du dossier actif (drill-down sur clic), rectangle virtuel "(directs)" pour les fichiers stockés directement dans un dossier mixte
- **Toggle Lissé/Réel** : sqrt + floor 8% par défaut pour gérer les disparités extrêmes (Autres 4500 vs autres 5), bascule en linéaire fidèle aux proportions
- **Highlight sélection** : quand un fichier est sélectionné, le rectangle de son dossier parent est mis en évidence dans le treemap
- **Ajout mapping** : **drag-drop** thème LLM → dossier (arbre ou treemap) OU bouton **"+ Mapper"** au hover avec popover autocomplete sur les dossiers
- **Toast d'impact** : `✓ "X" → /Y · N fichiers au prochain reclassify` (N pris dans `themes_llm[theme].count`)
- **Backup auto** : avant chaque write, `theme_mapping.yaml` copié dans `profiles/<p>/.cache/taxonomy-backups/theme_mapping-YYYYMMDD-HHMMSS.yaml` (rotation 20)
- **Lock concurrence** : `.taxonomy.lock` bloque les writes pendant un baseline_run ou autre tâche externe
- **Validation pré-write** : refuse les doublons (409), folders inconnus (400), thèmes vides (400)
- **Sélecteur profil** : header, change le profil actif (snapshot + cache memoire isolés par profil)

## Tests

- **44+ tests** dans [../tests/auto/test_dashboard.py](../tests/auto/test_dashboard.py) (routes + data.py)
- **9 tests** dans [../tests/auto/test_viewer_copy.py](../tests/auto/test_viewer_copy.py) (copy + clear destination, path traversal, refus de `default`)
- **20 tests** dans [../tests/auto/test_thumbnail.py](../tests/auto/test_thumbnail.py) (PDF, ePub2/3, placeholder, count_pages, clear_cache mixed format)
- **16 tests** dans [../tests/auto/test_taxonomy.py](../tests/auto/test_taxonomy.py) (write safety + agrégation snapshot + endpoints HTTP)
