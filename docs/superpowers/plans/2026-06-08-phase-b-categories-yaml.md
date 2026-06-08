# Phase B — Proposer aussi categories.yaml Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal :** Éliminer la divergence entre la proposition Phase B et la simulation Impact reclassify en faisant produire à Phase B un `categories-proposed.yaml` cohérent.

**Architecture :** Phase B passe de 1 à 6 étapes (toutes dans `propose_changes`) : 4 nouvelles (cascade déterministe + LLM mots-clés + merge + écriture artefact) + rationale markdown étendu + simulator qui lit le nouveau fichier avec fallback. Application en prod hors-scope.

**Tech Stack :** Python 3.13, Pydantic v2, LangChain (ChatOpenAI structured output), unittest, yaml.

**Spec source :** [docs/superpowers/specs/2026-06-08-phase-b-categories-yaml-design.md](../specs/2026-06-08-phase-b-categories-yaml-design.md)

---

## File Structure

### Modules touchés

| Fichier | Action | Responsabilité |
|---|---|---|
| `agents/refonte/proposition_tools.py` | Modifier | Orchestration : ajouter `_groupe_from_path_prefix`, `_cascade_categories_changes`, `_merge_categories_changes`. Étendre `propose_changes` (steps 2-4) + `_render_rationale_markdown` (step 5) |
| `agents/refonte/categories_llm.py` | Créer | Module dédié au prompt + Pydantic schemas + appel LLM structured output (~100 LOC) |
| `agents/refonte/simulator.py` | Modifier (~3 lignes) | Lire `categories-proposed.yaml` avec fallback sur prod |
| `tests/auto/test_agent_refonte_categories.py` | Créer | Tests unitaires des cascades + LLM module (~250 LOC) |
| `tests/auto/test_agent_refonte_proposition.py` | Étendre | Tests d'intégration `propose_changes` end-to-end avec categories |
| `tests/auto/test_agent_refonte_simulator.py` | Étendre | Tests simulator avec le nouveau fallback |

### Fixtures partagées

- Tests de cascade : un dict `categories.yaml` minimal en mémoire (pas besoin de fichier).
- Tests LLM : mock de `langchain_openai.ChatOpenAI.with_structured_output()` qui retourne un `_NewCategoriesProposal` pré-construit.
- Tests d'intégration : fixture `_make_profile()` réutilisée depuis `test_agent_refonte_proposition.py` existant.

---

## Task 1 : Helper `_groupe_from_path_prefix`

**Files :**

- Modify : `agents/refonte/proposition_tools.py` (ajouter helper en haut du fichier, après les imports)
- Test : `tests/auto/test_agent_refonte_categories.py` (créer)

- [ ] **Step 1 : Write the failing tests**

Créer `tests/auto/test_agent_refonte_categories.py` :

```python
#!/usr/bin/env python3
"""Tests pour les helpers de Phase B liés à categories.yaml.

Couvre :
  1. _groupe_from_path_prefix : inférence du groupe depuis le préfixe
  2. _cascade_categories_changes : application déterministe des renames/
     fusions/deletions sur les entries existantes
  3. categories_llm : Pydantic schemas + appel LLM + fallback
"""

import os
import sys
import unittest
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from agents.refonte.proposition_tools import _groupe_from_path_prefix  # noqa: E402


class TestGroupeInference(unittest.TestCase):

    def setUp(self):
        self.existing_categories = {
            "informatique": [
                {"chemin": "02-INFORMATIQUE/14-Web", "priorite": 3, "mots_cles": ["html"]},
                {"chemin": "02-INFORMATIQUE/05-IA-ML", "priorite": 2, "mots_cles": ["ml"]},
            ],
            "sciences": [
                {"chemin": "01-SCIENCES/PHYSIQUE", "priorite": 4, "mots_cles": ["physics"]},
            ],
            "bureautique": [
                {"chemin": "09-BUREAU/Excel", "priorite": 5, "mots_cles": ["excel"]},
            ],
        }

    def test_groupe_inference_from_path_prefix(self):
        # New path under 02-INFORMATIQUE → groupe "informatique"
        self.assertEqual(
            _groupe_from_path_prefix("02-INFORMATIQUE/05-IA-ML/RAG", self.existing_categories),
            "informatique",
        )
        # New path under 01-SCIENCES → groupe "sciences"
        self.assertEqual(
            _groupe_from_path_prefix("01-SCIENCES/CHIMIE/04-Materiaux", self.existing_categories),
            "sciences",
        )

    def test_groupe_inference_no_match_falls_back_to_autres(self):
        # No existing entry under 99-UNKNOWN → fallback "autres"
        self.assertEqual(
            _groupe_from_path_prefix("99-UNKNOWN/Whatever", self.existing_categories),
            "autres",
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2 : Run tests to verify they fail**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_categories.TestGroupeInference -v
```

Expected : `ImportError: cannot import name '_groupe_from_path_prefix' from 'agents.refonte.proposition_tools'`

- [ ] **Step 3 : Implement minimal `_groupe_from_path_prefix`**

Ajouter au haut de `agents/refonte/proposition_tools.py` (après les imports existants) :

```python
def _groupe_from_path_prefix(
    path: str,
    existing_categories: dict[str, list[dict]],
) -> str:
    """Infère le groupe d'un nouveau chemin à partir des préfixes des
    entries existantes. Si un préfixe path correspond à un groupe (le plus
    représenté en cas d'ambiguïté), retourne ce groupe. Sinon "autres".
    """
    # Compte, pour chaque groupe, combien d'entries partagent un préfixe
    # avec `path` (par segment, du plus long au plus court).
    parts = path.split("/")
    for n_segments in range(len(parts), 0, -1):
        prefix = "/".join(parts[:n_segments])
        scores: dict[str, int] = {}
        for groupe, entries in existing_categories.items():
            for entry in entries:
                chemin = entry.get("chemin", "")
                if chemin.startswith(prefix + "/") or chemin == prefix:
                    scores[groupe] = scores.get(groupe, 0) + 1
        if scores:
            # Groupe le plus représenté à ce niveau de préfixe
            return max(scores.items(), key=lambda kv: kv[1])[0]
    return "autres"
```

- [ ] **Step 4 : Run tests to verify they pass**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_categories.TestGroupeInference -v
```

Expected : `Ran 2 tests in 0.001s — OK`

- [ ] **Step 5 : Commit**

```bash
git add tests/auto/test_agent_refonte_categories.py agents/refonte/proposition_tools.py
git commit -m "feat(refonte-B): _groupe_from_path_prefix helper for categories cascade

Infers the groupe of a new path from existing categories.yaml entries by
matching the longest common prefix. Falls back to 'autres' when no match.

Used by upcoming _cascade_categories_changes and categories_llm modules."
```

---

## Task 2 : Cascade — rename simple

**Files :**

- Modify : `agents/refonte/proposition_tools.py` (ajouter `_cascade_categories_changes`)
- Modify : `tests/auto/test_agent_refonte_categories.py` (ajouter classe `TestCascadeCategories`)

- [ ] **Step 1 : Write the failing test**

Ajouter à `tests/auto/test_agent_refonte_categories.py` :

```python
from agents.refonte.proposition_tools import _cascade_categories_changes  # noqa: E402


class TestCascadeCategories(unittest.TestCase):

    def test_cascade_renames_simple_path(self):
        current = {
            "informatique": [
                {"chemin": "02-INFORMATIQUE/14-Web", "priorite": 3,
                 "mots_cles": ["html", "css"]},
            ],
        }
        renamings = [{"old_path": "02-INFORMATIQUE/14-Web",
                      "new_path": "02-INFORMATIQUE/14-Web-Frontend"}]
        new_cats, log = _cascade_categories_changes(
            current, renamings=renamings, fusions=[], deletions=[])
        self.assertEqual(new_cats["informatique"][0]["chemin"],
                         "02-INFORMATIQUE/14-Web-Frontend")
        # Mots_cles + priorite inchangés
        self.assertEqual(new_cats["informatique"][0]["mots_cles"], ["html", "css"])
        self.assertEqual(new_cats["informatique"][0]["priorite"], 3)
        # Log contient l'entry rename
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["type"], "rename")
        self.assertEqual(log[0]["old"], "02-INFORMATIQUE/14-Web")
        self.assertEqual(log[0]["new"], "02-INFORMATIQUE/14-Web-Frontend")
        self.assertEqual(log[0]["n_entries"], 1)
```

- [ ] **Step 2 : Run test to verify it fails**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_categories.TestCascadeCategories.test_cascade_renames_simple_path -v
```

Expected : `ImportError: cannot import name '_cascade_categories_changes'`

- [ ] **Step 3 : Implement minimal cascade (rename only)**

Ajouter à `agents/refonte/proposition_tools.py` (après `_groupe_from_path_prefix`) :

