# Functional Testing Skills — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create 4 Claude Code skills that standardize functional testing across all projects: generate test plans (YAML+MD), run tests, sync with GitHub Projects/Issues, and orchestrate the workflow.

**Architecture:** Each skill is a `SKILL.md` file installed in `~/.claude/skills/<skill-name>/`. Skills 1-3 are independent producers/consumers connected by shared file formats (tests.yaml, report.json). Skill 4 is a lightweight orchestrator that detects project state and routes to the right skill. No Python library is shared between skills — each SKILL.md contains instructions for Claude to generate project-specific artifacts.

**Tech Stack:** Claude Code skills (SKILL.md format), YAML, JSON, Python (generated runner), GitHub CLI (`gh`), GitHub Projects v2 (GraphQL)

**Spec:** `docs/superpowers/specs/2026-04-05-functional-testing-skills-design.md`

---

## File Structure

```
~/.claude/skills/
├── functional-test-plan/
│   └── SKILL.md                    # Skill 1 — generates tests.yaml + CAHIER.md
├── functional-test-runner/
│   └── SKILL.md                    # Skill 2 — generates run_functional.py + reports
├── test-issues-sync/
│   └── SKILL.md                    # Skill 3 — creates/updates GitHub Project + Issues
└── functional-testing/
    └── SKILL.md                    # Skill 4 — orchestrator
```

Each skill is a single SKILL.md file. No supporting scripts, references, or assets — all logic is Claude instructions.

---

### Task 1: Skill 1 — `functional-test-plan` SKILL.md

**Files:**
- Create: `~/.claude/skills/functional-test-plan/SKILL.md`

This is the foundation skill. It instructs Claude to analyze a project and generate `tests/functional/tests.yaml` + `tests/functional/CAHIER_TESTS_FONCTIONNELS.md`.

- [ ] **Step 1: Create the skill directory**

```bash
mkdir -p ~/.claude/skills/functional-test-plan
```

- [ ] **Step 2: Write the SKILL.md**

Create `~/.claude/skills/functional-test-plan/SKILL.md` with this content:

```markdown
---
name: functional-test-plan
description: "Generate a structured functional test plan (YAML + Markdown) for any project. Use when the user wants to create, update, or regenerate a functional test plan, test cahier, or acceptance tests. Analyzes the project's CLI, API, or interface to produce tests.yaml (source of truth) and CAHIER_TESTS_FONCTIONNELS.md (human-readable view)."
---

# Functional Test Plan Generator

Generate a structured functional test plan for the current project. Produces two files:
- `tests/functional/tests.yaml` — structured source of truth (machine-readable)
- `tests/functional/CAHIER_TESTS_FONCTIONNELS.md` — human-readable view (generated, never edit manually)

## Process

1. **Explore the project** — read the CLI entry point (argparse/click/typer), README, docs, and project structure to understand all commands, flags, modes, and behaviors.

2. **Identify testable surfaces** — for each command/endpoint/feature, identify:
   - Happy path (normal usage)
   - Flag combinations
   - Edge cases (empty input, missing config, bad paths, corrupted files)
   - Security (path traversal, injection, permission checks)
   - Performance (large volumes, concurrency)
   - Idempotence (running the same command twice)

3. **Organize into phases** — progressive difficulty:
   - Phase 0: Prerequisites and smoke tests
   - Phase 1-N: Feature-specific tests (one phase per major feature)
   - Second-to-last phase: Security and edge cases
   - Last phase: Performance and stability

4. **Detect variables** — extract project-specific paths, profiles, and config values into the `variables:` block. Any absolute path or environment-specific value MUST be a variable.

5. **Model dependencies** — use `requires:` / `provides:` to express test ordering. Use `pre_run:` / `post_run:` at session, phase, and series levels for setup/cleanup.

6. **Generate tests.yaml** — write to `tests/functional/tests.yaml`. If the file already exists, preserve the `custom:` section and overwrite everything else. Update `generated_at`.

7. **Generate Markdown** — write to `tests/functional/CAHIER_TESTS_FONCTIONNELS.md` from the YAML. This file is always fully regenerated.

## YAML Schema

```yaml
version: "1.0"
project: "<project name>"
generated_at: "<ISO 8601 timestamp>"

