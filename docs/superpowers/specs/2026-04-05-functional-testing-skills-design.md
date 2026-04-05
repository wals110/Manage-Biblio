# Functional Testing Skills — Design Spec

**Date :** 2026-04-05
**Scope :** 4 skills Claude Code pour standardiser les tests fonctionnels sur tous les projets
**Installation :** `~/.claude/skills/` (scope global, pas spécifique à un projet)

---

## Architecture

```
                     /functional-testing
                      (Skill 4 — orchestrateur)
                       /        |         \
                      /         |          \
            Skill 1             Skill 2             Skill 3
       functional-test-plan  functional-test-runner  test-issues-sync
            |                    |                       |
            v                    v                       v
       tests.yaml           run_functional.py        GitHub Project
       + CAHIER.md           + report.json            + Issues
                             + report.md              + Commentaires
```

### Pattern commun
Source de vérité structurée (YAML/JSON) + vue humaine Markdown générée. Appliqué partout :
- Cahier : `tests.yaml` → `CAHIER_TESTS_FONCTIONNELS.md`
- Rapport : `report.json` → `report.md`

### Arborescence générée

```
tests/functional/
├── tests.yaml                        # Source de vérité (generated + custom)
├── CAHIER_TESTS_FONCTIONNELS.md      # Vue Markdown (généré, ne pas éditer)
├── run_functional.py                 # Runner (généré par Skill 2)
└── reports/
    ├── report_2026-04-05_153000.json
    └── report_2026-04-05_153000.md
```

---

## Skill 1 — `functional-test-plan`

### Rôle
Analyse un projet et génère le cahier de tests fonctionnels sous forme structurée + Markdown.

### Déclenchement
- `/functional-test-plan`
- Via l'orchestrateur (Skill 4)

### Processus

1. Explorer le projet : CLI (argparse/click), docs, README, structure
2. Identifier les surfaces testables : commandes, flags, modes, cas limites, sécurité, performance
3. Organiser en phases progressives (smoke test → stress test)
4. Générer `tests/functional/tests.yaml`
5. Générer `tests/functional/CAHIER_TESTS_FONCTIONNELS.md` depuis le YAML

### Format `tests.yaml`

```yaml
version: "1.0"
project: "NomDuProjet"
generated_at: "2026-04-05T14:30:00"

variables:
  TARGET_DIR: "/chemin/vers/données"
  INBOX: "${TARGET_DIR}/_INBOX"
  PROFILE: "test"

session:
  pre_run:
    - "scripts/flatten_to_inbox.sh ${TARGET_DIR} --execute"
  post_run:
    - "scripts/flatten_to_inbox.sh ${TARGET_DIR} --execute"

phases:
  - id: phase_0
    name: "Vérifications préalables"
    priority: obligatoire       # obligatoire | haute | moyenne | basse
    pre_run: []
    post_run: []
    series:
      - id: T0.1
        name: "Intégrité de l'arborescence"
        description: "Vérifier que l'arborescence est vide mais intacte après flatten"
        provides: [clean_inbox]
        requires: []
        pre_run: []
        post_run: []
        setup:
          - "scripts/flatten_to_inbox.sh ${TARGET_DIR} --execute"
        checks:
          - id: T0.1a
            description: "Aucun PDF hors inbox"
            command: "find ${TARGET_DIR} -name '*.pdf' -not -path '*/_INBOX/*' | wc -l"
            assert:
              type: output_equals
              expected: "0"
            mode: auto

          - id: T0.1b
            description: "Les sous-dossiers existent toujours"
            command: "ls ${TARGET_DIR}/02-INFORMATIQUE/05-IA-ML/"
            assert:
              type: output_contains
              expected: ["Deep-Learning", "Machine-Learning"]
            mode: auto

custom: []    # Section protégée — jamais écrasée par le skill
```

### Variables