```python
def _cascade_categories_changes(
    current_categories: dict[str, list[dict]],
    renamings: list[dict],
    fusions: list[dict],
    deletions: list[dict],
) -> tuple[dict[str, list[dict]], list[dict]]:
    """Applique de manière déterministe renames/fusions/deletions sur les
    entries existantes de categories.yaml. Pas d'appel LLM.

    Retourne :
        - new_categories : structure YAML mise à jour
        - log_modifications : liste de dicts pour _render_rationale_markdown
    """
    # Copy défensive
    new_categories: dict[str, list[dict]] = {
        groupe: [dict(e) for e in entries]
        for groupe, entries in current_categories.items()
    }
    log: list[dict] = []

    rename_map = {r["old_path"]: r["new_path"] for r in renamings}

    for groupe, entries in new_categories.items():
        for entry in entries:
            chemin = entry.get("chemin", "")
            if chemin in rename_map:
                new_chemin = rename_map[chemin]
                entry["chemin"] = new_chemin

    for r in renamings:
        n = sum(
            1
            for entries in new_categories.values()
            for e in entries
            if e.get("chemin") == r["new_path"]
        )
        if n > 0:
            log.append({
                "type": "rename",
                "old": r["old_path"],
                "new": r["new_path"],
                "n_entries": n,
            })

    return new_categories, log
```

- [ ] **Step 4 : Run test to verify it passes**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_categories.TestCascadeCategories.test_cascade_renames_simple_path -v
```

Expected : `Ran 1 test in 0.001s — OK`

- [ ] **Step 5 : Commit**

```bash
git add tests/auto/test_agent_refonte_categories.py agents/refonte/proposition_tools.py
git commit -m "feat(refonte-B): _cascade_categories_changes — simple rename

Initial scaffolding of the deterministic cascade. Renames are remapped
in-place and logged. Fusions/deletions/prefix-propagation added in
follow-up tasks."
```

---

## Task 3 : Cascade — rename par préfixe

**Files :**

- Modify : `agents/refonte/proposition_tools.py` (étendre `_cascade_categories_changes`)
- Modify : `tests/auto/test_agent_refonte_categories.py` (ajouter test)

- [ ] **Step 1 : Write the failing test**

Ajouter à `TestCascadeCategories` :

```python
def test_cascade_renames_prefix_propagation(self):
    current = {
        "informatique": [
            {"chemin": "02-INFORMATIQUE/14-Web/React", "priorite": 4,
             "mots_cles": ["react", "jsx"]},
            {"chemin": "02-INFORMATIQUE/14-Web/Vue", "priorite": 4,
             "mots_cles": ["vue", "vuex"]},
        ],
    }
    renamings = [{"old_path": "02-INFORMATIQUE/14-Web",
                  "new_path": "02-INFORMATIQUE/14-Web-Frontend"}]
    new_cats, log = _cascade_categories_changes(
        current, renamings=renamings, fusions=[], deletions=[])
    chemins = [e["chemin"] for e in new_cats["informatique"]]
    self.assertIn("02-INFORMATIQUE/14-Web-Frontend/React", chemins)
    self.assertIn("02-INFORMATIQUE/14-Web-Frontend/Vue", chemins)
    # Log : type rename_prefix pour les 2 entries
    prefix_logs = [le for le in log if le["type"] == "rename_prefix"]
    self.assertEqual(len(prefix_logs), 2)
```

- [ ] **Step 2 : Run test to verify it fails**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_categories.TestCascadeCategories.test_cascade_renames_prefix_propagation -v
```

Expected : FAIL — les chemins préfixés ne sont pas renommés.

- [ ] **Step 3 : Extend cascade with prefix propagation**

Remplacer le bloc d'application des renames dans `_cascade_categories_changes` par :

```python
    rename_map = {r["old_path"]: r["new_path"] for r in renamings}

    for groupe, entries in new_categories.items():
        for entry in entries:
            chemin = entry.get("chemin", "")
            if chemin in rename_map:
                entry["chemin"] = rename_map[chemin]
                continue
            # Rename par préfixe : remplace old_path/* par new_path/*
            for old, new in rename_map.items():
                if chemin.startswith(old + "/"):
                    entry["chemin"] = new + chemin[len(old):]
                    entry["_was_prefix_renamed"] = (old, new)  # marqueur temp
                    break

    for r in renamings:
        n_exact = sum(
            1
            for entries in new_categories.values()
            for e in entries
            if e.get("chemin") == r["new_path"]
        )
        if n_exact > 0:
            log.append({
                "type": "rename",
                "old": r["old_path"],
                "new": r["new_path"],
                "n_entries": n_exact,
            })

    for groupe, entries in new_categories.items():
        for entry in entries:
            marker = entry.pop("_was_prefix_renamed", None)
            if marker:
                old, new = marker
                log.append({
                    "type": "rename_prefix",
                    "old": old,
                    "new": new,
                    "chemin_renamed": entry["chemin"],
                    "n_entries": 1,
                })
```

- [ ] **Step 4 : Run all cascade tests**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_categories.TestCascadeCategories -v
```

Expected : 2 tests OK (simple + prefix).

- [ ] **Step 5 : Commit**

```bash
git add tests/auto/test_agent_refonte_categories.py agents/refonte/proposition_tools.py
git commit -m "feat(refonte-B): cascade renames propagate to sub-paths

Sub-folders of renamed folders are also remapped. Each prefix-renamed
entry is logged as type='rename_prefix' for the markdown rationale."
```

---

## Task 4 : Cascade — collision sur rename

**Files :**

- Modify : `agents/refonte/proposition_tools.py` (gérer collisions)
- Modify : `tests/auto/test_agent_refonte_categories.py` (ajouter test)

- [ ] **Step 1 : Write the failing test**

Ajouter à `TestCascadeCategories` :

```python
def test_cascade_renames_collision_merges_mots_cles(self):
    # L'user avait déjà créé une entry pour le new_path → collision
    current = {
        "informatique": [
            {"chemin": "02-INFORMATIQUE/14-Web", "priorite": 3,
             "mots_cles": ["html", "css", "JavaScript"]},
            {"chemin": "02-INFORMATIQUE/14-Web-Frontend", "priorite": 5,
             "mots_cles": ["frontend", "javascript"]},  # collision target
        ],
    }
    renamings = [{"old_path": "02-INFORMATIQUE/14-Web",
                  "new_path": "02-INFORMATIQUE/14-Web-Frontend"}]
    new_cats, log = _cascade_categories_changes(
        current, renamings=renamings, fusions=[], deletions=[])

    # Une seule entry restante (les deux ont fusionné)
    chemins = [e["chemin"] for e in new_cats["informatique"]]
    self.assertEqual(chemins.count("02-INFORMATIQUE/14-Web-Frontend"), 1)
    self.assertNotIn("02-INFORMATIQUE/14-Web", chemins)

    merged = next(e for e in new_cats["informatique"]
                  if e["chemin"] == "02-INFORMATIQUE/14-Web-Frontend")
    # mots_cles : dedup case-insensitive (javascript == JavaScript)
    self.assertEqual(
        sorted([k.lower() for k in merged["mots_cles"]]),
        sorted(["html", "css", "javascript", "frontend"]),
    )
    # priorite = min(3, 5) = 3
    self.assertEqual(merged["priorite"], 3)
    # Log mentionne la collision
    rename_log = next(le for le in log if le["type"] == "rename")
    self.assertEqual(rename_log.get("n_collisions", 0), 1)
```

- [ ] **Step 2 : Run test to verify it fails**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_categories.TestCascadeCategories.test_cascade_renames_collision_merges_mots_cles -v
```

Expected : FAIL — duplicate entries dans la liste.

- [ ] **Step 3 : Implement collision handling**

Ajouter en bas de `_cascade_categories_changes`, après le traitement des renames (avant le `return`) :

```python
    # Détection de collisions après rename : si 2 entries du même groupe ont
    # le même chemin, fusionner (dedup mots_cles case-insensitive + min
    # priorite). Log la collision.
    n_collisions_by_target: dict[str, int] = {}
    for groupe, entries in new_categories.items():
        by_chemin: dict[str, list[dict]] = {}
        for entry in entries:
            by_chemin.setdefault(entry.get("chemin", ""), []).append(entry)
        merged_entries: list[dict] = []
        for chemin, group in by_chemin.items():
            if len(group) == 1:
                merged_entries.append(group[0])
                continue
            # Collision : merge
            n_collisions_by_target[chemin] = (
                n_collisions_by_target.get(chemin, 0) + len(group) - 1
            )
            seen: dict[str, str] = {}  # lower → original
            for e in group:
                for k in e.get("mots_cles", []) or []:
                    if isinstance(k, str) and k.lower() not in seen:
                        seen[k.lower()] = k
            merged = {
                "chemin": chemin,
                "priorite": min(int(e.get("priorite", 99)) for e in group),
                "mots_cles": list(seen.values()),
            }
            merged_entries.append(merged)
        new_categories[groupe] = merged_entries

    # Enrichir le log : annoter n_collisions sur les entries rename
    for le in log:
        if le["type"] == "rename" and le["new"] in n_collisions_by_target:
            le["n_collisions"] = n_collisions_by_target[le["new"]]
```

- [ ] **Step 4 : Run cascade tests**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_categories.TestCascadeCategories -v
```

Expected : 3 tests OK.

- [ ] **Step 5 : Commit**

```bash
git add tests/auto/test_agent_refonte_categories.py agents/refonte/proposition_tools.py
git commit -m "feat(refonte-B): cascade handles rename collisions