variables:
  VAR_NAME: "value"
  DERIVED_VAR: "${VAR_NAME}/subpath"

session:
  pre_run:
    - "command to run before all tests"
  post_run:
    - "command to run after all tests"

phases:
  - id: phase_0
    name: "Phase name"
    priority: obligatoire  # obligatoire | haute | moyenne | basse
    pre_run: []
    post_run: []
    series:
      - id: T0.1
        name: "Series name"
        description: "What this series validates"
        provides: [capability_name]
        requires: [other_capability]
        pre_run: []
        post_run: []
        setup:
          - "command to set up this test"
        checks:
          - id: T0.1a
            description: "What this check verifies"
            command: "shell command to run"
            assert:
              type: output_equals  # see Assertion Types below
              expected: "expected value"
            mode: auto  # auto | manual_check

custom: []  # User-added tests — NEVER overwrite this section
```

## Assertion Types

| Type | Params | Behavior |
|------|--------|----------|
| `output_equals` | `expected: "string"` | stdout (trimmed) must exactly equal expected |
| `output_contains` | `expected: ["a", "b"]` | stdout must contain ALL listed strings |
| `output_not_contains` | `expected: ["err"]` | stdout must contain NONE of the listed strings |
| `exit_code` | `expected: 0` | process exit code must equal expected |
| `file_exists` | `pattern: "glob/pattern"` | at least one file must match the glob |
| `file_not_exists` | `pattern: "glob/pattern"` | no file must match the glob |
| `line_count` | `file: "path", op: ">", value: 0` | line count of file compared with op |
| `manual_check` | `prompt: "Question?"` | ask the user in interactive mode; SKIP in auto mode |
| `duration_under` | `seconds: 60` | the setup + check must complete within N seconds |

## Markdown Format

The generated Markdown MUST follow this exact structure:

```
# Cahier de tests fonctionnels — <Project> <version>

## Prérequis
(extracted from session.pre_run and variables)

---

## Phase N — <Phase Name>

### TN.X — <Series Name>
<setup commands in a code block>

| # | Vérification | Attendu |
|---|-------------|---------|
| TN.Xa | <check description> | <expected behavior> |
| TN.Xb | ... | ... |

---

## Grille récapitulative

| Phase | Tests | Priorité | Durée estimée |
|-------|-------|----------|---------------|
| ... | ... | ... | ... |

---

## Checklist rapide (mini-run)
(only series with priority obligatoire or haute)
```

## Regeneration Rules

- When `tests/functional/tests.yaml` already exists:
  - Read the existing `custom:` section FIRST
  - Regenerate all `phases:` from scratch (analyze the project again)
  - Write back with the preserved `custom:` section
  - Fully regenerate the Markdown file
- Show the user `git diff tests/functional/tests.yaml` before committing

## Important

- Every command in `setup:`, `pre_run:`, `post_run:`, and `command:` MUST use `${VAR}` for any project-specific path. NEVER hardcode absolute paths in commands.
- Use `provides:` / `requires:` for logical dependencies. A series that creates a test profile MUST `provides: [profile_test]`, and series that need it MUST `requires: [profile_test]`.
- The `session.pre_run` should contain the global reset command (e.g., flatten everything, clear caches).
- Mark checks as `mode: manual_check` only when automated verification is genuinely impossible (subjective quality, visual inspection).
```

- [ ] **Step 3: Verify the skill is detected**

```bash
ls ~/.claude/skills/functional-test-plan/SKILL.md
```

Expected: file exists, no error.

- [ ] **Step 4: Test the skill in a Claude Code session**

Open a new Claude Code session in the Klodo project and run:
```
/functional-test-plan
```

Expected: Claude explores the project, generates `tests/functional/tests.yaml` and `tests/functional/CAHIER_TESTS_FONCTIONNELS.md`. Verify:
- YAML has `variables:` with no hardcoded paths
- `requires:`/`provides:` are used for dependent series
- `session.pre_run` contains the flatten command
- `custom: []` section exists
- Markdown matches the format of the existing `tests/CAHIER_TESTS_FONCTIONNELS.md`

