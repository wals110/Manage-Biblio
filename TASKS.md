# Tasks

## Active

- [ ] **Design du logo Klodo** - Créer le logo avec Walid
- [ ] **Prompts custom templatisés** - Surcharger les prompts LLM (refine, rename, classify) par des templates custom dans le profil YAML, avec variables ({filename}, {subdirs}, {current_folder}, etc.)

## Waiting On

## Someday

- [ ] **Embeddings locaux pour classification** - Remplacer le TF-IDF par `all-MiniLM-L6-v2` (sentence-transformers) pour une compréhension sémantique locale sur le Mac M4 Max. Dépendance ~500 Mo, gain marginal vs LLM Mapper + auto-apprentissage. À évaluer.
- [ ] **Serveur d'inférence local** - Ollama ou vLLM sur le PC 2× GTX 1080 Ti / 64 Go RAM
- [ ] **Interface web pour review des suggestions** - UI pour valider/rejeter les suggestions de nouveaux dossiers
- [ ] **Détection automatique de la langue du PDF**
- [ ] **Génération des couvertures PDF** - Extraire les thumbnails des premières pages pour aperçu visuel
- [ ] **Dashboard visuel** - Métriques : nombre de livres, répartition par thème, etc.
- [ ] **Index SQLite** - Base de données légère pour indexer les métadonnées (titre, auteur, thème, chemin)

## Done

- [x] ~~Refine v2 — scan récursif 3 niveaux + LLM fallback~~ (2026-04-03)
- [x] ~~Barre de progression passe 2 LLM~~ (2026-04-04)
- [x] ~~Escalade vision dans le refine (`--vision`)~~ (2026-04-04)
- [x] ~~Documentation complète (README + 6 docs modulaires + LICENSE)~~ (2026-04-04)
- [x] ~~Renommage complet biblio → klodo~~ (2026-04-04)
- [x] ~~Améliorer la classification LLM~~ (2026-04-04) — titre enrichi dans keywords, prompt mapper sous-dossiers, escalade vision mapper
