# Dashboard — Tests fonctionnels + Curation Klodo

FastAPI + Jinja2 + HTMX + Chart.js + SSE, dark theme. Point d'entrée : `uv run python -m dashboard.app` (port 8080).

## Architecture
- **Routes** : [app.py](app.py) — 10 pages (Overview, Tests, Rapports, Comparer, Métriques, Historique, Logs, Curation, Suggestions, Admin)
- **Couche données** : [data.py](data.py) — fusion YAML + JSON + DuckDB + CSV + helpers viewer
- **Backend** : DuckDB via [../tests/functional/db.py](../tests/functional/db.py) — tables `runs`, `series_results`, `check_results`, `manual_validations`
- **Templates** : 12+ templates avec **macros réutilisables** dans `templates/macros/widgets.html` (kpi_card, status_badge, run_banner, pagination, filter_bar, data_table)
- **Static JS** : modules dans `static/js/` (`common.js`, `validation.js`, `filters.js`, `tests.js`, `admin.js`, `viewer.js`) — tout extrait des templates
- **Static CSS** : `static/style.css` (dark theme + composants viewer + progress bar inline)

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

## Tests
- **44+ tests** dans [../tests/auto/test_dashboard.py](../tests/auto/test_dashboard.py) (routes + data.py)
- **9 tests** dans [../tests/auto/test_viewer_copy.py](../tests/auto/test_viewer_copy.py) (copy + clear destination, path traversal, refus de `default`)
- **20 tests** dans [../tests/auto/test_thumbnail.py](../tests/auto/test_thumbnail.py) (PDF, ePub2/3, placeholder, count_pages, clear_cache mixed format)