- [ ] **Step 5: Commit**

```bash
cd ~/.claude/skills && git init functional-test-plan 2>/dev/null; cd functional-test-plan && git add SKILL.md && git commit -m "feat: add functional-test-plan skill — generates YAML + Markdown test plans"
```

---

### Task 2: Skill 2 — `functional-test-runner` SKILL.md

**Files:**
- Create: `~/.claude/skills/functional-test-runner/SKILL.md`

This skill instructs Claude to generate a Python runner script from tests.yaml, then optionally execute it.

- [ ] **Step 1: Create the skill directory**

```bash
mkdir -p ~/.claude/skills/functional-test-runner
```

- [ ] **Step 2: Write the SKILL.md**

Create `~/.claude/skills/functional-test-runner/SKILL.md` with this content:

```markdown
---
name: functional-test-runner
description: "Generate and run a functional test runner from tests.yaml. Use when the user wants to execute functional tests, run the test plan, generate a test runner script, or check test results. Reads tests/functional/tests.yaml and produces run_functional.py + JSON/Markdown reports."
---

# Functional Test Runner

Generate a Python script `tests/functional/run_functional.py` from `tests/functional/tests.yaml`, then optionally execute it. The script is standalone — it can be run without Claude after generation.

## Prerequisites

- `tests/functional/tests.yaml` must exist (generated by `functional-test-plan` skill)
- Python 3.10+ available
- `pyyaml` package installed (the generated script uses it)
- `gh` CLI available and authenticated (only needed for GitHub issue integration in interactive mode)

## Process

1. **Read tests.yaml** — load the full test plan including custom section
2. **Generate run_functional.py** — write the runner script to `tests/functional/run_functional.py`. Always regenerate (the YAML may have changed).
3. **Ask the user how to run** — propose the execution modes:
   - Full-auto: `python tests/functional/run_functional.py`
   - Interactive: `python tests/functional/run_functional.py --interactive`
   - Dry-run: `python tests/functional/run_functional.py --dry-run`
   - Filtered: `--phase`, `--series`, `--priority`
4. **Execute** — run the chosen command and display results
5. **Show report** — display the Markdown report summary

## Runner Script Specification

The generated `run_functional.py` MUST implement ALL of the following. Do NOT skip any feature.

### CLI Arguments

```
python run_functional.py [OPTIONS]

Options:
  --interactive          Step-by-step mode with user prompts
  --phase N [N ...]      Only run these phase numbers
  --series ID [ID ...]   Only run these series IDs (e.g., T1.1 T2.3)
  --priority P [P ...]   Only run series with these priorities
  --var KEY=VALUE        Override a variable (repeatable)
  --dry-run              Show what would be executed without running
```

### Variable Resolution

1. Load `variables:` from YAML
2. Apply `--var` overrides
3. Resolve references: `${VAR}` in any string value is replaced with the variable's value
4. Resolution is recursive (a variable can reference another variable)
5. Unresolved `${VAR}` in a command raises an error before execution

### Dependency Resolution

1. Build a graph: for each series, map `requires:` to the series that `provides:` those capabilities
2. Within each phase, order series by dependency (topological sort)
3. Cross-phase dependencies are allowed (Phase 1 series can require something from Phase 0)
4. Circular dependencies raise an error before execution

### Execution Flow

```
1. Load and validate tests.yaml
2. Resolve variables
3. Resolve dependencies (topological sort)
4. Apply filters (--phase, --series, --priority)
5. Execute session.pre_run commands
6. For each phase (in order):
   a. Execute phase.pre_run
   b. For each series (in dependency order):
      i.   Check requires — if any provider FAILED → mark SKIP
      ii.  Execute series.pre_run
      iii. Execute series.setup commands
      iv.  For each check:
           - mode=auto: run command, evaluate assert → PASS/FAIL
           - mode=manual_check + --interactive: show prompt, ask user → PASS/FAIL/SKIP
           - mode=manual_check + auto mode: → SKIP
      v.   Execute series.post_run
      vi.  If ALL checks PASS → register provides as available
   c. Execute phase.post_run
