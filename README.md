# Manage-Biblio

Outils de gestion pour une bibliothèque PDF (~18 500 fichiers sur SSD externe).

## Structure du projet

```
Manage-Biblio/
├── renommage/              # Phase 1 : Renommage des fichiers
│   ├── biblio_renamer.py       # Script de renommage intelligent
│   ├── renommer.sh             # Lanceur bash
│   └── isbn_cache.json         # Cache des recherches ISBN
│
├── organiser/              # Phase 2 : Classement thématique
│   ├── biblio_organizer.py     # Méthode A — mots-clés + apprentissage TF-IDF
│   ├── biblio_organizer_api.py # Méthode B — API web (Google Books / Open Library)
│   ├── categories.yaml         # Configuration des catégories et mots-clés
│   └── categories_cache.json   # Cache des catégories trouvées par API (auto-généré)
│
├── logs/                   # Rapports et logs de toutes les opérations
│   ├── rapport_*.csv           # Rapports d'analyse (dry-run)
│   └── log_*.csv               # Logs d'exécution (pour undo)
│
├── docs/                   # Documentation
│   ├── arborescence_finale.md  # Arborescence validée
│   └── proposition_arborescence.md
│
├── organiser.sh            # Lanceur méthode A (mots-clés + apprentissage)
├── organiser_api.sh        # Lanceur méthode B (API web)
├── requirements.txt        # Dépendances Python
└── README.md
```

## Phase 1 — Renommage

Renomme les PDFs vers le format `Titre - Auteur.pdf` en utilisant un pipeline en cascade :

1. Nettoyage du nom de fichier (artefacts web, ISBN collés, etc.)
2. Recherche ISBN en ligne (Google Books / Open Library)
3. Extraction titre/auteur depuis les métadonnées et le texte du PDF
4. Extraction titre depuis le nom du dossier parent

```bash
./renommage/renommer.sh /Volumes/ExtSSD/BIBLIO              # Rapport
./renommage/renommer.sh /Volumes/ExtSSD/BIBLIO --execute     # Appliquer
./renommage/renommer.sh --undo logs/log_renommage_*.csv      # Annuler
```

## Phase 2 — Classement thématique

Classe les PDFs dans une arborescence par discipline en combinant :

1. **Mots-clés** configurables (`organiser/categories.yaml`)
2. **Apprentissage** par l'existant (profils TF-IDF des dossiers déjà classés)
3. **Extraction PDF** pour les fichiers aux noms vagues

```bash
./organiser.sh                                    # Rapport (dry-run)
./organiser.sh --execute                          # Appliquer
./organiser.sh --verbose                          # Détails
./organiser.sh --undo logs/log_classement_*.csv   # Annuler
```

### Ajouter un nouveau thème

Éditer `organiser/categories.yaml` et ajouter une entrée :

```yaml
  - chemin: "01-SCIENCES/INFORMATIQUE/XX-Nouveau-Theme"
    priorite: 5
    mots_cles:
      - "mot clé 1"
      - "mot clé 2"
```

Le système d'apprentissage enrichira automatiquement le vocabulaire
au fil des classements successifs.

## Phase 2B — Classement par API web

Alternative à la méthode A : interroge Google Books et Open Library pour
récupérer les catégories officielles de chaque livre. Exploite le cache
ISBN existant (3200+ entrées) pour minimiser les requêtes.

```bash
./organiser_api.sh                                        # Rapport
./organiser_api.sh --execute                              # Appliquer
./organiser_api.sh --max-api 100                          # Limiter à 100 requêtes
./organiser_api.sh --verbose                              # Détails
./organiser_api.sh --undo logs/log_classement_api_*.csv   # Annuler
```

Les résultats sont mis en cache (`organiser/categories_cache.json`),
donc les exécutions suivantes sont quasi instantanées pour les livres déjà identifiés.

### Comparer les deux méthodes

Lancer les deux en mode rapport (dry-run), puis comparer les CSV :

```bash
./organiser.sh           # → logs/rapport_classement_*.csv
./organiser_api.sh       # → logs/rapport_classement_api_*.csv
```

## Installation

```bash
pip install -r requirements.txt
```

## Licence

MIT