When the new_path already has an entry in categories.yaml, merge:
case-insensitive dedup of mots_cles + keep the highest priority
(numerically lowest). Log n_collisions on the rename entry."
```

---

## Task 5 : Cascade — fusion (sources mergent dans target)

**Files :**

- Modify : `agents/refonte/proposition_tools.py` (ajouter fusion handling)
- Modify : `tests/auto/test_agent_refonte_categories.py` (ajouter 2 tests)

- [ ] **Step 1 : Write the failing tests**

```python
def test_cascade_fusion_sources_merge_into_target(self):
    current = {
        "bureautique": [
            {"chemin": "09-BUREAU/Excel", "priorite": 5,
             "mots_cles": ["excel", "xlsx"]},
            {"chemin": "09-BUREAU/Microsoft-Excel", "priorite": 3,
             "mots_cles": ["microsoft excel"]},
        ],
    }
    fusions = [{"sources": ["09-BUREAU/Excel"],
                "target": "09-BUREAU/Microsoft-Excel"}]
    new_cats, log = _cascade_categories_changes(
        current, renamings=[], fusions=fusions, deletions=[])

    chemins = [e["chemin"] for e in new_cats["bureautique"]]
    self.assertEqual(chemins, ["09-BUREAU/Microsoft-Excel"])
    merged = new_cats["bureautique"][0]
    self.assertEqual(
        sorted([k.lower() for k in merged["mots_cles"]]),
        sorted(["microsoft excel", "excel", "xlsx"]),
    )
    self.assertEqual(merged["priorite"], 3)
    fusion_log = next(le for le in log if le["type"] == "fusion")
    self.assertEqual(fusion_log["new"], "09-BUREAU/Microsoft-Excel")

def test_cascade_fusion_target_does_not_preexist(self):
    # Target absent → fusion crée l'entry à partir des sources
    current = {
        "bureautique": [
            {"chemin": "09-BUREAU/Excel", "priorite": 5,
             "mots_cles": ["excel"]},
            {"chemin": "09-BUREAU/Calc", "priorite": 6,
             "mots_cles": ["libreoffice calc"]},
        ],
    }
    fusions = [{"sources": ["09-BUREAU/Excel", "09-BUREAU/Calc"],
                "target": "09-BUREAU/Tableurs"}]
    new_cats, log = _cascade_categories_changes(
        current, renamings=[], fusions=fusions, deletions=[])
    chemins = [e["chemin"] for e in new_cats["bureautique"]]
    self.assertEqual(chemins, ["09-BUREAU/Tableurs"])
    merged = new_cats["bureautique"][0]
    self.assertEqual(merged["priorite"], 5)  # min(5, 6)
    self.assertIn("excel", [k.lower() for k in merged["mots_cles"]])
    self.assertIn("libreoffice calc", [k.lower() for k in merged["mots_cles"]])
```

- [ ] **Step 2 : Run tests to verify they fail**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_categories.TestCascadeCategories.test_cascade_fusion_sources_merge_into_target tests.auto.test_agent_refonte_categories.TestCascadeCategories.test_cascade_fusion_target_does_not_preexist -v
```

Expected : FAIL — fusions ignorées.

- [ ] **Step 3 : Implement fusion handling**

Au début de `_cascade_categories_changes`, après la copie défensive, AVANT le bloc renames, ajouter :

```python
    # Apply fusions FIRST : transformer chaque source en target dans les
    # chemins, le mécanisme de collision merge ensuite naturellement.
    fusion_map: dict[str, str] = {}
    for f in fusions:
        for src in f["sources"]:
            fusion_map[src] = f["target"]

    if fusion_map:
        # Détermine quel groupe doit recevoir la nouvelle entry target
        # (utilise _groupe_from_path_prefix sur le target). Si toutes les
        # sources sont dans le même groupe, prendre ce groupe-là.
        for groupe, entries in new_categories.items():
            for entry in entries:
                chemin = entry.get("chemin", "")
                if chemin in fusion_map:
                    entry["chemin"] = fusion_map[chemin]
```

Et après le bloc collisions, ajouter le log fusion :

```python
    # Log fusions
    for f in fusions:
        n = sum(
            1
            for entries in new_categories.values()
            for e in entries
            if e.get("chemin") == f["target"]
        )
        if n > 0:
            log.append({
                "type": "fusion",
                "old": f["sources"],
                "new": f["target"],
                "n_entries": n,
            })
```

- [ ] **Step 4 : Run cascade tests**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_categories.TestCascadeCategories -v
```

Expected : 5 tests OK.

- [ ] **Step 5 : Commit**

```bash
git add tests/auto/test_agent_refonte_categories.py agents/refonte/proposition_tools.py
git commit -m "feat(refonte-B): cascade applies fusions

Fusion sources are remapped to the target chemin, then the existing
collision-merge step naturally dedups mots_cles and picks min priorite.
Works whether or not the target pre-exists."
```

---

## Task 6 : Cascade — deletion + idempotence

**Files :**

- Modify : `agents/refonte/proposition_tools.py` (ajouter deletion handling)
- Modify : `tests/auto/test_agent_refonte_categories.py` (ajouter 2 tests)

- [ ] **Step 1 : Write the failing tests**

```python
def test_cascade_deletion_drops_entries(self):
    current = {
        "informatique": [
            {"chemin": "02-INFORMATIQUE/14-Web", "priorite": 3,
             "mots_cles": ["html"]},
            {"chemin": "02-INFORMATIQUE/05-IA-ML", "priorite": 2,
             "mots_cles": ["ml"]},
        ],
    }
    deletions = [{"path": "02-INFORMATIQUE/14-Web"}]
    new_cats, log = _cascade_categories_changes(
        current, renamings=[], fusions=[], deletions=deletions)

    chemins = [e["chemin"] for e in new_cats["informatique"]]
    self.assertEqual(chemins, ["02-INFORMATIQUE/05-IA-ML"])
    del_log = next(le for le in log if le["type"] == "deletion")
    self.assertEqual(del_log["old"], "02-INFORMATIQUE/14-Web")
    self.assertEqual(del_log["n_entries"], 1)

def test_cascade_no_changes_no_modifications(self):
    # Aucun rename/fusion/deletion → categories inchangée + log vide
    current = {
        "informatique": [
            {"chemin": "02-INFORMATIQUE/14-Web", "priorite": 3,
             "mots_cles": ["html"]},
        ],
    }
    new_cats, log = _cascade_categories_changes(
        current, renamings=[], fusions=[], deletions=[])
    self.assertEqual(new_cats, current)
    self.assertEqual(log, [])
```

- [ ] **Step 2 : Run tests to verify they fail**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_categories.TestCascadeCategories.test_cascade_deletion_drops_entries tests.auto.test_agent_refonte_categories.TestCascadeCategories.test_cascade_no_changes_no_modifications -v
```

Expected : `test_cascade_deletion_drops_entries` FAIL (entry pas supprimée), `test_cascade_no_changes_no_modifications` OK probable.

- [ ] **Step 3 : Implement deletion handling**

À la fin de `_cascade_categories_changes`, AVANT le `return`, ajouter :

```python
    # Apply deletions : drop entries dont le chemin est dans la liste
    deletion_set = {d["path"] for d in deletions}
    if deletion_set:
        for groupe in list(new_categories.keys()):
            before = new_categories[groupe]
            after = [e for e in before if e.get("chemin") not in deletion_set]
            new_categories[groupe] = after

        for d in deletions:
            n = sum(
                1
                for entries in current_categories.values()
                for e in entries
                if e.get("chemin") == d["path"]
            )
            if n > 0:
                log.append({
                    "type": "deletion",
                    "old": d["path"],
                    "n_entries": n,
                })
```

- [ ] **Step 4 : Run all cascade tests**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_categories.TestCascadeCategories -v
```

Expected : 7 tests OK.

- [ ] **Step 5 : Commit**

```bash
git add tests/auto/test_agent_refonte_categories.py agents/refonte/proposition_tools.py
git commit -m "feat(refonte-B): cascade drops deleted entries + idempotence proven

Last step of the deterministic cascade. The no-changes test ensures
the function returns the original structure (plus an empty log) when
no renames/fusions/deletions are passed."
```

---

## Task 7 : LLM Pydantic schemas + zero-creation skip

**Files :**

- Create : `agents/refonte/categories_llm.py`
- Modify : `tests/auto/test_agent_refonte_categories.py` (ajouter classe `TestCategoriesLLM`)

- [ ] **Step 1 : Write the failing tests**

Ajouter à `tests/auto/test_agent_refonte_categories.py` :

```python
class TestCategoriesLLM(unittest.TestCase):

    def test_propose_keywords_zero_creations_no_call(self):
        from agents.refonte.categories_llm import propose_keywords_for_new_folders
        mock_llm = mock.MagicMock()
        result = propose_keywords_for_new_folders(
            llm=mock_llm,
            creations=[],
            existing_groupes=["informatique"],
            groupe_inference={},
            sample_entries={},
        )
        self.assertEqual(result, [])
        mock_llm.with_structured_output.assert_not_called()

    def test_propose_keywords_pydantic_min_max_mots_cles(self):
        from agents.refonte.categories_llm import _NewCategoryEntry
        # min 3 mots_cles
        with self.assertRaises(Exception):
            _NewCategoryEntry(chemin="X/Y", groupe="informatique",
                              priorite=5, mots_cles=["a", "b"])
        # max 15 mots_cles
        with self.assertRaises(Exception):
            _NewCategoryEntry(chemin="X/Y", groupe="informatique",
                              priorite=5, mots_cles=[f"k{i}" for i in range(16)])
        # OK in range
        entry = _NewCategoryEntry(chemin="X/Y", groupe="informatique",
                                  priorite=5, mots_cles=["a", "b", "c"])
        self.assertEqual(len(entry.mots_cles), 3)
