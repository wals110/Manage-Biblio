# Biblio Renamer

Renommage intelligent de bibliothèques PDF vers la nomenclature **"Titre - Auteur.pdf"**.

Concu pour nettoyer des bibliothèques PDF dont les fichiers ont des noms cryptiques (ISBN, DOI, identifiants numeriques, artefacts de telechargement Z-Library/Bookos, etc.).

## Fonctionnement

Le script applique un pipeline en cascade pour chaque fichier PDF. Si une etape produit un nom de qualite suffisante, les etapes suivantes sont ignorees.

```mermaid
flowchart TD
    A["📄 Fichier PDF"] --> B{"Le nom est-il\ndeja propre ?"}
    B -- Oui --> C["✅ Ignorer"]
    B -- Non --> D["Phase 1\nNettoyage du nom"]
    D --> D1["Suppression IDs, underscores,\nartefacts web, parentheses,\ncrochets, URL-encoding"]
    D1 --> D2{"Nom de\nqualite ?"}
    D2 -- Oui --> R["✏️ Renommer"]
    D2 -- Non --> E["Phase 2\nRecherche ISBN en ligne"]
    E --> E1["Extraction ISBN du nom\n→ Google Books API\n→ Open Library API"]
    E1 --> E2{"Titre\ntrouve ?"}
    E2 -- Oui --> R
    E2 -- Non --> F["Phase 3\nExtraction PDF"]
    F --> F1["Sous-processus isole\n+ timeout 15s"]
    F1 --> F2["Metadonnees pypdf\n→ Texte pdfplumber\npages 1 a 5"]
    F2 --> F3{"Titre\nvalide ?"}
    F3 -- Oui --> R
    F3 -- Non --> G["Phase 4\nNom du dossier parent"]
    G --> G1{"Dossier type\nTitre ISBN ?"}
    G1 -- Oui --> R
    G1 -- Non --> H["❌ Echec\nnom inchange"]
    R --> V{"Meilleur que\nl'ancien nom ?"}
    V -- Oui --> W["📝 Appliquer\nTitre - Auteur.pdf"]
    V -- Non --> H

    style A fill:#4a90d9,color:#fff
    style C fill:#27ae60,color:#fff
    style W fill:#27ae60,color:#fff
    style H fill:#e74c3c,color:#fff
    style D fill:#f39c12,color:#fff
    style E fill:#f39c12,color:#fff
    style F fill:#f39c12,color:#fff
    style G fill:#f39c12,color:#fff
```

Le script ne modifie **que les fichiers dont le nom est "pas propre"**. Il peut etre relance autant de fois que necessaire.

## Installation

```bash
git clone https://github.com/VOTRE_USER/biblio-renamer.git
cd biblio-renamer
pip install -r requirements.txt
```

## Usage

### Rapport seul (rien n'est modifie)

```bash
./renommer.sh /chemin/vers/biblio
```

Genere un fichier CSV avec tous les renommages proposes.

### Appliquer les renommages

```bash
./renommer.sh /chemin/vers/biblio --execute
```

### Annuler

```bash
./renommer.sh --undo log_renommage_XXXXXXXX.csv
```

### Options

```
--no-online    Desactiver la recherche ISBN en ligne
--no-pdf       Desactiver l'extraction depuis les PDFs (plus rapide)
--report X.csv Utiliser un rapport specifique pour --execute
```

## Nomenclature

- Format : `Titre - Premier Auteur.pdf` ou `Titre.pdf` (si auteur inconnu)
- Accents preserves
- Caracteres supprimes : `()[]{},:;`
- Caracteres conserves : lettres, chiffres, espaces, apostrophes, tirets, points

## Exemples

| Avant | Apres |
|-------|-------|
| `2738119042.pdf` | `Les origines animales de la culture - Dominique Lestel.pdf` |
| `[Ghaleb_Bencheikh]_Le_Coran.pdf` | `Le Coran - Ghaleb Bencheikh.pdf` |
| `0471137707 Computingverybest.pdf` | `Computing concepts with C++ essentials - Horstmann.pdf` |
| `Islam et politique (Mohamed Arkoun) (Z-Library).pdf` | `Islam et politique - Mohamed Arkoun.pdf` |
| `9780195153729 1.pdf` | `Adaptive Thinking - Rationality in the Real World - Gerd Gigerenzer.pdf` |

## Fichiers generes

- `rapport_XXXXXXXX.csv` : rapport des renommages proposes
- `log_renommage_XXXXXXXX.csv` : log des renommages effectues (pour annulation)
- `isbn_cache.json` : cache des recherches ISBN (evite les requetes repetees)

## Licence

MIT
