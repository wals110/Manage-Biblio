# Contribuer à Klodo

[← Retour au README](../README.md)

Merci de votre intérêt pour Klodo ! Ce guide explique comment contribuer au projet.

## Prérequis

- Python 3.9+ (pas de syntaxe 3.10+)
- poppler (extraction PDF → image)
- Les dépendances de `requirements.txt`

## Démarrer

```bash
git clone https://github.com/votre-user/klodo.git
cd klodo
pip3 install -r requirements.txt
```

## Conventions de code

### Python 3.9

Le projet cible **Python 3.9**. Les syntaxes 3.10+ sont interdites :

```python
# ✗ Interdit
def foo(x: dict | None): ...
def bar(items: list[str]): ...

# ✓ Correct
from typing import Optional, List, Dict
def foo(x: Optional[dict]): ...
def bar(items: List[str]): ...
```

### Architecture

- **Modules** dans `lib/` — logique métier, jamais de constantes en dur
- **Point d'entrée unique** : `klodo.py` avec sous-commandes argparse
- **Profils** : toute config externalisée dans `profiles/<nom>/` (YAML)
- **Paramètres CLI** : noms en anglais (`--execute`, `--workers`)
- **Messages et aide** : en français

### Style

- Docstrings pour toutes les fonctions publiques
- Type annotations (style commentaire `# type:` pour compatibilité 3.9)
- Pas de dépendances lourdes sans discussion préalable

## Tests

La suite de tests couvre 61 tests répartis en modules :

```bash
# Lancer tous les tests
./tests/run_all.sh

# Lancer un module spécifique
python -m unittest tests.test_refine -v

# Lancer un test spécifique
python -m unittest tests.test_refine.TestScanAndRefineVision -v
```

### Ajouter des tests

Les tests sont dans `tests/`. Chaque module a son fichier de tests correspondant. Utilisez `tempfile.mkdtemp()` pour les fichiers temporaires et nettoyez dans `tearDown()`.

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
4. Lancez les tests (`./tests/run_all.sh`)
5. Vérifiez la compatibilité Python 3.9
6. Ouvrez une Pull Request

## Idées de contribution

Voici des pistes pour contribuer :

- **Nouveaux providers LLM** — Support OpenAI, Anthropic, Mistral, Google
- **Interface web** — Dashboard pour visualiser et reviewer les suggestions
- **Détection de langue** — Identifier automatiquement la langue du PDF
- **Index SQLite** — Base de données légère pour recherche rapide
- **Couvertures** — Extraction et stockage des thumbnails pour aperçu visuel
- **Prompts templatisés** — Permettre de surcharger les prompts LLM par profil

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