7. Execute session.post_run
8. Write report JSON to tests/functional/reports/report_YYYY-MM-DD_HHMMSS.json
9. Write report Markdown to tests/functional/reports/report_YYYY-MM-DD_HHMMSS.md
10. Print summary to stdout
```

### Assertion Evaluation

Each assertion type must be implemented as follows:

- **output_equals**: run command, strip stdout, compare to `expected` string
- **output_contains**: run command, check that every string in `expected` list appears in stdout
- **output_not_contains**: run command, check that no string in `expected` list appears in stdout
- **exit_code**: run command, compare return code to `expected` integer
- **file_exists**: expand glob `pattern`, PASS if at least one match
- **file_not_exists**: expand glob `pattern`, PASS if zero matches
- **line_count**: count lines in `file`, compare with `op` (">", "<", ">=", "<=", "==") against `value`
- **manual_check**: in --interactive mode, display `prompt` and ask [P]ass/[F]ail/[S]kip. In auto mode, SKIP.
- **duration_under**: measure wall-clock time of setup + check command, PASS if under `seconds`

### Interactive Mode

When `--interactive` is used, after each series the runner displays:

```
── T1.1 — Series Name : PASS (2/2) ──

  Continuer avec T1.2 ? [Y]es / [S]kip / [Q]uit ? _
```

If a `github_issue` field exists for the series in tests.yaml:
- On PASS: `Issue GitHub #N associée → Fermer et poster le rapport ? [Y]es / [N]o / [A]ll ? _`
- On FAIL: `Issue GitHub #N associée → Poster le rapport d'erreur ? [Y]es / [N]o ? _`

If `github_issue` field is absent, skip the GitHub prompt entirely (no error).

The `[A]ll` option switches to automatic GitHub issue handling for the rest of the session.

GitHub actions use `gh` CLI:
- Close: `gh issue close N --comment "..."`
- Comment: `gh issue comment N --body "..."`

### Report JSON Format

```json
{
  "project": "<from tests.yaml>",
  "run_at": "<ISO 8601>",
  "duration_seconds": 842,
  "variables": { "KEY": "resolved_value" },
  "summary": { "total": 85, "pass": 72, "fail": 5, "skip": 8 },
  "phases": [
    {
      "id": "phase_1",
      "name": "Phase Name",
      "series": [
        {
          "id": "T1.1",
          "name": "Series Name",
          "status": "pass",
          "duration_ms": 3200,
          "checks": [
            {
              "id": "T1.1a",
              "description": "Check description",
              "status": "pass",
              "command": "actual command run",
              "output": "stdout captured",
              "duration_ms": 45
            }
          ]
        }
      ]
    }
  ]
}
```

For FAIL checks, also include `expected` and `error` fields.

### Report Markdown Format

```markdown
# Rapport de tests fonctionnels — <Project>
**Date :** YYYY-MM-DD HH:MM  |  **Durée :** Xm XXs

## Résumé
| Total | Pass | Fail | Skip |
|-------|------|------|------|
| N     | N    | N    | N    |

## Phase N — <Name>
### TN.X — <Series Name> : PASS/FAIL/SKIP
| # | Check | Status | Détail |
|---|-------|--------|--------|
| TN.Xa | description | PASS | output snippet |

## Échecs détaillés
### TN.Xb — <Check description>
- **Commande :** `...`
- **Attendu :** `...`
- **Obtenu :** `...`
```

## Important

