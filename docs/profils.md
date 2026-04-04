# Profils

[← Retour au README](../README.md) · [Architecture](architecture.md)

## Vue d'ensemble

Un profil définit une configuration complète pour une bibliothèque : arborescence cible, mappings de thèmes, mots-clés de classification et règles de raffinement. Chaque bibliothèque peut avoir son propre profil indépendant.

```
profiles/
├── default/                   Profil principal
│   ├── .cache/                Cache auto-géré (gitignored)
│   │   ├── progress.json      Checkpoint de progression
│   │   └── isbn_cache.json    Cache ISBN (~3400 entrées)
│   ├── profile.yaml           Config générale
│   ├── tree.yaml              Arborescence (88 dossiers)
│   ├── theme_mapping.yaml     Mapping thème → chemin (355+)
│   ├── categories.yaml        Mots-clés classification
│   └── refinement.yaml        Règles raffinement (45)
│
└── ma-biblio/                 Profil personnalisé
    ├── .cache/
    ├── profile.yaml
    ├── tree.yaml
    └── ...
```

Le dossier `.cache/` est créé automatiquement au premier run. Il contient les fichiers d'état liés au profil (checkpoint, cache ISBN). Nettoyable via `./klodo.sh clean all --profile <nom> --execute`.

## Créer un profil

```bash
# Créer un profil avec une cible spécifique
./klodo.sh init ma-biblio --target /chemin/vers/mes-livres

# Lister les profils disponibles
./klodo.sh profiles

# Utiliser un profil spécifique
./klodo.sh process /chemin --profile ma-biblio
```

La commande `init` crée un dossier dans `profiles/` avec les fichiers YAML par défaut. Vous pouvez ensuite les personnaliser.

## Configuration — `profile.yaml`

```yaml
# Identité du profil
name: default
description: "Bibliothèque principale"
target: /chemin/vers/biblio              # Dossier racine de la bibliothèque
inbox: /chemin/vers/biblio/_INBOX       # Dossier d'entrée des nouveaux PDFs
fallback: _A-TRIER                      # Sous-dossier pour les non-classifiés

# Configuration LLM
llm:
  provider: siliconflow                 # siliconflow | ollama
  model: Qwen/Qwen3-VL-32B-Instruct
  endpoint: https://api.siliconflow.com/v1/chat/completions

# Paramètres par défaut
defaults:
  workers: 10                # Threads parallèles
  delay: 0.2                 # Délai entre requêtes séquentielles (s)
  min_confidence: 0.5        # Seuil de confiance
  max_filename_length: 180   # Longueur max du nom de fichier
  pdf_timeout: 15            # Timeout extraction PDF (s)
  cost_per_call: 0.00034     # Coût estimé par appel LLM ($/appel)
  max_api_errors: 10         # Arrêt après N erreurs API consécutives
  max_retries: 3             # Tentatives par appel API
  llm_mapper: true           # Activer le LLM Mapper
  mapper_min_confidence: 0.6 # Confiance min pour auto-apprentissage
```

## Providers LLM

### SiliconFlow (cloud)

Provider par défaut. Accès gratuit pour les modèles open-source (Qwen, Llama, etc.).

```yaml
llm:
  provider: siliconflow
  model: Qwen/Qwen3-VL-32B-Instruct
  endpoint: https://api.siliconflow.com/v1/chat/completions
```

```bash
export SILICONFLOW_API_KEY=votre-clé-api
```

### Ollama (local)

Pour une utilisation 100% locale, sans appel cloud.

```yaml
llm:
  provider: ollama
  model: qwen2.5-vl:7b
  endpoint: http://localhost:11434/v1/chat/completions
```

Pas de clé API nécessaire. Nécessite [Ollama](https://ollama.ai) installé avec le modèle téléchargé.

## Arborescence — `tree.yaml`

Définit la structure de dossiers cible. Klodo crée automatiquement les dossiers manquants.

```yaml
# Extrait de tree.yaml (88 dossiers)
- 01-SCIENCES:
  - MATHEMATIQUES
  - PHYSIQUE
  - CHIMIE
  - BIOLOGIE
  - ASTRONOMIE
  - EPISTEMOLOGIE

- 02-INFORMATIQUE:
  - 01-Fondamentaux
  - 02-Algorithmes
  - 03-Langages
  - 04-Systemes
  - 05-IA-ML:
    - Deep-Learning
    - NLP
    - Vision-par-Ordinateur
  # ... 16 sous-sections au total
```

## Theme Mapping — `theme_mapping.yaml`

Dictionnaire thème → chemin. C'est le Niveau 1 de la classification (le plus rapide). Ce fichier est **auto-enrichi** par le LLM Mapper.

```yaml
# Entrées statiques
Machine Learning: 02-INFORMATIQUE/05-IA-ML
Quantum Physics: 01-SCIENCES/PHYSIQUE
Islamic Finance: 05-RELIGIONS/ISLAM

# Entrées auto-apprises (ajoutées par le LLM Mapper)
Mechatronics: 03-INGENIERIE/ROBOTIQUE
Aviation: 03-INGENIERIE/TELECOM
```

Le fichier passe de ~100 entrées initiales à 355+ après traitement de la bibliothèque. Chaque thème résolu par le LLM Mapper est ajouté ici pour être résolu instantanément la prochaine fois.

## Categories — `categories.yaml`

Mots-clés pondérés pour le Niveau 2 de la classification (Keyword Matcher).

```yaml
INFORMATIQUE:
  IA-ML:
    keywords:
      - neural: 3.0
      - machine learning: 2.5
      - deep learning: 2.5
      - tensorflow: 2.0
    word_boundary:
      - ai
      - ml
```

La section `word_boundary` force un matching par frontière de mot (évite "ai" dans "chair").

## Refinement — `refinement.yaml`

Règles explicites pour le raffinement des sous-catégories.

```yaml
- parent: 02-INFORMATIQUE/05-IA-ML
  target: Deep-Learning
  keywords: [neural, cnn, rnn, deep learning, tensorflow, pytorch]

- parent: 01-SCIENCES/PHYSIQUE
  target: Optique
  keywords: [light, laser, optics, photon]
```

Voir [Raffinement](raffinement.md) pour les détails.