```

- [ ] **Step 2 : Run tests to verify they fail**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_categories.TestCategoriesLLM -v
```

Expected : `ModuleNotFoundError: No module named 'agents.refonte.categories_llm'`

- [ ] **Step 3 : Create the module**

Créer `agents/refonte/categories_llm.py` :

```python
"""Module dédié au prompt + parsing du LLM pour proposer les mots-clés
des nouveaux folders dans categories.yaml (Phase B step 3).

Le LLM reçoit un contexte focalisé : la liste des nouveaux folders avec
leur rationale + 2-3 exemples d'entries existantes par groupe.

Sortie validée par Pydantic (structured output via LangChain).
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field, field_validator

log = logging.getLogger(__name__)


class _NewCategoryEntry(BaseModel):
    chemin: str
    groupe: str
    priorite: int = Field(ge=1, le=99, default=5)
    mots_cles: list[str] = Field(min_length=3, max_length=15)


class _NewCategoriesProposal(BaseModel):
    entries: list[_NewCategoryEntry]


def propose_keywords_for_new_folders(
    llm,
    creations: list[dict],
    existing_groupes: list[str],
    groupe_inference: dict[str, str],
    sample_entries: dict[str, list[dict]],
) -> list[dict]:
    """Appelle le LLM 1 fois avec un contexte focalisé pour proposer les
    mots-clés des nouveaux folders. Skip l'appel si `creations` est vide.

    Retourne une liste de dicts au format categories.yaml :
        [{chemin, groupe, priorite, mots_cles}, ...]
    """
    if not creations:
        return []
    # Reste implémenté dans les tâches suivantes.
    raise NotImplementedError("LLM call not implemented yet")
```

- [ ] **Step 4 : Run tests to verify they pass**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_categories.TestCategoriesLLM.test_propose_keywords_zero_creations_no_call tests.auto.test_agent_refonte_categories.TestCategoriesLLM.test_propose_keywords_pydantic_min_max_mots_cles -v
```

Expected : 2 tests OK.

- [ ] **Step 5 : Commit**

```bash
git add tests/auto/test_agent_refonte_categories.py agents/refonte/categories_llm.py
git commit -m "feat(refonte-B): categories_llm module skeleton + Pydantic schemas

Defines _NewCategoryEntry (3-15 mots_cles, priorite 1-99) and
_NewCategoriesProposal. propose_keywords_for_new_folders short-circuits
when creations is empty (no LLM call). Real LLM invocation comes next."
```

---

## Task 8 : LLM call avec structured output + validation groupe / chemin

**Files :**

- Modify : `agents/refonte/categories_llm.py` (implémenter l'appel LLM réel)
- Modify : `tests/auto/test_agent_refonte_categories.py` (ajouter 2 tests)

- [ ] **Step 1 : Write the failing tests**

```python
def test_propose_keywords_pydantic_groupe_validation(self):
    """Le LLM retourne un groupe inconnu → validation Pydantic refuse."""
    from agents.refonte.categories_llm import (
        _NewCategoriesProposal, propose_keywords_for_new_folders,
    )
    # Mock LLM retourne un groupe non listé dans existing_groupes
    fake_proposal = _NewCategoriesProposal(entries=[
        _new_category_entry_lax(chemin="02-INFO/RAG", groupe="INVALID",
                                priorite=5, mots_cles=["a", "b", "c"]),
    ])
    mock_llm = mock.MagicMock()
    mock_llm.with_structured_output.return_value.invoke.return_value = fake_proposal

    result = propose_keywords_for_new_folders(
        llm=mock_llm,
        creations=[{"path": "02-INFO/RAG", "rationale": "test"}],
        existing_groupes=["informatique", "sciences"],
        groupe_inference={"02-INFO/RAG": "informatique"},
        sample_entries={"informatique": []},
    )
    # Groupe invalide → fallback entry vide
    self.assertEqual(len(result), 1)
    self.assertEqual(result[0]["mots_cles"], [])
    self.assertEqual(result[0]["chemin"], "02-INFO/RAG")
    # Fallback utilise le groupe inféré, pas celui du LLM
    self.assertEqual(result[0]["groupe"], "informatique")

def test_propose_keywords_pydantic_chemin_mismatch_rejected(self):
    """Le LLM retourne un chemin différent des créations → refus."""
    from agents.refonte.categories_llm import (
        _NewCategoriesProposal, propose_keywords_for_new_folders,
    )
    fake_proposal = _NewCategoriesProposal(entries=[
        _new_category_entry_lax(chemin="02-INFO/HALLUCINATED",
                                groupe="informatique", priorite=5,
                                mots_cles=["a", "b", "c"]),
    ])
    mock_llm = mock.MagicMock()
    mock_llm.with_structured_output.return_value.invoke.return_value = fake_proposal

    result = propose_keywords_for_new_folders(
        llm=mock_llm,
        creations=[{"path": "02-INFO/RAG", "rationale": "test"}],
        existing_groupes=["informatique"],
        groupe_inference={"02-INFO/RAG": "informatique"},
        sample_entries={"informatique": []},
    )
    # Le LLM a renvoyé HALLUCINATED, la création était RAG → fallback
    self.assertEqual(len(result), 1)
    self.assertEqual(result[0]["chemin"], "02-INFO/RAG")
    self.assertEqual(result[0]["mots_cles"], [])
```

Helper en haut du fichier de test :

```python
def _new_category_entry_lax(**kwargs):
    """Construit un _NewCategoryEntry sans la validation 'groupe' /
    'chemin' (utile pour simuler le retour d'un LLM qui hallucinerait)."""
    from agents.refonte.categories_llm import _NewCategoryEntry
    return _NewCategoryEntry.model_construct(**kwargs)
```

- [ ] **Step 2 : Run tests to verify they fail**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_categories.TestCategoriesLLM.test_propose_keywords_pydantic_groupe_validation tests.auto.test_agent_refonte_categories.TestCategoriesLLM.test_propose_keywords_pydantic_chemin_mismatch_rejected -v
```

Expected : FAIL — `NotImplementedError`.

- [ ] **Step 3 : Implement the LLM call + post-validation**

Remplacer `raise NotImplementedError` dans `propose_keywords_for_new_folders` par :

```python
    from langchain_core.messages import HumanMessage, SystemMessage

    valid_paths = {c["path"] for c in creations}
    valid_groupes = set(existing_groupes) | {"autres"}

    def _fallback_entries() -> list[dict]:
        return [
            {
                "chemin": c["path"],
                "groupe": groupe_inference.get(c["path"], "autres"),
                "priorite": 99,
                "mots_cles": [],
            }
            for c in creations
        ]

    system_prompt = (
        "Tu génères des entries pour `categories.yaml` de Klodo (outil de "
        "classification PDF). Pour chaque nouveau folder dans la liste, "
        "propose 3 à 15 mots-clés représentatifs et une priorité (1=haute, "
        "99=basse). Le groupe est déjà inféré, garde-le. Le chemin doit "
        "rester strictement identique."
    )
    user_lines = []
    user_lines.append("Nouveaux folders à enrichir :\n")
    for c in creations:
        groupe = groupe_inference.get(c["path"], "autres")
        user_lines.append(f"- {c['path']} (groupe : {groupe})")
        user_lines.append(f"  rationale : {c.get('rationale', '')}")
    user_lines.append("\nExemples d'entries existantes (1-shot) :")
    for groupe, samples in sample_entries.items():
        for s in samples[:2]:
            user_lines.append(
                f"- groupe={groupe} chemin={s.get('chemin', '')} "
                f"priorite={s.get('priorite', 5)} "
                f"mots_cles={s.get('mots_cles', [])[:5]}"
            )

    structured = llm.with_structured_output(_NewCategoriesProposal)
    try:
        result = structured.invoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content="\n".join(user_lines)),
        ])
    except Exception as exc:
        log.warning("LLM call failed for categories proposal: %s", exc)
        return _fallback_entries()

    # Post-validation : drop entries hallucinées
    out: list[dict] = []
    seen_paths: set[str] = set()
    for entry in result.entries:
        if entry.chemin not in valid_paths:
            log.warning("LLM hallucinated chemin %r — skipping", entry.chemin)
            continue
        if entry.groupe not in valid_groupes:
            log.warning("LLM hallucinated groupe %r — skipping", entry.groupe)
            continue
        out.append({
            "chemin": entry.chemin,
            "groupe": entry.groupe,
            "priorite": entry.priorite,
            "mots_cles": list(entry.mots_cles),
        })
        seen_paths.add(entry.chemin)

    # Compléter avec fallback pour les créations non couvertes
    for c in creations:
        if c["path"] not in seen_paths:
            out.append({
                "chemin": c["path"],
                "groupe": groupe_inference.get(c["path"], "autres"),
                "priorite": 99,
                "mots_cles": [],
            })
    return out
```

- [ ] **Step 4 : Run tests**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_categories.TestCategoriesLLM -v
```

Expected : 4 tests OK.

- [ ] **Step 5 : Commit**

```bash
git add tests/auto/test_agent_refonte_categories.py agents/refonte/categories_llm.py
git commit -m "feat(refonte-B): LLM call for new folders + groupe/chemin validation

