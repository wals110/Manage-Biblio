# Plan de développement — Refine v2 (parcours récursif + classification profonde)

**Date** : 2026-04-03
**Objectif** : Tout fichier PDF de la bibliothèque doit se trouver dans un dossier feuille. Si un PDF traîne à un niveau intermédiaire (non-feuille), il doit être déplacé vers le bon sous-dossier.

---

## Diagnostic de l'existant

Le module `refine` actuel souffre de deux limites :

1. **Scan non récursif** — `scan_and_refine()` utilise `os.listdir()` sur chaque `parent` déclaré dans `refinement.yaml`. Il ne descend jamais dans les sous-dossiers. Résultat : sur 19 000+ fichiers, seuls 32 sont scannés.

2. **Logique découplée du classify** — Le pipeline de classification (`classify_combined`) dispose déjà de 4 niveaux de résolution (theme mapping → keyword → LLM mapper → fallback). Le refine a sa propre logique de mots-clés séparée dans `refinement.yaml`, sans aucun lien avec le classify. On se retrouve avec deux systèmes de classification parallèles.

**Principe directeur** : le refine ne doit pas devenir un deuxième classifieur. Il doit rester un outil léger de réorganisation qui réutilise la machinerie existante quand nécessaire.

---

## Architecture cible

```
Parcours récursif de l'arborescence (os.walk)
        │
        ▼
  Le dossier est non-feuille ?  ── non ──▶ skip (fichiers OK)
        │ oui
        ▼
  Collecter les PDF à ce niveau
        │
        ▼ pour chaque PDF :
  ┌─────────────────────────────────────────┐
  │  Étape 1 — Règles YAML (refinement.yaml)│
  │  Matching par mots-clés du fichier       │
  │  contre les règles du parent courant     │
  └────────────┬────────────────────────────┘
               │ pas de match
               ▼
  ┌─────────────────────────────────────────┐
  │  Étape 2 — Matching par nom de dossier  │
  │  Les noms des sous-dossiers existants    │
  │  sont utilisés comme mots-clés implicites│
  │  (ex: "Deep-Learning/" matche "deep      │
  │  learning" dans le nom de fichier)       │
  └────────────┬────────────────────────────┘
               │ pas de match
               ▼
  ┌─────────────────────────────────────────┐
  │  Étape 3 — LLM fallback (opt, phase 2) │
  │  Réutilise classify_combined() avec :   │
  │   - filename                             │
  │   - liste des sous-dossiers comme        │
  │     contrainte sur le résultat           │
  │  → Pas un nouvel appel LLM dédié,       │
  │    mais le même pipeline que classify    │
  └────────────┬────────────────────────────┘
               │ pas de match
               ▼
        Statut "non_classé" dans le rapport
        (PAS de dossier _A-CLASSER)
```

**Pourquoi pas de `_A-CLASSER`** : créer 30+ dossiers `_A-CLASSER` dispersés dans l'arborescence déplace le problème au lieu de le résoudre. Un rapport CSV centralisé avec le statut `non_classé` et le chemin source donne la même visibilité sans polluer l'arborescence. Si à terme on veut un dossier de triage, un seul `_A-TRIER` à la racine existe déjà.

---

## Statut

- **Phase 1** : ✅ TERMINÉE — Refine récursif + matching YAML + matching par nom de sous-dossier
- **Phase 2** : ✅ TERMINÉE — LLM fallback (option --llm) pour fichiers non-classés
- **Phase 3** : ⚠️ FUTURE — Améliorer classify pour viser directement les sous-dossiers

---

## Plan de développement

### Phase 1 — Refine récursif + keyword (pas d'appel LLM) ✅ DONE

**Objectif** : scanner toute l'arborescence, appliquer les règles existantes à tous les niveaux, et lister les non-matchés dans le rapport.

#### Tâche 1.1 — Réécrire `scan_and_refine()` dans `lib/refiner.py`

Remplacer le scan par `os.walk()` :

```python
for dirpath, dirnames, filenames in os.walk(base_path):
    # Si pas de sous-dossiers → dossier feuille → skip
    if not dirnames:
        continue

    # Chemin relatif du dossier courant
    rel_dir = os.path.relpath(dirpath, base_path)

    # Collecter les PDF à ce niveau
    pdf_files = [f for f in filenames if f.lower().endswith('.pdf')]

    for pdf in pdf_files:
        # Étape 1 : règles YAML (chercher les règles dont parent == rel_dir)
        # Étape 2 : matching par nom de sous-dossier
        # Sinon : statut non_classé
```

