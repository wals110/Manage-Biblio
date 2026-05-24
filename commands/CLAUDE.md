# commands/ — Sous-commandes CLI Klodo

Point d'entrée unique : [../klodo.py](../klodo.py) avec sous-commandes argparse. Lanceur : `./klodo.sh` (fait `uv run python klodo.py`).

## Modules
- **[helpers.py](helpers.py)** — Helpers partagés (copie, classify, LLM callbacks, SafetyError)
- **[classify.py](classify.py)** — `cmd_classify` + `scan_and_classify`
- **[rename.py](rename.py)** — `cmd_rename` (import direct `lib.renamer`)
- **[refine.py](refine.py)** — `cmd_refine`
- **[process.py](process.py)** — `cmd_process` (pipeline complet, import direct `lib.renamer`)
- **[misc.py](misc.py)** — `cmd_profiles`, `cmd_init`, `cmd_suggest`, `cmd_clean`
- **[detect.py](detect.py)** — `cmd_detect` : sélection interactive ou par `--files`/`--dir`, appelle `lib.pattern_detector.detect_pattern()`, affiche le résultat et peut injecter dans `profile.yaml` (préserve les commentaires) avec `--execute`
- **[thumbnails.py](thumbnails.py)** — `cmd_thumbnails` : backfill du cache thumbnail du dashboard. Itère sur tous les PDF/ePub du profil, calcule la clé content-based (MD5 head bytes), génère les pages manquantes via `lib.thumbnail.generate_thumbnail` (idempotent — skip si déjà en cache). Options `--pages N` (1-5), `--max M`, `--force`. Affiche progress + ETA + cost estimate

## Flags importants
- `--execute` : appliquer les changements (sinon dry-run). **Ne jamais lancer `--execute` sans review du rapport.**
- `--yes` / `-y` : bypasser les confirmations (dans le `common` parser, disponible sur toutes les commandes)
- `--force` : re-analyser tous les fichiers (rename, process)
- `--verbose` / `-v` : mode verbeux

## Usage CLI

```bash
# Pipeline complet (rename → classify → copie → refine)
./klodo.sh process /chemin/vers/pdfs --execute --workers 10
./klodo.sh process /chemin --llm --pages 2 --execute      # Avec LLM Vision pour rename
./klodo.sh process /chemin --no-rename --execute           # Sans renommage

# Sous-commandes individuelles
./klodo.sh classify /chemin --workers 10              # Classification seule
./klodo.sh classify /chemin --vision --pages 2        # + escalade vision
./klodo.sh rename /chemin --execute                   # Renommage seul
./klodo.sh refine --execute                           # Raffinement sous-catégories

# Renommage avec LLM Vision
./klodo.sh rename /chemin --llm                    # LLM Vision en fallback
./klodo.sh rename /chemin --llm --pages 2          # 2 pages
./klodo.sh rename /chemin --llm --force --pages 3  # LLM en priorité
./klodo.sh rename /chemin --max 10 --verbose       # Test limité

# Raffinement avec LLM fallback
./klodo.sh refine --llm --execute
./klodo.sh refine --llm --max 20 --verbose

# Suggestions / Gestion erreurs / Profils
./klodo.sh suggest --apply --execute
./klodo.sh classify /chemin --retry-errors --execute
./klodo.sh profiles
./klodo.sh init mon-profil --target /chemin

# Nettoyage cache
./klodo.sh clean progress --execute               # Supprimer le checkpoint
./klodo.sh clean isbn --execute                    # Supprimer le cache ISBN
./klodo.sh clean logs --execute                    # Supprimer les rapports CSV
./klodo.sh clean all --profile test --execute      # Tout nettoyer (profil test)
```

## Conventions
- **Paramètres CLI** : noms en anglais (`--execute`, `--workers`, etc.)
- **Messages et aide** : en français
- **Imports** : directs (`from lib import renamer`), pas de `sys.path` hacks