Real implementation of propose_keywords_for_new_folders. LangChain
structured output via Pydantic. Hallucinated entries (unknown groupe
or chemin not in creations) are dropped and replaced by empty fallback
entries so each creation gets one row in the output."
```

---

## Task 9 : LLM error fallback + retry on validation fail

**Files :**

- Modify : `agents/refonte/categories_llm.py` (retry sur validation fail)
- Modify : `tests/auto/test_agent_refonte_categories.py` (ajouter 2 tests)

- [ ] **Step 1 : Write the failing tests**

```python
def test_propose_keywords_llm_error_fallback(self):
    """LLM raise une exception → entries vides retournées."""
    from agents.refonte.categories_llm import propose_keywords_for_new_folders
    mock_llm = mock.MagicMock()
    mock_llm.with_structured_output.return_value.invoke.side_effect = \
        RuntimeError("rate limit")
    result = propose_keywords_for_new_folders(
        llm=mock_llm,
        creations=[{"path": "X/Y", "rationale": "test"}],
        existing_groupes=["informatique"],
        groupe_inference={"X/Y": "informatique"},
        sample_entries={"informatique": []},
    )
    self.assertEqual(len(result), 1)
    self.assertEqual(result[0]["mots_cles"], [])
    self.assertEqual(result[0]["priorite"], 99)
    self.assertEqual(result[0]["groupe"], "informatique")

def test_propose_keywords_retry_on_validation_fail(self):
    """Premier call retourne du n'importe quoi (1 entry hallucinée),
    retry réussit avec une entry valide."""
    from agents.refonte.categories_llm import (
        _NewCategoriesProposal, propose_keywords_for_new_folders,
    )
    bad = _NewCategoriesProposal(entries=[
        _new_category_entry_lax(chemin="WRONG/PATH", groupe="informatique",
                                priorite=5, mots_cles=["a", "b", "c"]),
    ])
    good = _NewCategoriesProposal(entries=[
        _new_category_entry_lax(chemin="X/Y", groupe="informatique",
                                priorite=5,
                                mots_cles=["alpha", "beta", "gamma"]),
    ])
    mock_llm = mock.MagicMock()
    mock_llm.with_structured_output.return_value.invoke.side_effect = [bad, good]

    result = propose_keywords_for_new_folders(
        llm=mock_llm,
        creations=[{"path": "X/Y", "rationale": "test"}],
        existing_groupes=["informatique"],
        groupe_inference={"X/Y": "informatique"},
        sample_entries={"informatique": []},
    )
    # 2 calls effectués (1er hallucination + retry)
    self.assertEqual(
        mock_llm.with_structured_output.return_value.invoke.call_count, 2)
    self.assertEqual(result[0]["chemin"], "X/Y")
    self.assertEqual(result[0]["mots_cles"], ["alpha", "beta", "gamma"])
```

- [ ] **Step 2 : Run tests to verify they fail**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_categories.TestCategoriesLLM.test_propose_keywords_llm_error_fallback tests.auto.test_agent_refonte_categories.TestCategoriesLLM.test_propose_keywords_retry_on_validation_fail -v
```

Expected : `test_propose_keywords_llm_error_fallback` peut PASS (déjà géré par le `try/except`), `test_propose_keywords_retry_on_validation_fail` FAIL (1 call seulement).

- [ ] **Step 3 : Add retry logic**

Dans `agents/refonte/categories_llm.py`, refactorer le bloc d'appel LLM pour boucler 1× sur validation fail :

```python
    structured = llm.with_structured_output(_NewCategoriesProposal)
    result = None
    last_error: str = ""
    for attempt in range(2):  # 1 try + 1 retry
        extra_user = ""
        if attempt == 1 and last_error:
            extra_user = (
                f"\n\nLE PRÉCÉDENT ESSAI A ÉCHOUÉ : {last_error}. "
                "Reprends en t'assurant que chaque `chemin` figure EXACTEMENT "
                "dans la liste fournie et que `groupe` est dans la liste autorisée."
            )
        try:
            result = structured.invoke([
                SystemMessage(content=system_prompt),
                HumanMessage(content="\n".join(user_lines) + extra_user),
            ])
        except Exception as exc:
            log.warning("LLM call failed for categories proposal: %s", exc)
            return _fallback_entries()

        # Vérifie la validité avant de quitter la boucle
        invalid = [
            e for e in result.entries
            if e.chemin not in valid_paths or e.groupe not in valid_groupes
        ]
        if not invalid:
            break
        last_error = (
            f"{len(invalid)} entries hallucinées (chemin ou groupe invalide)"
        )
        result = None  # force retry

    if result is None:
        return _fallback_entries()
```

Le reste du code (post-validation + complétion fallback) reste inchangé.

- [ ] **Step 4 : Run all LLM tests**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_categories.TestCategoriesLLM -v
```

Expected : 6 tests OK.

- [ ] **Step 5 : Commit**

```bash
git add tests/auto/test_agent_refonte_categories.py agents/refonte/categories_llm.py
git commit -m "feat(refonte-B): retry LLM call once on validation fail + fallback

When the LLM returns hallucinated chemins or groupes, the call is
retried once with an explicit error message. If retry also fails or
the call raises (rate limit, network), all creations get empty
fallback entries — Phase B doesn't crash."
```

---

## Task 10 : Merge cascades + LLM entries

**Files :**

- Modify : `agents/refonte/proposition_tools.py` (ajouter `_merge_categories_changes`)
- Modify : `tests/auto/test_agent_refonte_categories.py` (ajouter test)

- [ ] **Step 1 : Write the failing test**

```python
class TestMergeCategoriesChanges(unittest.TestCase):

    def test_merge_categories_intermediate_plus_new(self):
        from agents.refonte.proposition_tools import _merge_categories_changes
        intermediate = {
            "informatique": [
                {"chemin": "02-INFO/Web", "priorite": 3,
                 "mots_cles": ["html"]},
            ],
            "sciences": [],
        }
        new_entries = [
            {"chemin": "02-INFO/RAG", "groupe": "informatique",
             "priorite": 5, "mots_cles": ["retrieval", "embedding", "vector"]},
            {"chemin": "01-SCIENCES/CHIMIE/Materiaux", "groupe": "sciences",
             "priorite": 6, "mots_cles": ["alloy", "polymer", "ceramic"]},
        ]
        merged = _merge_categories_changes(intermediate, new_entries)
        chemins_info = [e["chemin"] for e in merged["informatique"]]
        chemins_sci = [e["chemin"] for e in merged["sciences"]]
        self.assertIn("02-INFO/Web", chemins_info)
        self.assertIn("02-INFO/RAG", chemins_info)
        self.assertIn("01-SCIENCES/CHIMIE/Materiaux", chemins_sci)
```

- [ ] **Step 2 : Run test to verify it fails**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_categories.TestMergeCategoriesChanges -v
```

Expected : `ImportError: cannot import name '_merge_categories_changes'`

- [ ] **Step 3 : Implement merger**

Ajouter à `agents/refonte/proposition_tools.py`, après `_cascade_categories_changes` :

```python
def _merge_categories_changes(
    intermediate: dict[str, list[dict]],
    new_entries: list[dict],
) -> dict[str, list[dict]]:
    """Combine la structure post-cascade avec les entries proposées par
    le LLM. Les nouvelles entries sont ajoutées dans leur groupe (créé si
    absent). Les champs `groupe` des new_entries sont consommés (le groupe
    est utilisé comme clé, pas conservé dans l'entry).
    """
    merged: dict[str, list[dict]] = {
        g: [dict(e) for e in entries] for g, entries in intermediate.items()
    }
    for entry in new_entries:
        groupe = entry.get("groupe", "autres")
        merged.setdefault(groupe, []).append({
            "chemin": entry["chemin"],
            "priorite": int(entry.get("priorite", 5)),
            "mots_cles": list(entry.get("mots_cles", [])),
        })
    return merged
```

- [ ] **Step 4 : Run test to verify it passes**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_categories.TestMergeCategoriesChanges -v
```

Expected : 1 test OK.

- [ ] **Step 5 : Commit**

```bash
git add tests/auto/test_agent_refonte_categories.py agents/refonte/proposition_tools.py
git commit -m "feat(refonte-B): _merge_categories_changes combines cascade + LLM

Combines the deterministic-cascade output with the LLM-proposed entries.
The 'groupe' field on new_entries is used as the destination key and not
serialized in the final YAML structure."
```

---

## Task 11 : Rationale markdown — section CATÉGORIES

**Files :**

- Modify : `agents/refonte/proposition_tools.py` (étendre `_render_rationale_markdown`)
- Modify : `tests/auto/test_agent_refonte_categories.py` (ajouter test)

- [ ] **Step 1 : Write the failing test**

```python
class TestRenderRationale(unittest.TestCase):

    def test_render_rationale_categories_section(self):
        from agents.refonte.proposition_tools import _render_categories_section
        cascade_log = [
            {"type": "rename", "old": "02-INFO/Web",
             "new": "02-INFO/Web-Frontend", "n_entries": 1, "n_collisions": 0},
            {"type": "fusion", "old": ["09-BUREAU/Excel"],
             "new": "09-BUREAU/Microsoft-Excel", "n_entries": 1},
            {"type": "deletion", "old": "02-INFO/Vieux", "n_entries": 1},
        ]
        new_entries = [
            {"chemin": "02-INFO/RAG", "groupe": "informatique",
             "priorite": 5, "mots_cles": ["retrieval", "embedding", "vector"]},
        ]
        md = _render_categories_section(cascade_log, new_entries)
        self.assertIn("## CATÉGORIES", md)
        self.assertIn("### Cascades automatiques", md)
        self.assertIn("rename", md)
        self.assertIn("02-INFO/Web", md)
        self.assertIn("02-INFO/Web-Frontend", md)
        self.assertIn("fusion", md)
        self.assertIn("deletion", md)
        self.assertIn("### Nouveaux folders", md)
        self.assertIn("02-INFO/RAG", md)
        self.assertIn("retrieval", md)