- The generated script MUST be standalone: no imports from the project, only stdlib + pyyaml.
- Create `tests/functional/reports/` directory if it does not exist.
- All subprocess commands run with `shell=True` and `cwd` set to the project root.
- Capture both stdout and stderr for each command.
- Set a default timeout of 300 seconds per command (5 minutes). The `duration_under` assertion uses its own `seconds` value.
- If `session.pre_run` fails, abort the entire run (the environment is not ready).
- If `phase.pre_run` fails, skip the entire phase.
- If `series.pre_run` fails, skip the series and do NOT register its `provides`.
```

- [ ] **Step 3: Verify the skill is detected**

```bash
ls ~/.claude/skills/functional-test-runner/SKILL.md
```

Expected: file exists.

- [ ] **Step 4: Test the skill on Klodo**

Prerequisites: Task 1 must be completed and `tests/functional/tests.yaml` must exist in the Klodo project.

Open a new Claude Code session and run:
```
/functional-test-runner
```

Expected: Claude generates `tests/functional/run_functional.py`. Verify:
- Script has all CLI arguments (--interactive, --phase, --series, --priority, --var, --dry-run)
- Variable resolution handles `${VAR}` recursion
- Dependency resolution with topological sort
- All 9 assertion types implemented
- JSON and Markdown reports generated in `tests/functional/reports/`
- Interactive mode with GitHub issue integration
- `--dry-run` shows plan without executing

Run `python tests/functional/run_functional.py --dry-run` to verify it parses the YAML correctly.

- [ ] **Step 5: Commit**

```bash
cd ~/.claude/skills && git init functional-test-runner 2>/dev/null; cd functional-test-runner && git add SKILL.md && git commit -m "feat: add functional-test-runner skill — generates and runs Python test scripts"
```

---

### Task 3: Skill 3 — `test-issues-sync` SKILL.md

**Files:**
- Create: `~/.claude/skills/test-issues-sync/SKILL.md`

This skill instructs Claude to create/update GitHub Projects and Issues from the test plan and reports.

- [ ] **Step 1: Create the skill directory**

```bash
mkdir -p ~/.claude/skills/test-issues-sync
```

- [ ] **Step 2: Write the SKILL.md**

Create `~/.claude/skills/test-issues-sync/SKILL.md` with this content:

```markdown
---
name: test-issues-sync
description: "Synchronize functional test plans and results with GitHub Projects and Issues. Use when the user wants to create GitHub issues from a test plan, sync test results to GitHub, update a project board with test status, close issues for passing tests, or manage functional test tracking on GitHub. Requires gh CLI authenticated."
---

# Test Issues Sync

Synchronize `tests/functional/tests.yaml` and test reports with GitHub: create a Project board, create Issues per test series, and update their status based on test results.

## Prerequisites

- `tests/functional/tests.yaml` must exist
- `gh` CLI must be installed and authenticated (`gh auth status`)
- Current directory must be a git repo with a GitHub remote

## Two Modes

### Mode: Create

Create the GitHub Project and Issues from the test plan.

**Steps:**

1. **Detect the repo** — run `gh repo view --json nameWithOwner -q .nameWithOwner` to get `owner/repo`.

2. **Find or create the Project** — search for a GitHub Project (v2) named `Tests Fonctionnels - <project>` (where `<project>` comes from `tests.yaml`).

   To list projects on the repo:
   ```bash
   gh project list --owner <owner> --format json
   ```
   
   If not found, create one:
   ```bash
   gh project create --owner <owner> --title "Tests Fonctionnels - <project>" --format json
   ```

   Store the project number for subsequent commands.

3. **Configure custom fields** on the project:

   Add a single-select field `Phase` with options matching the phases in tests.yaml:
   ```bash
   gh project field-create <PROJECT_NUMBER> --owner <owner> --name "Phase" --data-type "SINGLE_SELECT" --single-select-options "Phase 0,Phase 1,Phase 2,..."
   ```

   Add a single-select field `Priorité` with options:
   ```bash
   gh project field-create <PROJECT_NUMBER> --owner <owner> --name "Priorité" --data-type "SINGLE_SELECT" --single-select-options "obligatoire,haute,moyenne,basse"
   ```

   Note: The built-in "Status" field already exists on new projects. Update its options to: `Todo,In Progress,Pass,Fail,Skipped` using:
   ```bash
   gh api graphql -f query='mutation { ... }'
   ```

