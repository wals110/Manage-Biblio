# Classification

[← Retour au README](../README.md) · [Architecture](architecture.md)

## Vue d'ensemble

La classification est le cœur de Klodo. Elle transforme un thème détecté par LLM Vision en un chemin de dossier dans l'arborescence cible. Le système utilise une cascade de **5 priorités** implémentées dans `lib.classifier.classify_combined()` :

| Priorité | Mécanisme | Coût |
|---|---|---|
| **P1** | Theme Mapping si confiance ≥ seuil — avec `refine_to_subfolder` (affine vers sous-dossier sœur plus spécifique) + trigger conditionnel N3 sur catch-all | Gratuit (lookup) |
| **P2** | Keyword Matcher sur texte enrichi (titre + thème + filename) | Gratuit (regex + TF-IDF) |
| **P3** | LLM Mapper — résolution intelligente d'un thème inconnu (texte, ou vision si `--vision`) | 1 appel LLM |
| **P4** | Theme Mapping en fallback bas confiance — accepte le thème LLM même si confiance < seuil | Gratuit |
| **P5** | **FAILED** — rapporté en `non_identifié`, le fichier reste dans `_A-TRIER` | — |

Historiquement décrit comme "4 niveaux", la cascade est restée à 4 niveaux conceptuels (N1 mapping, N2 keyword, N3 LLM mapper, N4 suggestions) mais le code expose en réalité une 5e priorité de fallback (P4) avant l'échec total, et le N1 a été raffiné en mai 2026.

**N4 (Suggestions) est un mécanisme parallèle** : pas une priorité de classement mais un générateur de propositions de nouveaux dossiers (review humain), déclenché quand P3 répond `_AUCUN`.

<p align="center">
  <img src="diagrams/cascade-classification.svg" alt="Cascade de classification" width="850">
</p>

## Les fichiers YAML du pipeline

La classification s'appuie sur trois fichiers YAML distincts, chacun avec un rôle précis. Cette section les compare pour lever toute ambiguïté.

| Fichier | Étape pipeline | Modifié par | Rôle |
| --- | --- | --- | --- |
| `theme_mapping.yaml` | 1 — Theme Mapping (rapide, gratuit) | édition manuelle + drag-drop dashboard Taxonomie + auto-apprentissage LLM Mapper | mapping direct `thème → dossier` |
| `categories.yaml` | 2 — Keyword Classifier (fallback) | édition manuelle uniquement | mots-clés par dossier, scan multi-source (titre, thème, filename) |
| `.cache/taxonomy-backups/theme_mapping-YYYYMMDD-HHMMSS.yaml` | aucune (rollback) | écrit automatiquement avant chaque write du dashboard Taxonomie | snapshot horodaté pour annulation, rotation 20 |

### `theme_mapping.yaml` — étape 1

C'est la **table principale** : une lookup directe `clé thème → chemin dossier`. Utilisé par `classify_by_theme()` au début du pipeline.

```yaml
# Extrait
physics: 01-SCIENCES/PHYSIQUE
quantum mechanics: 01-SCIENCES/PHYSIQUE/05-Relativite-Quantique
java: 02-INFORMATIQUE/03-Langages-Programmation/Java
```

