# tests/auto/ — Tests unitaires et intégration

**360+ tests** (unitaires + intégration + dashboard + wordcheck + thumbnail + viewer copy + pattern detector + restore names).

## Lancer
```bash
./tests/auto/run_all.sh                    # Tous les tests
./tests/auto/run_all.sh -v                 # Verbose
./tests/auto/run_all.sh test_wordcheck     # Un seul module
python3 -m unittest discover -s tests/auto # Alternative
```

## Modules
- `test_compile.py` — Compilation `py_compile`
- `test_imports.py` — Imports croisés
- `test_safety.py` — Sécurité inbox → SafetyError (6 tests)
- `test_copy.py` — Copie + suppression (14 tests)
- `test_parser.py` — Parser CLI (22 tests)
- `test_classifier.py` — Classification (25 tests)
- `test_llm_client.py` — Client HTTP (20 tests)
- `test_rename_llm.py` — LLM rename + force + pages (20 tests)
- `test_refine.py` — Refine récursif + LLM (60+ tests)
- `test_integration.py` — Tests intégration (6 tests)
- `test_dashboard.py` — Routes + data.py (44+ tests dont viewer/curation)
- `test_wordcheck.py` — Validation dictionnaire + name_patterns (44 tests)
- `test_clean.py` — Nettoyage cache/logs
- `test_pattern_detector.py` — Pattern detector LLM (22 tests)
- `test_restore_names.py` — Restauration noms originaux dans flatten (9 tests)
- `test_thumbnail.py` — Génération thumbnails PDF/ePub multi-pages + cache (20 tests)
- `test_viewer_copy.py` — Copie source → destination + clear (9 tests)

## Conventions
- **Fixtures** : [fixtures/](fixtures/) (profils + PDFs de test)
- **Assertions** : `unittest.TestCase`, pas `assert`
- **Ruff** : per-file-ignores pour `E402` sur `tests/auto/`

## Datasets de test (SSD externe)
- `/Volumes/ExtSSD/BIBLIO-TEST` — ~18 445 PDFs (copie test générale)
- `/Volumes/ExtSSD/BIBLIO-TEST-FUNC` — 1000 PDFs (925 propres + 75 noms sales) pour tests fonctionnels
