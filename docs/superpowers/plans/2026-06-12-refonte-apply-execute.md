# Refonte Apply/Execute — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Appliquer une refonte Phase B depuis le dashboard : ① adopter la config proposée (promotion des YAML, backup, rollback) puis ② exécuter les déplacements physiques (journal de moves, skip+rapport, undo), avec preview/confirmation/lock.

**Architecture:** Nouveau module `dashboard/agent_refonte_apply.py` (état `apply/state.json` + `apply/status.json` par run, gating A→B, thread daemon + polling — patron `agent_refonte.py`). Nouveau `lib/move_journal.py` miroir de `rename_journal.py`. Sélection des moves dans `refonte_results.py` (pure). Spec : `docs/superpowers/specs/2026-06-12-refonte-apply-execute-design.md`.

**Tech Stack:** Python 3.13 / FastAPI / unittest (TestClient, mock de `dashboard.data.get_project_root`) / vanilla JS dans `agent_refonte_panel.html`.

**Branche:** `feature/refonte-apply-execute` (déjà créée, spec commitée).

**Conventions projet:** commits en anglais, doc/messages en français, `uv run python -m unittest …`, jamais de test sur le vrai SSD (tout en tmpdir).

---

## Contexte codebase (à lire avant Task 1)

- `profiles/<p>/.cache/refonte/<run_id>/` contient : `status.json` (`{status, phase, ...}`), `proposed/tree-proposed.yaml` (`{folders: [...]}`), `proposed/theme_mapping-proposed.yaml` (dict thème→dossier), `proposed/categories-proposed.yaml` (optionnel), `proposed/changes.json` (`{creations, fusions, renamings, deletions, mappings_added}`), `simulation/reclassify-projection.csv` (colonnes `rel_path, current_folder, proposed_folder, changed, source, top_theme, confidence, score`).
- **`rel_path` est le chemin complet relatif au target** (ex. `04-SHS/HIST/b.pdf`), `current_folder = dirname(rel_path)` (cf. `agents/refonte/simulator.py:32-43`). Donc `old = target/rel_path`, `new = target/proposed_folder/basename(rel_path)`.
- `dashboard/refonte_results.py` : `read_projection_rows(run_dir)`, `load_creations(run_dir)`, `enrich_row(row, creations)` (retourne `source_class`, `confidence` float, `risk`…), `_is_doubt(enriched)` = `not (source_class in ("p1_theme","p1_refined") and confidence >= 0.7)`.
- `agents/refonte/agent_backup.py` : `PROD_FILES = ("tree.yaml", "theme_mapping.yaml")` (ligne 32), `create_backup(profile, batch_id)` → nom du dossier snapshot, `restore_backup(profile, backup_dir_name)` (skippe les fichiers absents du snapshot). Racines via `dashboard.data.get_project_root()`.
- `dashboard/taxonomy.py` : `_lock_file(profile)` → `profiles/<p>/.cache/taxonomy.lock` (ligne 89), `_check_lock_free(profile)` lève `TaxonomyError(…, 423)` (ligne 1629), `_profile_target_path(profile)` lit `target:` de `profile.yaml` (ligne 417), `reset_cache(profile)` (ligne 564), `_locks` dict de threading.Lock (ligne 60).
- `lib/rename_journal.py` : modèle à mirrorer (JSONL `.cache/rename-journal.jsonl`, `append_rename`, `read_journal`, `list_batches`, `undo_record`, `undo_batch`).
- `dashboard/agent_refonte.py` : patron thread daemon + `status.json` + polling (lignes 70-117), `_run_dir(profile, run_id)`.
- Routes refonte dans `dashboard/app.py` lignes 2432-2832 (style : `JSONResponse`, erreurs `{"error": str}` + status code). Les nouvelles routes s'ajoutent après `/api/agent/refonte/proposition/{run_id}/doubt-files` (~ligne 2832).
- Tests HTTP : patron `tests/auto/test_agent_refonte_results_api.py` (tmpdir + `mock.patch("dashboard.data.get_project_root")` + `TestClient(app)`).
- UI : `dashboard/templates/partials/agent_refonte_panel.html` (~2021 lignes, JS inline). `renderPhaseBReport(runId, status)` ligne 922 construit les 3 onglets dans `reportEl`. Helpers dispo dans le scope : `showConfirm({...})` (Promise<bool>), `showToast(msg, type)`, `currentProfile()`.

## File structure

| Fichier | Action | Responsabilité |
| --- | --- | --- |
| `agents/refonte/agent_backup.py` | Modify (1 ligne) | `PROD_FILES` += `categories.yaml` |
| `lib/move_journal.py` | Create | Journal JSONL des moves + undo |
| `dashboard/refonte_results.py` | Modify | `select_move_rows(run_dir)` (sélection pure) |
| `dashboard/agent_refonte_apply.py` | Create | État apply, preview, adopt/restore, execute/undo (threads) |
| `dashboard/app.py` | Modify | 6 routes `/api/agent/refonte/apply/*` |
| `dashboard/templates/partials/agent_refonte_panel.html` | Modify | Bloc « Application » (stepper 2 étapes) |
| `dashboard/static/style.css` | Modify | Styles `rr-apply-*` |
| `tests/auto/test_move_journal.py` | Create | Tests journal |
| `tests/auto/test_refonte_apply.py` | Create | Tests apply (logique + HTTP) |
| `tests/auto/test_agent_journal_backup.py` | Modify | Test categories.yaml dans le backup |
| `dashboard/CLAUDE.md`, `lib/CLAUDE.md` | Modify | Doc |

---

### Task 1: `agent_backup.PROD_FILES` couvre `categories.yaml`

**Files:**
- Modify: `agents/refonte/agent_backup.py:32`
- Test: `tests/auto/test_agent_journal_backup.py`

- [ ] **Step 1: Write the failing test**

Ouvrir `tests/auto/test_agent_journal_backup.py`, repérer la classe de tests backup existante (celle qui crée un profil tmp avec `tree.yaml` + `theme_mapping.yaml` et mocke `dashboard.data.get_project_root`). Ajouter dans cette classe :

```python
    def test_backup_includes_categories_yaml(self):
        """categories.yaml est snapshoté quand il existe (couche A en dépend)."""
        prof = Path(self.tmp) / "profiles" / "default"
        (prof / "categories.yaml").write_text("informatique:\n- folder: X\n", encoding="utf-8")
        name = ab.create_backup("default", batch_id="b-cat-1")
        backup_dir = Path(self.tmp) / "profiles" / "default" / ".cache" / "taxonomy-backups" / name
        self.assertTrue((backup_dir / "categories.yaml").exists())

    def test_restore_tolerates_snapshot_without_categories(self):
        """Anciens snapshots sans categories.yaml : restore ne casse pas."""
        name = ab.create_backup("default", batch_id="b-cat-2")
        backup_dir = Path(self.tmp) / "profiles" / "default" / ".cache" / "taxonomy-backups" / name
        # Simule un ancien snapshot : pas de categories.yaml dedans
        cat = backup_dir / "categories.yaml"
        if cat.exists():
            cat.unlink()
        result = ab.restore_backup("default", name)
        self.assertNotIn("categories.yaml", result["restored"])
```

Adapter les noms (`ab`, `self.tmp`, chemin du profil) au module/fixture réellement utilisés dans ce fichier de test — lire son `setUp` d'abord. Si le `setUp` ne crée pas `categories.yaml`, le premier test le crée lui-même (comme ci-dessus).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest tests.auto.test_agent_journal_backup -k categories -v`
Expected: FAIL — `categories.yaml` absent du backup (PROD_FILES ne le liste pas).

- [ ] **Step 3: Write minimal implementation**

Dans `agents/refonte/agent_backup.py` ligne 32 :

```python
PROD_FILES = ("tree.yaml", "theme_mapping.yaml", "categories.yaml")
```