**Points d'attention** :
- Les règles YAML sont triées par spécificité (le plus long parent d'abord). Pour chaque fichier, on cherche la première règle dont le `parent` correspond au dossier courant.
- Le matching par nom de sous-dossier est nouveau : on extrait les noms des sous-dossiers à ce niveau et on les utilise comme mots-clés implicites. Ex: si `dirnames = ['Deep-Learning', 'NLP', 'Vision-par-Ordinateur']`, on matche "deep learning", "nlp", "vision" dans le nom de fichier.
- Normaliser les noms de dossiers pour le matching (remplacer `-` par espace, lowercase).

#### Tâche 1.2 — Ajouter le matching par nom de sous-dossier

Nouvelle fonction `match_subdirs()` :

```python
def match_subdirs(filename: str, subdirs: List[str]) -> Optional[str]:
    """
    Matching implicite : utilise les noms de sous-dossiers comme mots-clés.
    Retourne le nom du sous-dossier matché, ou None.
    """
```

Normalisation : `"Deep-Learning"` → `["deep learning", "deep-learning", "deeplearning"]`.

#### Tâche 1.3 — Enrichir le rapport CSV

Nouveau statut dans les résultats :
- `déplacé` / `à_déplacer` : match trouvé (comme avant)
- `déjà_présent` : fichier existe déjà dans la cible
- `non_classé` : aucun match → listé dans le rapport avec le chemin source pour review
- `erreur` : erreur de déplacement

Ajouter une colonne `source_match` : `"règle_yaml"` ou `"nom_dossier"` pour traçabilité.

#### Tâche 1.4 — Mettre à jour `cmd_refine` dans `klodo.py`

- Ajouter compteur de fichiers scannés et de dossiers non-feuille dans le résumé
- Afficher le nombre de `non_classé` séparément pour guider la phase 2
- Le `--verbose` affiche les fichiers non classés avec leur chemin

#### Tâche 1.5 — Tests

- `test_refine_recursive` : arborescence temp à 3 niveaux, vérifier que les fichiers intermédiaires sont détectés
- `test_refine_leaf_skip` : les fichiers en dossier feuille ne sont pas touchés
- `test_refine_match_subdirs` : matching par nom de sous-dossier
- `test_refine_non_classe` : fichier sans match → statut `non_classé` dans le rapport
- `test_refine_rule_priority` : règle YAML prioritaire sur matching par nom de dossier
- Vérifier la compatibilité avec les 77+ tests existants

#### Tâche 1.6 — Documentation

- Mettre à jour le README (section refine)
- Mettre à jour CLAUDE.md
- Mettre à jour CAHIER_DE_TESTS.md
- Mettre à jour le diagramme SVG `docs/refine-logic.svg`

**Livrable phase 1** : le refine scanne toute l'arborescence, déplace les fichiers matchés, et produit un rapport avec les non-classés identifiés.

---

### Phase 2 — LLM fallback dans le refine (réutilisation du classify) ✅ DONE

**Objectif** : pour les fichiers `non_classé` de la phase 1, utiliser le pipeline de classification existant comme fallback.

**Résumé de l'implémentation** :
- Ajout de `refine_with_llm()` dans `lib/refiner.py` qui réutilise `classify_combined()` pour les fichiers non-classés
- Option `--llm` ajoutée à la commande refine dans klodo.py
- Optionnels `--api-key` et `--max` pour le contrôle granulaire
- Fonction `parse_llm_json()` pour extraire les chemins JSON depuis les réponses LLM brutes
- Statut `raffiné_llm` et source_match `llm_fallback` ajoutés aux rapports CSV
- 16 nouveaux tests automatiques (A8.1 à A8.16) et 3 tests manuels (B4.3 à B4.5)

#### Tâche 2.1 — Créer `refine_with_llm()` dans `lib/refiner.py` ✅

Implémentée. Pour les fichiers `non_classé`, réutilise `classify_combined()` avec une contrainte :
- On passe le `filename` au keyword classifier
- On passe le thème LLM Vision (s'il existe dans le checkpoint) au theme mapping
- **Contrainte** : le résultat doit être un sous-dossier du dossier courant

```python
def refine_with_llm(
    unclassified: List[Dict],
    base_path: str,
    theme_mapping: Dict,
    classifier: Optional[Any] = None,
    llm_mapper: Optional[Any] = None,
) -> List[Dict]:
    """Deuxième passe : réutilise classify_combined pour les non_classé."""
```

Pas de nouveau système LLM. Le même `classify_combined()` est appelé et la contrainte de sous-dossier est vérifiée. Si le classify renvoie un chemin hors du dossier courant, le fichier reste non_classé.

#### Tâche 2.2 — Option `--llm` sur la commande refine ✅

Implémentée. Utilisation :

```bash
./klodo.sh refine --llm              # Active le fallback LLM
./klodo.sh refine --llm --execute    # Active + exécute
./klodo.sh refine --llm --max 20     # Test limité
```

Sans `--llm` : comportement phase 1 (keyword seulement).
Avec `--llm` : keyword d'abord, puis classify_combined en fallback pour les non_classé.

#### Tâche 2.3 — Tests et documentation ✅

Implémentés. Tests du fallback LLM avec mocks de classify_combined. Vérification que sans `--llm`, pas d'appel LLM. Documentation mise à jour (CLAUDE.md, README.md, CAHIER_DE_TESTS.md).

**Livrable phase 2** : le refine peut optionnellement exploiter le LLM pour les fichiers que les keywords n'ont pas su placer. Option `--llm` contrôle l'activation.

---

### Phase 3 (future, optionnelle) — Améliorer classify pour viser les sous-dossiers

**Constat** : si beaucoup de fichiers arrivent dans des dossiers non-feuille, c'est que le `classify` initial ne cible pas assez profond. Plutôt que de multiplier les passes de refine, on peut améliorer `classify_combined()` pour qu'il vise directement les sous-dossiers quand c'est possible.

Pistes :
- Enrichir `theme_mapping.yaml` pour mapper les thèmes directement vers les sous-dossiers (ex: `"Deep Learning" → "02-INFORMATIQUE/05-IA-ML/Deep-Learning"` au lieu de `"02-INFORMATIQUE/05-IA-ML"`)
- Enrichir `categories.yaml` avec des mots-clés pointant vers les sous-dossiers
- Le refine devient alors un filet de sécurité rarement sollicité, pas un passage obligé

---

## Résumé des priorités

| Phase | Effort  | Impact | Appel LLM |
|-------|---------|--------|-----------|
| 1     | Moyen   | Fort   | Non       |
| 2     | Moyen   | Moyen  | Oui (opt) |
| 3     | Faible  | Fort   | Non       |

**Recommandation** : commencer par la phase 1, mesurer combien de fichiers restent `non_classé`, puis décider si la phase 2 est nécessaire ou si la phase 3 (enrichir le classify) est plus rentable.
