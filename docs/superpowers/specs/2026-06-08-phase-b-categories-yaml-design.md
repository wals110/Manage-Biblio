# Phase B — Proposer aussi `categories.yaml` (spec)

> **Goal :** Éliminer la divergence entre la proposition Phase B et la simulation Impact reclassify, en faisant que Phase B produise aussi un `categories-proposed.yaml` cohérent avec le `tree-proposed.yaml`.

**Statut :** Design validé. Implémentation à planifier via `superpowers:writing-plans`.

**Date :** 2026-06-08

---

## Contexte

L'onglet "Impact reclassify" du rapport Phase B simule où chaque fichier irait après application de la refonte proposée. Aujourd'hui ce simulateur charge le `categories.yaml` de **production** pour le Niveau 2 (Keyword Classifier) de la cascade `classify_combined`. Conséquence : quand Phase B propose 85 créations + 200 mappings_added, les fichiers classés par P2 atterrissent dans les folders de l'**ancienne** taxonomie au lieu des nouveaux folders proposés. Les "Top dossiers cibles" affichent des chemins de prod, pas des chemins du tree-proposed.

**Cause racine identifiée** : Phase B ne touche pas à `categories.yaml`. Seul `theme_mapping-proposed.yaml` reflète la proposition. La cascade de classification est désynchronisée.