4. **Create labels** on the repo (idempotent — skip if they exist):
   ```bash
   gh label create "functional-test" --color "0E8A16" --description "Functional test series" 2>/dev/null || true
   gh label create "phase-0" --color "C5DEF5" 2>/dev/null || true
   gh label create "phase-1" --color "C5DEF5" 2>/dev/null || true
   # ... one per phase
   gh label create "priority-obligatoire" --color "B60205" 2>/dev/null || true
   gh label create "priority-haute" --color "D93F0B" 2>/dev/null || true
   gh label create "priority-moyenne" --color "FBCA04" 2>/dev/null || true
   gh label create "priority-basse" --color "0E8A16" 2>/dev/null || true
   ```

5. **Create one issue per series** — for each series in tests.yaml (both `phases` and `custom`):

   First check if the issue already exists (idempotent):
   ```bash
   gh issue list --label "functional-test" --search "[T0.1]" --json number -q '.[0].number'
   ```
   
   If not found, create it:
   ```bash
   gh issue create --title "[T0.1] Series Name" --label "functional-test,phase-0,priority-haute" --body "$(cat <<'EOF'
   ## Phase 0 — Phase Name
   **Série :** T0.1
   **Priorité :** haute
   **Dépendances :** capability_name (T0.0)

   ### Description
   What this series validates

   ### Setup
   ```bash
   command1
   command2
   ```

   ### Checks
   | # | Description | Type | Mode |
   |---|------------|------|------|
   | T0.1a | Check description | output_equals | auto |
   | T0.1b | Check description | manual_check | manual |

   ---
   *Généré par test-issues-sync*
   EOF
   )"
   ```

6. **Add each issue to the project** and set its custom field values:
   ```bash
   gh project item-add <PROJECT_NUMBER> --owner <owner> --url <issue_url>
   ```
   Then set the Phase and Priorité fields via GraphQL.

7. **Write `github_issue` back to tests.yaml** — for each created issue, add `github_issue: <number>` to the corresponding series in tests.yaml. Use a YAML-aware approach: load the file, add the field, write it back preserving structure and comments as much as possible.

### Mode: Update

Update GitHub Issues based on test results.

**Steps:**

1. **Find the latest report** — look for the most recent `tests/functional/reports/report_*.json` file.

2. **Load the report** and iterate over each series result.

3. **For each series with a `github_issue` in tests.yaml:**

   **If status = PASS:**
   ```bash
   gh issue close <number> --comment "$(cat <<'EOF'
   ## Rapport d'exécution — <date>

   | Check | Status | Détail |
   |-------|--------|--------|
   | T1.1a | PASS | output snippet |
   | T1.1b | PASS | validated manually |

   **Résultat : PASS** — Issue fermée automatiquement.
   EOF
   )"
   ```

   **If status = FAIL:**
   ```bash
   gh issue comment <number> --body "$(cat <<'EOF'
   ## Rapport d'exécution — <date>

   | Check | Status | Détail |
   |-------|--------|--------|
   | T1.1a | PASS | output snippet |
   | T1.1b | FAIL | output_equals: got '3', expected '0' |

   **Résultat : FAIL** — N échec(s) sur M checks.

   ### Détail de l'échec T1.1b
   - **Commande :** `...`
   - **Attendu :** `0`
   - **Obtenu :** `3`
   EOF
   )"
   ```

   **If status = SKIP:**
   ```bash
   gh issue comment <number> --body "**Skipped** — dependency failed: <reason>"
   ```

4. **Update project board status** for each issue via GraphQL:
   - PASS → `Pass`
   - FAIL → `Fail`
   - SKIP → `Skipped`

## Choosing the Mode

When the skill is invoked, check the state:
- If no issues exist with label `functional-test` → run **Create**
- If issues exist but no report → tell user to run the tests first
- If issues exist and a report exists → run **Update**
- Always offer both modes explicitly so the user can choose

## Important

