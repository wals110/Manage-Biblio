# Renommage

[← Retour au README](../README.md) · [Architecture](architecture.md)

## Vue d'ensemble

Le module de renommage transforme les noms de fichiers PDF cryptiques (`978-3-030-12345-6.pdf`, `scan_20240301.pdf`) en noms lisibles au format `Titre - Auteur.pdf`. Il utilise un pipeline en cascade de 5 sources d'information.

<p align="center">
  <img src="diagrams/pipeline-renommage.svg" alt="Pipeline de renommage" width="550">
</p>

## Pipeline en détail

### Étape 1 : Nettoyage + Wordcheck

Vérifie si le nom de fichier est déjà exploitable via `is_name_clean()`, qui combine deux vérifications :

1. **Structure** — Le nom doit contenir au moins 2 mots de 3+ caractères (pas un simple code ou numéro)
2. **Wordcheck** — Le module `lib/wordcheck.py` valide que le nom contient de vrais mots humains, pas du gibberish

Le wordcheck fonctionne en 3 étapes :

1. **Tokenization** — Extrait les mots de 3+ caractères via regex `[A-Za-zÀ-ÿ]{2,}`
2. **Validation de chaque mot** — Dans cet ordre :
   - Whitelist technique (~100 termes : kubernetes, tensorflow, graphql, etc.)
   - Dictionnaire anglais (pyspellchecker, ~130k mots)
   - Dictionnaire français (pyspellchecker, ~300k mots)
   - Heuristique acronyme/nom propre : ALL CAPS 2-6 chars (API, SQL), TitleCase (Einstein), CamelCase (JavaScript)
3. **Calcul du ratio** — `mots reconnus / total mots ≥ 3 chars`. Seuil : **40%**

| Fichier | Mots | Reconnus | Ratio | Résultat |
| --- | --- | --- | --- | --- |
| `'fh&itei.pdf` | fh, itei | 0 | 0% | GIBBERISH → renommer |
| `Algorithms.pdf` | Algorithms | 1 | 100% | PROPRE → garder |
| `MCAD MCSD NET.pdf` | MCAD, MCSD, NET | 3 (acronymes) | 100% | PROPRE → garder |

Si le ratio est inférieur à 40%, le fichier est considéré comme ayant un nom sale et passe aux étapes suivantes du pipeline de renommage.

Le wordcheck intervient aussi dans `_is_good_title()` pour rejeter les titres extraits des métadonnées PDF qui ne contiennent pas de vrais mots.

### Étape 2 : Recherche ISBN

Si le nom contient un ISBN (10 ou 13 chiffres), recherche dans Google Books et OpenLibrary pour récupérer le titre et l'auteur. Les résultats sont mis en cache dans `profiles/<nom>/.cache/isbn_cache.json` (~3400 entrées).

### Étape 3 : Métadonnées PDF

Extraction du titre et de l'auteur depuis les métadonnées internes du PDF via pypdf et pdfplumber. Filtre les titres génériques (`GENERIC_TITLES` : "title", "document", "untitled", etc.).

### Étape 4 : LLM Vision (optionnel, `--llm`)

Activé avec `--llm`. Le module `lib/vision.py` extrait les N premières pages du PDF comme images (via pdf2image + poppler, 150 dpi), les convertit en base64, et les envoie au modèle de vision (Qwen3-VL par défaut via SiliconFlow, ou un modèle local via Ollama).

Le prompt demande au LLM d'analyser la couverture et de retourner un JSON avec `title`, `author`, `theme`, `language` et `confidence`. Le titre doit être dans la langue originale du livre, l'auteur au format "Prénom Nom".

Avec une seule page (`--pages 1`, défaut), le prompt est orienté couverture. Avec plusieurs pages (`--pages 2` ou plus), un prompt alternatif est utilisé qui demande au LLM de chercher le titre spécifique du livre (pas le nom de la collection) — important pour les collections comme LNCS où la couverture affiche "Lecture Notes in Computer Science" et le vrai titre est sur la page 2 ou 3.

```
Entrée :
  - Image(s) base64 de la couverture (et pages internes si --pages > 1)

Réponse attendue :
  {"title": "Introduction to Algorithms", "author": "Thomas Cormen",
   "theme": "Computer Science", "language": "en", "confidence": 0.95}
```

Les réponses avec `confidence < 0.3` ou les titres génériques (listés dans `GENERIC_TITLES` : "title", "document", "untitled", etc.) sont rejetées.

