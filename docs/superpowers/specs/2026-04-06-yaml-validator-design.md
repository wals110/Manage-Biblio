# YAML Validator for Functional Tests — Design Spec

**Date :** 2026-04-06
**Scope :** Script Python standalone qui valide et auto-fixe `tests/functional/tests.yaml`
**Emplacement :** `tests/functional/validate_tests.py`

---

## Problème

Le Skill 1 (functional-test-plan) génère du YAML avec des erreurs récurrentes :
- `grep -c ... || echo 0` produit un double output
- Commandes `classify`/`process` sans `-y` bloquent le runner
- `clean all` dans les pre_run efface les rapports nécessaires
- Paths inventés qui n'existent pas dans l'arborescence réelle
- Noms de rapports incorrects (ex: `rapport_classify_*` au lieu de `rapport_process_*`)

Ces erreurs sont actuellement documentées dans le SKILL.md comme 7+ règles "CRITICAL" que le LLM n'arrive pas à toutes respecter.

## Solution

Un script Python déterministe qui encode ces règles et les applique automatiquement. Remplace les règles "CRITICAL" du SKILL.md par un appel au validateur.

## Usage

```bash
# Valider seulement — afficher les erreurs
uv run python tests/functional/validate_tests.py

# Valider + auto-fixer ce qui est fixable
uv run python tests/functional/validate_tests.py --fix

# Sortie JSON (pour CI ou intégration programmatique)
uv run python tests/functional/validate_tests.py --json
```

Exit codes :
- `0` : aucune erreur
- `1` : erreurs trouvées (ou fixées avec --fix)
- `2` : erreur fatale (YAML invalide, fichier introuvable)

## Architecture

```
validate_tests.py
    │
    ├── load_yaml()          — charger et parser tests.yaml
    ├── load_context()       — charger tree.yaml, scanner le code source pour les noms de rapports
    │
    ├── RULES (liste de fonctions de validation)
    │   ├── rule_grep_c_echo()       — auto-fixable
    │   ├── rule_grep_c_chain()      — auto-fixable
    │   ├── rule_missing_yes()       — auto-fixable
    │   ├── rule_clean_all()         — auto-fixable
    │   ├── rule_absolute_paths()    — warning
    │   ├── rule_paths_exist()       — error (vérification tree.yaml)
    │   ├── rule_report_names()      — error (vérification code source)
    │   ├── rule_yaml_structure()    — error (champs requis, types)
    │   ├── rule_dependencies()      — warning (cycles, orphelins)
    │   └── rule_blocking_commands() — warning
    │
    ├── apply_fixes()        — appliquer les auto-fixes sur le YAML
    ├── format_report()      — affichage terminal (couleurs, résumé)
    └── format_json()        — sortie JSON
```

Script standalone : seulement stdlib + pyyaml. Pas de dépendance sur le projet Klodo.

## Règles détaillées

### Règle 1 — `grep -c ... || echo 0` (auto-fixable)

**Détection :** regex sur toutes les valeurs `command:` :
```python
r'grep\s+-c.*\|\|\s*echo\s+0'
```

**Fix :** remplacer `|| echo 0` par `; true` dans la commande.

**Raison :** `grep -c` affiche toujours le count en stdout (même 0). `|| echo 0` s'exécute quand grep retourne exit code 1 (count=0), produisant `0\n0`.

---

### Règle 2 — `grep -c` chaîné avec `&&` (auto-fixable)

**Détection :** regex :
```python
r'\)&&\s*grep\s+-c'
```

**Fix :** remplacer `)&&grep` ou `)&& grep` par `); grep`.

**Raison :** Si la commande avant `&&` échoue (ex: `ls` ne trouve rien), le `grep -c` ne s'exécute pas et le `|| echo 0` produit un `0` supplémentaire.

---

### Règle 3 — Commandes `classify`/`process` sans `-y` (auto-fixable)

**Détection :** regex sur les commandes qui contiennent `classify` ou `process` mais pas `-y` ni `--yes` :
```python
r'(classify|process)\b' et not r'-y\b|--yes\b'
```
Exceptions : `classify --help`, `grep ... classify` (pas des appels CLI directs).

**Fix :** ajouter `-y` après `--profile ${PROF}` ou après le dernier flag connu.

**Raison :** Les commandes `classify` et `process` demandent confirmation via `input()`. Sans `-y`, le subprocess bloque.

---

### Règle 4 — `clean all` dans pre_run (auto-fixable)

**Détection :** string match `clean all` dans les valeurs `pre_run:` à tous les niveaux (session, phase, série).

**Fix :** remplacer `clean all` par `clean progress`.

**Raison :** `clean all` supprime les rapports CSV nécessaires pour la validation humaine des tests précédents.

---

### Règle 5 — Paths absolus hors `${VAR}` (warning)

**Détection :** regex dans toutes les commandes :
```python
r'(?<!\$\{)\b/(?:Users|home|Volumes|tmp|etc|var)\b'
```
Exclure les paths dans des variables `${...}`.

**Gravité :** WARNING — pas d'auto-fix car le validateur ne sait pas quelle variable utiliser.

---

### Règle 6 — Paths référencés existent dans tree.yaml (error)

**Détection :** extraire les paths de dossiers dans les commandes `ls`, `find`, assertions `file_exists`/`output_contains`. Résoudre les variables. Vérifier que les dossiers existent dans le `tree.yaml` du profil ou sur le disque.