**Décisions actées en brainstorming** :
1. Scope = cascade déterministe pour renames/fusions/deletions + LLM pour mots-clés des créations
2. Le LLM est invoqué via un 2ème appel séparé après `propose_changes`, pas en étendant le tool LangGraph existant
3. Application en prod = **hors-scope** (artefacts seuls, le user les inspecte/applique manuellement comme aujourd'hui pour `tree-proposed.yaml`)

> **Note sémantique** : dans `categories.yaml`, `priorite=1` est la **priorité la plus haute** (formule `score * 10 / (priorite + 5)` dans `lib/keyword_classifier.py`). C'est pourquoi les règles "min(both)" sur collision et "fallback=99" pour entries vides sont cohérentes : on garde la priorité dominante en collision, on ne supplante rien en fallback.

## Architecture cible

Phase B passe de 1 étape à 6, dont 4 nouvelles. Toutes les étapes s'exécutent à l'intérieur de `propose_changes` (le LangGraph agent reste inchangé) :

```
1. LangGraph agent appelle propose_changes (INCHANGÉ)
   → produit tree-proposed.yaml + theme_mapping-proposed.yaml + refonte-rationale.md + changes.json

2. [NEW] _cascade_categories_changes() — déterministe, Python pur
   → lit categories.yaml prod + applique renames/fusions/deletions/sub-prefix-cascades
   → produit un dict intermédiaire en mémoire

3. [NEW] propose_keywords_for_new_folders() — 1 appel LLM avec contexte focalisé
   → si N créations > 0 : 1 call structured output (Pydantic), sinon skip
   → produit liste d'entries {chemin, groupe, priorite, mots_cles} pour les créations

4. [NEW] _merge_categories_changes() — fusion 2+3
   → écrit categories-proposed.yaml dans .cache/refonte/<run_id>/proposed/

5. [NEW] _render_rationale_markdown() étendu
   → nouvelle section "## CATÉGORIES (N entries modifiées)" avec sous-sections
     "Cascades automatiques" et "Nouveaux folders (mots-clés générés)"

6. simulator.py (1-ligne modifiée)
   → cat_path = proposal_dir / "categories-proposed.yaml" (avec fallback prod)
   → KeywordClassifier respecte la proposition
   → Impact reclassify devient honnête
```

## Modules touchés / créés

| Module | Action | Détail |
|---|---|---|
| `agents/refonte/proposition_tools.py` | **Modifier** | Étendre `propose_changes`. Ajouter `_cascade_categories_changes`, `_merge_categories_changes`, `_groupe_from_path_prefix`. Étendre `_render_rationale_markdown` |
| `agents/refonte/proposition.py` | **Inchangé** | Le séquenceur LangGraph reste tel quel — c'est `propose_changes` qui orchestre les nouvelles étapes en interne |
| `agents/refonte/simulator.py` | **Modifier (~3 lignes)** | `cat_path = proposal_dir / "categories-proposed.yaml"` avec fallback `if not cat_path.exists(): cat_path = tax._profile_dir(profile) / "categories.yaml"` |
| `agents/refonte/categories_llm.py` | **Créer** (~100 LOC) | Prompt + schémas Pydantic + appel `llm.with_structured_output()` |
| `dashboard/categories.py` | **Réutiliser** | La logique de remap et de collision (déjà éprouvée par `cascade_rename_target`) est extraite en helpers privés réutilisables si pas déjà publiques |

## Interfaces

### `_cascade_categories_changes`

```python
# agents/refonte/proposition_tools.py

def _cascade_categories_changes(
    current_categories: dict[str, list[dict]],  # parsed YAML : groupe → list[entry]
    renamings: list[dict],   # [{old_path, new_path, rationale}]
    fusions: list[dict],     # [{sources: list[str], target: str, rationale}]
    deletions: list[dict],   # [{path, rationale}]
) -> tuple[dict[str, list[dict]], list[dict]]:
    """Applique de manière déterministe les renames/fusions/deletions sur
    les entries existantes de categories.yaml. Pas d'appel LLM.

    Algorithme :
      1. Construit rename_map et prefix_map (pour les sub-paths cascadés)
      2. Construit fusion_map (chaque source → target)
      3. Construit deletion_set
      4. Pour chaque entry de chaque groupe :
         - si chemin == src d'une fusion : drop (les mots_cles seront mergés
           dans l'entry target par étape 5)
         - sinon : applique remap (rename exact, rename par préfixe,
           collision si target existe → merge mots_cles dedup + min priorite)
         - si chemin ∈ deletion_set : drop entry
      5. Pour chaque fusion target : merge tous les mots_cles des sources
         dans l'entry target (qu'elle pré-existe ou pas)

    Retourne :
      - new_categories : structure YAML mise à jour
      - log_modifications : [{type: rename|rename_prefix|fusion|deletion,
                              old, new, n_entries, n_collisions}]
        utilisé par _render_rationale_markdown
    """
```

### `propose_keywords_for_new_folders` + schemas Pydantic

```python
# agents/refonte/categories_llm.py

from pydantic import BaseModel, Field

class _NewCategoryEntry(BaseModel):
    chemin: str                                        # validé == un des creations.path
    groupe: str                                        # validé ∈ existing_groupes
    priorite: int = Field(ge=1, le=99, default=5)
    mots_cles: list[str] = Field(min_length=3, max_length=15)


class _NewCategoriesProposal(BaseModel):
    entries: list[_NewCategoryEntry]


def propose_keywords_for_new_folders(
    llm,                                # langchain LLM (réutilise agents.llm.get_agent_llm)
    creations: list[dict],              # [{path, rationale}] — depuis propose_changes
    existing_groupes: list[str],        # noms des groupes de categories.yaml prod
    groupe_inference: dict[str, str],   # path → groupe, pré-calculé via préfixe
    sample_entries: dict[str, list[dict]],  # 2-3 exemples par groupe (1-shot in prompt)
) -> list[dict]:
    """Si creations vide → return []. Sinon, appelle le LLM avec contexte
    focalisé et retourne la liste validée des entries (déjà parsées).

    Gestion d'erreur :
      - LLM timeout/rate-limit → log warning, fallback à des entries vides
        (mots_cles=[], priorite=99) pour chaque création. Le run reste réussi.
      - Pydantic validation fails (groupe inconnu, chemin différent) → retry 1×
        avec un prompt précisant l'erreur ; sinon fallback ci-dessus.

    Prompt structure (système + user) :
      - Système : "Tu génères des entries pour categories.yaml de Klodo.
                   Pour chaque nouveau folder, propose mots-clés et priorité.
                   Format JSON validé contre _NewCategoriesProposal."
      - User : liste des nouveaux folders avec leur rationale + groupe inféré
               + exemples d'entries existantes par groupe (1-shot)
    """
```

### `_groupe_from_path_prefix`

```python
def _groupe_from_path_prefix(
    path: str,
    existing_categories: dict[str, list[dict]],
) -> str:
    """Infère le groupe d'un nouveau chemin à partir des préfixes des
    entries existantes. Ex : path = "01-SCIENCES/CHIMIE/04-Materiaux" et
    le groupe "sciences" contient déjà des entries préfixées "01-SCIENCES/*"
    → retourne "sciences".

    Si ambigu (plusieurs groupes matchent) → choisit le plus représenté.
    Si aucun match (path totalement nouveau) → "autres" (groupe par défaut,
    créé si absent).
    """
```

## Flux de données — exemple concret

Run Phase B avec 2 créations + 1 renaming + 1 fusion.

### Input (propose_changes args)

```python
creations  = [
    {"path": "02-INFORMATIQUE/05-IA-ML/RAG",       "rationale": "Retrieval-Augmented Generation"},
    {"path": "01-SCIENCES/CHIMIE/04-Materiaux",    "rationale": "78 fichiers orphelins"},
]
renamings  = [{"old_path": "02-INFORMATIQUE/14-Web", "new_path": "02-INFORMATIQUE/14-Web-Frontend"}]
fusions    = [{"sources": ["09-BUREAU/Excel", "09-BUREAU/Microsoft-Excel"],
               "target":  "09-BUREAU/Microsoft-Excel"}]
deletions  = []
```

### Étape 2 — Cascade déterministe

`current_categories` (prod, avant) :
```yaml
informatique:
  - chemin: "02-INFORMATIQUE/14-Web"
    priorite: 3
    mots_cles: [html, css, javascript]
  - chemin: "02-INFORMATIQUE/14-Web/React"
    priorite: 4
    mots_cles: [react, jsx]
bureautique:
  - chemin: "09-BUREAU/Excel"
    priorite: 5
    mots_cles: [excel, xlsx]
  - chemin: "09-BUREAU/Microsoft-Excel"
    priorite: 3
    mots_cles: [microsoft excel]
```

Après cascade :
```yaml
informatique:
  - chemin: "02-INFORMATIQUE/14-Web-Frontend"           # renamed
    priorite: 3
    mots_cles: [html, css, javascript]
  - chemin: "02-INFORMATIQUE/14-Web-Frontend/React"     # rename par préfixe
    priorite: 4
    mots_cles: [react, jsx]
bureautique:
  - chemin: "09-BUREAU/Microsoft-Excel"                 # fusionné
    priorite: 3                                          # min(5, 3) = 3
    mots_cles: [microsoft excel, excel, xlsx]            # merge dédupliqué
```

`log_modifications` :
```python
[
    {"type": "rename",        "old": "02-INFORMATIQUE/14-Web",       "new": "02-INFORMATIQUE/14-Web-Frontend", "n_entries": 1},
    {"type": "rename_prefix", "old": "02-INFORMATIQUE/14-Web/React",  "new": "02-INFORMATIQUE/14-Web-Frontend/React", "n_entries": 1},
    {"type": "fusion",        "old": ["09-BUREAU/Excel"],             "new": "09-BUREAU/Microsoft-Excel", "n_entries": 1, "n_collisions": 1},
]
```

### Étape 3 — LLM call (1 appel)

Input contexte :
- 2 nouveaux folders à enrichir
- `existing_groupes = [informatique, sciences, bureautique, ...]`
- `groupe_inference = {RAG: "informatique", Materiaux: "sciences"}`
- `sample_entries` = 2-3 entries pour `informatique` et `sciences` (1-shot)

Output `_NewCategoriesProposal` validé :
```python
entries = [
    _NewCategoryEntry(
        chemin="02-INFORMATIQUE/05-IA-ML/RAG",
        groupe="informatique",
        priorite=5,
        mots_cles=["retrieval augmented", "RAG", "vector database", "embedding",
                   "semantic search", "ChromaDB", "llamaindex"],
    ),
    _NewCategoryEntry(
        chemin="01-SCIENCES/CHIMIE/04-Materiaux",
        groupe="sciences",
        priorite=6,
        mots_cles=["materials science", "polymer", "composite", "alloy",
                   "ceramic", "nanotube", "metallurgy"],
    ),
]
```

### Étape 4 — Merge + écriture

Les 2 entries LLM s'ajoutent au dict cascadé. Output `categories-proposed.yaml` final écrit à `.cache/refonte/<run_id>/proposed/categories-proposed.yaml`.

### Étape 5 — Rationale markdown

```markdown
## CATÉGORIES (5 entries modifiées)

### Cascades automatiques (3)
- **rename** : `02-INFORMATIQUE/14-Web` → `02-INFORMATIQUE/14-Web-Frontend` (1 entry remappée)
- **rename par préfixe** : `02-INFORMATIQUE/14-Web/React` → `02-INFORMATIQUE/14-Web-Frontend/React` (1 entry remappée)
- **fusion** : `09-BUREAU/Excel` → `09-BUREAU/Microsoft-Excel` (1 entry mergée, mots_cles dédupliqués)

### Nouveaux folders (mots-clés générés par LLM) (2)
- `02-INFORMATIQUE/05-IA-ML/RAG` (groupe `informatique`, priorité 5)
  - mots-clés : retrieval augmented, RAG, vector database, embedding, semantic search, ChromaDB, llamaindex
- `01-SCIENCES/CHIMIE/04-Materiaux` (groupe `sciences`, priorité 6)
  - mots-clés : materials science, polymer, composite, alloy, ceramic, nanotube, metallurgy
```

### Étape 6 — Simulator

```python
# Dans simulator.py:simulate_reclassify, ligne ~93
cat_path = proposal_dir / "categories-proposed.yaml"
if not cat_path.exists():
    cat_path = tax._profile_dir(profile) / "categories.yaml"  # fallback ancien runs
classifier = load_keyword_classifier(str(cat_path)) if cat_path.exists() else None
```

Le KeywordClassifier P2 utilise désormais la taxonomie proposée. Quand un fichier "matériaux composite" passe par P2, l'entry "01-SCIENCES/CHIMIE/04-Materiaux" matche → le simulateur retourne ce nouveau chemin → "Top dossiers cibles" affiche bien le nouveau folder, pas l'ancien parent.

## Cas limites + erreurs

| Cas | Comportement |
|---|---|
| **Collision** : `new_path` (rename ou création) a déjà une entry dans categories | Merge : dedup `mots_cles` case-insensitive, `priorite = min(both)`. Loggé dans le markdown comme "entry pré-existante mergée". Réutilise la logique éprouvée de `dashboard/categories.py:cascade_rename_target` |
| **0 créations** dans la proposition | Skip l'étape 3 entièrement (pas d'appel LLM). Le `categories-proposed.yaml` ne contient que les cascades déterministes |
| **LLM échoue / timeout / rate-limit** | Fallback : génère des entries `mots_cles=[]`, `priorite=99` (basse, n'écrasera rien en match-by-priority) pour les nouveaux folders. Log warning dans le markdown : "⚠ Mots-clés LLM indisponibles, entries vides — à compléter manuellement via UI Catégories". Le run reste réussi |
| **LLM hallucine** (groupe inconnu, chemin différent de la création) | Validation Pydantic stricte : `groupe` ∈ `existing_groupes` ; `chemin` ∈ `[c.path for c in creations]`. Retry 1× avec un prompt précisant l'erreur, sinon fallback ci-dessus |
| **Profil sans `categories.yaml`** | Skip étapes 2-4 entièrement. Pas de fichier produit. Simulator continue avec son comportement actuel (classifier=None si `cat_path` n'existe pas) |
| **Anciens runs (sans categories-proposed.yaml)** | Simulator détecte l'absence du fichier et fallback sur `categories.yaml` de prod — pas de régression pour les runs Phase B antérieurs au déploiement |
| **LLM propose des mots-clés trop génériques** (ex. "informatique", "logiciel") | Pas de filtrage sémantique en V1. La validation Pydantic exige `min_length=3` et `max_length=15` mots. À l'user de revoir dans le rationale avant d'appliquer |
| **Groupe inférable comme `autres`** (aucun préfixe ne matche) | Le LLM peut proposer un groupe existant adapté ; sinon création d'un groupe `autres` par défaut (pas de cas connu mais filet de sécurité) |

## Tests

Approche TDD : un test par scénario, ordre par dépendance (tests unit déterministes en premier, puis intégration avec LLM mocké).

### Tests unitaires — `tests/auto/test_proposition_categories.py` (nouveau, ~250 LOC)

```
1.  test_cascade_renames_simple_path
2.  test_cascade_renames_prefix_propagation        (folder/sub → newfolder/sub)
3.  test_cascade_renames_collision_merges_mots_cles (dedup case-insensitive + min priorite)
4.  test_cascade_fusion_sources_merge_into_target
5.  test_cascade_fusion_target_does_not_preexist   (création implicite)
6.  test_cascade_deletion_drops_entries
7.  test_cascade_no_changes_no_modifications       (idempotence)
8.  test_propose_keywords_pydantic_groupe_validation
9.  test_propose_keywords_pydantic_chemin_mismatch_rejected
10. test_propose_keywords_pydantic_min_max_mots_cles
11. test_propose_keywords_llm_error_fallback       (mocked LLM raise → entries vides)
12. test_propose_keywords_zero_creations_no_call   (pas d'appel LLM)
13. test_propose_keywords_retry_on_validation_fail (1× retry avec correction)
14. test_groupe_inference_from_path_prefix         (01-SCIENCES/* → "sciences")
15. test_groupe_inference_no_match_falls_back_to_autres
16. test_merge_categories_intermediate_plus_new    (étape 4)
17. test_render_rationale_categories_section
```

### Tests d'intégration — `tests/auto/test_proposition.py` (étend l'existant)

```
18. test_propose_changes_writes_categories_proposed_yaml
19. test_propose_changes_no_categories_when_profile_has_none
20. test_propose_changes_no_categories_when_no_changes_apply
21. test_simulator_uses_proposed_categories_when_present
22. test_simulator_fallback_when_no_proposed_categories  (anciens runs)
23. test_phase_b_end_to_end_with_categories             (LLM mocké, vérifie tous les artefacts)
```

### Fixtures + mocks

- `mock_agent_llm()` : fixture qui retourne un Pydantic model `_NewCategoriesProposal` pré-construit. Tous les tests utilisent ce mock — pas d'appel API en CI.
- `sample_categories_yaml()` : fixture qui fournit un categories.yaml de test minimal (2 groupes, 5 entries).
- `sample_proposition()` : fixture avec quelques creations/renamings/fusions/deletions représentatifs.

## Hors-scope explicite

- **Application en prod** : pas de bouton "Appliquer la refonte". L'user inspecte les artefacts dans `.cache/refonte/<run_id>/proposed/` et copie manuellement vers `profiles/<p>/` s'il valide. Identique au workflow actuel pour `tree-proposed.yaml`.
- **Optimisation des entries existantes** : le LLM ne propose pas de réviser les mots-clés des entries existantes (juste cascades + nouveaux). Reste à l'UI Catégories du dashboard si besoin manuel.
- **Mise à jour de la priorité globale** : le LLM propose une `priorite` initiale (1-99) pour les nouvelles entries mais ne réordonne pas les priorités existantes.
- **Mutation de tree.yaml par cette PR** : aucune. Pareil pour `theme_mapping.yaml` et `categories.yaml` de prod.
- **Phase C** : reste désactivée comme depuis PR #170. Cette feature ne réactive rien.

## Critères de succès

1. Un run Phase B produit `categories-proposed.yaml` dans `.cache/refonte/<run_id>/proposed/`.
2. Le rationale markdown contient une section `## CATÉGORIES` avec sous-sections "Cascades automatiques" et "Nouveaux folders".
3. L'onglet "Impact reclassify" du dashboard affiche des top destinations qui INCLUENT les nouveaux folders créés par Phase B (vérification visuelle sur un run réel).
4. La répartition par source dans le CSV `reclassify-projection.csv` montre une proportion ≥ 50 % de classifications via P1 + P4 (theme_mapping basé) — avant le fix, P2 dominait avec des chemins de la prod.
5. Tous les tests verts (17 unitaires + 6 intégration ajoutés, plus les tests existants Phase B).
6. Aucune régression sur les runs Phase B antérieurs (fallback simulator vérifié).

## Branche & livraison

- Branche : **`feature/phase-b-categories-yaml`** (depuis `develop`, Gitflow respecté).
- Taille : **medium** — ~400 LOC Python (1 module créé + 2 modifiés), ~250 LOC tests, 1 fichier de spec, 1 plan d'implémentation à écrire.
- PR : 1 seule PR vers `develop` après implémentation, labels `agent-refonte` + `feature`.
- Branches connexes encore ouvertes au moment de la rédaction :
  - PR #169 (docs audit)
  - PR #171 (badge readonly wording)
  - À merger indépendamment ; cette PR ne dépend d'aucune.