- All `gh` commands must handle errors gracefully. If a command fails, log the error and continue (don't abort the entire sync).
- GitHub Projects v2 API uses GraphQL. Some operations (updating custom field values, modifying Status options) require `gh api graphql` calls. Use `gh project` CLI commands where possible for simplicity.
- The `github_issue` field in tests.yaml is the contract between this skill and the runner. Guard it: never remove existing values, only add new ones.
- Creating 40+ issues can hit GitHub rate limits. Add a 1-second pause between issue creations.
```

- [ ] **Step 3: Verify the skill is detected**

```bash
ls ~/.claude/skills/test-issues-sync/SKILL.md
```

Expected: file exists.

- [ ] **Step 4: Test the skill on Klodo**

Prerequisites: Task 1 completed (tests.yaml exists), Klodo repo has a GitHub remote.

Open a new Claude Code session and run:
```
/test-issues-sync
```

Expected: Claude detects Create mode, creates the project board, creates issues, writes `github_issue` fields back to tests.yaml. Verify:
- Project "Tests Fonctionnels - Klodo" visible on GitHub
- Issues created with correct titles `[T0.1] ...`, labels, and body
- `tests.yaml` now has `github_issue: N` for each series
- Issues appear on the project board with correct Phase and Priorité

- [ ] **Step 5: Commit**

```bash
cd ~/.claude/skills && git init test-issues-sync 2>/dev/null; cd test-issues-sync && git add SKILL.md && git commit -m "feat: add test-issues-sync skill — GitHub Project + Issues from test plans"
```

---

### Task 4: Skill 4 — `functional-testing` orchestrator SKILL.md

**Files:**
- Create: `~/.claude/skills/functional-testing/SKILL.md`

Lightweight orchestrator that detects project state and routes to the right skill.

- [ ] **Step 1: Create the skill directory**

```bash
mkdir -p ~/.claude/skills/functional-testing
```

- [ ] **Step 2: Write the SKILL.md**

Create `~/.claude/skills/functional-testing/SKILL.md` with this content:

```markdown
---
name: functional-testing
description: "Orchestrate functional testing workflow: generate test plans, run tests, sync with GitHub. Use when the user mentions functional tests, test plans, test cahier, running acceptance tests, or syncing test results. This is the main entry point — it detects the current state and routes to the right sub-skill."
---

# Functional Testing Orchestrator

Main entry point for the functional testing workflow. Detects the current project state and proposes the appropriate next action.

## State Detection

Check these artifacts in order:

1. Does `tests/functional/tests.yaml` exist?
2. Does `tests/functional/run_functional.py` exist?
3. Do any `tests/functional/reports/report_*.json` files exist?
4. Do GitHub issues with label `functional-test` exist? (check via `gh issue list --label functional-test --json number -q 'length'`)

## Decision Tree

### Case 1 — No tests.yaml (fresh project)

```
Aucun cahier de tests fonctionnels détecté.
→ Je génère le cahier ? (tests.yaml + Markdown)
```

Action: invoke the `functional-test-plan` skill.

### Case 2 — tests.yaml exists, no runner

```
Cahier trouvé (<N> séries, <M> checks, généré le <date>).
Pas de runner généré.

  1. Générer le runner
  2. Regénérer le cahier (code a changé depuis)
  3. Regénérer le cahier + générer le runner
```

Action: invoke `functional-test-runner` (option 1), `functional-test-plan` (option 2), or both sequentially (option 3).

### Case 3 — Runner exists, no report

```
Runner prêt. Aucun rapport d'exécution.

  1. Lancer les tests [full-auto / interactive / dry-run]
  2. Regénérer le cahier
```

Action: run the script directly or invoke `functional-test-plan`.

### Case 4 — Report exists, no GitHub issues

```
Dernier rapport : <date> (<pass> pass, <fail> fail, <skip> skip)
Issues GitHub : non créées

  1. Créer le project board et les issues + sync résultats
  2. Relancer les tests
  3. Regénérer le cahier
```

Action: invoke `test-issues-sync` (option 1), run script (option 2), or invoke `functional-test-plan` (option 3).

### Case 5 — Everything in place

```
État actuel :
  Cahier : <N> séries, <M> checks (mis à jour le <date>)
  Dernier run : <date> — <pass> pass, <fail> fail, <skip> skip
  Issues : <closed>/<total> fermées, <open> ouvertes

Que veux-tu faire ?
  1. Relancer les tests
  2. Relancer seulement les FAIL (<N> séries)
  3. Regénérer le cahier (après changement de code)
  4. Sync les issues avec le dernier rapport
```

## Rules

- **Always offer "Regénérer le cahier"** as an option whenever tests.yaml exists (Cases 2-5).
- **Respect explicit intent** — if the user says something specific, route directly:
  - "génère le cahier de tests" → `functional-test-plan`
  - "lance les tests fonctionnels" → `functional-test-runner`
  - "sync les issues" → `test-issues-sync`
  - "relance les tests qui ont échoué" → run script with `--series` filtered to FAIL series from last report
  - "mets à jour le cahier" → `functional-test-plan` (regeneration)
- **Read stats from files** to populate the status display:
  - Series/check count: parse tests.yaml
  - Generated date: `generated_at` field in tests.yaml
  - Report stats: parse the most recent report JSON `summary` field
  - Issue count: `gh issue list --label functional-test --state all --json state -q '[.[] | select(.state=="OPEN")] | length'`

## Important

- This skill does NOT generate any files. It only inspects state and routes to other skills.
- Keep the detection lightweight — only read file existence and small JSON fields, don't parse entire files unnecessarily.
- When invoking a sub-skill, use the Skill tool: `Skill(skill="functional-test-plan")`.
```

- [ ] **Step 3: Verify the skill is detected**

```bash
ls ~/.claude/skills/functional-testing/SKILL.md
```

Expected: file exists.

- [ ] **Step 4: Test the orchestrator on Klodo**

Open a new Claude Code session (with or without existing artifacts) and run:
```
/functional-testing
```

Expected: Claude detects the current state and presents the appropriate menu. Test each case by removing/adding artifacts:
- Remove tests.yaml → Case 1
- Have tests.yaml but no runner → Case 2
- Have runner but no report → Case 3
- Have report but no issues → Case 4
- Have everything → Case 5

- [ ] **Step 5: Commit**

```bash
cd ~/.claude/skills && git init functional-testing 2>/dev/null; cd functional-testing && git add SKILL.md && git commit -m "feat: add functional-testing orchestrator skill — routes to sub-skills based on project state"
```

---

### Task 5: End-to-End Validation on Klodo

**Files:**
- No new files — validation only

Full workflow test on the Klodo project to verify all 4 skills work together.

- [ ] **Step 1: Clean slate**

Remove any artifacts from previous testing:
```bash
rm -rf tests/functional/
```

- [ ] **Step 2: Run the orchestrator**

```
/functional-testing
```

Expected: Case 1 detected, proposes to generate the cahier.

- [ ] **Step 3: Generate the test plan**

Let the orchestrator invoke `functional-test-plan`. Verify:
- `tests/functional/tests.yaml` created with variables, phases, dependencies
- `tests/functional/CAHIER_TESTS_FONCTIONNELS.md` created and matches the format
- No hardcoded paths in commands
- `custom: []` section present

- [ ] **Step 4: Run the orchestrator again**

```
/functional-testing
```

Expected: Case 2 detected, proposes to generate the runner.

- [ ] **Step 5: Generate the runner**

Let it invoke `functional-test-runner`. Verify:
- `tests/functional/run_functional.py` created
- `python tests/functional/run_functional.py --dry-run` works and shows the plan
- All assertion types are implemented in the script

- [ ] **Step 6: Run tests in dry-run**

```bash
python tests/functional/run_functional.py --dry-run
```

Expected: shows all phases, series, checks without executing anything.

- [ ] **Step 7: Run the orchestrator again**

```
/functional-testing
```

Expected: Case 3, proposes to run the tests.

- [ ] **Step 8: Sync with GitHub**

After getting a report, invoke:
```
/test-issues-sync
```

Expected: Project board created, issues created, `github_issue` fields written to tests.yaml.

- [ ] **Step 9: Final state check**

```
/functional-testing
```

Expected: Case 5, shows full status with counts from report and GitHub.