Avec `--force`, le LLM Vision passe en priorité (avant ISBN et métadonnées) et re-analyse même les fichiers dont le nom est déjà considéré propre par `is_name_clean()`.

**Configuration** : endpoint et modèle dans `profile.yaml`. Paramètres d'appel : `max_tokens: 800`, `temperature: 0.1`, timeout de 120s.

### Étape 5 : Fallback dossier parent

Dernier recours : utilise le nom du dossier parent comme approximation du sujet.

## Commande CLI

```bash
# Dry-run (ISBN + métadonnées PDF)
./klodo.sh rename /chemin

# Appliquer les renommages
./klodo.sh rename /chemin --execute

# Activer LLM Vision en fallback
./klodo.sh rename /chemin --llm --execute

# Analyser 2 pages (couverture + page titre) — utile pour les LNCS
./klodo.sh rename /chemin --llm --pages 2 --execute

# Forcer LLM en priorité + 3 pages
./klodo.sh rename /chemin --llm --force --pages 3 --execute

# Test limité avec détails
./klodo.sh rename /chemin --max 10 --verbose

# Sans recherche ISBN en ligne
./klodo.sh rename /chemin --no-online --execute
```

### Options

| Option | Description |
|---|---|
| `--llm` | Activer LLM Vision en fallback (analyse couverture) |
| `--force` | LLM en priorité + re-analyser les noms "propres" |
| `--pages N` | Pages à analyser (défaut: 1, max: 5) |
| `--max N` | Limiter à N fichiers |
| ~~`--api-key`~~ | Supprimé — utiliser la variable `SILICONFLOW_API_KEY` |
| `--no-online` | Désactiver la recherche ISBN en ligne |
| `--no-pdf` | Désactiver l'extraction des métadonnées PDF |

## Patterns de nommage (`name_patterns`)

En complément du wordcheck, les profils peuvent imposer un format strict via une liste de regex dans `profile.yaml` :

```yaml
rename:
  name_patterns:
    - "^[A-ZÀ-Ÿ].+ - [A-ZÀ-Ÿ].+$"            # Titre - Auteur
    - "^[A-ZÀ-Ÿ][A-Za-zÀ-ÿ0-9 .·\\-']{4,}$"   # Titre seul ou auteurs avec ponctuation
    - "^\\d+\\s+[A-ZÀ-Ÿ].+ - [A-ZÀ-Ÿ].+$"     # 101 Titre - Auteur
```

Si configurés, `is_name_clean()` exige que le fichier matche **au moins un** pattern **après** le wordcheck. Cela rejette les artefacts de scan que le wordcheck laisse passer (ex: `00 0672318350 fm 05•02•2003 2 31 PM Page i.pdf` contient des vrais mots anglais "Page", "PM" mais ne respecte aucun format).

Les regex invalides du profil sont silencieusement ignorées (try/except `re.error`) pour ne pas crasher.

## Pattern Detector — détection automatique via LLM

La sous-commande `./klodo.sh detect` permet de générer des patterns regex à partir d'un échantillon de fichiers :

```bash
# Avec une liste explicite
./klodo.sh detect --files "Titre - Auteur.pdf" "Autre - Bob.pdf" --execute

# Avec un dossier
./klodo.sh detect --dir /Volumes/ExtSSD/BIBLIO/Informatique/ --execute

# Mode interactif
./klodo.sh detect
```

Le LLM analyse l'échantillon (1 seul appel), retourne une regex Python validée + description + score de confiance + exemples qui matchent et qui ne matchent pas. Avec `--execute`, le pattern est injecté dans `profile.yaml → rename.name_patterns` en préservant les commentaires existants.

Module : `lib/pattern_detector.py`. Commande : `commands/detect.py`.

## Cache ISBN

Les résultats de recherche ISBN sont stockés dans `profiles/<nom>/.cache/isbn_cache.json`. Ce cache accélère les prochains runs et réduit les appels réseau. Il contient ~3400 entrées après le traitement initial de la bibliothèque. Nettoyable via `./klodo.sh clean isbn --execute`.

## Renommage dashboard — audit, journal, undo

En complément du pipeline CLI ci-dessus, le dashboard expose une **feature audit de renommages** dans le sous-onglet **Rename** de la Taxonomie. Indépendante du pipeline classify : permet de renommer des fichiers déjà classifiés dont le nom diverge du titre extrait par LLM Vision.

### Pipeline audit