- Bloc `variables:` en tête du YAML
- Toutes les commandes utilisent `${VAR}` comme placeholders
- Les variables peuvent se référencer : `INBOX: "${TARGET_DIR}/_INBOX"`
- Override au runtime via le runner : `--var TARGET_DIR=/autre/chemin`
- Le skill détecte les chemins spécifiques au projet et les extrait automatiquement en variables

### Types d'assertions

| Type | Params | Description |
|------|--------|-------------|
| `output_equals` | `expected: "0"` | Sortie exacte |
| `output_contains` | `expected: ["mot1", "mot2"]` | Tous présents dans la sortie |
| `output_not_contains` | `expected: ["error"]` | Aucun présent |
| `exit_code` | `expected: 0` | Code retour |
| `file_exists` | `pattern: "logs/rapport_*.csv"` | Glob match |
| `file_not_exists` | `pattern: "..."` | Pas de match |
| `line_count` | `file: "...", op: ">", value: 0` | Nombre de lignes |
| `manual_check` | `prompt: "Les noms sont-ils propres ?"` | Validation humaine |
| `duration_under` | `seconds: 60` | Performance |

### Format Markdown généré

Le Markdown reproduit le format standard :
- Titre avec version du projet
- Section prérequis (extraite de `session.pre_run` et `variables`)
- Une section `## Phase N` par phase
- Une sous-section `### TN.X` par série
- Tableau `| # | Action/Vérification | Attendu |` par série
- Grille récapitulative en fin de document
- Checklist rapide (séries priorité haute + obligatoire uniquement)

### Regénération

- Relancer le skill écrase toutes les `phases:` (section `generated`)
- La section `custom:` est préservée intacte
- Le Markdown est entièrement regénéré (phases + custom → MD)
- `generated_at` est mis à jour
- L'utilisateur voit le diff git avant de commit

---

## Skill 2 — `functional-test-runner`

### Rôle
Lit `tests.yaml`, génère un script Python `run_functional.py`, l'exécute, produit un rapport JSON + Markdown.

### Déclenchement
- `/functional-test-runner`
- Via l'orchestrateur (Skill 4)

### Script généré

Le skill génère `tests/functional/run_functional.py` — un script autonome exécutable sans Claude après génération. Regénéré à chaque invocation (le YAML peut avoir changé).

### Modes d'exécution

```bash
# Full-auto : tout d'un coup
python tests/functional/run_functional.py

# Step-by-step : série par série, demande confirmation
python tests/functional/run_functional.py --interactive

# Filtrer par phase
python tests/functional/run_functional.py --phase 1 2

# Filtrer par série
python tests/functional/run_functional.py --series T1.1 T2.3

# Filtrer par priorité
python tests/functional/run_functional.py --priority haute obligatoire

# Override de variable
python tests/functional/run_functional.py --var TARGET_DIR=/autre/chemin

# Dry-run : montre ce qui serait exécuté sans rien lancer
python tests/functional/run_functional.py --dry-run
```

### Logique d'exécution

```
1. Charger tests.yaml
2. Résoudre les variables (${VAR} → valeurs, avec overrides --var)
3. Résoudre les dépendances (tri topologique sur requires/provides)
4. Exécuter session.pre_run
5. Pour chaque phase (dans l'ordre) :
   a. Exécuter phase.pre_run
   b. Pour chaque série (dans l'ordre des dépendances) :
      i.   Vérifier requires → si un provides a échoué → SKIP
      ii.  Exécuter serie.pre_run
      iii. Exécuter serie.setup
      iv.  Pour chaque check :
           - mode=auto → exécuter command, évaluer assert → PASS/FAIL
           - mode=manual_check + --interactive → afficher prompt, demander → PASS/FAIL/SKIP
           - mode=manual_check + full-auto → SKIP
      v.   Exécuter serie.post_run
      vi.  Si tous les checks PASS → marquer provides comme disponibles
   c. Exécuter phase.post_run
6. Exécuter session.post_run
7. Générer le rapport JSON + Markdown
```

### Gestion des dépendances

```
T0.2 (provides: [profile_test]) → FAIL
    └── T1.1 (requires: [profile_test]) → SKIPPED "dependency failed: profile_test (T0.2)"
        └── T1.2 (requires: [profile_test]) → SKIPPED (idem)
```