```

- [ ] **Step 2 : Run test to verify it fails**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_categories.TestRenderRationale -v
```

Expected : `ImportError: cannot import name '_render_categories_section'`

- [ ] **Step 3 : Implement render helper**

Ajouter à `agents/refonte/proposition_tools.py`, AVANT `_render_rationale_markdown` :

```python
def _render_categories_section(
    cascade_log: list[dict],
    new_entries: list[dict],
) -> str:
    """Render la section ## CATÉGORIES du rationale markdown."""
    if not cascade_log and not new_entries:
        return ""

    n_total = len(cascade_log) + len(new_entries)
    lines: list[str] = []
    lines.append(f"## CATÉGORIES ({n_total} entries modifiées)")
    lines.append("")

    if cascade_log:
        lines.append(f"### Cascades automatiques ({len(cascade_log)})")
        for entry in cascade_log:
            t = entry["type"]
            if t == "rename":
                extra = (f", {entry['n_collisions']} collision(s) mergée(s)"
                         if entry.get("n_collisions") else "")
                lines.append(
                    f"- **rename** : `{entry['old']}` → `{entry['new']}` "
                    f"({entry['n_entries']} entry remappée{extra})"
                )
            elif t == "rename_prefix":
                lines.append(
                    f"- **rename par préfixe** : `{entry['old']}/*` → "
                    f"`{entry['new']}/*` (entry : `{entry.get('chemin_renamed', '')}`)"
                )
            elif t == "fusion":
                srcs = " + ".join(f"`{s}`" for s in entry["old"])
                lines.append(
                    f"- **fusion** : {srcs} → `{entry['new']}` "
                    f"({entry['n_entries']} entry mergée, mots_cles dédupliqués)"
                )
            elif t == "deletion":
                lines.append(
                    f"- **deletion** : `{entry['old']}` "
                    f"({entry['n_entries']} entry supprimée)"
                )
        lines.append("")

    if new_entries:
        lines.append(f"### Nouveaux folders (mots-clés générés par LLM) ({len(new_entries)})")
        for entry in new_entries:
            chemin = entry["chemin"]
            groupe = entry["groupe"]
            priorite = entry["priorite"]
            mots = entry.get("mots_cles", [])
            if not mots:
                lines.append(
                    f"- `{chemin}` (groupe `{groupe}`, priorité {priorite}) "
                    "⚠ Mots-clés indisponibles (LLM) — à compléter manuellement"
                )
            else:
                lines.append(f"- `{chemin}` (groupe `{groupe}`, priorité {priorite})")
                lines.append(f"  - mots-clés : {', '.join(mots)}")
        lines.append("")

    return "\n".join(lines)
```

- [ ] **Step 4 : Run test**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_categories.TestRenderRationale -v
```

Expected : 1 test OK.

- [ ] **Step 5 : Commit**

```bash
git add tests/auto/test_agent_refonte_categories.py agents/refonte/proposition_tools.py
git commit -m "feat(refonte-B): markdown section for categories changes

_render_categories_section renders a CATÉGORIES section with two
sub-blocks: deterministic cascades (renames/rename_prefix/fusions/
deletions) and LLM-generated new folders. Returns '' when nothing to
report — caller can append unconditionally."
```

---

## Task 12 : `propose_changes` écrit `categories-proposed.yaml`

**Files :**

- Modify : `agents/refonte/proposition_tools.py` (intégrer steps 2-5 dans `propose_changes`)
- Modify : `tests/auto/test_agent_refonte_proposition.py` (ajouter test integration)

- [ ] **Step 1 : Write the failing test**

Ajouter à `tests/auto/test_agent_refonte_proposition.py` (à la fin, nouvelle classe) :

```python
class TestProposeChangesCategoriesIntegration(unittest.TestCase):
    """Tests d'intégration : propose_changes écrit categories-proposed.yaml."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.profiles_root = Path(self.tmp) / "profiles"
        self.profiles_root.mkdir(parents=True)
        target = Path(self.tmp) / "target"
        target.mkdir()
        _make_profile(
            self.profiles_root, "test_p",
            target=target,
            folders=["02-INFORMATIQUE/14-Web"],
            mapping={"Web Development": "02-INFORMATIQUE/14-Web"},
        )
        # Write categories.yaml
        (self.profiles_root / "test_p" / "categories.yaml").write_text(
            yaml.safe_dump({
                "informatique": [
                    {"chemin": "02-INFORMATIQUE/14-Web", "priorite": 3,
                     "mots_cles": ["html", "css"]},
                ],
            }, allow_unicode=True),
            encoding="utf-8",
        )
        # Patch chemins
        self.patcher_root = mock.patch(
            "dashboard.data.get_project_root",
            return_value=Path(self.tmp),
        )
        self.patcher_root.start()

    def tearDown(self):
        self.patcher_root.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_propose_changes_writes_categories_proposed_yaml(self):
        """Un rename est proposé → categories-proposed.yaml écrit avec
        l'entry remappée."""
        from agents.refonte.proposition_tools import propose_changes
        # Mock le LLM categories pour éviter l'appel réel
        with mock.patch(
            "agents.refonte.proposition_tools.propose_keywords_for_new_folders",
            return_value=[],
        ):
            result = propose_changes(
                profile="test_p",
                run_id="testrun-001",
                creations=[],
                renamings=[{
                    "old_path": "02-INFORMATIQUE/14-Web",
                    "new_path": "02-INFORMATIQUE/14-Web-Frontend",
                    "rationale": "clearer naming",
                }],
            )
        out_dir = self.profiles_root / "test_p" / ".cache" / "refonte" / "testrun-001" / "proposed"
        cat_path = out_dir / "categories-proposed.yaml"
        self.assertTrue(cat_path.exists())
        proposed = yaml.safe_load(cat_path.read_text(encoding="utf-8"))
        chemins = [e["chemin"] for e in proposed["informatique"]]
        self.assertIn("02-INFORMATIQUE/14-Web-Frontend", chemins)
        self.assertNotIn("02-INFORMATIQUE/14-Web", chemins)

    def test_propose_changes_no_categories_when_no_changes_apply(self):
        """Pas de renames/fusions/deletions/creations → pas de fichier écrit."""
        from agents.refonte.proposition_tools import propose_changes
        with mock.patch(
            "agents.refonte.proposition_tools.propose_keywords_for_new_folders",
            return_value=[],
        ):
            propose_changes(
                profile="test_p",
                run_id="testrun-002",
                mappings_added=[{
                    "theme": "Foo", "folder": "02-INFORMATIQUE/14-Web",
                    "rationale": "x",
                }],
            )
        cat_path = (self.profiles_root / "test_p" / ".cache" / "refonte"
                    / "testrun-002" / "proposed" / "categories-proposed.yaml")
        self.assertFalse(cat_path.exists())
```

- [ ] **Step 2 : Run tests to verify they fail**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_proposition.TestProposeChangesCategoriesIntegration -v
```

Expected : FAIL — `categories-proposed.yaml` n'est pas écrit.

- [ ] **Step 3 : Wire steps 2-5 into `propose_changes`**

Dans `agents/refonte/proposition_tools.py`, juste après le bloc d'écriture de `rationale_path` et `changes_json_path` (Task 1 du chemin Phase B existant), ajouter :

```python
    # ─── Steps 2-5 : categories.yaml proposition ──────────────────────────
    cat_path_prod = tax._profile_dir(profile) / "categories.yaml"
    has_cascade_change = bool(
        changes.renamings or changes.fusions or changes.deletions
    )
    has_creation_change = bool(changes.creations)

    if cat_path_prod.exists() and (has_cascade_change or has_creation_change):
        try:
            current_cats = yaml.safe_load(
                cat_path_prod.read_text(encoding="utf-8")
            ) or {}
        except yaml.YAMLError:
            current_cats = {}

        # Step 2 : cascade déterministe
        new_cats, cascade_log = _cascade_categories_changes(
            current_cats,
            renamings=[r.model_dump() for r in changes.renamings],
            fusions=[f.model_dump() for f in changes.fusions],
            deletions=[d.model_dump() for d in changes.deletions],
        )

        # Step 3 : LLM mots-clés pour créations
        new_llm_entries: list[dict] = []
        if changes.creations:
            from agents.llm import get_agent_llm
            from agents.refonte.categories_llm import (
                propose_keywords_for_new_folders,
            )
            llm = get_agent_llm()
            existing_groupes = list(current_cats.keys())
            groupe_inference = {
                c.path: _groupe_from_path_prefix(c.path, current_cats)
                for c in changes.creations
            }
            sample_entries = {
                g: current_cats[g][:2] for g in existing_groupes
            }
            new_llm_entries = propose_keywords_for_new_folders(
                llm=llm,
                creations=[c.model_dump() for c in changes.creations],
                existing_groupes=existing_groupes,
                groupe_inference=groupe_inference,
                sample_entries=sample_entries,
            )

        # Step 4 : merge + écriture
        merged_cats = _merge_categories_changes(new_cats, new_llm_entries)
        cat_proposed_path = out_dir / "categories-proposed.yaml"
        cat_proposed_path.write_text(
            yaml.safe_dump(merged_cats, allow_unicode=True, sort_keys=True),
            encoding="utf-8",
        )

        # Step 5 : extend rationale markdown
        section = _render_categories_section(cascade_log, new_llm_entries)
        if section:
            rationale_path.write_text(
                rationale_path.read_text(encoding="utf-8") + "\n" + section,
                encoding="utf-8",
            )
```

- [ ] **Step 4 : Run integration tests**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_proposition.TestProposeChangesCategoriesIntegration -v
```

Expected : 2 tests OK.

- [ ] **Step 5 : Commit**

```bash
git add tests/auto/test_agent_refonte_proposition.py agents/refonte/proposition_tools.py
git commit -m "feat(refonte-B): propose_changes writes categories-proposed.yaml

Steps 2-5 of the spec wired into propose_changes. Cascade runs first
(deterministic), then LLM for new-folder keywords (1 call, skipped if
no creations), then merge + write. Rationale markdown gains a
CATÉGORIES section. Skipped entirely when profile has no categories.yaml
or when no changes apply."
```

---

## Task 13 : `propose_changes` — profil sans `categories.yaml`

**Files :**

- Modify : `tests/auto/test_agent_refonte_proposition.py` (ajouter test)

- [ ] **Step 1 : Write the failing test**

```python
def test_propose_changes_no_categories_when_profile_has_none(self):
    """Profil sans categories.yaml → pas de fichier produit, pas de crash."""
    # Remove categories.yaml
    (self.profiles_root / "test_p" / "categories.yaml").unlink()

    from agents.refonte.proposition_tools import propose_changes
    with mock.patch(
        "agents.refonte.proposition_tools.propose_keywords_for_new_folders",
        return_value=[],
    ):
        result = propose_changes(
            profile="test_p",
            run_id="testrun-003",
            creations=[{
                "path": "02-INFORMATIQUE/05-IA-ML/RAG",
                "rationale": "RAG folder",
            }],
        )
    cat_path = (self.profiles_root / "test_p" / ".cache" / "refonte"
                / "testrun-003" / "proposed" / "categories-proposed.yaml")
    self.assertFalse(cat_path.exists())
    # propose_changes a quand même réussi (tree + mapping écrits)
    self.assertIn("tree_proposed_path", result)
```

- [ ] **Step 2 : Run test**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_proposition.TestProposeChangesCategoriesIntegration.test_propose_changes_no_categories_when_profile_has_none -v
```

Expected : devrait déjà passer grâce au `if cat_path_prod.exists()` ajouté en Task 12. Si FAIL, ajuster la guard.

- [ ] **Step 3 : Aucun changement nécessaire (sanity check existant)**

La guard `if cat_path_prod.exists() and (has_cascade_change or has_creation_change):` couvre déjà ce cas. Pas de code à ajouter.

- [ ] **Step 4 : Run test to verify it passes**

Idem step 2.

- [ ] **Step 5 : Commit**

```bash
git add tests/auto/test_agent_refonte_proposition.py
git commit -m "test(refonte-B): explicit coverage when profile lacks categories.yaml

The guard added in Task 12 (cat_path_prod.exists()) already handles
this case. This test pins down the behavior for regressions."
```

---

## Task 14 : Simulator utilise `categories-proposed.yaml` avec fallback

**Files :**

- Modify : `agents/refonte/simulator.py` (1 ligne + fallback)
- Modify : `tests/auto/test_agent_refonte_simulator.py` (ajouter 2 tests)

- [ ] **Step 1 : Write the failing tests**

Ajouter à `tests/auto/test_agent_refonte_simulator.py` :

```python
class TestSimulatorCategoriesIntegration(unittest.TestCase):
    """Le simulateur doit lire categories-proposed.yaml si présent, avec
    fallback sur le categories.yaml de prod (rétrocompat anciens runs)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.profiles_root = Path(self.tmp) / "profiles"
        self.profiles_root.mkdir(parents=True)
        target = Path(self.tmp) / "target"
        target.mkdir()
        # Fichier test à classifier
        (target / "alpha.pdf").write_bytes(b"%PDF-1.4\n")

        _make_profile(
            self.profiles_root, "test_sim",
            target=target,
            folders=["02-INFORMATIQUE/14-Web"],
            mapping={"Web Development": "02-INFORMATIQUE/14-Web"},
        )
        # Categories prod avec un keyword qui matche
        (self.profiles_root / "test_sim" / "categories.yaml").write_text(
            yaml.safe_dump({
                "informatique": [
                    {"chemin": "02-INFORMATIQUE/14-Web", "priorite": 3,
                     "mots_cles": ["alpha"]},
                ],
            }, allow_unicode=True),
            encoding="utf-8",
        )
        self.proposal_dir = (
            self.profiles_root / "test_sim" / ".cache" / "refonte"
            / "testrun-sim" / "proposed"
        )
        self.proposal_dir.mkdir(parents=True)
        # theme_mapping-proposed.yaml minimal
        (self.proposal_dir / "theme_mapping-proposed.yaml").write_text(
            yaml.safe_dump({}), encoding="utf-8",
        )

        self.patcher_root = mock.patch(
            "dashboard.data.get_project_root",
            return_value=Path(self.tmp),
        )
        self.patcher_root.start()

    def tearDown(self):
        self.patcher_root.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_simulator_uses_proposed_categories_when_present(self):
        """Si categories-proposed.yaml existe avec un chemin différent,
        le simulateur l'utilise."""
        (self.proposal_dir / "categories-proposed.yaml").write_text(
            yaml.safe_dump({
                "informatique": [
                    {"chemin": "02-INFORMATIQUE/14-Web-Frontend",
                     "priorite": 3, "mots_cles": ["alpha"]},
                ],
            }, allow_unicode=True),
            encoding="utf-8",
        )
        from agents.refonte.simulator import simulate_reclassify
        summary = simulate_reclassify("test_sim", self.proposal_dir)
        # Le top destination doit être le nouveau chemin
        top = summary.get("top_destinations") or []
        chemins = [d["folder"] for d in top]
        self.assertIn("02-INFORMATIQUE/14-Web-Frontend", chemins)

    def test_simulator_fallback_when_no_proposed_categories(self):
        """Si categories-proposed.yaml absent (ancien run), fallback prod."""
        # Pas de categories-proposed.yaml créé
        from agents.refonte.simulator import simulate_reclassify
        # Ne doit pas crash + utilise le categories.yaml de prod
        summary = simulate_reclassify("test_sim", self.proposal_dir)
        self.assertEqual(summary.get("n_files", -1), 1)
```

- [ ] **Step 2 : Run tests to verify they fail**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_simulator.TestSimulatorCategoriesIntegration -v
```

Expected : `test_simulator_uses_proposed_categories_when_present` FAIL (simulator utilise toujours prod). `test_simulator_fallback_when_no_proposed_categories` peut PASS (comportement actuel).

- [ ] **Step 3 : Modify simulator**

Dans `agents/refonte/simulator.py:simulate_reclassify`, remplacer le bloc qui charge le classifier (ligne ~93) :

```python
    cat_path = tax._profile_dir(profile) / "categories.yaml"
    classifier = load_keyword_classifier(str(cat_path)) if cat_path.exists() else None
```

par :

```python
    # Prefer categories-proposed.yaml (Phase B output) si présent. Fallback
    # sur categories.yaml de prod pour les anciens runs sans cet artefact.
    cat_path = proposal_dir / "categories-proposed.yaml"
    if not cat_path.exists():
        cat_path = tax._profile_dir(profile) / "categories.yaml"
    classifier = load_keyword_classifier(str(cat_path)) if cat_path.exists() else None
```

- [ ] **Step 4 : Run tests**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_simulator.TestSimulatorCategoriesIntegration -v
```

Expected : 2 tests OK.

- [ ] **Step 5 : Commit**

```bash
git add tests/auto/test_agent_refonte_simulator.py agents/refonte/simulator.py
git commit -m "feat(refonte-B): simulator reads categories-proposed.yaml with fallback

When categories-proposed.yaml is present in the proposal_dir, the
simulator's KeywordClassifier loads from that file — Impact reclassify
now respects the Phase B proposition. Fallback to prod categories.yaml
for older runs (no regression on previous Phase B runs)."
```

---

## Task 15 : End-to-end test Phase B avec categories

**Files :**

- Modify : `tests/auto/test_agent_refonte_proposition.py` (ajouter test E2E)

- [ ] **Step 1 : Write the failing test**

Ajouter à la fin de `TestProposeChangesCategoriesIntegration` :

```python
def test_phase_b_end_to_end_with_categories(self):
    """End-to-end : 1 rename + 1 création + 1 fusion + 1 deletion.
    Vérifie que les 4 artefacts proposed existent + rationale.md contient
    la section CATÉGORIES."""
    # Étendre categories.yaml avec entries supplémentaires
    (self.profiles_root / "test_p" / "categories.yaml").write_text(
        yaml.safe_dump({
            "informatique": [
                {"chemin": "02-INFORMATIQUE/14-Web", "priorite": 3,
                 "mots_cles": ["html"]},
                {"chemin": "02-INFORMATIQUE/Old-Tag", "priorite": 5,
                 "mots_cles": ["legacy"]},
            ],
            "bureautique": [
                {"chemin": "09-BUREAU/Excel", "priorite": 5,
                 "mots_cles": ["excel"]},
                {"chemin": "09-BUREAU/Microsoft-Excel", "priorite": 3,
                 "mots_cles": ["microsoft excel"]},
            ],
        }, allow_unicode=True),
        encoding="utf-8",
    )

    from agents.refonte.proposition_tools import propose_changes
    # Mock LLM categories : retourne 1 entry pour la création
    fake_llm_entries = [{
        "chemin": "02-INFORMATIQUE/05-IA-ML/RAG",
        "groupe": "informatique",
        "priorite": 5,
        "mots_cles": ["retrieval augmented", "RAG", "embedding"],
    }]
    with mock.patch(
        "agents.refonte.proposition_tools.propose_keywords_for_new_folders",
        return_value=fake_llm_entries,
    ):
        result = propose_changes(
            profile="test_p",
            run_id="testrun-e2e",
            creations=[{
                "path": "02-INFORMATIQUE/05-IA-ML/RAG",
                "rationale": "RAG retrieval",
            }],
            renamings=[{
                "old_path": "02-INFORMATIQUE/14-Web",
                "new_path": "02-INFORMATIQUE/14-Web-Frontend",
                "rationale": "clearer",
            }],
            fusions=[{
                "sources": ["09-BUREAU/Excel"],
                "target": "09-BUREAU/Microsoft-Excel",
                "rationale": "dedup",
            }],
            deletions=[{
                "path": "02-INFORMATIQUE/Old-Tag",
                "rationale": "unused",
            }],
        )

    out_dir = self.profiles_root / "test_p" / ".cache" / "refonte" / "testrun-e2e" / "proposed"
    # 4 artefacts existent
    self.assertTrue((out_dir / "tree-proposed.yaml").exists())
    self.assertTrue((out_dir / "theme_mapping-proposed.yaml").exists())
    self.assertTrue((out_dir / "categories-proposed.yaml").exists())
    self.assertTrue((out_dir / "refonte-rationale.md").exists())

    # categories-proposed.yaml reflète toutes les ops
    proposed = yaml.safe_load((out_dir / "categories-proposed.yaml").read_text(encoding="utf-8"))
    chemins = sorted(
        e["chemin"]
        for entries in proposed.values()
        for e in entries
    )
    self.assertIn("02-INFORMATIQUE/14-Web-Frontend", chemins)   # rename
    self.assertIn("02-INFORMATIQUE/05-IA-ML/RAG", chemins)      # creation
    self.assertIn("09-BUREAU/Microsoft-Excel", chemins)         # fusion target
    self.assertNotIn("02-INFORMATIQUE/14-Web", chemins)         # ancien rename
    self.assertNotIn("09-BUREAU/Excel", chemins)                # source fusion
    self.assertNotIn("02-INFORMATIQUE/Old-Tag", chemins)        # deletion

    # rationale.md contient la section
    rationale = (out_dir / "refonte-rationale.md").read_text(encoding="utf-8")
    self.assertIn("## CATÉGORIES", rationale)
    self.assertIn("rename", rationale)
    self.assertIn("fusion", rationale)
    self.assertIn("deletion", rationale)
    self.assertIn("02-INFORMATIQUE/05-IA-ML/RAG", rationale)
    self.assertIn("retrieval augmented", rationale)
```

- [ ] **Step 2 : Run test**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_proposition.TestProposeChangesCategoriesIntegration.test_phase_b_end_to_end_with_categories -v
```

Expected : OK (toutes les briques précédentes sont en place).

- [ ] **Step 3 : Verify whole suite passes**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_categories tests.auto.test_agent_refonte_proposition tests.auto.test_agent_refonte_simulator -v 2>&1 | tail -5
```

Expected : 23+ tests OK (17 unit + 6 integ ajoutés ce chantier + existants).

- [ ] **Step 4 : Verify ruff clean**

```bash
uv run ruff check agents/refonte/ tests/auto/test_agent_refonte_categories.py
```

Expected : `All checks passed!`

- [ ] **Step 5 : Commit + open PR**

```bash
git add tests/auto/test_agent_refonte_proposition.py
git commit -m "test(refonte-B): end-to-end Phase B with categories proposal

Covers all 4 mutation types (creation + rename + fusion + deletion) in
one run. Asserts: 4 proposed artifacts exist on disk, categories-proposed
.yaml reflects every op, rationale.md contains the CATÉGORIES section
with cascade entries and LLM-generated keywords."

git push -u origin feature/phase-b-categories-yaml
gh pr create --base develop \
  --title "feat(refonte-B): Phase B propose aussi categories.yaml" \
  --body "$(cat <<'EOF'
## Summary

Phase B (Agent Refonte) propose désormais aussi un `categories-proposed.yaml`. Le simulateur Impact reclassify utilise cet artefact (avec fallback sur prod pour anciens runs), corrigeant la divergence où les top destinations affichaient des folders de l'ancienne taxonomie.

Spec : [docs/superpowers/specs/2026-06-08-phase-b-categories-yaml-design.md](docs/superpowers/specs/2026-06-08-phase-b-categories-yaml-design.md)
Plan : [docs/superpowers/plans/2026-06-08-phase-b-categories-yaml.md](docs/superpowers/plans/2026-06-08-phase-b-categories-yaml.md)

## Architecture

6 étapes dans `propose_changes` (toutes additives, agent LangGraph inchangé) :
1. Tool LangGraph appelle propose_changes — INCHANGÉ
2. NEW : cascade déterministe (renames + fusions + deletions sur categories existantes)
3. NEW : 1 appel LLM pour mots-clés des nouveaux folders (Pydantic structured output, retry 1×, fallback gracieux)
4. NEW : merge cascade + LLM → categories-proposed.yaml
5. NEW : rationale.md gagne section ## CATÉGORIES
6. simulator.py : 1-ligne (lit categories-proposed avec fallback)

## Hors-scope

- Pas de bouton Appliquer en prod (artefacts seuls)
- Pas d'optimisation des entries existantes (sauf cascades)
- LLM ne ré-ordonne pas les priorités existantes

## Test plan

- [x] 23 nouveaux tests (15 unit cascades + LLM + render, 8 integ propose_changes + simulator + e2e)
- [x] LLM mocké partout (zero cost CI)
- [x] Rétrocompat : test simulator fallback quand pas de categories-proposed.yaml

## Critères de succès

1. ✅ categories-proposed.yaml produit dans .cache/refonte/<run_id>/proposed/
2. ✅ Section CATÉGORIES dans rationale markdown
3. ⏳ À valider sur run réel : top destinations Impact reclassify incluent les nouveaux folders
4. ⏳ À valider sur run réel : répartition par source CSV montre P1+P4 ≥ 50%

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review

**Spec coverage** :

| Spec requirement | Task |
|---|---|
| `_groupe_from_path_prefix` algorithme | Task 1 |
| `_cascade_categories_changes` — rename simple + prefix + collision | Tasks 2, 3, 4 |
| `_cascade_categories_changes` — fusion (target préexiste ou non) | Task 5 |
| `_cascade_categories_changes` — deletion + idempotence | Task 6 |
| `_NewCategoryEntry` + `_NewCategoriesProposal` Pydantic + min/max mots_cles | Task 7 |
| `propose_keywords_for_new_folders` — skip zero-creations + LLM real call + validation groupe/chemin | Tasks 7, 8 |
| `propose_keywords_for_new_folders` — fallback on LLM error + retry on validation fail | Task 9 |
| `_merge_categories_changes` | Task 10 |
| `_render_categories_section` (markdown) | Task 11 |
| `propose_changes` wire steps 2-5 + écrit categories-proposed.yaml | Task 12 |
| Skip si profil sans categories.yaml | Task 13 |
| Simulator lit categories-proposed avec fallback | Task 14 |
| End-to-end run avec 4 ops | Task 15 |

Toutes les requirements de la spec ont une task. ✓

**Placeholder scan** : Aucun "TBD", "TODO", "implement later". Tous les code blocks contiennent du code exécutable. ✓

**Type consistency** :

- `_cascade_categories_changes` retourne `tuple[dict, list[dict]]` partout (Tasks 2-6)
- `_merge_categories_changes` accepte la sortie de la cascade (Task 10)
- `_NewCategoryEntry` mêmes champs partout (Tasks 7, 8, 9)
- `propose_keywords_for_new_folders` signature stable (Tasks 7, 8, 9, 12, 15)
- Le format de `cascade_log` (avec champs `type`, `old`, `new`, `n_entries`, etc.) reste cohérent entre Tasks 2-6 et Task 11 (render)
- `categories-proposed.yaml` produit (Task 12) consommé par simulator (Task 14)
- Mock partout via `mock.patch("agents.refonte.proposition_tools.propose_keywords_for_new_folders", return_value=...)` (Tasks 12, 13, 15)

Toutes cohérentes. ✓
