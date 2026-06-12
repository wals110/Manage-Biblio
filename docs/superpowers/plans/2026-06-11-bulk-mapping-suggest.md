# Plan — Mappings : affectation de thèmes EN LOT + suggestion hybride

## Context

Dans l'onglet **Taxonomie → Mappings**, colonne **Thèmes LLM**, affecter un thème à un dossier se fait **un par un** (drag-drop ou bouton « + Mapper »), ce qui est lent quand il y a beaucoup de thèmes à router. Le user veut deux capacités :
1. **Affecter en lot** : sélectionner N thèmes et les envoyer d'un coup vers UN dossier choisi.
2. **Optimiser l'affectation** : que le système **suggère** le meilleur dossier pour chaque thème sélectionné, l'user revoyant une liste et acceptant tout/partie.

Décisions validées : moteur de suggestion **hybride** (déterministe gratuit d'abord, LLM seulement pour les ambigus), périmètre = **sélection manuelle** (pas de « tout traiter » sur les ~6800 orphelins).

Beaucoup d'éléments existent déjà et sont réutilisés : multi-sélection (patron `bulkMappedSelection`), endpoint **bulk-delete** (à mirrorer), `add_mapping` sécurisé (lock + validation + backup atomique), `llm_mapper.resolve` (suggestion LLM), `KeywordClassifier` (suggestion déterministe), autocomplete de dossiers.

## Approche

### Backend — `dashboard/taxonomy.py` (2 nouvelles fonctions, calquées sur l'existant)

- **`add_mappings_bulk(profile, mappings: list[dict]) -> dict`** — miroir de `delete_mappings_bulk` (taxonomy.py:1970). Sous `_locks[profile]` + `_check_lock_free` (423 si `.taxonomy.lock`). Pour chaque `{theme, folder}` : `_validate_theme` (<200 chars) + `_validate_folder` (existe dans tree.yaml) ; **skip rapporté** (pas d'abort) si invalide ou déjà mappé (`reason="already_mapped"`, jamais d'écrasement) ; dédup par clé thème. **UN seul** `_backup_mapping` avant le batch (rien si 0 ajout), puis `_write_mapping` atomique + `reset_cache`. Retour `{ok, n_added, added:[...], skipped:[{theme,reason}], backup}`.

- **`suggest_mappings(profile, themes: list[str], use_llm: bool, max_llm: int = 60) -> dict`** — **lecture seule, sans lock**. Passe 1 déterministe gratuite par thème : `KeywordClassifier.classify(theme)` (keyword_classifier.py:244 — un nom de thème est du texte) + matching de nom normalisé (`categories._normalize_path`). Si score ≥ `SEUIL_DET` (0.6) → `{folder, source:"deterministic", confidence}`. Passe 2 : pour les thèmes restants **et** `use_llm`, appeler `LLMMapper.resolve(theme, title=sample_titles[0])` (llm_mapper.py:147, valide folder + confidence ≥ 0.6) — **parallélisé** en `ThreadPoolExecutor(max_workers=8)` (patron simulator.py:179), borné par `max_llm`, désactivé sans `SILICONFLOW_API_KEY`. LLMMapper construit comme `commands/helpers.py:317-326` (`Profile(profile)`). Sinon `source:"unresolved"`. Retour `{suggestions:[{theme, folder|null, confidence, source, reason}], n_llm_calls}`.

### Backend — `dashboard/app.py` (2 routes, style app.py:2096)

- `POST /api/taxonomy/mappings/bulk-add` `{profile, mappings:[{theme,folder}]}` → `add_mappings_bulk`.
- `POST /api/taxonomy/mappings/suggest` `{profile, themes:[str], use_llm:bool}` → `suggest_mappings`.

### Frontend — `dashboard/static/js/taxonomy.js`

- État `state.bulkLLMSelection = new Set()` (thèmes en minuscule) + `state.suggestReview`.
- Dans `renderLLMPanel()` (2697) : ajouter une **checkbox** par `.tax-llm-item` (patron `bulkMappedSelection`, taxonomy.js:2274/2481), en **préservant le `draggable`** existant (drag-drop unitaire conservé).
- **Toolbar** (apparaît si ≥1 sélectionné) : compteur, « Tout sélectionner (filtrés) », « Effacer », + 2 actions :
  - **Mapper la sélection → [dossier]** : picker réutilisant l'autocomplete `renderPopoverSuggestions` (2775) → `postBulkAddMappings(themes, folder)` (POST bulk-add). Toast = somme des `themes_llm[t].count` (patron `doAddMapping`, 2941).
  - **💡 Suggérer + Mapper** : `runSuggestAndMap()` → POST suggest → **panneau de revue** : `thème → dossier suggéré (badge source déterministe/LLM/non-résolu) + confiance + raison`, chaque ligne avec checkbox d'acceptation (non-résolus **décochés** par défaut) + dossier **éditable inline** (réutilise l'autocomplete) → « Appliquer les N acceptés » → `applySuggestions()` (POST bulk-add).