**Logique de match** (dans l'ordre) :

1. Match **exact** insensible à la casse
2. Match **substring** longest-wins, avec **word-boundary** pour les clés mono-mot (évite `art` dans `Particle`, `surface` dans `surfaces`)
3. Match **substring inverse** (le thème apparaît dans une clé plus longue)

### `categories.yaml` — étape 2

C'est le **filet de sauvetage** : utilisé seulement si l'étape 1 a échoué. La structure est différente — c'est une liste de catégories, chacune avec ses mots-clés et sa priorité.

```yaml
# Extrait
- chemin: 01-SCIENCES/PHYSIQUE/01-Mecanique
  priorite: 5
  mots_cles:
    - mechanics
    - mécanique
    - fluid dynamics
    - Newton
    - Lagrange
```

Le `KeywordClassifier` scanne **titre + thème + filename** combinés, à la recherche des mots-clés. Une catégorie gagne si elle accumule plus de matches (pondérés par priorité). Word-boundary appliqué aux clés mono-mot pour les mêmes raisons que `theme_mapping`.

### `.cache/taxonomy-backups/theme_mapping-*.yaml` — rollback

Snapshots horodatés de `theme_mapping.yaml` créés automatiquement par le dashboard Taxonomie **avant chaque écriture** (drag-drop, popover Mapper). Rotation à 20 backups (les plus anciens sont supprimés).

Pour annuler une modification ratée : copie le backup le plus récent par-dessus `theme_mapping.yaml`, recharge le dashboard.

```text
profiles/default/.cache/taxonomy-backups/
├── theme_mapping-20260511-204512.yaml   ← le plus récent
├── theme_mapping-20260511-185233.yaml
├── theme_mapping-20260510-145822.yaml
└── …  (max 20)
```

### Quand modifier quoi

| Symptôme | Fichier à éditer | Mécanisme |
| --- | --- | --- |
| Un thème LLM revient souvent et n'est pas mappé | `theme_mapping.yaml` | Drag-drop dans dashboard Taxonomie (recommandé) ou édition à la main |
| Un mot-clé spécifique dans le titre/filename devrait toujours router vers un dossier (indépendamment du thème LLM) | `categories.yaml` | Édition manuelle uniquement |
| Une mauvaise mapping vient d'être ajoutée et il faut revenir en arrière | restaurer depuis `taxonomy-backups/` | Copie manuelle du backup le plus récent |

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

### Prompt v2 — Multi-candidate themes (mai 2026)

Depuis le commit `3d89316`, le prompt vision demande au LLM de retourner une **liste rankée de thèmes** plutôt qu'un seul, avec `confidence` et `reason` pour chacun :

```json
{
  "title": "Quantum Field Theory in a Nutshell",
  "author": "A. Zee",
  "themes": [
    {"theme": "Quantum Field Theory", "confidence": 0.95, "reason": "titre explicite"},
    {"theme": "Theoretical Physics",  "confidence": 0.85, "reason": "discipline parente"},
    {"theme": "Particle Physics",     "confidence": 0.70, "reason": "domaine connexe"}
  ]
}
```

Le `classify_combined()` itère sur ces candidats par ordre de confiance et applique la logique **`best_specific` vs `best_generic`** : il garde le 1er thème qui mappe vers un dossier spécifique (plusieurs segments) ; s'il n'en trouve qu'avec des dossiers génériques (racine de section), il garde le meilleur en fallback. Ce traitement multi-candidate permet de récupérer un classement précis quand le thème top-1 du LLM est correct mais pointe vers un dossier trop généraliste.

Le format legacy (un seul champ `theme`) reste supporté — un seul candidat est alors évalué.

## Niveau 1 : Theme Mapping (+ refine_to_subfolder + trigger conditionnel N3)

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

### refine_to_subfolder — affinement N1 avant N3

Avant même de déclencher le trigger N3, le code applique `_refine_to_subfolder()` (lib/classifier.py:169). L'idée : si N1 mappe vers un dossier parent `02-INFORMATIQUE/03-Langages-Programmation`, on regarde s'il existe un sous-dossier sœur plus spécifique (`Java`, `Python`, `C-Cpp-CSharp`, …) qui matche mieux le thème, le titre ou le nom de fichier.

Le raffinement opère **dans la même branche top-level** (pas de cross-section) et exige que :

- Le sous-dossier candidat existe dans `tree.yaml`
- Un de ses keywords ou la racine de son nom matche le thème / titre / filename
- Le chemin retourné est strictement plus profond que le N1 d'origine

Si plusieurs sous-dossiers matchent, le scoring est pondéré (boost si le thème exact = dernier segment du sous-dossier). Cette étape est gratuite (pas d'appel LLM) et s'applique à 100 % des fichiers où N1 a réussi.

L'output est labellisé **`LLM (theme→refined)`** dans le CSV pour distinguer du N1 brut.

### Trigger conditionnel N3 sur catch-all

Le mapping résout parfois sur un **dossier catch-all** : `02-INFORMATIQUE/03-Langages-Programmation/Autres`, `05-RELIGIONS/AUTRES-RELIGIONS`, `01-SCIENCES/MATHEMATIQUES/08-Mathematiques-Generales`, etc. C'est l'aveu éditorial que `theme_mapping.yaml` n'a pas plus précis pour ce thème — alors qu'un sous-dossier sœur plus spécifique existe dans `tree.yaml`.

Pour ces ~12% des fichiers, le système appelle conditionnellement le **LLM Mapper (Niveau 3)** pour challenger N1, **sans toucher aux 88% restants** (qui sortent du Niveau 1 propres et gratuits).

Garde-fous (cf. `lib.classifier._challenge_generic_fallback`) :

- **Même section top-level** : refus des swaps inter-sections (ceux-là sont de vrais désaccords, traités ailleurs)
- **Cible non catch-all elle-même** : pas de swap inutile vers un autre `/Autres`
- **Profondeur ≥ N1** : N3 ne peut pas promouvoir vers un ancêtre générique

Exemples mesurés empiriquement (audit 100 fichiers, seed=42) :

| N1 (catch-all) | N3 (trigger fired) |
|---|---|
| `/Langages-Programmation/Autres` | `/Langages-Programmation/Java` (Java I/O) |
| `/Langages-Programmation/Autres` | `/Langages-Programmation/C-Cpp-CSharp` (Exceptional C++) |
| `/Langages-Programmation/Autres` | `/Systemes-OS/Linux-Unix` (Advanced UNIX Programming) |

**Coût / bénéfice mesuré** :

- Trigger fire sur **12%** des fichiers (les catch-all)
- Upgrade effectif sur **6%** des fichiers (50% taux de succès sur les catch-all)
- Coût LLM additionnel : **~+12%** vs cascade pure
- **5/6 upgrades objectivement meilleurs** (revue manuelle des 6 cas)

L'alternative envisagée (parallélisation systématique N1+N2+N3 sur 100% des fichiers) aurait coûté ×5 pour ~3% de gain marginal — non rentable. Le trigger ciblé est le bon compromis.

Le résultat retourne sous le label **`LLM (theme→N3-refined)`** dans le CSV de classification pour distinguer les cas où le trigger a tiré.

## Niveau 2 : Keyword Matcher

Si le theme mapping échoue, le système cherche des mots-clés dans un texte enrichi combinant le titre détecté par Vision, le thème et le nom de fichier. Cela permet de classer même les fichiers aux noms illisibles (hash, IDs numériques) grâce au titre identifié par le LLM.

Les mots-clés sont définis dans `categories.yaml` avec des scores pondérés. Le classifieur TF-IDF (`lib/keyword_classifier.py`) complète avec un matching statistique.

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

## Priorité 4 : Theme Mapping fallback bas confiance

Avant de déclarer l'échec total, le code tente une dernière fois `classify_by_theme()` avec le thème LLM même si sa confiance était sous `CONFIDENCE_THRESHOLD` (0.6 par défaut). Si le thème existe quand même dans `theme_mapping.yaml`, le fichier est rangé là, avec le label **`LLM (fallback)`** dans le CSV.

L'intuition : si le LLM a hésité (conf 0.45 sur "Mécanique quantique") mais que le mapping a bien une entrée pour ce thème, il vaut mieux ranger dans un dossier probablement correct que de finir en `non_identifié`. Le score reporté reste la confiance d'origine (0.45) — l'utilisateur sait que le placement est moins fiable qu'un N1 confident.

Cas concret typique : le LLM Vision retourne `confidence: 0.42` parce que la couverture est floue, mais `theme: "Linear Algebra"` est sans ambiguïté dans le titre extrait. P1 refuse (conf trop basse) ; P2 (keyword) ne trouve rien dans le filename hashé ; P3 (LLM mapper) refuse aussi (conf source < seuil → pas d'appel) ; P4 sauve le fichier en `01-SCIENCES/MATHEMATIQUES/01-Algebre`.

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
| ~~`--api-key`~~ | — | Supprimé — utiliser la variable `SILICONFLOW_API_KEY` |
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
