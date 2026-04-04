# Classification

[← Retour au README](../README.md) · [Architecture](architecture.md)

## Vue d'ensemble

La classification est le cœur de Klodo. Elle transforme un thème détecté par LLM Vision en un chemin de dossier dans l'arborescence cible. Le système utilise 4 niveaux en cascade : chaque niveau n'est essayé que si le précédent échoue.

<p align="center">
  <img src="diagrams/cascade-classification.svg" alt="Cascade de classification" width="550">
</p>

## LLM Vision

L'étape préliminaire extrait la couverture du PDF comme image (via pdf2image + poppler, 150 dpi), la convertit en base64, et l'envoie au modèle de vision. Le LLM analyse l'image et retourne un JSON structuré :

| Champ | Exemple | Détail |
|---|---|---|
| `title` | "Deep Learning" | Titre dans la langue originale du livre |
| `author` | "Ian Goodfellow" | Format "Prénom Nom", vide si non visible |
| `theme` | "Machine Learning" | Discipline principale en anglais, le plus spécifique possible |
| `language` | "en" | Code langue du livre |
| `confidence` | 0.92 | 0.0 à 1.0 — sous 0.3 si le titre est illisible |

Le prompt demande au LLM d'être spécifique sur le thème (ex: "Machine Learning" plutôt que "Computer Science"). C'est ce thème qui sera ensuite utilisé par la cascade de classification (Niveaux 1-4).

Le modèle par défaut est **Qwen3-VL** via SiliconFlow. Il est configurable dans `profile.yaml` (voir [Profils](profils.md)). Un endpoint Ollama local peut aussi être utilisé.

L'option `--pages N` envoie les N premières pages au lieu de la seule couverture. Quand N > 1, un prompt alternatif est utilisé qui demande explicitement de chercher le titre spécifique du livre et non le nom de la collection — important pour les Springer LNCS, les ACM Proceedings, etc., où la couverture affiche le nom de la série et le vrai titre est sur la page 2 ou 3.

**Configuration** : `max_tokens: 300`, `temperature: 0.1`, timeout 30s. Les images non-livre (confiance 0) et les titres génériques sont rejetés.

**Module** : `lib/vision.py` — Fonctions : `extract_cover_image()`, `image_to_base64()`, `call_vision_api()`, `analyze_cover()`.

## Niveau 1 : Theme Mapping

Recherche directe du thème dans un dictionnaire de 355+ entrées (`theme_mapping.yaml`).

```yaml
# Extrait de theme_mapping.yaml
Machine Learning: 02-INFORMATIQUE/05-IA-ML
Deep Learning: 02-INFORMATIQUE/05-IA-ML
Natural Language Processing: 02-INFORMATIQUE/05-IA-ML/NLP
Quantum Physics: 01-SCIENCES/PHYSIQUE
Islamic Finance: 05-RELIGIONS/ISLAM
```

C'est le chemin le plus rapide : une simple lookup dans un dictionnaire. Aucun appel API, aucun calcul. Résout ~80% des fichiers au premier passage.

## Niveau 2 : Keyword Matcher

Si le theme mapping échoue, le système cherche des mots-clés dans un texte enrichi combinant le titre détecté par Vision, le thème et le nom de fichier. Cela permet de classer même les fichiers aux noms illisibles (hash, IDs numériques) grâce au titre identifié par le LLM.

Les mots-clés sont définis dans `categories.yaml` avec des scores pondérés. Le classifieur TF-IDF (dans `organiser/klodo_organizer.py`) complète avec un matching statistique.

**Pièges connus** et protections :
- `WORD_BOUNDARY_KEYWORDS` pour les mots courts (≤3 chars) : évite "AI" dans "CHAIR"
- Pénalité ×0.1 pour changement de catégorie top-level : évite les faux positifs inter-sections
- Exclusions explicites : "bert" dans "Albert", "christ" dans "Christopher", "bible" dans "Linux Bible"

## Niveau 3 : LLM Mapper

Pour les thèmes que ni le mapping ni les mots-clés n'ont résolus, un appel LLM texte envoie au modèle le thème, le titre, le nom du fichier et **la liste complète des 88 dossiers** de `tree.yaml`. Le LLM doit choisir le dossier le plus précis (le plus profond dans l'arborescence) parmi ceux qui existent.

Le prompt impose des règles strictes : le dossier retourné doit être une copie exacte d'un dossier existant, la confiance est entre 0.0 et 1.0, et si aucun dossier ne convient le LLM doit répondre `_AUCUN`. Le résultat est un objet JSON avec `folder`, `confidence` et `reason`.