Un test SKIP ne bloque pas les tests qui ne dépendent pas de lui. Seul l'arbre de dépendances est affecté.

### Mode interactif (`--interactive`)

```
═══ Phase 1 — Renommage ═══

── T1.1 — Dry-run sur un petit lot ──
  pre_run: scripts/flatten_to_inbox.sh ${TARGET_DIR} --execute
  setup:   klodo.sh rename ${INBOX} --max 20 --verbose

  [T1.1a] Rapport CSV généré dans logs/
          → command: ls logs/rapport_rename_*.csv
          → assert: file_exists
          → Résultat: PASS

  [T1.1b] Aucun fichier renommé
          → assert: manual_check
          → "Les noms sur disque sont-ils inchangés ?"
          → [P]ass / [F]ail / [S]kip ? _

── T1.1 : 2 PASS, 0 FAIL ──

  Issue GitHub #12 associée → Fermer l'issue et poster le rapport ?
  [Y]es / [N]o / [A]ll (fermer auto pour le reste) ? _

Continuer avec T1.2 ? [Y]es / [S]kip / [Q]uit ? _
```

En cas de FAIL d'une série :
```
── T1.2 — Renommage effectif : FAIL (3/4) ──

  Issue GitHub #13 associée → Poster le rapport d'erreur en commentaire ?
  [Y]es / [N]o ? _
```

Le mode `[A]ll` permet de basculer en fermeture automatique des issues pour le reste de la session.

### Intégration GitHub en mode interactif

Le runner lit le champ `github_issue` du `tests.yaml` (écrit par le Skill 3) pour résoudre le mapping `série → issue`. Quand une série termine en interactif, il propose de fermer/commenter l'issue via `gh`.

Si le champ `github_issue` est absent pour une série (Skill 3 pas encore exécuté), le runner ne propose pas l'action GitHub et se contente d'afficher le résultat. Aucune erreur.

### Format du rapport JSON

```json
{
  "project": "Klodo",
  "run_at": "2026-04-05T15:30:00",
  "duration_seconds": 842,
  "variables": {
    "TARGET_DIR": "/Volumes/ExtSSD/BIBLIO-TEST"
  },
  "summary": {
    "total": 85,
    "pass": 72,
    "fail": 5,
    "skip": 8
  },
  "phases": [
    {
      "id": "phase_1",
      "name": "Renommage",
      "series": [
        {
          "id": "T1.1",
          "name": "Dry-run sur un petit lot",
          "status": "pass",
          "duration_ms": 3200,
          "checks": [
            {
              "id": "T1.1a",
              "description": "Rapport CSV généré",
              "status": "pass",
              "command": "ls logs/rapport_rename_*.csv",
              "output": "logs/rapport_rename_20260405.csv",
              "duration_ms": 45
            },
            {
              "id": "T1.1b",
              "status": "fail",
              "command": "...",
              "output": "actual output here",
              "expected": "0",
              "error": "output_equals: got '3', expected '0'"
            }
          ]
        }
      ]
    }
  ]
}
```

### Format du rapport Markdown (généré depuis le JSON)

```markdown
# Rapport de tests fonctionnels — Klodo
**Date :** 2026-04-05 15:30  |  **Durée :** 14m02s

## Résumé
| Total | Pass | Fail | Skip |
|-------|------|------|------|
| 85    | 72   | 5    | 8    |

## Phase 1 — Renommage
### T1.1 — Dry-run sur un petit lot : PASS
| # | Check | Status | Détail |
|---|-------|--------|--------|
| T1.1a | Rapport CSV généré | PASS | logs/rapport_rename_20260405.csv |
| T1.1b | Aucun fichier renommé | FAIL | got '3', expected '0' |

## Échecs détaillés
### T1.1b — Aucun fichier renommé
- **Commande :** `...`
- **Attendu :** `0`
- **Obtenu :** `3`
- **Output complet :** ...
```

---

## Skill 3 — `test-issues-sync`