(`create_backup` et `restore_backup` itèrent `PROD_FILES` en skippant les fichiers absents — aucun autre changement.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest tests.auto.test_agent_journal_backup -v`
Expected: PASS (toute la classe, pas seulement les nouveaux).

- [ ] **Step 5: Commit**

```bash
git add agents/refonte/agent_backup.py tests/auto/test_agent_journal_backup.py
git commit -m "feat(refonte): include categories.yaml in agent backups"
```

---

### Task 2: `lib/move_journal.py` — journal JSONL des déplacements

**Files:**
- Create: `lib/move_journal.py`
- Test: `tests/auto/test_move_journal.py`

- [ ] **Step 1: Write the failing tests**

Créer `tests/auto/test_move_journal.py` :

```python
#!/usr/bin/env python3
"""Tests for lib/move_journal.py — append-only JSONL + undo helpers."""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from lib import move_journal as mj  # noqa: E402


class JournalTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="klodo-movejnl-"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_file(self, rel: str) -> Path:
        path = self.tmpdir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.4 x")
        return path


class TestAppendAndRead(JournalTestBase):
    def test_append_creates_journal_file(self):
        rec = mj.append_move(self.tmpdir, old_abs="/old/a.pdf", new_abs="/new/a.pdf", batch_id="b1")
        self.assertTrue((self.tmpdir / ".cache" / "move-journal.jsonl").exists())
        self.assertEqual(rec["old"], "/old/a.pdf")
        self.assertEqual(rec["new"], "/new/a.pdf")
        self.assertEqual(rec["batch"], "b1")
        self.assertIn("ts", rec)

    def test_read_journal_returns_records_oldest_first(self):
        mj.append_move(self.tmpdir, "/o/1.pdf", "/n/1.pdf", batch_id="b1")
        mj.append_move(self.tmpdir, "/o/2.pdf", "/n/2.pdf", batch_id="b1")
        recs = mj.read_journal(self.tmpdir)
        self.assertEqual([r["old"] for r in recs], ["/o/1.pdf", "/o/2.pdf"])

    def test_read_journal_skips_malformed_lines(self):
        mj.append_move(self.tmpdir, "/o/1.pdf", "/n/1.pdf", batch_id="b1")
        with open(self.tmpdir / ".cache" / "move-journal.jsonl", "a", encoding="utf-8") as f:
            f.write("{broken json\n")
        self.assertEqual(len(mj.read_journal(self.tmpdir)), 1)

    def test_read_journal_empty_when_missing(self):
        self.assertEqual(mj.read_journal(self.tmpdir), [])


class TestUndoRecord(JournalTestBase):
    def test_undo_moves_file_back_and_journals_inverse(self):
        old = self.tmpdir / "A" / "f.pdf"
        new = self._make_file("B/f.pdf")
        rec = {"old": str(old), "new": str(new), "batch": "b1", "ts": "t"}
        mj.undo_record(self.tmpdir, rec)
        self.assertTrue(old.exists())
        self.assertFalse(new.exists())
        last = mj.read_journal(self.tmpdir)[-1]
        self.assertEqual(last["batch"], "undo-b1")
        self.assertEqual(last["old"], str(new))
        self.assertEqual(last["new"], str(old))

    def test_undo_raises_when_new_missing(self):
        rec = {"old": str(self.tmpdir / "A" / "f.pdf"),
               "new": str(self.tmpdir / "B" / "gone.pdf"), "batch": "b1"}
        with self.assertRaises(FileNotFoundError):
            mj.undo_record(self.tmpdir, rec)

    def test_undo_raises_on_collision_at_old(self):
        old = self._make_file("A/f.pdf")
        new = self._make_file("B/f.pdf")
        rec = {"old": str(old), "new": str(new), "batch": "b1"}
        with self.assertRaises(FileExistsError):
            mj.undo_record(self.tmpdir, rec)


class TestUndoBatch(JournalTestBase):
    def _move_and_journal(self, src_rel: str, dst_rel: str, batch: str) -> None:
        src = self._make_file(src_rel)
        dst = self.tmpdir / dst_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.rename(src, dst)
        mj.append_move(self.tmpdir, str(src), str(dst), batch_id=batch)

    def test_undo_batch_reverses_all_in_reverse_order(self):
        self._move_and_journal("A/1.pdf", "X/1.pdf", "b1")
        self._move_and_journal("A/2.pdf", "X/2.pdf", "b1")
        result = mj.undo_batch(self.tmpdir, "b1")
        self.assertEqual(result["n_undone"], 2)
        self.assertEqual(result["n_failed"], 0)
        self.assertTrue((self.tmpdir / "A" / "1.pdf").exists())
        self.assertTrue((self.tmpdir / "A" / "2.pdf").exists())

    def test_undo_batch_skips_failures_and_reports(self):
        self._move_and_journal("A/1.pdf", "X/1.pdf", "b1")
        self._move_and_journal("A/2.pdf", "X/2.pdf", "b1")
        (self.tmpdir / "X" / "2.pdf").unlink()  # fichier disparu → échec individuel
        result = mj.undo_batch(self.tmpdir, "b1")
        self.assertEqual(result["n_undone"], 1)
        self.assertEqual(result["n_failed"], 1)
        self.assertEqual(len(result["failures"]), 1)
        self.assertIn("2.pdf", result["failures"][0]["new"])

    def test_undo_batch_ignores_other_batches_and_undos(self):
        self._move_and_journal("A/1.pdf", "X/1.pdf", "b1")
        self._move_and_journal("A/2.pdf", "X/2.pdf", "b2")
        result = mj.undo_batch(self.tmpdir, "b1")
        self.assertEqual(result["n_undone"], 1)
        self.assertTrue((self.tmpdir / "X" / "2.pdf").exists())  # b2 intact
        # Re-undo du même batch : les records déjà annulés échouent (new absent)
        result2 = mj.undo_batch(self.tmpdir, "b1")
        self.assertEqual(result2["n_undone"], 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest tests.auto.test_move_journal -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lib.move_journal'`.

- [ ] **Step 3: Write the implementation**

Créer `lib/move_journal.py` :

```python
"""Journal append-only des déplacements de fichiers (refonte Apply/Execute).

Miroir de lib/rename_journal.py pour les MOVES inter-dossiers :
JSONL ``profiles/<p>/.cache/move-journal.jsonl``, records
``{ts, old, new, batch}``. Les undos sont eux-mêmes journalisés sous
``batch: "undo-<batch_id>"`` — audit trail complet, append-only.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path

JOURNAL_NAME = "move-journal.jsonl"


def _journal_path(profile_dir: Path) -> Path:
    return Path(profile_dir) / ".cache" / JOURNAL_NAME


def generate_batch_id() -> str:
    """Identifiant de batch unique (uuid4 hex)."""
    return uuid.uuid4().hex


def append_move(
    profile_dir: Path,
    old_abs: str,
    new_abs: str,
    batch_id: str = "",
) -> dict:
    """Journalise un move déjà effectué (ordre : move PUIS journal,
    comme le ``commit_rename`` de rename_journal)."""
    journal = _journal_path(profile_dir)
    journal.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "old": str(old_abs),
        "new": str(new_abs),
        "batch": batch_id,
    }
    with open(journal, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def read_journal(profile_dir: Path) -> list[dict]:
    """Tous les records, du plus ancien au plus récent. Lignes
    malformées skippées silencieusement."""
    journal = _journal_path(profile_dir)
    if not journal.exists():
        return []
    out: list[dict] = []
    with open(journal, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def undo_record(profile_dir: Path, record: dict) -> dict:
    """Reverse un move : ``new`` → ``old``. L'inverse est journalisé
    (``batch: "undo-<batch>"``).

    Raises:
      FileNotFoundError — ``new`` n'existe plus (déplacé hors dashboard).
      FileExistsError   — un fichier occupe déjà ``old`` (collision).
    """
    old = record["old"]
    new = record["new"]
    if not os.path.exists(new):
        raise FileNotFoundError(f"new path missing, cannot undo: {new}")
    if os.path.exists(old):
        raise FileExistsError(f"collision at original path: {old}")
    os.makedirs(os.path.dirname(old), exist_ok=True)
    os.rename(new, old)
    inverse_batch = "undo-" + (record.get("batch") or "")
    return append_move(profile_dir, old_abs=new, new_abs=old, batch_id=inverse_batch)


def undo_batch(profile_dir: Path, batch_id: str) -> dict:
    """Reverse tous les moves d'un batch, du plus récent au plus ancien.
    Échecs individuels skippés + rapportés (jamais d'abort global).

    Returns:
        {"batch": str, "n_undone": int, "n_failed": int,
         "failures": [{"old", "new", "error"}]}
    """
    records = [r for r in read_journal(profile_dir)
               if r.get("batch") == batch_id]
    n_undone = 0
    failures: list[dict] = []
    for rec in reversed(records):
        try:
            undo_record(profile_dir, rec)
            n_undone += 1
        except OSError as exc:
            failures.append({
                "old": rec.get("old", ""),
                "new": rec.get("new", ""),
                "error": str(exc),
            })
    return {
        "batch": batch_id,
        "n_undone": n_undone,
        "n_failed": len(failures),
        "failures": failures,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest tests.auto.test_move_journal -v`
Expected: PASS (11 tests).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check lib/move_journal.py tests/auto/test_move_journal.py
git add lib/move_journal.py tests/auto/test_move_journal.py
git commit -m "feat(lib): move_journal — append-only JSONL move log with undo"
```

---

### Task 3: `refonte_results.select_move_rows` — sélection des moves (pure)

**Files:**
- Modify: `dashboard/refonte_results.py` (après `load_creations`, ~ligne 266)
- Test: `tests/auto/test_refonte_results.py`

- [ ] **Step 1: Write the failing test**

Dans `tests/auto/test_refonte_results.py`, ajouter en fin de fichier (avant le `if __name__` s'il existe) — réutiliser le style des classes existantes du fichier :

```python
class TestSelectMoveRows(unittest.TestCase):
    """select_move_rows : changed + non-doute uniquement."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="klodo-selmoves-"))
        (self.tmp / "simulation").mkdir(parents=True)
        (self.tmp / "proposed").mkdir(parents=True)
        (self.tmp / "simulation" / "reclassify-projection.csv").write_text(
            "rel_path,current_folder,proposed_folder,changed,source,top_theme,confidence,score\n"
            # P1 sûr + changed → INCLUS
            "A/sure.pdf,A,B/Dest,True,LLM (theme),X,0.95,0.9\n"
            # P1 raffiné sûr + changed → INCLUS
            "A/ref.pdf,A,B/Ref,True,LLM (theme→refined),X,0.8,0.8\n"
            # P1 mais confiance < 0.7 → DOUTE (exclu)
            "A/lowconf.pdf,A,B/Low,True,LLM (theme),X,0.5,0.5\n"
            # Keyword → DOUTE (exclu)
            "A/kw.pdf,A,B/Kw,True,Keyword,X,0.9,0.9\n"
            # changed=False → STABLE
            "A/stay.pdf,A,A,False,LLM (theme),X,0.95,0.9\n"
            # FAILED sans destination → STABLE (pas de move possible)
            "A/fail.pdf,A,,False,FAILED,,0.0,0.0\n",
            encoding="utf-8")
        (self.tmp / "proposed" / "changes.json").write_text(
            json.dumps({"creations": [{"path": "B/Dest", "rationale": "x"}]}),
            encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_partitions_moves_doubt_stable(self):
        r = refonte_results.select_move_rows(self.tmp)
        self.assertEqual(r["n_moves"], 2)
        self.assertEqual(r["n_doubt_excluded"], 2)
        self.assertEqual(r["n_stable"], 2)
        rels = {m["rel_path"] for m in r["moves"]}
        self.assertEqual(rels, {"A/sure.pdf", "A/ref.pdf"})

    def test_moves_are_enriched_rows(self):
        r = refonte_results.select_move_rows(self.tmp)
        m = next(x for x in r["moves"] if x["rel_path"] == "A/sure.pdf")
        self.assertEqual(m["proposed_folder"], "B/Dest")
        self.assertEqual(m["source_class"], "p1_theme")
        self.assertIsInstance(m["confidence"], float)

    def test_empty_run_dir(self):
        empty = Path(tempfile.mkdtemp(prefix="klodo-selmoves-empty-"))
        try:
            r = refonte_results.select_move_rows(empty)
            self.assertEqual(r, {"moves": [], "n_moves": 0,
                                 "n_doubt_excluded": 0, "n_stable": 0})
        finally:
            shutil.rmtree(empty, ignore_errors=True)
```

Vérifier en tête de `test_refonte_results.py` que `tempfile`, `shutil`, `json`, `Path` et le module (`refonte_results` ou alias) sont importés — sinon les ajouter en suivant le style du fichier.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest tests.auto.test_refonte_results.TestSelectMoveRows -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'select_move_rows'`.

- [ ] **Step 3: Write minimal implementation**

Dans `dashboard/refonte_results.py`, après `load_creations` :

```python
def select_move_rows(run_dir: Path | str) -> dict:
    """Partitionne la projection en moves exécutables / doute / stables.

    Décision actée (spec Apply/Execute) : on ne déplace QUE les fichiers
    `changed` ET hors zone de doute (`_is_doubt`) — c.-à-d. P1/P1-raffiné
    avec confiance ≥ 0.7. Le reste reste en place pour revue manuelle.

    Returns:
        {"moves": [enriched_row...], "n_moves": int,
         "n_doubt_excluded": int, "n_stable": int}
    """
    rows = read_projection_rows(run_dir)
    creations = load_creations(run_dir)
    moves: list[dict] = []
    n_doubt = 0
    n_stable = 0
    for row in rows:
        changed = str(row.get("changed", "")).strip().lower() in ("true", "1")
        enriched = enrich_row(row, creations)
        if not changed or not enriched["proposed_folder"]:
            n_stable += 1
            continue
        if _is_doubt(enriched):
            n_doubt += 1
            continue
        moves.append(enriched)
    return {
        "moves": moves,
        "n_moves": len(moves),
        "n_doubt_excluded": n_doubt,
        "n_stable": n_stable,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest tests.auto.test_refonte_results -v`
Expected: PASS (classe nouvelle + toutes les existantes).

- [ ] **Step 5: Commit**

```bash
git add dashboard/refonte_results.py tests/auto/test_refonte_results.py
git commit -m "feat(refonte): select_move_rows — partition projection into safe moves"
```

---

### Task 4: `agent_refonte_apply.py` — état, gating, preview

**Files:**
- Create: `dashboard/agent_refonte_apply.py`
- Test: `tests/auto/test_refonte_apply.py` (nouveau)

- [ ] **Step 1: Write the failing tests**

Créer `tests/auto/test_refonte_apply.py` avec la fixture commune à TOUTES les tasks suivantes :

```python
#!/usr/bin/env python3
"""Tests for dashboard/agent_refonte_apply.py — apply/execute d'une refonte."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from dashboard import agent_refonte_apply as ara  # noqa: E402
from dashboard import taxonomy  # noqa: E402


class ApplyTestBase(unittest.TestCase):
    """Profil fixture + target tmp + run Phase B done complet."""

    RUN_ID = "run-apply-1"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="klodo-apply-")
        self.root = Path(self.tmp)
        self.profile_dir = self.root / "profiles" / "default"
        self.target = self.root / "BIBLIO"

        # ── Profil prod
        (self.profile_dir).mkdir(parents=True)
        (self.profile_dir / "profile.yaml").write_text(
            yaml.safe_dump({"target": str(self.target)}), encoding="utf-8")
        (self.profile_dir / "tree.yaml").write_text(
            yaml.safe_dump({"folders": ["A", "B"]}), encoding="utf-8")
        (self.profile_dir / "theme_mapping.yaml").write_text(
            yaml.safe_dump({"old theme": "A"}), encoding="utf-8")

        # ── Bibliothèque physique
        for rel in ("A/sure.pdf", "A/ref.pdf", "A/lowconf.pdf", "A/stay.pdf"):
            p = self.target / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"%PDF-1.4 x")

        # ── Run Phase B done
        self.run_dir = self.profile_dir / ".cache" / "refonte" / self.RUN_ID
        (self.run_dir / "proposed").mkdir(parents=True)
        (self.run_dir / "simulation").mkdir(parents=True)
        (self.run_dir / "status.json").write_text(json.dumps({
            "run_id": self.RUN_ID, "profile": "default",
            "status": "done", "phase": "B"}), encoding="utf-8")
        (self.run_dir / "proposed" / "tree-proposed.yaml").write_text(
            yaml.safe_dump({"folders": ["A", "B/Dest", "B/Ref"]}), encoding="utf-8")
        (self.run_dir / "proposed" / "theme_mapping-proposed.yaml").write_text(
            yaml.safe_dump({"old theme": "B/Dest", "new theme": "B/Ref"}),
            encoding="utf-8")
        (self.run_dir / "proposed" / "changes.json").write_text(json.dumps({
            "creations": [{"path": "B/Dest", "rationale": "x"},
                          {"path": "B/Ref", "rationale": "x"}],
            "fusions": [], "renamings": [], "deletions": [],
            "mappings_added": [{"theme": "new theme", "folder": "B/Ref",
                                "rationale": "x"}],
        }), encoding="utf-8")
        (self.run_dir / "simulation" / "reclassify-projection.csv").write_text(
            "rel_path,current_folder,proposed_folder,changed,source,top_theme,confidence,score\n"
            "A/sure.pdf,A,B/Dest,True,LLM (theme),X,0.95,0.9\n"
            "A/ref.pdf,A,B/Ref,True,LLM (theme→refined),X,0.8,0.8\n"
            "A/lowconf.pdf,A,B/Low,True,LLM (theme),X,0.5,0.5\n"
            "A/stay.pdf,A,A,False,LLM (theme),X,0.95,0.9\n",
            encoding="utf-8")

        self.patch = mock.patch("dashboard.data.get_project_root",
                                return_value=self.root)
        self.patch.start()
        taxonomy.reset_cache()

    def tearDown(self):
        self.patch.stop()
        taxonomy.reset_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestStateAndPreview(ApplyTestBase):
    def test_default_state(self):
        st = ara.read_state("default", self.RUN_ID)
        self.assertFalse(st["adopted"])
        self.assertFalse(st["executed"])
        self.assertIsNone(st["config_backup"])
        self.assertIsNone(st["move_batch_id"])

    def test_write_then_read_state(self):
        ara.write_state("default", self.RUN_ID, {"adopted": True})
        st = ara.read_state("default", self.RUN_ID)
        self.assertTrue(st["adopted"])
        self.assertFalse(st["executed"])  # défauts préservés

    def test_preview_counts(self):
        p = ara.build_preview("default", self.RUN_ID)
        self.assertEqual(p["n_moves"], 2)
        self.assertEqual(p["n_doubt_excluded"], 1)
        self.assertEqual(p["n_stable"], 1)
        self.assertEqual(p["n_creations"], 2)
        self.assertEqual(p["n_mappings_added"], 1)
        self.assertEqual(p["n_renames"], 0)
        dests = {d["folder"]: d["n"] for d in p["top_destinations"]}
        self.assertEqual(dests, {"B/Dest": 1, "B/Ref": 1})
        self.assertIn("state", p)

    def test_preview_unknown_run_raises_404(self):
        with self.assertRaises(ara.ApplyError) as ctx:
            ara.build_preview("default", "nope")
        self.assertEqual(ctx.exception.status, 404)

    def test_assert_run_done_rejects_phase_a(self):
        (self.run_dir / "status.json").write_text(json.dumps({
            "status": "done", "phase": "A"}), encoding="utf-8")
        with self.assertRaises(ara.ApplyError) as ctx:
            ara._assert_run_phase_b_done("default", self.RUN_ID)
        self.assertEqual(ctx.exception.status, 409)

    def test_assert_run_done_rejects_running(self):
        (self.run_dir / "status.json").write_text(json.dumps({
            "status": "running", "phase": "B"}), encoding="utf-8")
        with self.assertRaises(ara.ApplyError) as ctx:
            ara._assert_run_phase_b_done("default", self.RUN_ID)
        self.assertEqual(ctx.exception.status, 409)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest tests.auto.test_refonte_apply -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'dashboard.agent_refonte_apply'`.

- [ ] **Step 3: Write the implementation (état + preview + gating de base)**

Créer `dashboard/agent_refonte_apply.py` :

```python
"""Apply/Execute d'une refonte Phase B — adoption config + déplacements.

Spec : docs/superpowers/specs/2026-06-12-refonte-apply-execute-design.md

Deux couches séquencées par run :
  ① adopt_structure  — promotion des YAML proposés vers la prod (sync, lock)
  ② start_execute    — déplacements physiques selon la projection figée
                       (thread daemon + polling apply/status.json)
Rollbacks : restore_config (snapshot agent_backup) / start_undo_moves
(journal lib/move_journal).
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import threading
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agents.refonte import agent_backup
from dashboard import data, taxonomy
from dashboard.refonte_results import select_move_rows
from lib import move_journal

# ─── Erreur transport (status HTTP porté par l'exception) ────────────────


class ApplyError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ─── Chemins & état ──────────────────────────────────────────────────────

_STATE_DEFAULTS: dict[str, Any] = {
    "adopted": False,
    "adopted_at": None,
    "config_backup": None,
    "move_batch_id": None,
    "executed": False,
    "executed_at": None,
    "n_moved": 0,
    "n_failed": 0,
    "n_skipped": 0,
    "rolled_back_config": False,
    "rolled_back_moves": False,
}


def _profile_dir(profile: str) -> Path:
    return data.get_project_root() / "profiles" / profile


def _run_dir(profile: str, run_id: str) -> Path:
    return _profile_dir(profile) / ".cache" / "refonte" / run_id


def _apply_dir(profile: str, run_id: str) -> Path:
    return _run_dir(profile, run_id) / "apply"


def read_state(profile: str, run_id: str) -> dict[str, Any]:
    """state.json fusionné avec les défauts (fichier absent = état vierge)."""
    path = _apply_dir(profile, run_id) / "state.json"
    state = dict(_STATE_DEFAULTS)
    if path.exists():
        try:
            state.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    return state


def write_state(profile: str, run_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    """Merge ``updates`` dans state.json et retourne l'état complet."""
    state = read_state(profile, run_id)
    state.update(updates)
    apply_dir = _apply_dir(profile, run_id)
    apply_dir.mkdir(parents=True, exist_ok=True)
    (apply_dir / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


def _write_progress(profile: str, run_id: str, payload: dict[str, Any]) -> None:
    apply_dir = _apply_dir(profile, run_id)
    apply_dir.mkdir(parents=True, exist_ok=True)
    (apply_dir / "status.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _read_progress(profile: str, run_id: str) -> dict[str, Any] | None:
    path = _apply_dir(profile, run_id) / "status.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def get_apply_status(profile: str, run_id: str) -> dict[str, Any]:
    """state.json + status.json fusionnés (payload de polling)."""
    if not _run_dir(profile, run_id).is_dir():
        raise ApplyError(f"run not found: {run_id}", 404)
    return {
        "run_id": run_id,
        "state": read_state(profile, run_id),
        "progress": _read_progress(profile, run_id),
    }


# ─── Gating ──────────────────────────────────────────────────────────────


def _assert_run_phase_b_done(profile: str, run_id: str) -> None:
    run_dir = _run_dir(profile, run_id)
    if not run_dir.is_dir():
        raise ApplyError(f"run not found: {run_id}", 404)
    status_path = run_dir / "status.json"
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ApplyError(f"run status unreadable: {run_id}", 500) from exc
    if status.get("phase") != "B" or status.get("status") != "done":
        raise ApplyError(
            "le run doit être une proposition Phase B terminée", 409)


def _assert_no_op_in_progress(profile: str, run_id: str) -> None:
    progress = _read_progress(profile, run_id)
    if progress and progress.get("status") == "running":
        raise ApplyError("une opération est déjà en cours sur ce run", 409)


def _find_other_adopted(profile: str, run_id: str) -> str | None:
    """run_id d'un AUTRE run adopté non restauré, ou None."""
    runs_root = _profile_dir(profile) / ".cache" / "refonte"
    if not runs_root.is_dir():
        return None
    for entry in runs_root.iterdir():
        if not entry.is_dir() or entry.name == run_id:
            continue
        state_path = entry / "apply" / "state.json"
        if not state_path.exists():
            continue
        try:
            st = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if st.get("adopted"):
            return entry.name
    return None


def _target_path(profile: str) -> Path:
    target = taxonomy._profile_target_path(profile)
    if target is None or not target.exists():
        raise ApplyError(
            "target du profil introuvable (SSD non monté ?)", 500)
    return target


def _load_changes(profile: str, run_id: str) -> dict[str, Any]:
    path = _run_dir(profile, run_id) / "proposed" / "changes.json"
    if not path.exists():
        raise ApplyError("changes.json manquant pour ce run", 500)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ApplyError("changes.json illisible", 500) from exc


# ─── Preview ─────────────────────────────────────────────────────────────


def build_preview(profile: str, run_id: str) -> dict[str, Any]:
    """Compteurs pour les modals de confirmation (lecture seule)."""
    if not _run_dir(profile, run_id).is_dir():
        raise ApplyError(f"run not found: {run_id}", 404)
    changes = _load_changes(profile, run_id)
    selection = select_move_rows(_run_dir(profile, run_id))
    dest_counts = Counter(m["proposed_folder"] for m in selection["moves"])
    return {
        "run_id": run_id,
        "state": read_state(profile, run_id),
        "n_moves": selection["n_moves"],
        "n_doubt_excluded": selection["n_doubt_excluded"],
        "n_stable": selection["n_stable"],
        "top_destinations": [
            {"folder": folder, "n": n}
            for folder, n in dest_counts.most_common(10)
        ],
        "n_creations": len(changes.get("creations") or []),
        "n_renames": len(changes.get("renamings") or []),
        "n_fusions": len(changes.get("fusions") or []),
        "n_deletions": len(changes.get("deletions") or []),
        "n_mappings_added": len(changes.get("mappings_added") or []),
    }
```

**Imports par task** (ruff refuse les imports inutilisés — n'ajouter que le nécessaire à chaque task) : Task 4 = `json`, `Counter`, `Path`, `Any`, `data`, `taxonomy`, `select_move_rows`. Task 5 ajoute `shutil`, `UTC`/`datetime`, `agent_backup`. Task 6 ajoute `csv`, `os`, `threading`, `move_journal`. Le docstring de module ci-dessus reste valable ; retirer du bloc d'imports ci-dessus ceux qui ne servent pas encore.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest tests.auto.test_refonte_apply -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check dashboard/agent_refonte_apply.py tests/auto/test_refonte_apply.py
git add dashboard/agent_refonte_apply.py tests/auto/test_refonte_apply.py
git commit -m "feat(refonte): apply module — state, gating, preview"
```

---

### Task 5: Couche A — `adopt_structure` + `restore_config`

**Files:**
- Modify: `dashboard/agent_refonte_apply.py`
- Test: `tests/auto/test_refonte_apply.py`

- [ ] **Step 1: Write the failing tests**

Ajouter dans `tests/auto/test_refonte_apply.py` :

```python
class TestAdoptStructure(ApplyTestBase):
    def test_adopt_promotes_yaml_and_creates_dirs(self):
        result = ara.adopt_structure("default", self.RUN_ID)
        self.assertTrue(result["ok"])
        # tree.yaml promu
        tree = yaml.safe_load((self.profile_dir / "tree.yaml").read_text())
        self.assertIn("B/Dest", tree["folders"])
        # mapping promu
        mapping = yaml.safe_load(
            (self.profile_dir / "theme_mapping.yaml").read_text())
        self.assertEqual(mapping["old theme"], "B/Dest")
        self.assertEqual(mapping["new theme"], "B/Ref")
        # dossiers physiques créés
        self.assertTrue((self.target / "B" / "Dest").is_dir())
        self.assertTrue((self.target / "B" / "Ref").is_dir())
        # état + backup
        st = ara.read_state("default", self.RUN_ID)
        self.assertTrue(st["adopted"])
        self.assertIsNotNone(st["config_backup"])
        backup_dir = (self.profile_dir / ".cache" / "taxonomy-backups"
                      / st["config_backup"])
        self.assertTrue((backup_dir / "tree.yaml").exists())

    def test_adopt_promotes_categories_when_present(self):
        (self.run_dir / "proposed" / "categories-proposed.yaml").write_text(
            yaml.safe_dump({"informatique": [{"folder": "B/Dest"}]}),
            encoding="utf-8")
        ara.adopt_structure("default", self.RUN_ID)
        self.assertTrue((self.profile_dir / "categories.yaml").exists())

    def test_adopt_without_categories_proposed_is_fine(self):
        ara.adopt_structure("default", self.RUN_ID)
        self.assertFalse((self.profile_dir / "categories.yaml").exists())

    def test_adopt_twice_raises_409(self):
        ara.adopt_structure("default", self.RUN_ID)
        with self.assertRaises(ara.ApplyError) as ctx:
            ara.adopt_structure("default", self.RUN_ID)
        self.assertEqual(ctx.exception.status, 409)

    def test_adopt_blocked_by_other_adopted_run(self):
        other = self.profile_dir / ".cache" / "refonte" / "run-other" / "apply"
        other.mkdir(parents=True)
        (other / "state.json").write_text(
            json.dumps({"adopted": True}), encoding="utf-8")
        with self.assertRaises(ara.ApplyError) as ctx:
            ara.adopt_structure("default", self.RUN_ID)
        self.assertEqual(ctx.exception.status, 409)

    def test_adopt_blocked_by_lock(self):
        lock = self.profile_dir / ".cache" / "taxonomy.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("busy")
        with self.assertRaises(ara.ApplyError) as ctx:
            ara.adopt_structure("default", self.RUN_ID)
        self.assertEqual(ctx.exception.status, 423)

    def test_adopt_missing_target_raises_500(self):
        shutil.rmtree(self.target)
        with self.assertRaises(ara.ApplyError) as ctx:
            ara.adopt_structure("default", self.RUN_ID)
        self.assertEqual(ctx.exception.status, 500)


class TestRestoreConfig(ApplyTestBase):
    def test_restore_brings_back_yaml_and_removes_empty_created_dirs(self):
        ara.adopt_structure("default", self.RUN_ID)
        result = ara.restore_config("default", self.RUN_ID)
        self.assertTrue(result["ok"])
        mapping = yaml.safe_load(
            (self.profile_dir / "theme_mapping.yaml").read_text())
        self.assertEqual(mapping, {"old theme": "A"})
        self.assertFalse((self.target / "B" / "Dest").exists())
        st = ara.read_state("default", self.RUN_ID)
        self.assertFalse(st["adopted"])
        self.assertTrue(st["rolled_back_config"])

    def test_restore_preserves_nonempty_created_dirs(self):
        ara.adopt_structure("default", self.RUN_ID)
        keeper = self.target / "B" / "Dest" / "manual.pdf"
        keeper.write_bytes(b"%PDF-1.4 x")
        result = ara.restore_config("default", self.RUN_ID)
        self.assertTrue((self.target / "B" / "Dest").is_dir())
        self.assertIn("B/Dest", result["kept_nonempty"])

    def test_restore_not_adopted_raises_409(self):
        with self.assertRaises(ara.ApplyError) as ctx:
            ara.restore_config("default", self.RUN_ID)
        self.assertEqual(ctx.exception.status, 409)

    def test_restore_blocked_after_execute(self):
        ara.adopt_structure("default", self.RUN_ID)
        ara.write_state("default", self.RUN_ID, {"executed": True})
        with self.assertRaises(ara.ApplyError) as ctx:
            ara.restore_config("default", self.RUN_ID)
        self.assertEqual(ctx.exception.status, 409)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest tests.auto.test_refonte_apply.TestAdoptStructure -v`
Expected: FAIL — `AttributeError: ... no attribute 'adopt_structure'`.

- [ ] **Step 3: Write the implementation**

Ajouter dans `dashboard/agent_refonte_apply.py` (après `build_preview`) :

```python
# ─── Couche A — adoption de la structure ─────────────────────────────────

# (source dans proposed/, destination dans le profil)
_PROMOTED_FILES = (
    ("tree-proposed.yaml", "tree.yaml"),
    ("theme_mapping-proposed.yaml", "theme_mapping.yaml"),
    ("categories-proposed.yaml", "categories.yaml"),  # optionnel
)
_REQUIRED_PROPOSED = ("tree-proposed.yaml", "theme_mapping-proposed.yaml")


def adopt_structure(profile: str, run_id: str) -> dict[str, Any]:
    """Couche A : snapshot config → promotion des YAML proposés →
    mkdir des créations → reset_cache. Synchrone, sous lock."""
    with taxonomy._locks[profile]:
        _assert_run_phase_b_done(profile, run_id)
        try:
            taxonomy._check_lock_free(profile)
        except taxonomy.TaxonomyError as exc:
            raise ApplyError(str(exc), 423) from exc
        state = read_state(profile, run_id)
        if state["adopted"]:
            raise ApplyError("structure déjà adoptée pour ce run", 409)
        other = _find_other_adopted(profile, run_id)
        if other:
            raise ApplyError(
                f"un autre run est déjà adopté ({other}) — restaure sa "
                "config d'abord", 409)
        target = _target_path(profile)
        proposed_dir = _run_dir(profile, run_id) / "proposed"
        for fname in _REQUIRED_PROPOSED:
            if not (proposed_dir / fname).exists():
                raise ApplyError(f"artefact manquant : proposed/{fname}", 500)
        changes = _load_changes(profile, run_id)

        # 1. Snapshot (tree + mapping + categories, rotation 50)
        backup_name = agent_backup.create_backup(
            profile, batch_id=f"apply-{run_id}")

        # 2. Promotion des YAML proposés
        promoted: list[str] = []
        for src_name, dst_name in _PROMOTED_FILES:
            src = proposed_dir / src_name
            if not src.exists():
                continue  # categories-proposed.yaml est optionnel
            shutil.copy2(src, _profile_dir(profile) / dst_name)
            promoted.append(dst_name)

        # 3. Création physique des nouveaux dossiers
        created: list[str] = []
        for creation in changes.get("creations") or []:
            rel = (creation.get("path") or "").strip("/")
            if not rel:
                continue
            (target / rel).mkdir(parents=True, exist_ok=True)
            created.append(rel)

        # 4. État + caches
        taxonomy.reset_cache(profile)
        write_state(profile, run_id, {
            "adopted": True,
            "adopted_at": datetime.now(UTC).isoformat(),
            "config_backup": backup_name,
            "rolled_back_config": False,
        })
        return {
            "ok": True,
            "backup": backup_name,
            "promoted": promoted,
            "created_dirs": created,
        }


def restore_config(profile: str, run_id: str) -> dict[str, Any]:
    """Rollback A : restore du snapshot + suppression des dossiers créés
    SEULEMENT s'ils sont vides (jamais de suppression de contenu)."""
    with taxonomy._locks[profile]:
        state = read_state(profile, run_id)
        if not state["adopted"]:
            raise ApplyError("structure non adoptée pour ce run", 409)
        if state["executed"] and not state["rolled_back_moves"]:
            raise ApplyError(
                "déplacements exécutés — annule-les d'abord "
                "(undo-moves) avant de restaurer la config", 409)
        try:
            taxonomy._check_lock_free(profile)
        except taxonomy.TaxonomyError as exc:
            raise ApplyError(str(exc), 423) from exc
        backup_name = state.get("config_backup")
        if not backup_name:
            raise ApplyError("aucun backup enregistré pour ce run", 500)
        try:
            restored = agent_backup.restore_backup(profile, backup_name)
        except agent_backup.BackupError as exc:
            raise ApplyError(str(exc), 500) from exc

        # Dossiers créés à l'adoption : rmdir si vides (enfants d'abord)
        target = _target_path(profile)
        changes = _load_changes(profile, run_id)
        creation_paths = sorted(
            ((c.get("path") or "").strip("/")
             for c in changes.get("creations") or []),
            key=lambda p: p.count("/"), reverse=True)
        removed: list[str] = []
        kept: list[str] = []
        for rel in creation_paths:
            if not rel:
                continue
            d = target / rel
            if not d.is_dir():
                continue
            if any(d.iterdir()):
                kept.append(rel)
            else:
                d.rmdir()
                removed.append(rel)

        taxonomy.reset_cache(profile)
        write_state(profile, run_id, {
            "adopted": False,
            "rolled_back_config": True,
        })
        return {
            "ok": True,
            "restored": restored.get("restored", []),
            "removed_dirs": removed,
            "kept_nonempty": kept,
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest tests.auto.test_refonte_apply -v`
Expected: PASS (18 tests).

- [ ] **Step 5: Commit**

```bash
git add dashboard/agent_refonte_apply.py tests/auto/test_refonte_apply.py
git commit -m "feat(refonte): adopt_structure + restore_config (layer A)"
```

---

### Task 6: Couche B — `_run_moves` + `start_execute`

**Files:**
- Modify: `dashboard/agent_refonte_apply.py`
- Test: `tests/auto/test_refonte_apply.py`

- [ ] **Step 1: Write the failing tests**

Ajouter dans `tests/auto/test_refonte_apply.py` :

```python
class TestExecuteMoves(ApplyTestBase):
    def setUp(self):
        super().setUp()
        ara.adopt_structure("default", self.RUN_ID)
        # logs/ du projet mocké → tmpdir
        (self.root / "logs").mkdir(exist_ok=True)

    def test_run_moves_moves_safe_files_only(self):
        result = ara._run_moves("default", self.RUN_ID)
        self.assertEqual(result["n_moved"], 2)
        self.assertEqual(result["n_failed"], 0)
        self.assertTrue((self.target / "B" / "Dest" / "sure.pdf").exists())
        self.assertTrue((self.target / "B" / "Ref" / "ref.pdf").exists())
        self.assertFalse((self.target / "A" / "sure.pdf").exists())
        # exclus : doute + stable restent en place
        self.assertTrue((self.target / "A" / "lowconf.pdf").exists())
        self.assertTrue((self.target / "A" / "stay.pdf").exists())

    def test_run_moves_journals_each_move(self):
        ara._run_moves("default", self.RUN_ID)
        from lib import move_journal as mj
        recs = mj.read_journal(self.profile_dir)
        self.assertEqual(len(recs), 2)
        st = ara.read_state("default", self.RUN_ID)
        self.assertEqual({r["batch"] for r in recs}, {st["move_batch_id"]})

    def test_run_moves_skips_stale_and_collision_without_abort(self):
        # stale : source disparue
        (self.target / "A" / "sure.pdf").unlink()
        # collision : destination occupée
        dest = self.target / "B" / "Ref" / "ref.pdf"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"%PDF-1.4 other")
        result = ara._run_moves("default", self.RUN_ID)
        self.assertEqual(result["n_moved"], 0)
        self.assertEqual(result["n_skipped"], 2)
        # l'original de la collision n'a pas bougé ni été écrasé
        self.assertTrue((self.target / "A" / "ref.pdf").exists())
        self.assertEqual(dest.read_bytes(), b"%PDF-1.4 other")

    def test_run_moves_writes_report_csv(self):
        ara._run_moves("default", self.RUN_ID)
        reports = list((self.root / "logs").glob("rapport_apply_*.csv"))
        self.assertEqual(len(reports), 1)
        content = reports[0].read_text(encoding="utf-8")
        self.assertIn("A/sure.pdf", content)
        self.assertIn("moved", content)

    def test_run_moves_prunes_empty_source_dirs(self):
        # A devient vide si on retire les 2 fichiers restants
        (self.target / "A" / "lowconf.pdf").unlink()
        (self.target / "A" / "stay.pdf").unlink()
        ara._run_moves("default", self.RUN_ID)
        self.assertFalse((self.target / "A").exists())
        self.assertTrue(self.target.exists())  # jamais le target lui-même

    def test_run_moves_updates_state(self):
        ara._run_moves("default", self.RUN_ID)
        st = ara.read_state("default", self.RUN_ID)
        self.assertTrue(st["executed"])
        self.assertEqual(st["n_moved"], 2)
        self.assertIsNotNone(st["move_batch_id"])
        progress = ara._read_progress("default", self.RUN_ID)
        self.assertEqual(progress["status"], "done")

    def test_start_execute_gating(self):
        # non adopté → 409
        ara.write_state("default", self.RUN_ID, {"adopted": False})
        with self.assertRaises(ara.ApplyError) as ctx:
            ara.start_execute("default", self.RUN_ID)
        self.assertEqual(ctx.exception.status, 409)
        # déjà exécuté → 409
        ara.write_state("default", self.RUN_ID,
                        {"adopted": True, "executed": True})
        with self.assertRaises(ara.ApplyError) as ctx:
            ara.start_execute("default", self.RUN_ID)
        self.assertEqual(ctx.exception.status, 409)

    def test_start_execute_blocked_by_lock(self):
        lock = self.profile_dir / ".cache" / "taxonomy.lock"
        lock.write_text("busy")
        with self.assertRaises(ara.ApplyError) as ctx:
            ara.start_execute("default", self.RUN_ID)
        self.assertEqual(ctx.exception.status, 423)

    def test_execute_job_releases_lock_even_on_error(self):
        lock = self.profile_dir / ".cache" / "taxonomy.lock"
        lock.write_text("refonte-apply")
        with mock.patch.object(ara, "_run_moves",
                               side_effect=RuntimeError("boom")):
            ara._execute_job("default", self.RUN_ID)
        self.assertFalse(lock.exists())
        progress = ara._read_progress("default", self.RUN_ID)
        self.assertEqual(progress["status"], "error")
        self.assertIn("boom", progress["error"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest tests.auto.test_refonte_apply.TestExecuteMoves -v`
Expected: FAIL — `AttributeError: ... no attribute '_run_moves'`.

- [ ] **Step 3: Write the implementation**

Ajouter dans `dashboard/agent_refonte_apply.py` :

```python
# ─── Couche B — exécution des déplacements ───────────────────────────────

_PROGRESS_EVERY = 50


def _spawn(target, args, name: str) -> None:
    """Lance ``target`` dans un thread daemon. Indirection volontaire :
    les tests HTTP patchent ``_spawn`` pour exécuter en synchrone (patcher
    ``threading.Thread`` casserait le portal anyio du TestClient)."""
    thread = threading.Thread(target=target, args=args, daemon=True, name=name)
    thread.start()


def _logs_dir() -> Path:
    d = data.get_project_root() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _prune_empty_dirs(target: Path, rel_folders: set[str]) -> None:
    """Supprime les dossiers sources devenus vides, en remontant —
    jamais le target lui-même."""
    target = target.resolve()
    for rel in sorted(rel_folders, key=lambda p: p.count("/"), reverse=True):
        d = (target / rel).resolve()
        while d != target and d.is_relative_to(target):
            try:
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
                else:
                    break
            except OSError:
                break
            d = d.parent


def _run_moves(profile: str, run_id: str) -> dict[str, Any]:
    """Boucle de déplacement (synchrone — appelée par le thread).

    Pour chaque move de la projection figée : garde de fraîcheur →
    garde de collision → os.rename (atomique, même volume) → journal.
    Échec individuel = skip + rapport, jamais d'abort.
    """
    target = _target_path(profile)
    selection = select_move_rows(_run_dir(profile, run_id))
    moves = selection["moves"]
    batch_id = move_journal.generate_batch_id()
    profile_dir = _profile_dir(profile)

    n_moved = 0
    n_failed = 0
    n_skipped = 0
    report_rows: list[dict[str, str]] = []
    source_folders: set[str] = set()
    n_total = len(moves)
    _write_progress(profile, run_id, {
        "op": "execute", "status": "running",
        "n_done": 0, "n_total": n_total, "n_failed": 0, "error": None,
    })

    for i, m in enumerate(moves, start=1):
        rel = m["rel_path"]
        old = target / rel
        new = target / m["proposed_folder"] / os.path.basename(rel)
        status = ""
        detail = ""
        if not old.exists():
            status, n_skipped = "stale", n_skipped + 1
            detail = "source absente (déplacée depuis la simulation)"
        elif new.exists():
            status, n_skipped = "collision", n_skipped + 1
            detail = "destination occupée — jamais d'écrasement"
        else:
            try:
                new.parent.mkdir(parents=True, exist_ok=True)
                os.rename(old, new)
                move_journal.append_move(
                    profile_dir, str(old), str(new), batch_id=batch_id)
                status, n_moved = "moved", n_moved + 1
                source_folders.add(os.path.dirname(rel))
            except OSError as exc:
                status, n_failed = "error", n_failed + 1
                detail = str(exc)
        report_rows.append({
            "rel_path": rel, "old": str(old), "new": str(new),
            "status": status, "detail": detail,
        })
        if i % _PROGRESS_EVERY == 0:
            _write_progress(profile, run_id, {
                "op": "execute", "status": "running",
                "n_done": i, "n_total": n_total,
                "n_failed": n_failed + n_skipped, "error": None,
            })

    # Rapport CSV horodaté
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = _logs_dir() / f"rapport_apply_{ts}.csv"
    with report_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["rel_path", "old", "new", "status", "detail"])
        writer.writeheader()
        writer.writerows(report_rows)

    _prune_empty_dirs(target, source_folders)
    write_state(profile, run_id, {
        "executed": True,
        "executed_at": datetime.now(UTC).isoformat(),
        "move_batch_id": batch_id,
        "n_moved": n_moved,
        "n_failed": n_failed,
        "n_skipped": n_skipped,
        "rolled_back_moves": False,
    })
    _write_progress(profile, run_id, {
        "op": "execute", "status": "done",
        "n_done": n_total, "n_total": n_total,
        "n_failed": n_failed + n_skipped, "error": None,
        "report": report_path.name,
    })
    return {"n_moved": n_moved, "n_failed": n_failed,
            "n_skipped": n_skipped, "report": report_path.name,
            "batch_id": batch_id}


def _execute_job(profile: str, run_id: str) -> None:
    """Wrapper thread : exécute, capture les erreurs, libère TOUJOURS
    le lock et invalide les caches."""
    try:
        _run_moves(profile, run_id)
    except Exception as exc:  # noqa: BLE001 — frontière de thread
        _write_progress(profile, run_id, {
            "op": "execute", "status": "error",
            "n_done": 0, "n_total": 0, "n_failed": 0, "error": str(exc),
        })
    finally:
        taxonomy._lock_file(profile).unlink(missing_ok=True)
        taxonomy.reset_cache(profile)


def start_execute(profile: str, run_id: str) -> dict[str, Any]:
    """Valide le gating, pose le lock sentinel, lance le thread."""
    _assert_run_phase_b_done(profile, run_id)
    _assert_no_op_in_progress(profile, run_id)
    state = read_state(profile, run_id)
    if not state["adopted"]:
        raise ApplyError("adopte d'abord la structure (étape ①)", 409)
    if state["executed"]:
        raise ApplyError("déplacements déjà exécutés pour ce run", 409)
    try:
        taxonomy._check_lock_free(profile)
    except taxonomy.TaxonomyError as exc:
        raise ApplyError(str(exc), 423) from exc
    _target_path(profile)  # SSD monté ?
    n_total = select_move_rows(_run_dir(profile, run_id))["n_moves"]

    lock = taxonomy._lock_file(profile)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("refonte-apply\n", encoding="utf-8")
    _spawn(_execute_job, (profile, run_id), f"refonte-apply-{run_id[:8]}")
    return {"ok": True, "run_id": run_id, "n_total": n_total}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest tests.auto.test_refonte_apply -v`
Expected: PASS (27 tests).

- [ ] **Step 5: Commit**

```bash
git add dashboard/agent_refonte_apply.py tests/auto/test_refonte_apply.py
git commit -m "feat(refonte): execute moves with freshness/collision guards + report (layer B)"
```

---

### Task 7: Couche B — `start_undo_moves`

**Files:**
- Modify: `dashboard/agent_refonte_apply.py`
- Test: `tests/auto/test_refonte_apply.py`

- [ ] **Step 1: Write the failing tests**

```python
class TestUndoMoves(ApplyTestBase):
    def setUp(self):
        super().setUp()
        ara.adopt_structure("default", self.RUN_ID)
        (self.root / "logs").mkdir(exist_ok=True)
        ara._run_moves("default", self.RUN_ID)

    def test_undo_restores_files_and_state(self):
        result = ara._run_undo_moves("default", self.RUN_ID)
        self.assertEqual(result["n_undone"], 2)
        self.assertTrue((self.target / "A" / "sure.pdf").exists())
        self.assertFalse((self.target / "B" / "Dest" / "sure.pdf").exists())
        st = ara.read_state("default", self.RUN_ID)
        self.assertFalse(st["executed"])
        self.assertTrue(st["rolled_back_moves"])

    def test_undo_then_restore_config_allowed(self):
        ara._run_undo_moves("default", self.RUN_ID)
        result = ara.restore_config("default", self.RUN_ID)
        self.assertTrue(result["ok"])

    def test_undo_skips_individual_failures(self):
        (self.target / "B" / "Dest" / "sure.pdf").unlink()  # disparu
        result = ara._run_undo_moves("default", self.RUN_ID)
        self.assertEqual(result["n_undone"], 1)
        self.assertEqual(result["n_failed"], 1)

    def test_start_undo_gating_not_executed(self):
        ara._run_undo_moves("default", self.RUN_ID)
        with self.assertRaises(ara.ApplyError) as ctx:
            ara.start_undo_moves("default", self.RUN_ID)
        self.assertEqual(ctx.exception.status, 409)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest tests.auto.test_refonte_apply.TestUndoMoves -v`
Expected: FAIL — `AttributeError: ... no attribute '_run_undo_moves'`.

- [ ] **Step 3: Write the implementation**

Ajouter dans `dashboard/agent_refonte_apply.py` :

```python
# ─── Couche B — annulation des déplacements ──────────────────────────────


def _run_undo_moves(profile: str, run_id: str) -> dict[str, Any]:
    """Reverse le batch de moves de ce run (synchrone — thread)."""
    state = read_state(profile, run_id)
    batch_id = state.get("move_batch_id")
    if not batch_id:
        raise ApplyError("aucun batch de moves enregistré", 409)
    _write_progress(profile, run_id, {
        "op": "undo", "status": "running",
        "n_done": 0, "n_total": state.get("n_moved", 0),
        "n_failed": 0, "error": None,
    })
    result = move_journal.undo_batch(_profile_dir(profile), batch_id)
    write_state(profile, run_id, {
        "executed": False,
        "rolled_back_moves": True,
    })
    _write_progress(profile, run_id, {
        "op": "undo", "status": "done",
        "n_done": result["n_undone"],
        "n_total": result["n_undone"] + result["n_failed"],
        "n_failed": result["n_failed"], "error": None,
    })
    return result


def _undo_job(profile: str, run_id: str) -> None:
    try:
        _run_undo_moves(profile, run_id)
    except Exception as exc:  # noqa: BLE001 — frontière de thread
        _write_progress(profile, run_id, {
            "op": "undo", "status": "error",
            "n_done": 0, "n_total": 0, "n_failed": 0, "error": str(exc),
        })
    finally:
        taxonomy._lock_file(profile).unlink(missing_ok=True)
        taxonomy.reset_cache(profile)


def start_undo_moves(profile: str, run_id: str) -> dict[str, Any]:
    """Valide le gating, pose le lock, lance le thread d'annulation."""
    _assert_no_op_in_progress(profile, run_id)
    state = read_state(profile, run_id)
    if not state["executed"]:
        raise ApplyError("aucun déplacement à annuler pour ce run", 409)
    try:
        taxonomy._check_lock_free(profile)
    except taxonomy.TaxonomyError as exc:
        raise ApplyError(str(exc), 423) from exc
    _target_path(profile)
    lock = taxonomy._lock_file(profile)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("refonte-apply-undo\n", encoding="utf-8")
    _spawn(_undo_job, (profile, run_id), f"refonte-undo-{run_id[:8]}")
    return {"ok": True, "run_id": run_id,
            "n_total": state.get("n_moved", 0)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest tests.auto.test_refonte_apply -v`
Expected: PASS (31 tests).

- [ ] **Step 5: Commit**

```bash
git add dashboard/agent_refonte_apply.py tests/auto/test_refonte_apply.py
git commit -m "feat(refonte): undo moves via move journal (layer B rollback)"
```

---

### Task 8: Routes API `/api/agent/refonte/apply/*`

**Files:**
- Modify: `dashboard/app.py` (après la route `doubt-files`, ~ligne 2832)
- Test: `tests/auto/test_refonte_apply.py`

- [ ] **Step 1: Write the failing tests**

Ajouter dans `tests/auto/test_refonte_apply.py` (imports en tête : `from fastapi.testclient import TestClient` et `from dashboard.app import app`) :

```python
class TestApplyEndpoints(ApplyTestBase):
    def setUp(self):
        super().setUp()
        (self.root / "logs").mkdir(exist_ok=True)
        self.client = TestClient(app)

    def test_preview_endpoint(self):
        r = self.client.get(
            f"/api/agent/refonte/apply/{self.RUN_ID}/preview?profile=default")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["n_moves"], 2)
        self.assertFalse(d["state"]["adopted"])

    def test_preview_404(self):
        r = self.client.get(
            "/api/agent/refonte/apply/nope/preview?profile=default")
        self.assertEqual(r.status_code, 404)

    def test_adopt_endpoint_then_409_on_repeat(self):
        r = self.client.post("/api/agent/refonte/apply/adopt",
                             json={"profile": "default", "run_id": self.RUN_ID})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        r2 = self.client.post("/api/agent/refonte/apply/adopt",
                              json={"profile": "default", "run_id": self.RUN_ID})
        self.assertEqual(r2.status_code, 409)

    def test_adopt_missing_fields_400(self):
        r = self.client.post("/api/agent/refonte/apply/adopt",
                             json={"profile": "default"})
        self.assertEqual(r.status_code, 400)

    def test_execute_endpoint_requires_adoption(self):
        r = self.client.post("/api/agent/refonte/apply/execute",
                             json={"profile": "default", "run_id": self.RUN_ID})
        self.assertEqual(r.status_code, 409)

    def test_full_flow_via_http(self):
        self.client.post("/api/agent/refonte/apply/adopt",
                         json={"profile": "default", "run_id": self.RUN_ID})
        # Exécution SYNCHRONE pour le test : on courtcircuite _spawn
        # (ne JAMAIS patcher threading.Thread — le TestClient en dépend)
        sync_spawn = lambda target, args, name: target(*args)  # noqa: E731
        with mock.patch.object(ara, "_spawn", sync_spawn):
            r = self.client.post(
                "/api/agent/refonte/apply/execute",
                json={"profile": "default", "run_id": self.RUN_ID})
        self.assertEqual(r.status_code, 200)
        s = self.client.get(
            f"/api/agent/refonte/apply/{self.RUN_ID}/status?profile=default")
        self.assertEqual(s.status_code, 200)
        d = s.json()
        self.assertTrue(d["state"]["executed"])
        self.assertEqual(d["progress"]["status"], "done")
        # Undo (synchrone aussi)
        with mock.patch.object(ara, "_spawn", sync_spawn):
            u = self.client.post(
                "/api/agent/refonte/apply/undo-moves",
                json={"profile": "default", "run_id": self.RUN_ID})
        self.assertEqual(u.status_code, 200)
        # Restore config
        rc = self.client.post(
            "/api/agent/refonte/apply/restore-config",
            json={"profile": "default", "run_id": self.RUN_ID})
        self.assertEqual(rc.status_code, 200)

    def test_status_404(self):
        r = self.client.get(
            "/api/agent/refonte/apply/nope/status?profile=default")
        self.assertEqual(r.status_code, 404)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest tests.auto.test_refonte_apply.TestApplyEndpoints -v`
Expected: FAIL — 404 sur toutes les routes (inexistantes).

- [ ] **Step 3: Write the routes**

Dans `dashboard/app.py`, ajouter en tête de fichier l'import (à côté de `from dashboard import agent_refonte`) :

```python
from dashboard import agent_refonte_apply
```

Puis après la route `/api/agent/refonte/proposition/{run_id}/doubt-files` :

```python
# ════════════════════════════════════════════════════════════════════════
#  Agent Refonte — Apply / Execute (adoption + déplacements)
# ════════════════════════════════════════════════════════════════════════


def _apply_error_response(exc: agent_refonte_apply.ApplyError):
    from fastapi.responses import JSONResponse
    return JSONResponse({"error": str(exc)}, status_code=exc.status)


@app.get("/api/agent/refonte/apply/{run_id}/preview")
async def api_refonte_apply_preview(run_id: str, profile: str):
    """Compteurs pour les modals de confirmation (lecture seule)."""
    from fastapi.responses import JSONResponse
    try:
        return JSONResponse(agent_refonte_apply.build_preview(profile, run_id))
    except agent_refonte_apply.ApplyError as exc:
        return _apply_error_response(exc)


@app.get("/api/agent/refonte/apply/{run_id}/status")
async def api_refonte_apply_status(run_id: str, profile: str):
    """state.json + status.json fusionnés (polling)."""
    from fastapi.responses import JSONResponse
    try:
        return JSONResponse(
            agent_refonte_apply.get_apply_status(profile, run_id))
    except agent_refonte_apply.ApplyError as exc:
        return _apply_error_response(exc)


async def _apply_post_body(request: Request) -> tuple[str, str] | None:
    body = await request.json()
    profile = (body.get("profile") or "").strip()
    run_id = (body.get("run_id") or "").strip()
    if not profile or not run_id:
        return None
    return profile, run_id


@app.post("/api/agent/refonte/apply/adopt")
async def api_refonte_apply_adopt(request: Request):
    """Couche A : promotion de la config proposée (backup + mkdir)."""
    from fastapi.responses import JSONResponse
    parsed = await _apply_post_body(request)
    if parsed is None:
        return JSONResponse({"error": "profile et run_id requis"},
                            status_code=400)
    try:
        return JSONResponse(agent_refonte_apply.adopt_structure(*parsed))
    except agent_refonte_apply.ApplyError as exc:
        return _apply_error_response(exc)


@app.post("/api/agent/refonte/apply/restore-config")
async def api_refonte_apply_restore(request: Request):
    """Rollback couche A : restore du snapshot config."""
    from fastapi.responses import JSONResponse
    parsed = await _apply_post_body(request)
    if parsed is None:
        return JSONResponse({"error": "profile et run_id requis"},
                            status_code=400)
    try:
        return JSONResponse(agent_refonte_apply.restore_config(*parsed))
    except agent_refonte_apply.ApplyError as exc:
        return _apply_error_response(exc)


@app.post("/api/agent/refonte/apply/execute")
async def api_refonte_apply_execute(request: Request):
    """Couche B : lance le thread de déplacements (202-like, poll status)."""
    from fastapi.responses import JSONResponse
    parsed = await _apply_post_body(request)
    if parsed is None:
        return JSONResponse({"error": "profile et run_id requis"},
                            status_code=400)
    try:
        return JSONResponse(agent_refonte_apply.start_execute(*parsed))
    except agent_refonte_apply.ApplyError as exc:
        return _apply_error_response(exc)


@app.post("/api/agent/refonte/apply/undo-moves")
async def api_refonte_apply_undo(request: Request):
    """Rollback couche B : annule le batch de moves (thread + poll)."""
    from fastapi.responses import JSONResponse
    parsed = await _apply_post_body(request)
    if parsed is None:
        return JSONResponse({"error": "profile et run_id requis"},
                            status_code=400)
    try:
        return JSONResponse(agent_refonte_apply.start_undo_moves(*parsed))
    except agent_refonte_apply.ApplyError as exc:
        return _apply_error_response(exc)
```

(Vérifier que `Request` est déjà importé en tête d'`app.py` — oui, utilisé partout.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest tests.auto.test_refonte_apply -v`
Expected: PASS (38 tests).

- [ ] **Step 5: Full suite + lint + commit**

```bash
uv run python -m unittest discover tests/auto -q
uv run ruff check dashboard/ lib/ tests/auto/
git add dashboard/app.py tests/auto/test_refonte_apply.py
git commit -m "feat(refonte): apply/execute HTTP routes"
```

---

### Task 9: UI — bloc « Application de la refonte »

**Files:**
- Modify: `dashboard/templates/partials/agent_refonte_panel.html` (fonction `renderPhaseBReport` ~ligne 922 + nouvelles fonctions JS)
- Modify: `dashboard/static/style.css` (fin de fichier)

Pas de test automatisé du JS (pas d'infra JS-test dans le projet) — vérification : `node --check` n'est pas applicable (JS inline dans le HTML), donc test manuel via le dashboard + revue de code.

- [ ] **Step 1: Brancher le bloc dans `renderPhaseBReport`**

Dans `renderPhaseBReport(runId, status)` (~ligne 922), juste APRÈS l'affectation `reportEl.innerHTML = ...` (la chaîne des 3 onglets), ajouter :

```javascript
        // Bloc Application (Apply/Execute) au-dessus des onglets
        reportEl.insertAdjacentHTML('afterbegin',
            '<div class="rr-apply" id="rr-apply"></div>');
        renderApplyBlock(runId);
```

- [ ] **Step 2: Ajouter les fonctions JS du bloc Application**

Insérer AVANT `function renderPhaseBReport(runId, status) {` :

```javascript
    // ─── Application de la refonte (Apply/Execute) ───────────────────
    let applyPollTimer = null;

    function stopApplyPoll() {
        if (applyPollTimer) { clearInterval(applyPollTimer); applyPollTimer = null; }
    }

    async function fetchApplyPreview(runId) {
        const r = await fetch('/api/agent/refonte/apply/' + runId
            + '/preview?profile=' + encodeURIComponent(currentProfile()));
        if (!r.ok) throw new Error((await r.json()).error || r.status);
        return r.json();
    }

    async function applyPost(path, runId) {
        const r = await fetch('/api/agent/refonte/apply/' + path, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({profile: currentProfile(), run_id: runId}),
        });
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
        return d;
    }

    function applyStepHtml(p) {
        const st = p.state || {};
        let html = '<div class="rr-apply-title">⚙️ Application de la refonte</div>';
        // ── Étape ①
        html += '<div class="rr-apply-step">';
        html += '<span class="rr-apply-num">①</span> Adopter la structure ';
        if (st.adopted) {
            html += '<span class="rr-apply-ok">✓ adoptée</span>';
        } else {
            html += '<button class="btn-secondary rr-apply-btn" id="rr-apply-adopt">Adopter…</button>';
        }
        html += '<div class="muted small">' + p.n_creations + ' création(s) · '
              + p.n_renames + ' rename(s) · ' + p.n_fusions + ' fusion(s) · '
              + p.n_mappings_added + ' mapping(s)</div>';
        if (st.adopted && !st.executed) {
            html += '<button class="btn-secondary rr-apply-btn rr-apply-danger" id="rr-apply-restore">↩ Restaurer la config</button>';
        }
        html += '</div>';
        // ── Étape ②
        html += '<div class="rr-apply-step' + (st.adopted ? '' : ' rr-apply-disabled') + '">';
        html += '<span class="rr-apply-num">②</span> Exécuter les déplacements ';
        if (st.executed) {
            html += '<span class="rr-apply-ok">✓ ' + st.n_moved + ' déplacé(s)'
                  + (st.n_skipped ? ' · ' + st.n_skipped + ' skippé(s)' : '')
                  + (st.n_failed ? ' · ' + st.n_failed + ' échec(s)' : '') + '</span>';
            html += '<button class="btn-secondary rr-apply-btn rr-apply-danger" id="rr-apply-undo">↩ Annuler les déplacements</button>';
        } else if (st.adopted) {
            html += '<button class="btn-primary rr-apply-btn" id="rr-apply-execute">▶ Exécuter…</button>';
        }
        html += '<div class="muted small">' + p.n_moves + ' à déplacer · '
              + p.n_doubt_excluded + ' exclus (à vérifier) · '
              + p.n_stable + ' stables</div>';
        html += '<div id="rr-apply-progress"></div>';
        html += '</div>';
        return html;
    }

    async function renderApplyBlock(runId) {
        const host = document.getElementById('rr-apply');
        if (!host) return;
        stopApplyPoll();
        let p;
        try { p = await fetchApplyPreview(runId); }
        catch (e) { host.innerHTML = ''; return; }
        host.innerHTML = applyStepHtml(p);

        const adoptBtn = document.getElementById('rr-apply-adopt');
        if (adoptBtn) adoptBtn.addEventListener('click', async () => {
            const ok = await showConfirm({
                title: 'Adopter la structure proposée ?',
                body: p.n_creations + ' dossier(s) créé(s), ' + p.n_renames
                    + ' renommage(s), ' + p.n_fusions + ' fusion(s), '
                    + p.n_mappings_added + ' mapping(s) ajouté(s).\n'
                    + 'Un backup de la config actuelle sera créé (restaurable).',
                confirmLabel: 'Adopter', variant: 'primary'});
            if (!ok) return;
            try {
                const d = await applyPost('adopt', runId);
                showToast('✓ Structure adoptée (backup ' + d.backup + ')', 'success');
                renderApplyBlock(runId);
            } catch (e) { showToast('✗ ' + e.message, 'error'); }
        });

        const restoreBtn = document.getElementById('rr-apply-restore');
        if (restoreBtn) restoreBtn.addEventListener('click', async () => {
            const ok = await showConfirm({
                title: 'Restaurer la config d\'avant adoption ?',
                body: 'tree.yaml / theme_mapping.yaml / categories.yaml seront '
                    + 'restaurés depuis le backup. Les dossiers créés vides '
                    + 'seront supprimés (les non-vides sont préservés).',
                confirmLabel: 'Restaurer', variant: 'danger'});
            if (!ok) return;
            try {
                await applyPost('restore-config', runId);
                showToast('✓ Config restaurée', 'success');
                renderApplyBlock(runId);
            } catch (e) { showToast('✗ ' + e.message, 'error'); }
        });

        const execBtn = document.getElementById('rr-apply-execute');
        if (execBtn) execBtn.addEventListener('click', async () => {
            const dests = (p.top_destinations || []).slice(0, 5)
                .map(d => d.folder + ' (' + d.n + ')').join(', ');
            const ok = await showConfirm({
                title: 'Exécuter ' + p.n_moves + ' déplacement(s) ?',
                body: p.n_doubt_excluded + ' fichier(s) « à vérifier » resteront '
                    + 'en place. Top destinations : ' + dests + '.\n'
                    + 'Chaque déplacement est journalisé — annulable tant que '
                    + 'les fichiers ne sont pas re-déplacés à la main.',
                confirmLabel: 'Exécuter', variant: 'danger'});
            if (!ok) return;
            try {
                await applyPost('execute', runId);
                showToast('▶ Déplacements lancés', 'info');
                startApplyPoll(runId);
            } catch (e) { showToast('✗ ' + e.message, 'error'); }
        });

        const undoBtn = document.getElementById('rr-apply-undo');
        if (undoBtn) undoBtn.addEventListener('click', async () => {
            const ok = await showConfirm({
                title: 'Annuler les ' + (p.state.n_moved || 0) + ' déplacement(s) ?',
                body: 'Chaque fichier sera re-déplacé vers son emplacement '
                    + 'd\'origine via le journal. Échecs individuels rapportés.',
                confirmLabel: 'Annuler les déplacements', variant: 'danger'});
            if (!ok) return;
            try {
                await applyPost('undo-moves', runId);
                showToast('↩ Annulation lancée', 'info');
                startApplyPoll(runId);
            } catch (e) { showToast('✗ ' + e.message, 'error'); }
        });

        // Reprise auto du poll si une op tourne déjà (reload de page)
        const prog = p.state && p.progress;  // progress vient du status, pas du preview
        startApplyPollIfRunning(runId);
    }

    async function startApplyPollIfRunning(runId) {
        try {
            const r = await fetch('/api/agent/refonte/apply/' + runId
                + '/status?profile=' + encodeURIComponent(currentProfile()));
            if (!r.ok) return;
            const d = await r.json();
            if (d.progress && d.progress.status === 'running') startApplyPoll(runId);
        } catch (e) { /* silencieux */ }
    }

    function startApplyPoll(runId) {
        stopApplyPoll();
        const tick = async () => {
            let d;
            try {
                const r = await fetch('/api/agent/refonte/apply/' + runId
                    + '/status?profile=' + encodeURIComponent(currentProfile()));
                d = await r.json();
            } catch (e) { return; }
            const prog = d.progress;
            const host = document.getElementById('rr-apply-progress');
            if (!prog) return;
            if (prog.status === 'running') {
                const pct = prog.n_total
                    ? Math.round(100 * prog.n_done / prog.n_total) : 0;
                if (host) host.innerHTML =
                    '<div class="rr-apply-bar"><div class="rr-apply-fill" style="width:'
                    + pct + '%"></div></div><div class="muted small">'
                    + prog.n_done + '/' + prog.n_total
                    + (prog.n_failed ? ' · ' + prog.n_failed + ' échec(s)/skip(s)' : '')
                    + '</div>';
            } else {
                stopApplyPoll();
                if (prog.status === 'done') {
                    showToast('✓ Opération terminée'
                        + (prog.report ? ' — rapport logs/' + prog.report : ''),
                        'success');
                } else if (prog.status === 'error') {
                    showToast('✗ ' + (prog.error || 'erreur'), 'error');
                }
                renderApplyBlock(runId);
            }
        };
        tick();
        applyPollTimer = setInterval(tick, 2000);
    }
```

Note : dans `renderApplyBlock`, la ligne `const prog = p.state && p.progress;` est inutile — la retirer à l'implémentation (le commentaire « Reprise auto » suffit, `startApplyPollIfRunning` fait le travail).

- [ ] **Step 3: Ajouter le CSS**

En fin de `dashboard/static/style.css` :

```css
/* ── Refonte Apply/Execute ─────────────────────────────────────────── */
.rr-apply {
    border: 1px solid var(--border, #2a2f3a);
    border-radius: 8px;
    padding: 12px 14px;
    margin-bottom: 14px;
    background: var(--panel-2, rgba(255, 255, 255, 0.02));
}
.rr-apply-title { font-weight: 600; margin-bottom: 8px; }
.rr-apply-step { padding: 6px 0 6px 4px; }
.rr-apply-step.rr-apply-disabled { opacity: 0.45; pointer-events: none; }
.rr-apply-num { font-weight: 700; margin-right: 4px; }
.rr-apply-ok { color: #4caf50; font-weight: 600; margin-left: 6px; }
.rr-apply-btn { margin-left: 10px; }
.rr-apply-danger { color: #e57373; }
.rr-apply-bar {
    height: 8px; border-radius: 4px; overflow: hidden;
    background: rgba(255, 255, 255, 0.08); margin-top: 8px;
}
.rr-apply-fill {
    height: 100%; background: #4caf50; transition: width 0.4s ease;
}
```

- [ ] **Step 4: Vérification manuelle**

```bash
uv run python -m unittest tests.auto.test_refonte_apply -q   # rien cassé côté back
./klodo.sh dashboard   # → Taxonomie → 🤖 Refonte → sélectionner un run Phase B done
```

Vérifier : le bloc « Application » apparaît au-dessus des onglets Verdict/Arbre/Plan ; ① affiche les compteurs et le bouton Adopter ; ② est grisé tant que ① n'est pas fait ; les modals de confirmation s'affichent ; après adoption, « Restaurer la config » apparaît.

- [ ] **Step 5: Commit**

```bash
git add dashboard/templates/partials/agent_refonte_panel.html dashboard/static/style.css
git commit -m "feat(refonte): apply/execute UI block — 2-step stepper with confirm + poll"
```

---

### Task 10: Documentation

**Files:**
- Modify: `dashboard/CLAUDE.md`
- Modify: `lib/CLAUDE.md`

- [ ] **Step 1: `dashboard/CLAUDE.md`**

Dans la section qui décrit l'agent Refonte / les modules du dashboard, ajouter :

```markdown
### Apply/Execute (application d'une refonte Phase B)

- **`agent_refonte_apply.py`** — adoption de la config proposée (promotion
  des YAML `proposed/` vers la prod, snapshot `agent_backup`, mkdir des
  créations) puis exécution des déplacements physiques (projection figée,
  exclusion zone de doute via `select_move_rows`, journal
  `lib/move_journal.py`, skip stale/collision/error + rapport CSV dans
  `logs/rapport_apply_*.csv`). État par run dans
  `profiles/<p>/.cache/refonte/<run_id>/apply/{state,status}.json`.
- Gating : ① avant ② ; restore-config bloqué tant que les moves ne sont
  pas annulés ; un seul run adopté à la fois ; lock `.taxonomy.lock`
  posé pendant les moves (writes mappings bloqués).
- Routes : `GET …/apply/{run_id}/preview|status`,
  `POST …/apply/adopt|restore-config|execute|undo-moves`.
- UI : bloc « Application » (stepper 2 étapes) au-dessus des onglets
  Verdict/Arbre/Plan d'un run Phase B done.
```

Mettre à jour le compteur de tests du dashboard s'il est mentionné (le mesurer : `uv run python -m unittest tests.auto.test_refonte_apply -q 2>&1 | tail -2` et compter l'ensemble si un total global est cité).

- [ ] **Step 2: `lib/CLAUDE.md`**

Dans la section « Audit & journal (dashboard Rename) », ajouter après la ligne sur `rename_journal.py` :

```markdown
- **[move_journal.py](move_journal.py)** — même patron que rename_journal
  pour les MOVES inter-dossiers (refonte Apply/Execute) : JSONL
  `profile/.cache/move-journal.jsonl`, `append_move`, `undo_record`,
  `undo_batch` (échecs individuels skippés + rapportés, undos journalisés
  `undo-<batch_id>`)
```

- [ ] **Step 3: Commit**

```bash
git add dashboard/CLAUDE.md lib/CLAUDE.md
git commit -m "docs: document refonte apply/execute + move journal"
```

---

## Vérification end-to-end finale

1. `uv run python -m unittest discover tests/auto -q` → tous verts.
2. `uv run ruff check .` → clean.
3. `./klodo.sh dashboard` sur un profil de TEST (jamais le profil prod en premier) : run Phase B → Adopter (vérifier `tree.yaml` + backup dans `.cache/taxonomy-backups/`) → Exécuter (vérifier moves + `logs/rapport_apply_*.csv` + `move-journal.jsonl`) → Annuler les déplacements (fichiers revenus) → Restaurer la config (YAML revenus, dossiers vides supprimés).
4. Pendant l'exécution : vérifier que l'onglet Mappings refuse les writes (423).
5. PR vers `develop`, CI verte.

## Notes pour l'exécuteur

- **Jamais** de test sur `/Volumes/ExtSSD/BIBLIO` — tout en tmpdir.
- `taxonomy._locks` est un `defaultdict(threading.Lock)` — l'accès `taxonomy._locks[profile]` crée le lock à la volée.
- `TaxonomyError` expose `.status` (pas `.status_code`).
- Si un test HTTP échoue sur du cache : `taxonomy.reset_cache()` dans setUp/tearDown (déjà dans la fixture).
- Threads synchrones dans les tests HTTP : patcher `ara._spawn` (`mock.patch.object(ara, "_spawn", lambda target, args, name: target(*args))`). Ne JAMAIS patcher `threading.Thread` — le portal anyio du TestClient en crée et ça deadlock.