```
Entrée envoyée au LLM :
  - Thème : "Mechatronics"
  - Titre : "Introduction to Mechatronics and Measurement Systems"
  - Fichier : "intro_mechatronics.pdf"
  - 88 dossiers : [01-SCIENCES/MATHEMATIQUES, ..., 09-BUSINESS/FINANCE]

Réponse attendue :
  {"folder": "03-INGENIERIE/ROBOTIQUE", "confidence": 0.85, "reason": "mécatronique = robotique + électronique"}
```

### Validation du résultat

Le résultat est validé par `_process_mapper_result()` avant d'être accepté : le dossier doit exister dans `tree.yaml`, la confiance doit dépasser `min_confidence` (défaut 0.6), et les réponses `_AUCUN` ou `_A-TRIER` sont rejetées.

### Auto-apprentissage

Si le LLM résout un thème avec une confiance suffisante, l'entrée est ajoutée automatiquement dans `theme_mapping.yaml`. La prochaine fois qu'un fichier a le même thème, il sera résolu en Niveau 1 (instantané, gratuit). C'est ce qui fait que le système s'améliore avec le temps : les premiers runs coûtent plus d'appels API, les suivants sont de plus en plus rapides.

### Escalade Vision (option `--vision`)

Si le mapper texte échoue (réponse `_AUCUN`) et que `--vision` est activé, le système tente une escalade : la couverture du PDF est extraite, convertie en base64, et envoyée au LLM avec un prompt multimodal et la même liste de dossiers. Le LLM peut ainsi exploiter le contenu visuel (titre imprimé, logos, illustrations) pour classer le fichier.

```
Exemple 1 — Succès texte :
  Thème : "Mechatronics"
  → LLM Mapper texte : "03-INGENIERIE/ROBOTIQUE" (confiance 0.85)
  → Auto-ajout dans theme_mapping.yaml
  → Prochain fichier "Mechatronics" → résolu au Niveau 1

Exemple 2 — Escalade vision (avec --vision) :
  Thème : "Unknown" / fichier : "a3f2b9c.pdf"
  → LLM Mapper texte : _AUCUN (le thème et le nom ne disent rien)
  → Escalade vision : couverture du PDF envoyée au LLM
  → La couverture montre "Principles of Optics - Max Born"
  → "01-SCIENCES/PHYSIQUE" (confiance 0.80)
```

### Configuration

Le LLM Mapper utilise le même endpoint et modèle que le reste du pipeline, configurés dans `profile.yaml`. Les paramètres d'appel : `max_tokens: 150`, `temperature: 0.1` (réponses déterministes), timeout de 20s avec 3 tentatives en cas d'erreur 429 (rate limit).

**Module** : `lib/llm_mapper.py`

## Niveau 4 : Suggestions

Si même le LLM Mapper (texte + vision) ne trouve pas de dossier adapté, il génère une **suggestion de nouveau dossier**. Le LLM reçoit un prompt différent : au lieu de choisir parmi les dossiers existants, il doit en **proposer un nouveau** qui s'intégrerait dans l'arborescence.

### Ce que le LLM reçoit

Le prompt de suggestion envoie le thème, le titre du livre, et la liste complète des dossiers existants. Le LLM doit proposer un chemin `SECTION/NOUVEAU-DOSSIER` dont le parent est l'une des 9 sections racines (01-SCIENCES, 02-INFORMATIQUE, etc.). Le nom doit suivre le style existant (majuscules, tirets, pas d'accents) et être assez large pour accueillir plusieurs livres.

```
Entrée :
  - Thème : "Aviation"
  - Titre : "Principles of Flight"
  - 88 dossiers existants

Réponse :
  {"folder": "03-INGENIERIE/AVIATION", "parent": "03-INGENIERIE", "reason": "aéronautique = ingénierie spécialisée"}
```

### Stockage des suggestions

Les suggestions sont accumulées en mémoire pendant le scan, puis sauvegardées dans `logs/suggestions.yaml` à la fin du run. Le fichier est fusionné avec les suggestions existantes (pas de doublons par thème). Chaque suggestion a un statut `pending` ou `applied`.

```yaml
# Extrait de logs/suggestions.yaml
- theme: "Aviation"
  title: "Principles of Flight"
  filename: "principles_flight.pdf"
  folder: "03-INGENIERIE/AVIATION"
  parent: "03-INGENIERIE"
  reason: "aéronautique = ingénierie spécialisée"
  status: pending
```

### La commande `suggest`

La commande `suggest` a trois modes d'utilisation :

**Review** (par défaut) : `./klodo.sh suggest` affiche un tableau avec toutes les suggestions en attente — thème, dossier proposé, raison. L'utilisateur peut ensuite éditer `logs/suggestions.yaml` à la main pour supprimer les suggestions non voulues ou ajuster les chemins proposés.

**Apply** : `./klodo.sh suggest --apply` prend toutes les suggestions `pending` et pour chacune effectue 4 mises à jour :

