# Contribuer à Klodo

[← Retour au README](../README.md)

Merci de votre intérêt pour Klodo ! Ce guide explique comment contribuer au projet.

## Prérequis

- Python 3.13 (via [uv](https://docs.astral.sh/uv/))
- poppler (extraction PDF → image)

## Démarrer

```bash
git clone https://github.com/votre-user/klodo.git
cd klodo
uv sync
```

## Conventions de code

### Python 3.13

Le projet cible **Python 3.13**. Utilisez les type hints modernes :

```python
# ✓ Correct — syntaxe moderne
def foo(x: dict | None): ...
def bar(items: list[str]): ...
from collections.abc import Callable

# ✗ Obsolète — ne pas utiliser
from typing import Optional, List, Dict, Callable
def foo(x: Optional[dict]): ...
```

### Architecture

- **Modules** dans `lib/` — logique métier, jamais de constantes en dur
- **Point d'entrée unique** : `klodo.py` avec sous-commandes argparse
- **Profils** : toute config externalisée dans `profiles/<nom>/` (YAML)
- **Paramètres CLI** : noms en anglais (`--execute`, `--workers`)
- **Messages et aide** : en français

### Style

- Docstrings pour toutes les fonctions publiques
- Type annotations modernes (Python 3.13 : `X | None`, `list[str]`, `dict[str, int]`)
- Linter : `ruff` (configuré dans `pyproject.toml`)
- Pas de dépendances lourdes sans discussion préalable

## Tests

La suite de tests couvre 360+ tests répartis en modules :

```bash
# Lancer tous les tests
./tests/auto/run_all.sh

# Lancer un module spécifique
uv run python -m unittest tests.auto.test_refine -v

# Lancer un test spécifique
uv run python -m unittest tests.auto.test_refine.TestScanAndRefineVision -v
```

### Ajouter des tests

Les tests automatisés (unitaires + intégration) sont dans `tests/auto/`. Chaque module a son fichier de tests correspondant. Utilisez `tempfile.mkdtemp()` pour les fichiers temporaires et nettoyez dans `tearDown()`.

```python
class TestMaFeature(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_comportement_attendu(self):
        """Description claire du test en français."""
        # Arrange
        _create_tree(self.tmp, {'Folder': {'Sub': {}, 'file.pdf': None}})
        # Act
        results = ma_fonction(self.tmp)
        # Assert
        self.assertEqual(len(results), 1)
```

## Workflow de contribution

1. Forkez le repo
2. Créez une branche (`git checkout -b feature/ma-feature`)
3. Commitez vos changements
4. Lancez les tests (`./tests/auto/run_all.sh`)
5. Vérifiez avec `ruff check .`
6. Ouvrez une Pull Request

## Idées de contribution

Voici des pistes pour contribuer :

- **Nouveaux providers LLM** — Support OpenAI, Anthropic, Mistral, Google
- **Détection de langue** — Identifier automatiquement la langue du PDF
- **Index SQLite** — Base de données légère pour recherche rapide
- **Prompts templatisés** — Permettre de surcharger les prompts LLM par profil
- **Agents IA** — 3 agents IA spécifiés, à développer dans l'ordre :
  1. [refonte-agent-spec.md](refonte-agent-spec.md) — refonte de taxonomy en 3 phases (diagnostic → proposition → dialog), ~35-50 j-h
  2. [onboarding-agent-spec.md](onboarding-agent-spec.md) — bootstrap d'un nouveau profil depuis une inbox (discovery → proposition → bootstrap), ~28-42 j-h
  3. [curation-agent-spec.md](curation-agent-spec.md) — constitution de sous-ensembles intelligents entre profils (analyze → select → apply), ~22-35 j-h

Idées déjà livrées (à titre de référence) :

- ✅ **Interface web** — dashboard FastAPI + HTMX (Taxonomie, Rename, Curation, etc.)
- ✅ **Couvertures** — `lib/thumbnail.py` + cache content-keyed unifié + CLI `./klodo.sh thumbnails`
- ✅ **Audit de renommages + undo** — journal append-only JSONL avec batch + override par contenu

## Structure des rapports

Les rapports CSV générés dans `logs/` suivent cette convention :

| Fichier | Contenu |
|---|---|
| `classify_YYYYMMDD_HHMMSS.csv` | Résultats de classification |
| `refine_YYYYMMDD_HHMMSS.csv` | Résultats de raffinement |
| `rename_YYYYMMDD_HHMMSS.csv` | Résultats de renommage |
| `suggestions_YYYYMMDD_HHMMSS.json` | Suggestions de nouveaux dossiers |

## Licence

En contribuant, vous acceptez que vos contributions soient soumises à la [licence MIT](../LICENSE) du projet.
