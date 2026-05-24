# lib/ — Modules core Klodo

## Architecture
- **Constantes** : [constants.py](constants.py) — timeouts, seuils, retries centralisés
- **Exceptions** : [exceptions.py](exceptions.py) — hiérarchie `KlodoError → ConfigError, LLMError, ClassificationError, SafetyError`
- **Client HTTP** : [llm_client.py](llm_client.py) — `requests.Session` (connection pooling, retries)
- **Version unique** : [__init__.py](__init__.py) → `__version__ = "1.0.0-dev"` (source unique)
- **Sanitization LLM** : `sanitize_for_prompt()` dans [utils.py](utils.py) — **toujours l'utiliser** avant d'interpoler un nom de fichier ou titre dans un prompt LLM

## Pipeline de classification (4 niveaux)
```
LLM Vision (thème, titre) → 1. Theme Mapping (355+ entrées, gratuit)
                            → 2. Keyword Classifier (titre+thème+filename, YAML + TF-IDF)
                            → 3. LLM Mapper (texte + escalade vision, auto-apprentissage)
                            → 4. Suggestion de nouveau dossier (review humain)
```

### Pièges connus
- **Mots-clés courts** (≤3 chars ou ambigus) : utiliser `WORD_BOUNDARY_KEYWORDS` dans `KeywordClassifier`
- **Faux positifs résolus** : "bert" dans "Albert", "christ" dans "Christopher", "bible" dans "Linux Bible", "Pascal" (prénom)
- **Isolation de catégorie** : pénalité ×0.1 pour changements de catégorie top-level
- **`KeywordClassifier.classify()`** retourne `list[tuple[str, float, str]]`

## Renommage
- **[renamer.py](renamer.py)** — Moteur CLI : ISBN, PDF, LLM Vision
- **`GENERIC_TITLES`** : liste des titres placeholder rejetés (dans renamer.py)
- **`is_name_clean()`** et **`_is_good_title()`** utilisent [wordcheck.py](wordcheck.py)
- **`is_name_clean(filename, name_patterns=None)`** accepte une liste de regex venant de `profile.yaml` (`rename.name_patterns`). Si configurés, le fichier doit matcher au moins un pattern **après** le wordcheck. Les regex invalides du profil sont silencieusement ignorées (try/except `re.error`)

### Audit & journal (dashboard Rename)
- **[rename_template.py](rename_template.py)** — Parser + renderer du template `{title}{ - author}` (blocs optionnels, sanitization NFKC + chars FS-safe, troncature word-boundary, fallback)
- **[rename_journal.py](rename_journal.py)** — JSONL append-only `profile/.cache/rename-journal.jsonl`. Helpers : `append_rename`, `list_renames`, `list_batches`, `undo_record` (avec guard case-insensitive APFS via `samefile()`), `undo_batch` (groupé par `batch_id` partagé)
- Les undos sont eux-mêmes journalisés sous `batch: "undo-<batch_id>"` → audit trail complet, append-only par construction
- L'overrides utilisateur `rename-overrides.json` est keyé par cache_key MD5 (head bytes du fichier) → **survit aux renames**
- Cf. [dashboard/rename.py](../dashboard/rename.py) pour le wrapper FastAPI (audit + commit + cache patch ciblé)

## Pattern detector — détection automatique de patterns de nommage
- **[pattern_detector.py](pattern_detector.py)** — `detect_pattern(filenames, ...)` envoie un échantillon de noms à un LLM qui retourne une regex + description + confiance
- `test_coverage(result, all_filenames)` mesure le taux de matching sur l'ensemble de la bibliothèque
- Validation : `re.compile()` avant de retourner, gère les fences markdown dans la réponse JSON
- Utilisé par la sous-commande `./klodo.sh detect --files ... --execute` (cf. `commands/detect.py`) qui injecte le pattern validé dans `profile.yaml` en préservant les commentaires

## Thumbnail (cache unifié dashboard + LLM pipeline)
- **[thumbnail.py](thumbnail.py)** — `generate_thumbnail(source, doc_dir, n_pages=1, start_page=1)` produit `doc_dir/{1..n}.jpg` (400×550 JPEG). **Idempotent au niveau page** : skip si `N.jpg` existe (resumable runs + upgrades incrémentaux `--pages 1` → `--pages 5`)
- **`compute_content_key(path)`** — clé content-based (MD5 des head bytes [16 chars]). Utilisée par le dashboard pour son cache `profile/.cache/thumbnails/<content_key>/{1..n}.jpg` — **survit aux renames** (clé liée au contenu, pas au rel_path)
- **`save_pil_images_as_thumbnails(images, doc_dir, overwrite=False)`** — exposé pour le pipeline LLM : après `extract_cover_image()` dans `analyze_cover()`, les PIL images peuvent être dumpées vers le cache thumbnail au lieu d'être jetées. Capitalise sur l'extraction gratuite.
- **Helpers** : `count_pages(cache_dir, stem)`, `clear_cache(cache_dir)` (gère legacy + sous-dossiers), `get_cache_stats(cache_dir)`
- **Formats** : PDF (via `lib/pdf_cover.get_cover_image(page_indices=...)` pour batch ciblé Poppler) + ePub (zipfile + parsing manifest OPF) + placeholder pour le reste
- **ePub start_page > 1** : retourne 0 (pas d'extraction de pages internes)
- **CLI dédiée** : `./klodo.sh thumbnails --execute --pages N` pour backfill batch (cf. `commands/thumbnails.py`)

## Wordcheck — validation noms par dictionnaire

## Wordcheck — validation noms par dictionnaire

Valide que les noms de fichiers contiennent de vrais mots humains, pas du gibberish.

### Mécanisme en 3 étapes
1. **Tokenization** — Extrait les mots de 3+ caractères via regex `[A-Za-zÀ-ÿ]{2,}`
2. **Validation de chaque mot** — Dans cet ordre :
   - Whitelist technique (~100 termes : kubernetes, tensorflow, graphql, etc.)
   - Dictionnaire anglais (pyspellchecker, ~130k mots)
   - Dictionnaire français (pyspellchecker, ~300k mots)
   - Heuristique acronyme/nom propre : ALL CAPS 2-6 chars (API, SQL), TitleCase (Einstein), CamelCase (JavaScript)
3. **Calcul du ratio** — `mots reconnus / total mots ≥ 3 chars`. Seuil : **40%**

### Exemples
| Fichier | Mots | Reconnus | Ratio | Résultat |
|---------|------|----------|-------|----------|
| `'fh&itei.pdf` | fh, itei | 0 | 0% | GIBBERISH → renommer |
| `Algorithms.pdf` | Algorithms | 1 | 100% | PROPRE → garder |
| `MCAD MCSD NET.pdf` | MCAD, MCSD, NET | 3 (acronymes) | 100% | PROPRE → garder |
| `bxcg laud gyxj.pdf` | bxcg, laud, gyxj | 1 (laud=EN) | 33% | GIBBERISH → renommer |

### Configuration
- Whitelist : `TECH_WORDS` dans [wordcheck.py](wordcheck.py) (extensible)
- Seuil : `min_ratio=0.4` (param de `contains_real_words()`)
- Langues : EN + FR (extensible via pyspellchecker)