**Gravité :** ERROR.

**Contexte nécessaire :** charger `profiles/<profile>/tree.yaml` et parser l'arborescence.

**Note :** ne s'applique que si le profil est accessible (pas en CI). Si tree.yaml n'est pas trouvé, SKIP cette règle.

---

### Règle 7 — Noms de rapports valides (error)

**Détection :** extraire les patterns `rapport_*`, `refine_*`, `log_*` dans les commandes et assertions. Scanner le code source (`commands/*.py`, `lib/*.py`) pour les appels `save_report(...)` et extraire les préfixes réels.

**Gravité :** ERROR si un pattern du YAML ne correspond à aucun préfixe trouvé dans le code.

**Note :** ne s'applique que si le code source est accessible (même répertoire projet). Sinon SKIP.

---

### Règle 8 — Structure YAML valide (error)

**Détection :**
- Champs requis au top-level : `version`, `project`, `phases`
- Champs requis par phase : `id`, `name`, `series`
- Champs requis par série : `id`, `name`, `checks`
- Champs requis par check : `id`, `description`, `command`, `assert`, `mode`
- Types d'assertion connus : `output_equals`, `output_contains`, `output_not_contains`, `exit_code`, `file_exists`, `file_not_exists`, `line_count`, `manual_check`, `duration_under`
- Modes valides : `auto`, `manual_check`

**Gravité :** ERROR pour les champs manquants ou types inconnus.

---

### Règle 9 — Dépendances cohérentes (warning)

**Détection :**
- Cycle dans le graphe requires/provides → ERROR
- `requires` une capability que personne ne `provides` → WARNING
- `provides` une capability que personne ne `requires` → INFO (pas grave, juste informatif)

**Gravité :** ERROR pour les cycles, WARNING pour les orphelins.

---

### Règle 10 — Commandes potentiellement bloquantes (warning)

**Détection :** regex dans les commandes :
```python
r'\bread\b|\binput\b|\bconfirm\b'
```
Exclure les commandes qui ont déjà `-y`, `--yes`, `--force`, ou sont dans des pipes.

**Gravité :** WARNING.

---

## Format de sortie (terminal)

```
validate_tests.py — tests/functional/tests.yaml

AUTO-FIXED (4):
  ✓ T2.2b command: grep -c || echo 0 → ; true
  ✓ T3.1 setup: classify sans -y → ajouté -y
  ✓ Phase 5 pre_run: clean all → clean progress
  ✓ T7.2d command: && grep → ; grep

ERRORS (1):
  ✗ T0.1b command: path "02-INFORMATIQUE/05-IA-ML/" introuvable
    Dossiers dans 02-INFORMATIQUE/ : 01-Programmation, 03-IA-Machine-Learning, 04-Data-Science, ...

WARNINGS (2):
  ⚠ T3.3e command: path absolu /Users/walidnamane/...
  ⚠ T4.2c: requires 'refine_llm_ok' — aucune série ne provides ce capability

──────────────────────────────────────
Summary: 4 fixed, 1 error, 2 warnings
```

## Format de sortie (JSON)

```json
{
  "file": "tests/functional/tests.yaml",
  "fixed": [
    {"rule": "grep_c_echo", "location": "T2.2b.command", "before": "...", "after": "..."}
  ],
  "errors": [
    {"rule": "paths_exist", "location": "T0.1b.command", "message": "...", "suggestion": "..."}
  ],
  "warnings": [
    {"rule": "absolute_paths", "location": "T3.3e.command", "message": "..."}
  ],
  "summary": {"fixed": 4, "errors": 1, "warnings": 2}
}
```

## Intégration avec le Skill 1

Après implémentation du validateur, le SKILL.md de `functional-test-plan` sera simplifié :

**Avant (7+ règles CRITICAL) :**
```
- CRITICAL — Non-blocking commands: ...
- CRITICAL — Shell command chaining with grep -c: ...
- CRITICAL — Verify assertions against real data: ...
- CRITICAL — Never delete reports/logs in pre_run: ...
- Benchmarking and time comparisons: ...
...
```

**Après (1 instruction) :**
```
## Post-Generation Validation

After generating tests.yaml, run the validator:
  uv run python tests/functional/validate_tests.py --fix

If ERRORS remain after auto-fix, correct them manually before generating the Markdown.
The validator encodes all known shell pitfalls and YAML conventions — trust it over these instructions.
```

Les règles détaillées sont supprimées du SKILL.md — elles vivent dans le code du validateur.

## Fichiers générés par le runner à connaître

Le validateur doit savoir que certains fichiers sont créés **au runtime** par le runner, pas par le projet. Ne pas les signaler comme paths inexistants :

- `${LOGS}/.setup_output_<series>.txt` — stdout du setup capturé par le runner pour chaque série
- `${LOGS}/.bench_<label>.txt` — temps de benchmark capturés par les commandes `date +%s`
- `tests/functional/reports/report_*.json` / `*.md` — rapports générés par le runner

Ces paths dans les commandes des checks sont **légitimes** — le validateur ne doit pas les signaler en erreur.

## Contraintes

- Script standalone : stdlib + pyyaml uniquement
- Compatible Python 3.10+ (détection via projet, comme le runner)
- Pas de dépendance sur Klodo — le validateur est générique
- Les règles 6 et 7 (tree.yaml, code source) sont optionnelles — elles SKIP si le contexte n'est pas disponible