### Rôle
Synchronise le `tests.yaml` et les rapports d'exécution avec GitHub : project board, issues, commentaires, fermeture.

### Déclenchement
- `/test-issues-sync`
- Via l'orchestrateur (Skill 4)

### Mode `create`

1. Détecter le repo via `gh repo view`
2. Chercher un GitHub Project nommé "Tests Fonctionnels - {project}" rattaché au repo
   - Trouvé → le réutiliser
   - Pas trouvé → en créer un via l'API GitHub Projects v2 (GraphQL via `gh`)
3. Configurer le board :
   - Champ Status : `Todo` / `In Progress` / `Pass` / `Fail` / `Skipped`
   - Champ Phase : `Phase 0`, `Phase 1`, ...
   - Champ Priorité : `obligatoire` / `haute` / `moyenne` / `basse`
4. Créer une issue par série :
   - Titre : `[T1.1] Dry-run sur un petit lot`
   - Body : description, setup, tableau des checks, dépendances
   - Labels : `functional-test`, `phase-1`, `priority-haute`
5. Ajouter chaque issue au project board (status `Todo`, bonne phase)
6. Écrire le champ `github_issue: N` dans `tests.yaml` pour chaque série créée

### Format d'une issue créée

```markdown
## Phase 1 — Renommage
**Série :** T1.1
**Priorité :** haute
**Dépendances :** profile_test (T0.2)

### Setup
\`\`\`bash
klodo.sh rename ${INBOX} --max 20 --verbose
\`\`\`

### Checks
| # | Description | Type | Mode |
|---|------------|------|------|
| T1.1a | Rapport CSV généré | file_exists | auto |
| T1.1b | Aucun fichier renommé | manual_check | manual |

---
*Généré par test-issues-sync*
```

### Mode `update`

Lit le dernier rapport JSON dans `tests/functional/reports/` puis :

| Résultat série | Action sur l'issue | Status board |
|---|---|---|
| **PASS** | Ferme l'issue + commentaire rapport | → `Pass` |
| **FAIL** | Laisse ouverte + commentaire détail des échecs | → `Fail` |
| **SKIP** | Laisse ouverte + commentaire "dependency failed" | → `Skipped` |

### Commentaire posté (PASS)

```markdown
## Rapport d'exécution — 2026-04-05 15:30

| Check | Status | Détail |
|-------|--------|--------|
| T1.1a | PASS | logs/rapport_rename_20260405.csv |
| T1.1b | PASS | validated manually |

**Résultat : PASS** — Issue fermée automatiquement.
```

### Commentaire posté (FAIL)

```markdown
## Rapport d'exécution — 2026-04-05 15:30

| Check | Status | Détail |
|-------|--------|--------|
| T1.1a | PASS | logs/rapport_rename_20260405.csv |
| T1.1b | FAIL | output_equals: got '3', expected '0' |

**Résultat : FAIL** — 1 échec sur 2 checks.

### Détail de l'échec T1.1b
- **Commande :** `...`
- **Attendu :** `0`
- **Obtenu :** `3`
```

### Idempotence