À partir de `vision_cache.json` (titre + auteur extraits par LLM), le système calcule pour chaque fichier :

1. Un **nom suggéré** via un template configurable (par défaut `{title}{ - author}`) avec sanitization NFKC + filesystem-safe (`/` → `-`, `:` → `—`, chars interdits drop)
2. Une **similarité Jaccard 3-grams** entre nom actuel et nom suggéré
3. Une **catégorie** : `placeholder` (pattern "Title Author"), `divergent` (sim < 0.4), `minor_case` (0.4 ≤ sim < 0.7), `ok` (sim ≥ 0.7)

L'utilisateur peut renommer **unitairement** ou **en bulk**, ou marquer manuellement un fichier comme **OK** (override) — dans tous les cas, chaque opération est journalisée.

### Le journal — `rename-journal.jsonl`

Append-only JSONL stocké dans `profiles/<p>/.cache/rename-journal.jsonl`. Chaque entrée :

```json
{"ts": "2026-05-17T19:09:06", "old": "/abs/Title Author.pdf",
 "new": "/abs/Real Title - Real Author.pdf",
 "batch": "20260517-190906-c94aee"}
```

- `batch` est partagé par tous les renames d'un même bulk (auto-undo batch en un clic)
- Les undos sont eux-mêmes journalisés sous `batch: "undo-<original_batch_id>"` → audit trail complet, append-only par construction
- `is_undone` calculé positionnellement (un record est annulé ssi un record d'undo plus récent dans le journal a son `old` = ce record's `new`) — pas d'ambiguïté sur les chemins qui se répètent au fil du temps

### UI

- **Sous-onglet Rename** : col 1 liste filtrable + multi-select bulk, col 2 viewer + LLM card avec sous-bloc Rename (cross-link Mappings ↔ Rename via `tax-navigate-file` event), col 3 input éditable + bouton Renommer + confirm modal
- **Modale 📜 Renommages** (compteur + sub-badge ⚠ session-active) : 2 onglets (Tous / Par lot) avec bouton ↶ Annuler par ligne ou par batch entier
- **Override** : bouton "Marquer comme OK" persisté dans `rename-overrides.json` keyé par cache_key MD5 — **survit aux renames** (sur le contenu, pas sur le path)
- **Session log** (col 1, persistance `sessionStorage`) : trace en mémoire des actions du tab courant + bouton ↶ inline
- **Search bar** col 1 + bulkbar sticky (Renommer suggestion / Marquer OK / Tout désélectionner)
- **🔄 Rescan** : invalidation du cache audit + scan complet (utile après ajout manuel ou nouveau cycle `klodo.sh rename`)

### Endpoints API

| Méthode | Route | Rôle |
|---|---|---|
| `GET` | `/api/rename/audit?profile=&force=` | Charger l'audit (force = bypass cache) |
| `POST` | `/api/rename/file` | Renommer un fichier (commit + journal) |
| `POST` | `/api/rename/bulk` | Renommer N fichiers en batch (shared batch_id) |
| `GET` | `/api/rename/journal?profile=&limit=` | Historique du profil (newest first) |
| `POST` | `/api/rename/undo/record` | Annuler un rename individuel (match ts+old+new) |
| `POST` | `/api/rename/undo/batch` | Annuler tout un batch en une opération |
| `POST` | `/api/rename/override` | Marquer N fichiers comme OK (ou clear) |

### Performance — cache patch ciblé

Avant : chaque rename invalidait tout l'audit cache (`~5s` de rescan os.walk + ThreadPoolExecutor sur 18k fichiers). Aujourd'hui : **patch ciblé en place** (`<50ms`) — drop de l'entrée concernée + re-audit du seul fichier + tri unique en fin de batch. Fallback automatique sur full-rebuild si le patch ne peut pas s'appliquer (profil mal configuré, fichier disparu, bucket non-candidat).

## Module

**Fichiers** : `lib/renamer.py` (moteur CLI), `lib/rename_template.py` (parser de template + sanitization), `lib/rename_journal.py` (journal append-only + undo), `lib/vision.py` (LLM Vision), `lib/wordcheck.py` (validation dictionnaire), `lib/utils.py` (sanitize).

**Dashboard** : `dashboard/rename.py` (audit + apply + journal viewing + override + bulk + cache patch), templates `dashboard/templates/taxonomy.html` (sous-onglet + modale), JS `dashboard/static/js/taxonomy_rename.js`.
