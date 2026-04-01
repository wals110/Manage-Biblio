# Biblio v4.0 — Gestionnaire de bibliothèque PDF

Outil en ligne de commande pour organiser automatiquement une bibliothèque de fichiers PDF :
renommage intelligent, identification par LLM Vision, classement thématique et raffinement.

## Installation

```bash
# macOS
brew install poppler          # Extraction de couvertures PDF
pip3 install -r requirements.txt

# Clé API (SiliconFlow, pour le LLM Vision)
export SILICONFLOW_API_KEY=sk-xxx
```

## Démarrage rapide

```bash
# Pipeline complet : identifier + classer des nouveaux PDFs
./biblio.sh process /chemin/vers/nouveaux_pdfs              # Dry-run
./biblio.sh process /chemin/vers/nouveaux_pdfs --execute     # Appliquer

# Étapes individuelles
./biblio.sh rename /chemin/vers/dossier                      # Renommage seul
./biblio.sh classify /chemin --workers 10                    # LLM Vision + classement
./biblio.sh refine                                           # Raffinement sous-catégories

# Gestion d'erreurs
./biblio.sh classify /chemin --retry-errors --execute        # Retraiter les erreurs
./biblio.sh classify /chemin --reclassify --execute          # Re-mapper sans appel LLM
```

## Sous-commandes

| Commande | Description |
|---|---|
| `process` | Pipeline complet (LLM + classement + raffinement) |
| `classify` | LLM Vision + classement thématique |
| `rename` | Renommage "Titre - Auteur.pdf" (ISBN / métadonnées) |
| `refine` | Déplace les fichiers des catégories parentes vers les bonnes sous-catégories |
| `profiles` | Liste les profils disponibles |
| `init` | Crée un nouveau profil |

## Options principales

| Option | Description |
|---|---|
| `--profile NAME` | Profil à utiliser (défaut: `default`) |
| `--execute` | Appliquer (sinon dry-run) |
| `--report` | Générer un rapport CSV |
| `--workers N` | Threads parallèles (pour classify/process) |
| `--max N` | Limiter à N fichiers |
| `--retry-errors` | Retraiter les fichiers en erreur |
| `--reclassify` | Re-mapper les thèmes sans rappeler le LLM |
| `--reset` | Supprimer le checkpoint et recommencer |
| `--verbose` | Mode détaillé |

## Profils

Un profil définit l'arborescence cible, les mappings de thèmes, les mots-clés de classification
et les règles de raffinement. Chaque utilisateur peut avoir ses propres profils.

```bash
# Créer un nouveau profil
./biblio.sh init mon-profil --target /Volumes/MonDisque/MA_BIBLIO

# Utiliser un profil spécifique
./biblio.sh process /chemin --profile mon-profil --execute

# Lister les profils
./biblio.sh profiles
```

Fichiers d'un profil (`profiles/<nom>/`) :

| Fichier | Contenu |
|---|---|
| `profile.yaml` | Config générale (nom, chemin cible, LLM, defaults) |
| `tree.yaml` | Arborescence des dossiers |
| `theme_mapping.yaml` | Mapping thème LLM → chemin cible |
| `categories.yaml` | Mots-clés de classification (fallback) |
| `refinement.yaml` | Règles de raffinement sous-catégories |

## Structure du projet

```
Manage-Biblio/
├── biblio.py              Point d'entrée unique (CLI)
├── biblio.sh              Lanceur (vérifie deps, SSD, API key)
├── lib/
│   ├── profile.py         Gestion des profils
│   ├── vision.py          Appel LLM Vision (SiliconFlow / Ollama)
│   ├── classifier.py      Classification thème + mots-clés
│   ├── refiner.py         Raffinement sous-catégories
│   ├── checkpoint.py      Reprise / checkpoint thread-safe
│   └── utils.py           Utilitaires (renommage, rapports)
├── profiles/
│   └── default/           Profil par défaut (355 thèmes, 87 dossiers)
├── organiser/
│   ├── ocr_cover.py       Moteur LLM Vision v3 (legacy)
│   ├── biblio_organizer.py  Classifieur mots-clés YAML + TF-IDF
│   └── categories.yaml    Config mots-clés (legacy, copié dans le profil)
├── renommage/
│   ├── biblio_renamer.py  Moteur de renommage
│   └── isbn_cache.json    Cache ISBN (~3400 entrées)
├── requirements.txt
└── logs/                  Rapports CSV + checkpoints
```

## Fonctionnalités clés

- **Checkpoint / reprise** : interruption Ctrl+C avec sauvegarde, reprise automatique au relancement
- **Skip doublons** : les fichiers déjà présents dans la cible sont ignorés
- **Classification hybride** : LLM Vision (prioritaire) + mots-clés YAML (fallback)
- **Multithreading** : `--workers N` pour paralléliser les appels API
- **LLM flexible** : SiliconFlow (cloud) ou Ollama (local) via le profil