- `create` : si l'issue `[T1.1]` existe déjà (détection par titre), elle est skippée. Seules les séries manquantes sont créées.
- `update` : chaque run poste un nouveau commentaire (historique visible dans l'issue). Peut être relancé.
- Nouvelles séries ajoutées au YAML : `create` ne crée que les manquantes.

---

## Skill 4 — `functional-testing` (orchestrateur)

### Rôle
Point d'entrée unique. Détecte l'état du projet et propose la bonne action.

### Déclenchement
- `/functional-testing`
- "lance les tests fonctionnels", "mets à jour le cahier", "synchronise les issues"

### Logique de détection

Le skill inspecte :
- `tests/functional/tests.yaml` existe ?
- `tests/functional/run_functional.py` existe ?
- `tests/functional/reports/report_*.json` existe ?
- Issues GitHub avec label `functional-test` existent ?

### Cas et propositions

**Cas 1 — Projet vierge** (pas de tests.yaml)
```
Aucun cahier de tests fonctionnels détecté.
→ Je génère le cahier ? (tests.yaml + Markdown)
```
→ Invoque Skill 1

**Cas 2 — Cahier existe, pas de runner**
```
Cahier trouvé (40 séries, 85 checks, généré le 2026-04-03).
Pas de runner généré.

  1. Générer le runner
  2. Regénérer le cahier d'abord (code a changé depuis)
  3. Regénérer le cahier + générer le runner
```

**Cas 3 — Runner existe, pas de rapport**
```
Runner prêt. Aucun rapport d'exécution.

  1. Lancer les tests [full-auto / interactive / dry-run]
  2. Regénérer le cahier d'abord
```

**Cas 4 — Rapport existe, issues pas sync**
```
Dernier rapport : 2026-04-05 (72 pass, 5 fail, 8 skip)
Issues GitHub : non créées

  1. Créer le project board et les issues + sync résultats
  2. Relancer les tests d'abord
  3. Regénérer le cahier
```

**Cas 5 — Tout est en place**
```
État actuel :
  Cahier : 40 séries, 85 checks (mis à jour le 2026-04-05)
  Dernier run : 2026-04-05 — 72 pass, 5 fail, 8 skip
  Issues : 35/40 fermées, 5 ouvertes

Que veux-tu faire ?
  1. Relancer les tests
  2. Relancer seulement les FAIL (5 séries)
  3. Regénérer le cahier (après changement de code)
  4. Sync les issues avec le dernier rapport
```

### Règle
Dès que `tests.yaml` existe, "Regénérer le cahier" est toujours proposé comme option, quel que soit le cas.

### Raccourcis

L'orchestrateur respecte l'intention explicite de l'utilisateur :

| L'utilisateur dit | Action |
|---|---|
| "génère le cahier de tests" | → Skill 1 directement |
| "lance les tests fonctionnels" | → Skill 2 (ou Skill 1+2 si pas de cahier) |
| "sync les issues" | → Skill 3 directement |
| "relance les tests qui ont échoué" | → Skill 2 avec --series filtrées sur les FAIL |
| "mets à jour le cahier" | → Skill 1 en mode regénération |

### Limites

- Pas de logique métier — délègue tout aux 3 skills
- Pas de génération de fichiers — inspecte et route
- Pas de state propre — lit les artefacts existants pour décider

---

## Résumé des décisions de design

| Décision | Choix | Raison |
|----------|-------|--------|
| Source de vérité | YAML (pas Markdown) | Parser fiable, assertions typées |
| Vue humaine | Markdown généré depuis YAML | Lisible sans toucher au YAML |
| Rapport runner | JSON + Markdown | Parsable par Skill 3 + lisible par humain |
| Granularité issues | 1 issue par série de tests | Atomique, trackable |
| GitHub Project | 1 project par cahier, rattaché au repo | Centralisé, visuel |
| Mapping série→issue | Champ `github_issue` dans tests.yaml (écrit par Skill 3) | Fiable, pas de réseau pendant les tests |
| Dépendances tests | `requires` / `provides` | Tri topologique, skip en cascade |
| Setup/cleanup | `pre_run` / `post_run` à 3 niveaux (session, phase, série) | Flexible, explicite |
| Variables | Bloc `variables:` + `${VAR}` | Chemins portables entre machines |
| Regénération | Écraser `phases:`, préserver `custom:` | Simple, git pour l'historique |
| Scope skills | Global (`~/.claude/skills/`) | Réutilisable sur tous les projets |
| Architecture | 3 skills + 1 orchestrateur | Simple, composable, scalable |

## Ordre de réalisation

1. **Skill 1** — `functional-test-plan` (fondation : le format YAML + Markdown)
2. **Skill 2** — `functional-test-runner` (consomme le YAML, produit les rapports)
3. **Skill 3** — `test-issues-sync` (consomme YAML + rapports, gère GitHub)
4. **Skill 4** — `functional-testing` (orchestrateur léger, routing)
