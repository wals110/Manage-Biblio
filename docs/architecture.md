# Architecture

[← Retour au README](../README.md)

## Vue d'ensemble

Klodo est un outil CLI modulaire construit autour d'un pipeline de traitement de fichiers PDF. Chaque étape du pipeline est implémentée dans un module indépendant sous `lib/`, orchestré par le point d'entrée `klodo.py`.

<p align="center">
  <img src="diagrams/architecture-modules.svg" alt="Architecture des modules" width="650">
</p>

## Structure du projet

```
klodo/
├── klodo.py                   Point d'entrée CLI (sous-commandes argparse)
├── klodo.sh                   Lanceur shell (vérifie deps, SSD, API key)
│
├── lib/                       Modules métier
│   ├── __init__.py            Version (1.0.0-dev)
│   ├── constants.py           Constantes centralisées (timeouts, seuils, retries)
│   ├── exceptions.py          Hiérarchie d'exceptions (KlodoError → ...)
│   ├── llm_client.py          Client HTTP unifié (requests.Session, retries)
│   ├── vision.py              Extraction couverture + appel LLM Vision
│   ├── classifier.py          Classification hybride (4 niveaux)
│   ├── keyword_classifier.py  Classifieur YAML + TF-IDF
│   ├── llm_mapper.py          Résolution LLM de thèmes + auto-apprentissage
│   ├── refiner.py             Raffinement récursif (3 niveaux + vision)
│   ├── renamer.py             Moteur de renommage (ISBN, PDF, LLM Vision)
│   ├── checkpoint.py          Checkpoint / reprise thread-safe
│   ├── profile.py             Chargement profils YAML
│   ├── utils.py               Sanitize, rapports CSV, résumés
│   └── logger.py              Logger configurable (console + fichier)
│
├── commands/                  Sous-commandes CLI
│   ├── __init__.py            Exports (re-exports explicites)
│   ├── helpers.py             Helpers partagés (copie, classify, SafetyError)
│   ├── classify.py            cmd_classify + scan_and_classify
│   ├── rename.py              cmd_rename
│   ├── refine.py              cmd_refine
│   ├── process.py             cmd_process (pipeline complet)
│   └── misc.py                cmd_profiles, cmd_init, cmd_suggest, cmd_clean
│
├── profiles/                  Profils de configuration
│   └── default/
│       ├── .cache/            Cache du profil (gitignored)
│       │   ├── progress.json  Checkpoint de progression
│       │   └── isbn_cache.json Cache ISBN (~3400 entrées)
│       ├── profile.yaml       Config générale (LLM, seuils, options)
│       ├── tree.yaml          Arborescence cible (88 dossiers)
│       ├── theme_mapping.yaml Mapping thème → chemin (355+ entrées)
│       ├── categories.yaml    Mots-clés de classification (7 sections)
│       └── refinement.yaml    Règles de raffinement (45 règles)
│
├── scripts/                   Scripts utilitaires
│   ├── flatten_to_inbox.sh    Remise à plat des PDFs vers _INBOX (tests)
│   └── clean_logs.sh          Nettoyage des rapports CSV
│
├── tests/                     Suite de tests (281 tests)
│   ├── run_all.sh             Lanceur de tests
│   ├── test_compile.py        Compilation
│   ├── test_imports.py        Imports croisés
│   ├── test_safety.py         Sécurité inbox (6 tests)
│   ├── test_copy.py           Copie + suppression (14 tests)
│   ├── test_parser.py         Parser CLI (22 tests)
│   ├── test_classifier.py     Classification (25 tests)
│   ├── test_llm_client.py     Client HTTP (20 tests)
│   ├── test_rename_llm.py     Rename + LLM (18+ tests)
│   ├── test_refine.py         Refine récursif + vision (60+ tests)
│   ├── test_integration.py    Tests intégration (6 tests)
│   └── CAHIER_DE_TESTS.md     Documentation complète des tests
│
├── docs/                      Documentation
├── logs/                      Rapports CSV horodatés (gitignored)
├── pyproject.toml             Config projet, dépendances, ruff
└── LICENSE
```

## Modules

### `vision.py` — LLM Vision

Gère l'extraction de couvertures PDF et l'appel à un modèle de vision (SiliconFlow ou Ollama). Retourne un titre, auteur, thème et score de confiance.

Fonctions principales : `extract_cover_image()`, `image_to_base64()`, `call_vision_api()`, `analyze_cover()`.

→ [Documentation complète](classification.md)

### `classifier.py` — Classification hybride

Orchestre les 4 niveaux de classification : theme mapping, keyword matcher (TF-IDF), LLM mapper, et fallback basse confiance. Délègue à `lib/keyword_classifier.py` pour le matching par mots-clés.

→ [Documentation complète](classification.md)

### `llm_mapper.py` — Résolution de thèmes

Quand le theme mapping et les mots-clés échouent, envoie le thème au LLM pour trouver le bon dossier. Le résultat est auto-appris dans `theme_mapping.yaml` pour les prochaines fois.

→ [Documentation complète](classification.md#niveau-3--llm-mapper)

### `refiner.py` — Raffinement

Parcours récursif de l'arborescence avec `os.walk()`. Détecte les fichiers mal placés (dans un dossier non-feuille) et les déplace vers le bon sous-dossier via 3 niveaux de matching + escalade vision.

→ [Documentation complète](raffinement.md)

### `checkpoint.py` — Reprise

Sauvegarde atomique et thread-safe de l'état de progression. Permet l'interruption par Ctrl+C et la reprise automatique au relancement.

### `profile.py` — Profils

Charge et valide les fichiers YAML d'un profil. Supporte les valeurs par défaut et la résolution de chemins.

→ [Documentation complète](profils.md)

### `utils.py` — Utilitaires

Sanitize des noms de fichiers, génération de rapports CSV, résumés console, et fonctions de renommage partagées.

## Flux de données

<p align="center">
  <img src="diagrams/flux-donnees.svg" alt="Flux de données" width="650">
</p>

## Technologies

| Composant | Technologie |
|---|---|
| Langage | Python 3.13 (via uv) |
| LLM Vision | Qwen3-VL (SiliconFlow / Ollama) |
| Extraction PDF | pdf2image + poppler |
| Métadonnées PDF | pypdf + pdfplumber |
| Classification | TF-IDF (lib/keyword_classifier.py) |
| Configuration | YAML (pyyaml) |
| Parallélisme | ThreadPoolExecutor |
| Persistance | Fichiers JSON dans profiles/\<nom\>/.cache/ |