1. **Dossier physique** — crée le dossier sur le disque dans la bibliothèque (ex: `/chemin/vers/biblio/03-INGENIERIE/AVIATION/`)
2. **`tree.yaml`** — ajoute le nouveau chemin dans la liste des dossiers de l'arborescence, pour que le LLM Mapper le connaisse lors des prochains runs
3. **`theme_mapping.yaml`** — ajoute l'entrée thème → chemin (ex: `Aviation: 03-INGENIERIE/AVIATION`), pour que le Niveau 1 (Theme Mapping) résolve directement ce thème la prochaine fois
4. **`suggestions.yaml`** — marque la suggestion comme `applied`

**Apply + reclassify** : `./klodo.sh suggest --apply --execute` fait tout ce que `--apply` fait, puis relance un `reclassify` sur le checkpoint existant. Les fichiers qui étaient en `_A-TRIER` parce que leur thème n'existait pas peuvent maintenant être classés grâce aux nouveaux dossiers.

```bash
# 1. Review : voir les suggestions
./klodo.sh suggest

# 2. Éditer logs/suggestions.yaml si nécessaire

# 3. Appliquer : créer les dossiers + enrichir les mappings
./klodo.sh suggest --apply

# 4. Appliquer + reclassifier les fichiers non classés
./klodo.sh suggest --apply --execute
```

C'est un cycle d'amélioration itératif : `classify` génère des suggestions → l'utilisateur les valide → `suggest --apply` enrichit l'arborescence → les prochains `classify` bénéficient des nouveaux dossiers.

## Commandes CLI

### `classify` — Classification seule

```bash
./klodo.sh classify /chemin --workers 10           # Classifier avec 10 threads
./klodo.sh classify /chemin --retry-errors --execute  # Retraiter les erreurs
./klodo.sh classify /chemin --reclassify --execute    # Re-mapper sans rappeler l'API
./klodo.sh classify /chemin --reset                   # Recommencer à zéro
```

### `process` — Pipeline complet (4 étapes)

Le pipeline `process` orchestre les 4 étapes dans l'ordre :

1. **Rename** — renommage intelligent des fichiers (sauf si `--no-rename`)
2. **Classify** — LLM Vision + classification thématique
3. **Copie** — déplacement vers l'arborescence cible (avec `--execute`)
4. **Refine** — raffinement des sous-catégories (avec `--execute`)

Le renommage en premier est important : un fichier bien nommé fournit plus de signal au keyword classifier, ce qui améliore la classification.

```bash
./klodo.sh process /chemin --execute --workers 10        # Pipeline complet
./klodo.sh process /chemin --llm --pages 2 --execute     # Avec LLM Vision pour rename
./klodo.sh process /chemin --no-rename --execute          # Sans renommage
./klodo.sh process /chemin --max 50 --verbose             # Test limité avec détails
```

### Options

| Option | Commandes | Description |
|---|---|---|
| `--api-key KEY` | classify, process | Clé API (ou variable `SILICONFLOW_API_KEY`) |
| `--workers N`, `-w N` | classify, process | Threads parallèles (0 = défaut du profil) |
| `--max N` | classify, process | Limiter à N fichiers |
| `--delay SEC` | classify, process | Délai entre requêtes séquentielles (défaut: 0.2) |
| `--reset` | classify, process | Supprimer le checkpoint et recommencer |
| `--retry-errors` | classify, process | Retraiter les fichiers en erreur |
| `--reclassify` | classify, process | Re-mapper les thèmes sans rappeler l'API |
| `--vision` | classify, process | Escalade vision dans le LLM Mapper quand le texte échoue |
| `--pages N` | classify, process, rename | Nombre de pages analysées par LLM Vision (défaut: 1) |
| `--no-rename` | process | Sauter l'étape de renommage |
| `--llm` | process, rename | Activer LLM Vision pour le renommage |
| `--force` | process, rename | Re-analyser tous les fichiers (ignore is_name_clean) |
| `--no-online` | process, rename | Désactiver la recherche ISBN en ligne |
| `--no-pdf` | process, rename | Désactiver l'extraction PDF |

## Séparation classify / rename

La commande `classify` fait **uniquement** de la classification. Elle ne renomme pas les fichiers. Le renommage est géré exclusivement par la commande `rename` (voir [Renommage](renommage.md)), qui dispose d'un pipeline complet en 5 sources (nettoyage, ISBN, métadonnées PDF, LLM Vision, dossier parent).

Deux parcours sont possibles :

- **Parcours complet** : `process` = rename → classify → copie → refine (pour des fichiers bruts mal nommés)
- **Parcours classification** : `classify` → `refine` → `suggest` (pour des fichiers déjà bien nommés, pas de renommage)

## Checkpoint et reprise

Le `CheckpointManager` (`lib/checkpoint.py`) sauvegarde l'état de progression de façon atomique et thread-safe. En cas d'interruption (Ctrl+C), le relancement reprend automatiquement là où il s'est arrêté.