### Réutilisation (ne rien réinventer)
Picker dossier = `renderPopoverSuggestions` · multi-sélection = patron `bulkMappedSelection` · sécurité write = `add_mapping` (lock/validate/backup/atomic) · suggestion LLM = `LLMMapper` · suggestion déterministe = `KeywordClassifier` · toast impact = `themes_llm[t].count`.

### Tests — `tests/auto/test_taxonomy.py` (patron des classes existantes)
- `TestBulkAddMapping` (calque `TestBulkDeleteMapping` l.1686) : backup unique, déjà-mappés/doublons skippés sans écrasement, folder inconnu rapporté, lock → 423, pas de write si batch vide.
- `TestSuggestMappings` : déterministe résout (nom/keyword) sans appel LLM (assert `LLMMapper.resolve` non appelé) ; ambigu → LLM mocké (`mock.patch.object`) ; `unresolved` ; pas d'appel sans clé API.
- Endpoints HTTP étendus dans `TestDormantAndBulkEndpoints` (l.1735) : bulk-add (200 + skipped), suggest (200 + suggestions).
- Rendu : la toolbar apparaît, les checkboxes sont présentes sur `.tax-llm-item`.

### Découpage TDD (test → fail → impl → pass → commit)
Branche **`feature/bulk-mapping-suggest`** depuis `develop`. Ordre : (1) `add_mappings_bulk` + tests, (2) route bulk-add, (3) `suggest_mappings` passe déterministe + tests, (4) passe LLM parallélisée + tests (LLM mocké), (5) route suggest, (6) frontend multi-sélection + toolbar, (7) mode « Mapper la sélection → dossier », (8) mode « Suggérer + Mapper » + panneau de revue, (9) MAJ `dashboard/CLAUDE.md`.

## Points de vigilance
- **Coût LLM** maîtrisé : déterministe absorbe les évidents (gratuit), `max_llm` plafonne, parallélisme 8-way (~5-10 s pour 40 ambigus), zéro appel sans clé.
- **Lock** : bulk-add sous mutex + `_check_lock_free` ; suggest sans lock (lecture seule).
- **Thread-safety** : `requests.Session` du LLMMapper est OK pour requêtes indépendantes ; on n'écrit PAS `theme_mapping.yaml` dans suggest (les `learned`/`calls` muables restent best-effort, non persistés).
- **Validation/dédup** par item, skip rapporté plutôt qu'abort.
- **Défauts** : `SEUIL_DET=0.6`, `max_llm=60` (ajustables).

## Fichiers critiques
- `dashboard/taxonomy.py` — `add_mappings_bulk`, `suggest_mappings` (réutilise `_validate_theme`/`_validate_folder`/`_backup_mapping`/`_write_mapping`/`_locks`/`_check_lock_free`, `_aggregate_themes_llm` pour count+sample_titles).
- `dashboard/app.py` — 2 routes (~app.py:2096).
- `dashboard/static/js/taxonomy.js` — checkboxes + toolbar + 2 modes (~`renderLLMPanel` 2697, patron `bulkMapped` 2481, autocomplete 2775).
- `dashboard/categories.py` — `_normalize_path` + `KeywordClassifier` pour la passe déterministe.
- `tests/auto/test_taxonomy.py` — nouvelles classes de tests.
- `lib/llm_mapper.py`, `lib/keyword_classifier.py`, `commands/helpers.py` (construction LLMMapper) — réutilisés tels quels.

## Vérification end-to-end
1. `uv run python -m unittest tests.auto.test_taxonomy -v` → tous verts (nouvelles classes incluses).
2. `uv run ruff check dashboard/taxonomy.py dashboard/app.py tests/auto/test_taxonomy.py` → clean.
3. `./klodo.sh dashboard` → Taxonomie → Mappings : cocher plusieurs thèmes LLM → la toolbar apparaît → « Mapper la sélection → 02-INFORMATIQUE/05-IA-ML » → toast « N thèmes · M fichiers », les thèmes passent en « mappés ».
4. Cocher d'autres thèmes → « 💡 Suggérer + Mapper » → le panneau de revue liste thème→dossier (badges déterministe/LLM), décocher les non pertinents, corriger un dossier inline → « Appliquer » → bulk-add → vérifier `theme_mapping.yaml` (et le backup unique dans `.cache/taxonomy-backups/`).
5. Smoke endpoints : `curl -s -XPOST .../api/taxonomy/mappings/suggest -d '{"profile":"default","themes":["Deep Learning","Galois Theory"],"use_llm":true}'` → JSON suggestions cohérentes.
