# Apply global — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Une action profil-level qui synchronise physiquement la bibliothèque avec la config live (Mappings + Dédupli + Catégories), en réutilisant le moteur de déplacement de PR #176.

**Architecture:** On extrait le moteur de moves source-agnostique dans `dashboard/apply_engine.py` (réutilisé par refonte ET global). La projection live vient d'une fonction sœur de `reclassify_dryrun`. La canonicalisation (Dédupli) est corrigée dans `classify_combined`. Un nouveau module mince `dashboard/reclassify_apply.py` orchestre preview→execute→undo. UI greffée sur la modale « Voir ce qui bougerait » existante + carte Overview.

**Tech Stack:** Python 3.13 / FastAPI / unittest (TestClient, mock de `dashboard.data.get_project_root`) / vanilla JS.

**Branche:** `feature/reclassify-apply-global` (déjà créée depuis `develop`, contient le moteur de PR #176 ; spec commitée).

**Spec:** `docs/superpowers/specs/2026-06-13-reclassify-apply-global-design.md`.

**Conventions projet:** commits en anglais, doc/messages français, `uv run python -m unittest …`, jamais de test sur le vrai SSD (tout en tmpdir + vision_cache de fixture, zéro LLM).

---

## Contexte codebase (à lire avant Task 1)

- **Moteur PR #176** dans `dashboard/agent_refonte_apply.py` : `_run_moves(profile, run_id)` (lignes 345-444) lit `select_move_rows(_run_dir(...))` puis boucle (pré-vol `_safe_target_subdir`, stale/collision/error, `os.rename`, `move_journal.append_move`, rapport CSV `logs/rapport_apply_*.csv`, `_prune_empty_dirs`). `_execute_job` (447-459) wrappe en thread + libère le lock en `finally`. Helpers source-agnostiques : `_spawn` (314), `_logs_dir` (322), `_prune_empty_dirs` (328), `_safe_target_subdir` (184), `_target_path` (176), `_PROGRESS_EVERY` (311), exception `ApplyError`. État run-anchré : `read_state`/`write_state`/`_write_progress`/`_read_progress`/`_apply_dir` (81-119).
- **48 tests** dans `tests/auto/test_refonte_apply.py` réfèrent `ara._spawn`, `ara._run_moves`, `ara.ApplyError` (via `mock.patch.object`) → ces noms doivent rester dans le namespace de `agent_refonte_apply`.
- **`lib/move_journal.py`** : `generate_batch_id()`, `append_move(profile_dir, old, new, batch_id)`, `undo_batch(profile_dir, batch_id) -> {batch, n_undone, n_failed, failures}`. Déjà **par-profil** (`.cache/move-journal.jsonl`).
- **`reclassify_dryrun`** (`dashboard/taxonomy.py:870-1078`) : scan full read-only, `_process(item)` classe un fichier via `classify_combined(result, filename, mapping, classifier=..., llm_mapper=None, pdf_path=...)`, retourne aggregates + `sample_moves` (cappé). `_FILE_EXTS = ('.pdf', '.epub')`.
- **`classify_combined`** (`lib/classifier.py:346`) + **`classify_by_theme`** (`:44`) : `classify_by_theme` canonicalise rien aujourd'hui ; `classify_combined` appelle `classify_by_theme(cand_theme, theme_mapping)` (P1, ligne 410) et `classify_by_theme(theme, theme_mapping)` (P4, ligne 481).
- **`lib/theme_canon.py`** : `load_canon_table(profile) -> dict[str,str] | None`, `canonicalize(theme, canon_table) -> str` (fallback identité).
- **`dashboard/categories.py`** : `add_entry`/`update_entry`/`delete_entry` font `with _locks[profile]:` mais **pas** `_check_lock_free`. Exception locale `CategoriesError(message, status=400)` (attribut `.status`). N'importe pas `taxonomy`.
- **`_check_lock_free`** (`taxonomy.py:1629`) : lève `TaxonomyError(..., 423)` si `.cache/taxonomy.lock` présent.
- **Modale UI** : bouton `#tax-reclassify` + modale `#tax-reclassify-modal` / `#tax-reclassify-body` (`templates/taxonomy.html:53,562`). JS `openReclassifyModal` + `renderReclassifyBody` (`static/js/taxonomy.js:511-769`). Helpers `showToast` (389), `withBusy` (443), `showConfirm({title,body,confirmLabel,cancelLabel,variant})` (475).
- **Overview** : route `app.py:96-124`, template `templates/partials/overview_profile.html`, cartes via macro `kpi_card_big`, snapshot construit dans `dashboard/overview.py:build_profile_snapshot` (527).

## File structure

| Fichier | Action | Responsabilité |
| --- | --- | --- |
| `lib/classifier.py` | Modify | param `canon_table` sur `classify_by_theme` + `classify_combined` |
| `dashboard/apply_engine.py` | Create | moteur de moves source-agnostique (extrait de #176) |
| `dashboard/agent_refonte_apply.py` | Modify | importe/ré-exporte depuis apply_engine ; `_run_moves` délègue |
| `dashboard/taxonomy.py` | Modify | `_scan_and_classify` + `build_reclassify_projection` |
| `agents/refonte/simulator.py` | Modify | passe `canon_table` (cohérence) |
| `dashboard/reclassify_apply.py` | Create | état, hash config, preview/freeze, execute/undo global |
| `dashboard/app.py` | Modify | 5 routes `/api/taxonomy/reclassify/apply/*` |
| `dashboard/categories.py` | Modify | `_check_lock_free` sur les 3 writes |
| `dashboard/static/js/taxonomy.js` + `templates/taxonomy.html` | Modify | modale étendue preview→apply→undo |
| `dashboard/overview.py` + `templates/partials/overview_profile.html` | Modify | carte « Synchroniser » |
| `dashboard/CLAUDE.md`, `lib/CLAUDE.md` | Modify | doc |
| `tests/auto/test_classifier*.py`, `test_taxonomy.py`, `test_reclassify_apply.py` | Create/Modify | tests |

---

### Task 1: Canonicalisation dans `classify_by_theme` / `classify_combined`

**Files:**
- Modify: `lib/classifier.py:44-47` (signature `classify_by_theme`), `:346-353` (signature `classify_combined`)
- Test: `tests/auto/test_classifier_canon.py` (nouveau)

- [ ] **Step 1: Write the failing test**

Créer `tests/auto/test_classifier_canon.py` :

```python
#!/usr/bin/env python3
"""Canonicalisation des thèmes avant le lookup theme_mapping (P1)."""

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from lib.classifier import classify_by_theme, classify_combined  # noqa: E402


class TestCanonInClassifyByTheme(unittest.TestCase):
    def setUp(self):
        self.mapping = {"Deep Learning": "02-INFO/IA/Deep-Learning"}

    def test_variant_routes_via_canon_table(self):
        # "deep learning (variante)" n'est PAS dans le mapping, mais la table
        # de canon le ramène à "Deep Learning" → doit router.
        canon = {"dl variant": "Deep Learning"}
        self.assertEqual(
            classify_by_theme("dl variant", self.mapping, canon_table=canon),
            "02-INFO/IA/Deep-Learning")

    def test_no_canon_table_is_unchanged(self):
        self.assertIsNone(
            classify_by_theme("dl variant", self.mapping, canon_table=None))
        self.assertEqual(
            classify_by_theme("deep learning", self.mapping),
            "02-INFO/IA/Deep-Learning")  # exact match insensible casse, inchangé


class TestCanonInClassifyCombined(unittest.TestCase):
    def test_combined_threads_canon_to_p1(self):
        mapping = {"Deep Learning": "02-INFO/IA/Deep-Learning"}
        canon = {"dl variant": "Deep Learning"}
        result = {"themes": [{"theme": "dl variant", "confidence": 0.95}],
                  "confidence": 0.95}
        dest, _score, source = classify_combined(
            result, "x.pdf", mapping, canon_table=canon)
        self.assertEqual(dest, "02-INFO/IA/Deep-Learning")
        self.assertTrue(source.startswith("LLM"))

    def test_combined_without_canon_unchanged(self):
        mapping = {"Deep Learning": "02-INFO/IA/Deep-Learning"}
        result = {"themes": [{"theme": "dl variant", "confidence": 0.95}],
                  "confidence": 0.95}
        dest, _score, _source = classify_combined(result, "x.pdf", mapping)
        self.assertIsNone(dest)  # pas de canon → pas de match


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest tests.auto.test_classifier_canon -v`
Expected: FAIL — `classify_by_theme() got an unexpected keyword argument 'canon_table'`.

- [ ] **Step 3: Modify `classify_by_theme`**

Dans `lib/classifier.py`, signature ligne 44-47 → ajouter le param, et canonicaliser juste après le guard (ligne ~75) :

```python
def classify_by_theme(
    theme: str,
    theme_mapping: dict[str, str],
    canon_table: dict[str, str] | None = None,
) -> str | None:
```

Juste après `if not theme or not theme_mapping: return None` (ligne 75-76), insérer :

```python
    # Canonicalisation (Dédupli) : ramène les variantes orthographiques au
    # thème canonique AVANT le matching. Fallback identité si pas de table.
    from lib.theme_canon import canonicalize
    theme = canonicalize(theme, canon_table)
```

- [ ] **Step 4: Modify `classify_combined`**

Signature ligne 346-353 → ajouter `canon_table` :

```python
def classify_combined(
    vision_result: dict[str, object],
    filename: str,
    theme_mapping: dict[str, str],
    classifier: object | None = None,
    llm_mapper: object | None = None,
    pdf_path: str | None = None,
    canon_table: dict[str, str] | None = None,
) -> tuple[str | None, float, str]:
```

Puis passer `canon_table` à **chaque** appel `classify_by_theme` dans cette fonction :
- ligne 410 : `path = classify_by_theme(cand_theme, theme_mapping, canon_table=canon_table)`
- ligne 481 : `path = classify_by_theme(theme, theme_mapping, canon_table=canon_table)`

(Repérer tout autre appel `classify_by_theme(` dans `classify_combined` via `grep -n "classify_by_theme(" lib/classifier.py` et y ajouter `canon_table=canon_table`.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run python -m unittest tests.auto.test_classifier_canon -v`
Expected: PASS (4 tests).
Run aussi la non-régression classifier : `uv run python -m unittest tests.auto.test_classifier -q 2>&1 | tail -3` (si le fichier existe) → vert.

- [ ] **Step 6: Commit**

```bash
uv run ruff check lib/classifier.py tests/auto/test_classifier_canon.py
git add lib/classifier.py tests/auto/test_classifier_canon.py
git commit -m "feat(classifier): canonicalize themes before theme_mapping lookup"
```

---

### Task 2: Extraire le moteur source-agnostique dans `apply_engine.py`

**Files:**
- Create: `dashboard/apply_engine.py`
- Modify: `dashboard/agent_refonte_apply.py` (imports + `_run_moves` délègue)
- Test (filet de sécurité) : `tests/auto/test_refonte_apply.py` (existant, **doit rester vert**)

Refacto **behavior-preserving** : la suite refonte existante est le test. On extrait `ApplyError`, `_PROGRESS_EVERY`, les helpers FS/thread, et un nouveau `execute_move_batch` (corps de la boucle de `_run_moves`).

- [ ] **Step 1: Vérifier le point de départ (suite refonte verte)**

Run: `uv run python -m unittest tests.auto.test_refonte_apply -q 2>&1 | tail -3`
Expected: OK (compter le nombre, ~48-51).

- [ ] **Step 2: Créer `dashboard/apply_engine.py`**

```python
"""Moteur de déplacement de fichiers source-agnostique.

Partagé par l'apply refonte (agent_refonte_apply) et l'apply global
(reclassify_apply). Ne connaît NI run_id NI refonte : il reçoit
(target, liste de moves, profile_dir, callback de progression) et
exécute la boucle dangereuse une seule fois, ici.
"""

from __future__ import annotations

import csv
import os
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from dashboard import data, taxonomy
from lib import move_journal

_PROGRESS_EVERY = 50


class ApplyError(Exception):
    """Erreur transport (status HTTP porté par l'exception)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def spawn(target: Callable, args: tuple, name: str) -> None:
    """Lance ``target`` dans un thread daemon. Indirection volontaire :
    les tests patchent ``spawn`` pour exécuter en synchrone."""
    thread = threading.Thread(target=target, args=args, daemon=True, name=name)
    thread.start()


def logs_dir() -> Path:
    d = data.get_project_root() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def target_path(profile: str) -> Path:
    target = taxonomy._profile_target_path(profile)
    if target is None or not target.exists():
        raise ApplyError("target du profil introuvable (SSD non monté ?)", 500)
    return target


def safe_target_subdir(target: Path, rel: str) -> Path:
    """Résout ``rel`` sous le target et refuse toute évasion (../…)."""
    d = (target / rel).resolve()
    try:
        d.relative_to(target.resolve())
    except ValueError as exc:
        raise ApplyError(f"chemin hors du target : {rel!r}", 400) from exc
    return d


def prune_empty_dirs(target: Path, rel_folders: set[str]) -> None:
    """Supprime les dossiers sources devenus vides en remontant —
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


def execute_move_batch(
    target: Path,
    moves: list[dict],
    profile_dir: Path,
    on_progress: Callable[[dict], None],
) -> dict[str, Any]:
    """Boucle de déplacement (synchrone). Pré-vol anti-évasion → par
    fichier : garde fraîcheur (stale) → garde collision → os.rename →
    journal. Échec individuel = skip + rapport, jamais d'abort. Écrit le
    rapport CSV, prune les dossiers vides. ``on_progress`` reçoit les
    payloads de progression (sans clé 'op' — l'appelant l'ajoute).

    Crash mid-batch : trou d'1 record max (ordre move→journal) — voir doc
    refonte.
    """
    for m in moves:
        safe_target_subdir(target, m["rel_path"])
        safe_target_subdir(target, m["proposed_folder"])

    batch_id = move_journal.generate_batch_id()
    n_moved = n_failed = n_skipped = 0
    report_rows: list[dict[str, str]] = []
    source_folders: set[str] = set()
    n_total = len(moves)
    on_progress({"status": "running", "n_done": 0, "n_total": n_total,
                 "n_failed": 0, "n_skipped": 0, "error": None})

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
        report_rows.append({"rel_path": rel, "old": str(old), "new": str(new),
                            "status": status, "detail": detail})
        if i % _PROGRESS_EVERY == 0:
            on_progress({"status": "running", "n_done": i, "n_total": n_total,
                         "n_failed": n_failed, "n_skipped": n_skipped,
                         "error": None})

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = logs_dir() / f"rapport_apply_{ts}.csv"
    with report_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["rel_path", "old", "new", "status", "detail"])
        writer.writeheader()
        writer.writerows(report_rows)

    prune_empty_dirs(target, source_folders)
    return {"n_moved": n_moved, "n_failed": n_failed, "n_skipped": n_skipped,
            "n_total": n_total, "report": report_path.name, "batch_id": batch_id}
```

- [ ] **Step 3: Rebrancher `agent_refonte_apply.py`**

En tête du fichier (bloc d'imports ~13-29), ajouter et **aliaser** pour la compat des tests :

```python
from dashboard.apply_engine import (
    ApplyError,
    execute_move_batch,
    logs_dir as _logs_dir,
    prune_empty_dirs as _prune_empty_dirs,
    safe_target_subdir as _safe_target_subdir,
    spawn as _spawn,
    target_path as _target_path,
)
from dashboard.apply_engine import _PROGRESS_EVERY  # noqa: F401  (compat)
```

Puis **supprimer** de `agent_refonte_apply.py` les définitions désormais dans le moteur : la classe `ApplyError` (ligne 34-39), `_PROGRESS_EVERY` (311), `_spawn` (314-319), `_logs_dir` (322-325), `_prune_empty_dirs` (328-342), `_safe_target_subdir` (184-193), `_target_path` (176-181). (Les noms restent accessibles via les alias d'import.) **Garder** `_validate_run_id`/`_RUN_ID_RE` (45/42, spécifiques refonte). NB : l'import `ApplyError` (sans alias) écrase proprement l'ancienne définition supprimée — `_validate_run_id` et les autres continuent de lever `ApplyError`.

Remplacer le **corps** de `_run_moves` (345-444) par une délégation :

```python
def _run_moves(profile: str, run_id: str) -> dict[str, Any]:
    """Construit les moves du run figé puis délègue au moteur partagé ;
    persiste l'état + la progression finale (anchrés run_id)."""
    target = _target_path(profile)
    moves = select_move_rows(_run_dir(profile, run_id))["moves"]
    result = execute_move_batch(
        target, moves, _profile_dir(profile),
        on_progress=lambda p: _write_progress(profile, run_id, {**p, "op": "execute"}))
    write_state(profile, run_id, {
        "executed": True,
        "executed_at": datetime.now(UTC).isoformat(),
        "move_batch_id": result["batch_id"],
        "n_moved": result["n_moved"],
        "n_failed": result["n_failed"],
        "n_skipped": result["n_skipped"],
        "rolled_back_moves": False,
    })
    _write_progress(profile, run_id, {
        "op": "execute", "status": "done",
        "n_done": result["n_total"], "n_total": result["n_total"],
        "n_failed": result["n_failed"], "n_skipped": result["n_skipped"],
        "error": None, "report": result["report"],
    })
    return result
```

Vérifier que les imports devenus inutiles dans `agent_refonte_apply.py` (`csv`, `os`, `threading`, `Callable`, `move_journal` si plus utilisés ailleurs) sont retirés — lancer ruff pour les détecter.

- [ ] **Step 4: Run the refonte suite (filet de sécurité)**

Run: `uv run python -m unittest tests.auto.test_refonte_apply -v 2>&1 | tail -6`
Expected: **même nombre de tests, tous verts** (comportement identique).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check dashboard/apply_engine.py dashboard/agent_refonte_apply.py
git add dashboard/apply_engine.py dashboard/agent_refonte_apply.py
git commit -m "refactor(apply): extract source-agnostic move engine into apply_engine"
```

---

### Task 3: `build_reclassify_projection` (projection live complète + canon)

**Files:**
- Modify: `dashboard/taxonomy.py` (extraire `_scan_and_classify`, refactor `reclassify_dryrun`, ajouter `build_reclassify_projection`)
- Modify: `agents/refonte/simulator.py` (passer `canon_table`)
- Test: `tests/auto/test_taxonomy.py` (nouvelle classe)

- [ ] **Step 1: Write the failing test**

Dans `tests/auto/test_taxonomy.py`, ajouter (adapter les imports `tempfile/shutil/json/yaml/Path/mock` au style du fichier) :

```python
class TestBuildReclassifyProjection(unittest.TestCase):
    """Projection live complète : matérialise tous les moves, applique
    la canonicalisation, sépare P1/P2."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="klodo-proj-")
        self.root = Path(self.tmp)
        self.prof = self.root / "profiles" / "default"
        self.target = self.root / "BIBLIO"
        self.prof.mkdir(parents=True)
        (self.prof / "profile.yaml").write_text(
            yaml.safe_dump({"target": str(self.target),
                            "llm": {"model": "M"}, "defaults": {"pages": 2}}),
            encoding="utf-8")
        (self.prof / "theme_mapping.yaml").write_text(
            yaml.safe_dump({"Deep Learning": "B/DL"}), encoding="utf-8")
        # Fichiers physiques
        for rel in ("A/a.pdf", "A/b.pdf"):
            p = self.target / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"%PDF-1.4 x")
        # theme-canon : "dl variant" → "Deep Learning"
        cache_dir = self.prof / ".cache"
        cache_dir.mkdir(exist_ok=True)
        (cache_dir / "theme-canon.json").write_text(
            json.dumps({"mapping": {"dl variant": "Deep Learning"}}),
            encoding="utf-8")
        # vision_cache : a.pdf → "dl variant" (canon→DL→B/DL), b.pdf → inconnu
        import lib.vision_cache as vc
        cache = {}
        for rel, theme in (("A/a.pdf", "dl variant"), ("A/b.pdf", "zzz unknown")):
            key = vc.compute_cache_key(str(self.target / rel), model="M", n_pages=2)
            cache[key] = {"result": {"themes": [{"theme": theme, "confidence": 0.95}],
                                     "confidence": 0.95}}
        (self.prof / ".cache" / "vision_cache.json").write_text(
            json.dumps(cache), encoding="utf-8")
        self.patch = mock.patch("dashboard.data.get_project_root",
                                return_value=self.root)
        self.patch.start()
        taxonomy.reset_cache()

    def tearDown(self):
        self.patch.stop()
        taxonomy.reset_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_canonicalization_routes_variant(self):
        moves = taxonomy.build_reclassify_projection("default", include_keyword=False)
        rels = {m["rel_path"]: m for m in moves}
        # a.pdf : "dl variant" canon→ "Deep Learning" → B/DL, current A → move
        self.assertIn("A/a.pdf", rels)
        self.assertEqual(rels["A/a.pdf"]["proposed_folder"], "B/DL")
        self.assertEqual(rels["A/a.pdf"]["signal"], "p1")
        # b.pdf : thème inconnu, pas de prédiction → pas dans les moves
        self.assertNotIn("A/b.pdf", rels)

    def test_keyword_excluded_unless_opted_in(self):
        # categories.yaml route b.pdf par mot-clé "unknown" → P2
        (self.prof / "categories.yaml").write_text(
            yaml.safe_dump({"sci": [{"folder": "B/KW", "priorite": 5,
                                     "mots_cles": ["unknown"]}]}),
            encoding="utf-8")
        taxonomy.reset_cache()
        no_kw = taxonomy.build_reclassify_projection("default", include_keyword=False)
        with_kw = taxonomy.build_reclassify_projection("default", include_keyword=True)
        self.assertNotIn("A/b.pdf", {m["rel_path"] for m in no_kw})
        kw_rels = {m["rel_path"]: m for m in with_kw}
        self.assertIn("A/b.pdf", kw_rels)
        self.assertEqual(kw_rels["A/b.pdf"]["signal"], "p2")

    def test_returns_empty_without_target(self):
        shutil.rmtree(self.target)
        taxonomy.reset_cache()
        self.assertEqual(
            taxonomy.build_reclassify_projection("default", include_keyword=False), [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest tests.auto.test_taxonomy.TestBuildReclassifyProjection -v`
Expected: FAIL — `module 'dashboard.taxonomy' has no attribute 'build_reclassify_projection'`.

- [ ] **Step 3: Extraire `_scan_and_classify` + refactor `reclassify_dryrun`**

Dans `dashboard/taxonomy.py`, ajouter une fonction qui contient l'init + scan + `_process` (lignes 905-995 actuelles), en **passant la canon table** à `classify_combined` :

```python
def _scan_and_classify(profile: str, *, include_step2: bool) -> list[dict]:
    """Scan full read-only de la bibliothèque + classement par fichier
    (P1+P2, pas de LLM Mapper). Applique la canonicalisation (Dédupli).
    Retourne la liste brute des résultats par fichier."""
    from concurrent.futures import ThreadPoolExecutor

    from lib.classifier import classify_combined, load_keyword_classifier
    from lib.theme_canon import load_canon_table

    target = _profile_target_path(profile)
    if target is None or not target.exists():
        return []

    mapping = _load_mapping(profile)
    canon_table = load_canon_table(profile)

    classifier = None
    if include_step2:
        cat_path = _profile_dir(profile) / "categories.yaml"
        if cat_path.exists():
            classifier = load_keyword_classifier(str(cat_path))

    cache_path = _vision_cache_path(profile)
    cache: dict = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cache = {}
    if not isinstance(cache, dict):
        cache = {}

    cfg = _load_profile_yaml(profile)
    model = (cfg.get("llm") or {}).get("model") or "Qwen/Qwen3-VL-32B-Instruct"
    n_pages = int((cfg.get("defaults") or {}).get("pages") or 2)

    target_str = str(target)
    file_list: list[tuple[str, str, str]] = []
    for root, dirs, files in os.walk(target_str):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.startswith(".") or not f.lower().endswith(_FILE_EXTS):
                continue
            abs_path = os.path.join(root, f)
            try:
                rel = os.path.relpath(abs_path, target_str).replace("\\", "/")
            except ValueError:
                continue
            file_list.append((abs_path, rel, f))

    def _process(item: tuple[str, str, str]) -> dict:
        abs_path, rel, filename = item
        i = rel.rfind("/")
        current_folder = rel[:i] if i >= 0 else ""
        key = vision_cache.compute_cache_key(abs_path, model=model, n_pages=n_pages)
        result = vision_cache.lookup(cache, key) if key else None
        if not isinstance(result, dict):
            result = {}
        top_theme = ""
        top_conf = 0.0
        themes_arr = result.get("themes")
        if isinstance(themes_arr, list) and themes_arr:
            best = max((t for t in themes_arr if isinstance(t, dict)),
                       key=lambda t: float(t.get("confidence") or 0.0), default=None)
            if best is not None:
                top_theme = str(best.get("theme") or "")
                top_conf = float(best.get("confidence") or 0.0)
        elif result.get("theme"):
            top_theme = str(result.get("theme") or "")
            top_conf = float(result.get("confidence") or 0.0)
        dest, score, source = classify_combined(
            result, filename, mapping,
            classifier=classifier, llm_mapper=None, pdf_path=abs_path,
            canon_table=canon_table)
        return {"rel_path": rel, "current_folder": current_folder,
                "predicted_folder": dest, "source": source,
                "score": float(score) if score else 0.0,
                "top_theme": top_theme, "top_confidence": round(top_conf, 3)}

    with ThreadPoolExecutor(max_workers=8) as ex:
        return list(ex.map(_process, file_list))
```

Puis **refactorer `reclassify_dryrun`** pour qu'il appelle ce helper au lieu de dupliquer l'init/scan/_process : remplacer son bloc init+scan+_process+ThreadPoolExecutor (lignes ~905-995) par :

```python
    processed = _scan_and_classify(profile, include_step2=include_step2)
    if not processed and (_profile_target_path(profile) is None
                          or not _profile_target_path(profile).exists()):
        return _empty_dryrun(include_step2)
```

(Le reste de `reclassify_dryrun` — l'agrégation `stats`/`by_destination`/`sample_moves` à partir de `processed` — reste inchangé.)

- [ ] **Step 4: Ajouter `build_reclassify_projection`**

Après `reclassify_dryrun` :

```python
def build_reclassify_projection(profile: str, include_keyword: bool) -> list[dict]:
    """Liste COMPLÈTE des moves de la config live (P1 + P2 optionnel).

    Un move = fichier `changed` (current != predicted, predicted non vide)
    et hors no-prediction. ``signal`` = 'p1' (theme_mapping) ou 'p2'
    (mot-clé). P2 inclus seulement si ``include_keyword``. Déterministe
    (pas de LLM Mapper). Réutilise _scan_and_classify (canon appliqué).
    """
    processed = _scan_and_classify(profile, include_step2=include_keyword)
    moves: list[dict] = []
    for r in processed:
        dest = r["predicted_folder"]
        if not dest or dest == r["current_folder"]:
            continue
        signal = "p2" if str(r["source"]).startswith("Keyword") else "p1"
        if signal == "p2" and not include_keyword:
            continue
        moves.append({
            "rel_path": r["rel_path"],
            "current_folder": r["current_folder"],
            "proposed_folder": dest,
            "source": r["source"],
            "top_theme": r["top_theme"],
            "confidence": r["top_confidence"],
            "signal": signal,
        })
    return moves
```

- [ ] **Step 5: Wire `simulator.py` (cohérence canon)**

Dans `agents/refonte/simulator.py`, à l'appel `classify_combined(...)` (~ligne 159-165), charger et passer la canon table. En tête (avec les autres imports) ajouter `from lib.theme_canon import load_canon_table`, charger une fois avant la boucle `_process` (`canon_table = load_canon_table(profile)`), et ajouter `canon_table=canon_table` à l'appel `classify_combined`. (Repérer le nom exact de la variable profil dans la fonction via lecture du fichier.)

- [ ] **Step 6: Run tests**

Run: `uv run python -m unittest tests.auto.test_taxonomy.TestBuildReclassifyProjection -v` → PASS (3 tests).
Run: `uv run python -m unittest tests.auto.test_taxonomy tests.auto.test_agent_refonte_simulator -q 2>&1 | tail -3` → non-régression verte.

- [ ] **Step 7: Smoke test pipeline (RÈGLE PROJET — pipeline touché)**

Smoke test 3-5 PDFs réels que la classification + canon fonctionnent encore (pas de None/vide) :

```bash
uv run python -c "
from dashboard import taxonomy
# Profil réel avec vision_cache déjà peuplé :
moves = taxonomy.build_reclassify_projection('default', include_keyword=False)
print('n_moves =', len(moves))
for m in moves[:5]:
    print(m['rel_path'], '→', m['proposed_folder'], '|', m['signal'], m['top_theme'])
"
```

Vérifier que ça tourne sans exception et que `proposed_folder` n'est pas systématiquement vide. (Si le profil `default` n'a pas de target monté, tester sur un profil dont le SSD est monté.)

- [ ] **Step 8: Lint + commit**

```bash
uv run ruff check dashboard/taxonomy.py agents/refonte/simulator.py tests/auto/test_taxonomy.py
git add dashboard/taxonomy.py agents/refonte/simulator.py tests/auto/test_taxonomy.py
git commit -m "feat(taxonomy): build_reclassify_projection — full live projection with canon"
```

---

### Task 4: Module `reclassify_apply.py` (état, hash, preview, execute, undo)

**Files:**
- Create: `dashboard/reclassify_apply.py`
- Test: `tests/auto/test_reclassify_apply.py` (nouveau)

- [ ] **Step 1: Write the failing tests**

Créer `tests/auto/test_reclassify_apply.py` :

```python
#!/usr/bin/env python3
"""Apply global — état, hash config, preview figé, execute, undo."""

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

from dashboard import reclassify_apply as rca  # noqa: E402
from dashboard import taxonomy  # noqa: E402


class GlobalApplyBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="klodo-gapply-")
        self.root = Path(self.tmp)
        self.prof = self.root / "profiles" / "default"
        self.target = self.root / "BIBLIO"
        self.prof.mkdir(parents=True)
        (self.prof / "profile.yaml").write_text(
            yaml.safe_dump({"target": str(self.target),
                            "llm": {"model": "M"}, "defaults": {"pages": 2}}),
            encoding="utf-8")
        (self.prof / "theme_mapping.yaml").write_text(
            yaml.safe_dump({"Deep Learning": "B/DL"}), encoding="utf-8")
        (self.prof / "tree.yaml").write_text(
            yaml.safe_dump({"folders": ["A", "B/DL"]}), encoding="utf-8")
        for rel in ("A/a.pdf", "A/b.pdf"):
            p = self.target / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"%PDF-1.4 x")
        cache_dir = self.prof / ".cache"
        cache_dir.mkdir(exist_ok=True)
        import lib.vision_cache as vc
        cache = {}
        for rel, theme in (("A/a.pdf", "Deep Learning"), ("A/b.pdf", "zzz")):
            key = vc.compute_cache_key(str(self.target / rel), model="M", n_pages=2)
            cache[key] = {"result": {"themes": [{"theme": theme, "confidence": 0.95}],
                                     "confidence": 0.95}}
        (cache_dir / "vision_cache.json").write_text(json.dumps(cache), encoding="utf-8")
        self.patch = mock.patch("dashboard.data.get_project_root", return_value=self.root)
        self.patch.start()
        taxonomy.reset_cache()

    def tearDown(self):
        self.patch.stop()
        taxonomy.reset_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestStateAndPending(GlobalApplyBase):
    def test_default_state(self):
        st = rca.read_state("default")
        self.assertFalse(st["executed"])
        self.assertIsNone(st["move_batch_id"])

    def test_config_hash_changes_with_mapping(self):
        h1 = rca._config_hash("default")
        (self.prof / "theme_mapping.yaml").write_text(
            yaml.safe_dump({"Deep Learning": "B/DL", "X": "A"}), encoding="utf-8")
        self.assertNotEqual(h1, rca._config_hash("default"))

    def test_pending_true_until_applied(self):
        self.assertTrue(rca.is_pending("default"))  # jamais appliqué
        rca.write_state("default", {"last_applied": {"ts": "t",
                        "config_hash": rca._config_hash("default")}})
        self.assertFalse(rca.is_pending("default"))


class TestPreviewAndExecute(GlobalApplyBase):
    def test_preview_freezes_projection(self):
        p = rca.build_preview("default", include_keyword=False)
        self.assertEqual(p["n_moves"], 1)   # a.pdf → B/DL
        self.assertEqual(p["n_p1"], 1)
        self.assertEqual(p["n_p2"], 0)
        # CSV figé écrit
        self.assertTrue((self.prof / ".cache" / "reclassify" / "apply"
                         / "projection.csv").exists())

    def test_run_moves_global_executes_frozen(self):
        rca.build_preview("default", include_keyword=False)
        result = rca._run_moves_global("default")
        self.assertEqual(result["n_moved"], 1)
        self.assertTrue((self.target / "B" / "DL" / "a.pdf").exists())
        self.assertFalse((self.target / "A" / "a.pdf").exists())
        st = rca.read_state("default")
        self.assertTrue(st["executed"])
        self.assertIsNotNone(st["move_batch_id"])
        self.assertIn("config_hash", st["last_applied"])
        # rapport
        self.assertTrue(list((self.root / "logs").glob("rapport_apply_*.csv")))

    def test_execute_without_preview_409(self):
        with self.assertRaises(rca.ApplyError) as ctx:
            rca._run_moves_global("default")   # pas de projection figée
        self.assertEqual(ctx.exception.status, 409)

    def test_start_execute_gating_lock(self):
        rca.build_preview("default", include_keyword=False)
        (self.prof / ".cache" / "taxonomy.lock").write_text("busy")
        with self.assertRaises(rca.ApplyError) as ctx:
            rca.start_execute("default")
        self.assertEqual(ctx.exception.status, 423)


class TestUndoGlobal(GlobalApplyBase):
    def test_undo_restores(self):
        rca.build_preview("default", include_keyword=False)
        rca._run_moves_global("default")
        result = rca._run_undo_global("default")
        self.assertEqual(result["n_undone"], 1)
        self.assertTrue((self.target / "A" / "a.pdf").exists())
        st = rca.read_state("default")
        self.assertFalse(st["executed"])
        self.assertTrue(st["rolled_back_moves"])

    def test_undo_nothing_409(self):
        with self.assertRaises(rca.ApplyError) as ctx:
            rca._run_undo_global("default")
        self.assertEqual(ctx.exception.status, 409)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest tests.auto.test_reclassify_apply -v`
Expected: FAIL — `No module named 'dashboard.reclassify_apply'`.

- [ ] **Step 3: Create `dashboard/reclassify_apply.py`**

```python
"""Apply global — synchroniser la bibliothèque avec la config live.

Spec : docs/superpowers/specs/2026-06-13-reclassify-apply-global-design.md

Couche B uniquement (pas d'adoption — la config est déjà live) :
  preview  → fige la projection (build_reclassify_projection) en CSV
  execute  → rejoue le CSV figé via le moteur partagé apply_engine
  undo     → move_journal.undo_batch
État global unique par profil (pas de run_id).
"""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dashboard import apply_engine, data, taxonomy
from dashboard.apply_engine import ApplyError  # ré-export
from lib import move_journal

_STATE_DEFAULTS: dict[str, Any] = {
    "executed": False,
    "executed_at": None,
    "move_batch_id": None,
    "include_keyword": False,
    "n_moved": 0,
    "n_failed": 0,
    "n_skipped": 0,
    "rolled_back_moves": False,
    "last_applied": None,   # {ts, config_hash}
}

_CONFIG_FILES = ("theme_mapping.yaml", "categories.yaml", "tree.yaml")


def _profile_dir(profile: str) -> Path:
    return data.get_project_root() / "profiles" / profile


def _apply_dir(profile: str) -> Path:
    return _profile_dir(profile) / ".cache" / "reclassify" / "apply"


def _projection_path(profile: str) -> Path:
    return _apply_dir(profile) / "projection.csv"


# ─── État ────────────────────────────────────────────────────────────────


def read_state(profile: str) -> dict[str, Any]:
    path = _apply_dir(profile) / "state.json"
    state = dict(_STATE_DEFAULTS)
    if path.exists():
        try:
            state.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    return state


def write_state(profile: str, updates: dict[str, Any]) -> dict[str, Any]:
    state = read_state(profile)
    state.update(updates)
    apply_dir = _apply_dir(profile)
    apply_dir.mkdir(parents=True, exist_ok=True)
    (apply_dir / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


def _write_progress(profile: str, payload: dict[str, Any]) -> None:
    apply_dir = _apply_dir(profile)
    apply_dir.mkdir(parents=True, exist_ok=True)
    (apply_dir / "status.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _read_progress(profile: str) -> dict[str, Any] | None:
    path = _apply_dir(profile) / "status.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def get_status(profile: str) -> dict[str, Any]:
    return {"state": read_state(profile), "progress": _read_progress(profile)}


# ─── Hash de config (détection « en attente ») ───────────────────────────


def _config_hash(profile: str) -> str:
    h = hashlib.sha256()
    for fname in _CONFIG_FILES:
        p = _profile_dir(profile) / fname
        h.update(p.read_bytes() if p.exists() else b"")
        h.update(b"\0")
    canon = _profile_dir(profile) / ".cache" / "theme-canon.json"
    h.update(canon.read_bytes() if canon.exists() else b"")
    return h.hexdigest()


def is_pending(profile: str) -> bool:
    """True si la config a changé depuis le dernier apply (instantané)."""
    last = read_state(profile).get("last_applied") or {}
    return last.get("config_hash") != _config_hash(profile)


# ─── Preview (fige la projection) ────────────────────────────────────────


def build_preview(profile: str, include_keyword: bool) -> dict[str, Any]:
    """Calcule la projection live, l'écrit figée en CSV, retourne les
    compteurs pour la modale."""
    try:
        taxonomy._check_lock_free(profile)
    except taxonomy.TaxonomyError as exc:
        raise ApplyError(str(exc), 423) from exc
    apply_engine.target_path(profile)  # 400 si SSD absent
    moves = taxonomy.build_reclassify_projection(profile, include_keyword)
    apply_dir = _apply_dir(profile)
    apply_dir.mkdir(parents=True, exist_ok=True)
    with _projection_path(profile).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "rel_path", "current_folder", "proposed_folder", "source",
            "top_theme", "confidence", "signal"])
        writer.writeheader()
        writer.writerows(moves)
    write_state(profile, {"include_keyword": include_keyword})
    n_p1 = sum(1 for m in moves if m["signal"] == "p1")
    n_p2 = sum(1 for m in moves if m["signal"] == "p2")
    from collections import Counter
    dest_counts = Counter(m["proposed_folder"] for m in moves)
    return {
        "n_moves": len(moves), "n_p1": n_p1, "n_p2": n_p2,
        "include_keyword": include_keyword,
        "top_destinations": [{"folder": d, "n": n}
                             for d, n in dest_counts.most_common(10)],
        "state": read_state(profile),
        "pending": is_pending(profile),
    }


def _read_frozen_moves(profile: str) -> list[dict]:
    path = _projection_path(profile)
    if not path.exists():
        raise ApplyError("aucune projection figée — lance d'abord le preview", 409)
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ─── Execute ─────────────────────────────────────────────────────────────


def _run_moves_global(profile: str) -> dict[str, Any]:
    moves = _read_frozen_moves(profile)   # 409 si pas de preview
    target = apply_engine.target_path(profile)
    result = apply_engine.execute_move_batch(
        target, moves, _profile_dir(profile),
        on_progress=lambda p: _write_progress(profile, {**p, "op": "execute"}))
    write_state(profile, {
        "executed": True,
        "executed_at": datetime.now(UTC).isoformat(),
        "move_batch_id": result["batch_id"],
        "n_moved": result["n_moved"],
        "n_failed": result["n_failed"],
        "n_skipped": result["n_skipped"],
        "rolled_back_moves": False,
        "last_applied": {"ts": datetime.now(UTC).isoformat(),
                         "config_hash": _config_hash(profile)},
    })
    _write_progress(profile, {
        "op": "execute", "status": "done",
        "n_done": result["n_total"], "n_total": result["n_total"],
        "n_failed": result["n_failed"], "n_skipped": result["n_skipped"],
        "error": None, "report": result["report"]})
    return result


def _execute_job(profile: str) -> None:
    try:
        _run_moves_global(profile)
    except Exception as exc:  # noqa: BLE001 — frontière de thread
        _write_progress(profile, {"op": "execute", "status": "error",
                        "n_done": 0, "n_total": 0, "n_failed": 0,
                        "n_skipped": 0, "error": str(exc)})
    finally:
        taxonomy._lock_file(profile).unlink(missing_ok=True)
        taxonomy.reset_cache(profile)


def start_execute(profile: str) -> dict[str, Any]:
    with taxonomy._locks[profile]:
        progress = _read_progress(profile)
        if progress and progress.get("status") == "running":
            raise ApplyError("une opération est déjà en cours", 409)
        if not _projection_path(profile).exists():
            raise ApplyError("aucune projection figée — lance d'abord le preview", 409)
        try:
            taxonomy._check_lock_free(profile)
        except taxonomy.TaxonomyError as exc:
            raise ApplyError(str(exc), 423) from exc
        apply_engine.target_path(profile)
        lock = taxonomy._lock_file(profile)
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("reclassify-apply\n", encoding="utf-8")
        apply_engine.spawn(_execute_job, (profile,), f"reclassify-apply-{profile}")
        return {"ok": True, "profile": profile}


# ─── Undo ────────────────────────────────────────────────────────────────


def _run_undo_global(profile: str) -> dict[str, Any]:
    state = read_state(profile)
    if not state["executed"]:
        raise ApplyError("aucun déplacement à annuler", 409)
    batch_id = state.get("move_batch_id")
    if not batch_id:
        raise ApplyError("aucun batch de moves enregistré", 409)
    _write_progress(profile, {"op": "undo", "status": "running",
                    "n_done": 0, "n_total": state.get("n_moved", 0),
                    "n_failed": 0, "n_skipped": 0, "error": None})
    result = move_journal.undo_batch(_profile_dir(profile), batch_id)
    write_state(profile, {"executed": False, "rolled_back_moves": True})
    _write_progress(profile, {"op": "undo", "status": "done",
                    "n_done": result["n_undone"],
                    "n_total": result["n_undone"] + result["n_failed"],
                    "n_failed": result["n_failed"], "n_skipped": 0, "error": None})
    return result


def _undo_job(profile: str) -> None:
    try:
        _run_undo_global(profile)
    except Exception as exc:  # noqa: BLE001
        _write_progress(profile, {"op": "undo", "status": "error",
                        "n_done": 0, "n_total": 0, "n_failed": 0,
                        "n_skipped": 0, "error": str(exc)})
    finally:
        taxonomy._lock_file(profile).unlink(missing_ok=True)
        taxonomy.reset_cache(profile)


def start_undo(profile: str) -> dict[str, Any]:
    with taxonomy._locks[profile]:
        progress = _read_progress(profile)
        if progress and progress.get("status") == "running":
            raise ApplyError("une opération est déjà en cours", 409)
        if not read_state(profile)["executed"]:
            raise ApplyError("aucun déplacement à annuler", 409)
        try:
            taxonomy._check_lock_free(profile)
        except taxonomy.TaxonomyError as exc:
            raise ApplyError(str(exc), 423) from exc
        lock = taxonomy._lock_file(profile)
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("reclassify-apply-undo\n", encoding="utf-8")
        apply_engine.spawn(_undo_job, (profile,), f"reclassify-undo-{profile}")
        return {"ok": True, "profile": profile}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest tests.auto.test_reclassify_apply -v`
Expected: PASS (toutes les classes).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check dashboard/reclassify_apply.py tests/auto/test_reclassify_apply.py
git add dashboard/reclassify_apply.py tests/auto/test_reclassify_apply.py
git commit -m "feat(reclassify): global apply module — preview, execute, undo, config-hash"
```

---

### Task 5: Routes API `/api/taxonomy/reclassify/apply/*`

**Files:**
- Modify: `dashboard/app.py` (après la route `/api/taxonomy/reclassify/dryrun`, ~ligne 2024)
- Test: `tests/auto/test_reclassify_apply.py`

- [ ] **Step 1: Write the failing tests**

Ajouter dans `tests/auto/test_reclassify_apply.py` (en tête : `from fastapi.testclient import TestClient` + `from dashboard.app import app`, style `test_agent_refonte_results_api.py`) :

```python
class TestGlobalApplyEndpoints(GlobalApplyBase):
    def setUp(self):
        super().setUp()
        (self.root / "logs").mkdir(exist_ok=True)
        self.client = TestClient(app)

    def test_preview_endpoint(self):
        r = self.client.get("/api/taxonomy/reclassify/apply/preview?profile=default")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["n_moves"], 1)

    def test_pending_endpoint(self):
        r = self.client.get("/api/taxonomy/reclassify/apply/pending?profile=default")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["pending"])

    def test_execute_requires_preview_409(self):
        r = self.client.post("/api/taxonomy/reclassify/apply/execute",
                             json={"profile": "default"})
        self.assertEqual(r.status_code, 409)

    def test_full_flow(self):
        self.client.get("/api/taxonomy/reclassify/apply/preview?profile=default")
        sync = lambda target, args, name: target(*args)  # noqa: E731
        with mock.patch.object(rca.apply_engine, "spawn", sync):
            r = self.client.post("/api/taxonomy/reclassify/apply/execute",
                                 json={"profile": "default"})
        self.assertEqual(r.status_code, 200)
        s = self.client.get("/api/taxonomy/reclassify/apply/status?profile=default")
        self.assertTrue(s.json()["state"]["executed"])
        with mock.patch.object(rca.apply_engine, "spawn", sync):
            u = self.client.post("/api/taxonomy/reclassify/apply/undo",
                                 json={"profile": "default"})
        self.assertEqual(u.status_code, 200)

    def test_execute_missing_profile_400(self):
        r = self.client.post("/api/taxonomy/reclassify/apply/execute", json={})
        self.assertEqual(r.status_code, 400)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest tests.auto.test_reclassify_apply.TestGlobalApplyEndpoints -v`
Expected: FAIL — 404 sur les routes.

- [ ] **Step 3: Add the routes**

En tête de `dashboard/app.py` (avec les imports `from dashboard import ...`) : `from dashboard import reclassify_apply`. Puis après la route dryrun (~2024) :

```python
def _rca_error(exc: reclassify_apply.ApplyError):
    from fastapi.responses import JSONResponse
    return JSONResponse({"error": str(exc)}, status_code=exc.status)


@app.get("/api/taxonomy/reclassify/apply/preview")
async def api_reclassify_apply_preview(profile: str, keyword: bool = False):
    from fastapi.responses import JSONResponse
    try:
        return JSONResponse(reclassify_apply.build_preview(profile, keyword))
    except reclassify_apply.ApplyError as exc:
        return _rca_error(exc)


@app.get("/api/taxonomy/reclassify/apply/pending")
async def api_reclassify_apply_pending(profile: str):
    from fastapi.responses import JSONResponse
    return JSONResponse({"pending": reclassify_apply.is_pending(profile)})


@app.get("/api/taxonomy/reclassify/apply/status")
async def api_reclassify_apply_status(profile: str):
    from fastapi.responses import JSONResponse
    return JSONResponse(reclassify_apply.get_status(profile))


@app.post("/api/taxonomy/reclassify/apply/execute")
async def api_reclassify_apply_execute(request: Request):
    from fastapi.responses import JSONResponse
    body = await request.json()
    profile = (body.get("profile") or "").strip()
    if not profile:
        return JSONResponse({"error": "profile requis"}, status_code=400)
    try:
        return JSONResponse(reclassify_apply.start_execute(profile))
    except reclassify_apply.ApplyError as exc:
        return _rca_error(exc)


@app.post("/api/taxonomy/reclassify/apply/undo")
async def api_reclassify_apply_undo(request: Request):
    from fastapi.responses import JSONResponse
    body = await request.json()
    profile = (body.get("profile") or "").strip()
    if not profile:
        return JSONResponse({"error": "profile requis"}, status_code=400)
    try:
        return JSONResponse(reclassify_apply.start_undo(profile))
    except reclassify_apply.ApplyError as exc:
        return _rca_error(exc)
```

- [ ] **Step 4: Run tests + full suite**

Run: `uv run python -m unittest tests.auto.test_reclassify_apply -v 2>&1 | tail -6` → PASS.
Run: `uv run python -m unittest discover tests/auto -q 2>&1 | tail -3` → tout vert.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check dashboard/app.py tests/auto/test_reclassify_apply.py
git add dashboard/app.py tests/auto/test_reclassify_apply.py
git commit -m "feat(reclassify): global apply HTTP routes"
```

---

### Task 6: Fencing `categories.py` pendant un apply

**Files:**
- Modify: `dashboard/categories.py` (`add_entry`/`update_entry`/`delete_entry`)
- Test: `tests/auto/test_categories.py` (ou le fichier de tests categories existant)

- [ ] **Step 1: Write the failing test**

Repérer le fichier de tests de `categories.py` (`grep -rl "from dashboard import categories\|dashboard.categories" tests/auto/`). Y ajouter (adapter la fixture au style du fichier — il mocke déjà `get_project_root`) :

```python
    def test_add_entry_blocked_by_lock(self):
        from dashboard import categories
        # self.prof = profiles/<p> dans la fixture ; créer le sentinel
        lock = self.prof / ".cache" / "taxonomy.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("reclassify-apply")
        with self.assertRaises(categories.CategoriesError) as ctx:
            categories.add_entry("default", "informatique", "B/X", 5, ["kw"])
        self.assertEqual(ctx.exception.status, 423)
```

(Si la fixture n'expose pas `self.prof`, construire le chemin du sentinel comme elle construit les autres : `<root>/profiles/<profile>/.cache/taxonomy.lock`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest tests.auto.test_categories -k lock -v` (adapter le module)
Expected: FAIL — pas de garde, `add_entry` réussit (pas de `CategoriesError`).

- [ ] **Step 3: Add the guard**

Dans `dashboard/categories.py`, ajouter un helper (réutilise le même sentinel `.cache/taxonomy.lock` que taxonomy) :

```python
def _check_lock_free(profile: str) -> None:
    """Refuse une écriture si le sentinel .taxonomy.lock est présent
    (apply/baseline en cours). Même fichier que taxonomy._lock_file."""
    lock = _profile_dir(profile) / ".cache" / "taxonomy.lock"
    if lock.exists():
        raise CategoriesError(
            "modifications verrouillées (apply/run en cours ?)", 423)
```

(Repérer le helper de chemin profil existant dans `categories.py` — `_profile_dir` ou équivalent — et l'utiliser. Sinon construire `data.get_project_root() / "profiles" / profile`.)

Puis dans `add_entry`, `update_entry`, `delete_entry`, **juste après** `with _locks[profile]:`, ajouter `_check_lock_free(profile)` en première ligne du bloc.

- [ ] **Step 4: Run tests**

Run: `uv run python -m unittest tests.auto.test_categories -v 2>&1 | tail -4`
Expected: PASS (nouveau + existants).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check dashboard/categories.py
git add dashboard/categories.py tests/auto/test_categories.py
git commit -m "feat(categories): fence writes behind taxonomy.lock during apply"
```

---

### Task 7: UI — modale « Voir ce qui bougerait » étendue + carte Overview

**Files:**
- Modify: `dashboard/static/js/taxonomy.js` (`renderReclassifyBody` ~661-769)
- Modify: `dashboard/templates/partials/overview_profile.html` + `dashboard/overview.py`

Pas de test JS automatisé (pas d'infra) — vérif = `node --check` sur le JS extrait + suite Python intacte + revue.

- [ ] **Step 1: Étendre `renderReclassifyBody`**

Dans `dashboard/static/js/taxonomy.js`, en haut de la fonction (ou juste après les stats cards ~686, avant le caveat P3), injecter les contrôles d'apply. Ajouter ces fonctions dans le même scope (avant `renderReclassifyBody`) :

```javascript
    async function fetchApplyPreview(keyword) {
        const r = await fetch('/api/taxonomy/reclassify/apply/preview?profile='
            + encodeURIComponent(currentProfile()) + '&keyword=' + (keyword ? 'true' : 'false'));
        if (!r.ok) throw new Error((await r.json()).error || r.status);
        return r.json();
    }
    async function applyPost(path) {
        const r = await fetch('/api/taxonomy/reclassify/apply/' + path, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({profile: currentProfile()})});
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
        return d;
    }
    let applyPollTimer = null;
    function stopApplyPoll() {
        if (applyPollTimer) { clearInterval(applyPollTimer); applyPollTimer = null; }
    }
    function renderApplyControls(host, keyword) {
        host.innerHTML = '<div class="muted small">Chargement…</div>';
        fetchApplyPreview(keyword).then(p => {
            const st = p.state || {};
            let html = '<div class="rr-apply-bar-host">';
            html += '<label><input type="checkbox" id="rca-kw"' + (keyword ? ' checked' : '')
                  + '> inclure les matchs par mot-clé (P2, faux positifs possibles)</label>';
            html += '<div class="muted small">' + p.n_moves + ' à déplacer ('
                  + p.n_p1 + ' P1' + (keyword ? ' · ' + p.n_p2 + ' P2' : '') + ')</div>';
            if (st.executed) {
                html += '<button class="btn-secondary rr-apply-danger" id="rca-undo">↩ Annuler le dernier apply</button>';
            } else {
                html += '<button class="btn-primary" id="rca-apply"'
                      + (p.n_moves ? '' : ' disabled') + '>Appliquer (' + p.n_moves + ')</button>';
            }
            html += '<div id="rca-progress"></div></div>';
            host.innerHTML = html;
            document.getElementById('rca-kw').addEventListener('change', e =>
                renderApplyControls(host, e.target.checked));
            const applyBtn = document.getElementById('rca-apply');
            if (applyBtn) applyBtn.addEventListener('click', async () => {
                const dests = (p.top_destinations || []).slice(0, 5)
                    .map(d => d.folder + ' (' + d.n + ')').join(', ');
                const ok = await showConfirm({
                    title: 'Appliquer ' + p.n_moves + ' déplacement(s) ?',
                    body: p.n_p1 + ' P1' + (keyword ? ' + ' + p.n_p2 + ' P2' : '')
                        + '. Top destinations : ' + dests
                        + '.\nAnnulable via le journal.',
                    confirmLabel: 'Appliquer', variant: 'danger'});
                if (!ok) return;
                applyBtn.disabled = true;
                try { await applyPost('execute'); startApplyPoll(host, keyword); }
                catch (e) { applyBtn.disabled = false; showToast('✗ ' + e.message, 'error'); }
            });
            const undoBtn = document.getElementById('rca-undo');
            if (undoBtn) undoBtn.addEventListener('click', async () => {
                const ok = await showConfirm({title: 'Annuler le dernier apply ?',
                    body: 'Re-déplace les fichiers vers leur emplacement d\'origine.',
                    confirmLabel: 'Annuler', variant: 'danger'});
                if (!ok) return;
                undoBtn.disabled = true;
                try { await applyPost('undo'); startApplyPoll(host, keyword); }
                catch (e) { undoBtn.disabled = false; showToast('✗ ' + e.message, 'error'); }
            });
        }).catch(() => { host.innerHTML = ''; });
    }
    function startApplyPoll(host, keyword) {
        stopApplyPoll();
        const tick = async () => {
            let d;
            try {
                const r = await fetch('/api/taxonomy/reclassify/apply/status?profile='
                    + encodeURIComponent(currentProfile()));
                d = await r.json();
            } catch (e) { return; }
            const prog = d.progress; const pe = document.getElementById('rca-progress');
            if (!prog) return;
            if (prog.status === 'running') {
                const pct = prog.n_total ? Math.round(100 * prog.n_done / prog.n_total) : 0;
                if (pe) pe.innerHTML = '<div class="rr-apply-bar"><div class="rr-apply-fill" style="width:'
                    + pct + '%"></div></div><div class="muted small">' + prog.n_done + '/' + prog.n_total
                    + (prog.n_skipped ? ' · ' + prog.n_skipped + ' skip(s)' : '')
                    + (prog.n_failed ? ' · ' + prog.n_failed + ' échec(s)' : '') + '</div>';
            } else {
                stopApplyPoll();
                if (prog.status === 'done') showToast('✓ Apply terminé'
                    + (prog.report ? ' — logs/' + prog.report : ''), 'success');
                else if (prog.status === 'error') showToast('✗ ' + (prog.error || 'erreur'), 'error');
                renderApplyControls(host, keyword);
            }
        };
        tick(); applyPollTimer = setInterval(tick, 2000);
    }
```

Dans `renderReclassifyBody(data)`, après le bloc des stats cards et **avant** le caveat P3, insérer un conteneur + l'init :

```javascript
        html += '<div id="rca-apply-controls" class="rca-apply-controls"></div>';
```

et, après l'affectation finale de `#tax-reclassify-body`.innerHTML, appeler :

```javascript
        renderApplyControls(document.getElementById('rca-apply-controls'), false);
```

(Adapter au mécanisme exact d'insertion de `renderReclassifyBody` — repérer comment il pose son HTML dans `#tax-reclassify-body`.)

Ajouter le CSS en fin de `dashboard/static/style.css` :

```css
.rca-apply-controls { border-top: 1px solid var(--border, #334155); margin-top: 12px; padding-top: 12px; }
.rca-apply-controls .rr-apply-danger { color: #e57373; }
.rca-apply-controls .rr-apply-bar { height: 8px; border-radius: 4px; overflow: hidden;
    background: rgba(255,255,255,.08); margin-top: 8px; }
.rca-apply-controls .rr-apply-fill { height: 100%; background: #4caf50; transition: width .4s ease; }
```

- [ ] **Step 2: Carte « Synchroniser » dans Overview**

Dans `dashboard/overview.py`, fonction `build_profile_snapshot` (~527), ajouter une entrée `sync` au snapshot :

```python
    from dashboard import reclassify_apply
    snap["sync"] = {"pending": reclassify_apply.is_pending(profile)}
```

(Placer l'import en tête du module si possible, sinon local. Adapter `snap`/`profile` aux noms réels de la fonction.)

Dans `dashboard/templates/partials/overview_profile.html`, ajouter une carte (calquée sur le pattern `kpi_card_big`) après une rangée existante :

```html
<div class="ov-card ov-sync {{ 'ov-sync-pending' if s.sync.pending else '' }}">
  <div class="ov-card-label">🔄 Bibliothèque</div>
  <div class="ov-card-value">
    {% if s.sync.pending %}Config modifiée depuis le dernier apply{% else %}À jour{% endif %}
  </div>
  <a class="btn-secondary" href="/taxonomy?profile={{ profile }}#reclassify-apply">Synchroniser</a>
</div>
```

(Adapter `s.sync`, `profile` aux variables réelles du template. Le lien ouvre l'onglet Taxonomie ; l'auto-ouverture de la modale via le hash `#reclassify-apply` est optionnelle — au minimum le lien amène au bon onglet.)

- [ ] **Step 3: Vérification**

```bash
python3 -c "
import re, subprocess, tempfile, pathlib
html = pathlib.Path('dashboard/static/js/taxonomy.js').read_text()
p = tempfile.NamedTemporaryFile(suffix='.js', delete=False, mode='w'); p.write(html); p.close()
print(subprocess.run(['node','--check',p.name], capture_output=True, text=True).returncode)
"
uv run python -m unittest tests.auto.test_reclassify_apply tests.auto.test_taxonomy -q 2>&1 | tail -3
```

Puis manuel : `./klodo.sh dashboard` → Taxonomie → Mappings → « Voir ce qui bougerait » → la case P2 + bouton Appliquer + progression + Annuler apparaissent ; Overview montre la carte « Bibliothèque ».

- [ ] **Step 4: Commit**

```bash
git add dashboard/static/js/taxonomy.js dashboard/static/style.css dashboard/templates/partials/overview_profile.html dashboard/overview.py
git commit -m "feat(reclassify): global apply UI — extended dryrun modal + Overview card"
```

---

### Task 8: Documentation

**Files:**
- Modify: `dashboard/CLAUDE.md`, `lib/CLAUDE.md`

- [ ] **Step 1: `dashboard/CLAUDE.md`**

Ajouter une section :

```markdown
### Apply global (synchroniser la bibliothèque avec la config live)

- **`apply_engine.py`** — moteur de déplacement source-agnostique (extrait de
  l'apply refonte) : `execute_move_batch(target, moves, profile_dir, on_progress)`,
  garde-fous `safe_target_subdir`/`prune_empty_dirs`, `spawn`, `ApplyError`.
  Partagé par refonte ET global.
- **`reclassify_apply.py`** — apply global (couche B seule, pas d'adoption) :
  preview (projection live `build_reclassify_projection` figée en CSV) → execute
  (rejoue le CSV via apply_engine) → undo (`move_journal.undo_batch`). État global
  par profil `.cache/reclassify/apply/{state,status,projection.csv}`. Détection
  « en attente » via hash de config (theme_mapping+categories+tree+theme-canon).
  Déterministe P1+P2 (P1 par défaut, P2 opt-in), canonicalisation appliquée.
- Routes : `GET/POST …/api/taxonomy/reclassify/apply/{preview,pending,status,execute,undo}`.
- UI : modale « Voir ce qui bougerait » étendue + carte Overview.
- Fencing : `categories.py` vérifie désormais `.taxonomy.lock` (423 pendant un apply).
```

- [ ] **Step 2: `lib/CLAUDE.md`**

Dans la section pipeline de classification, noter le param `canon_table` :

```markdown
- `classify_combined` / `classify_by_theme` acceptent un `canon_table` optionnel
  (theme-canon.json) qui canonicalise le thème AVANT le lookup theme_mapping —
  rend Dédupli effectif sur les destinations (utilisé par reclassify_dryrun,
  build_reclassify_projection, simulator refonte).
```

- [ ] **Step 3: Commit**

```bash
uv run python -m unittest discover tests/auto -q 2>&1 | tail -3   # tout vert
git add dashboard/CLAUDE.md lib/CLAUDE.md
git commit -m "docs: document global apply + apply_engine + canon_table"
```

---

## Vérification end-to-end finale

1. `uv run python -m unittest discover tests/auto -q` → tous verts (incl. non-régression refonte).
2. `uv run ruff check .` → clean.
3. **Smoke test** (Task 3) déjà fait — pipeline canon OK sur 3-5 PDFs réels.
4. `./klodo.sh dashboard` sur un profil de TEST : éditer un mapping → Overview montre « config modifiée » → Mappings → « Voir ce qui bougerait » → cocher/décocher P2 (compteurs changent) → Appliquer → progression → fichiers déplacés + `logs/rapport_apply_*.csv` → Annuler le dernier apply (fichiers revenus) → Overview repasse « À jour ».
5. Pendant un apply : l'onglet Mappings ET Catégories refusent les writes (423).
6. PR vers `develop`, CI verte.

## Notes pour l'exécuteur

- **Jamais** de test sur `/Volumes/ExtSSD/BIBLIO` — tout en tmpdir + vision_cache de fixture.
- Threads synchrones dans les tests HTTP : patcher `rca.apply_engine.spawn` (ou `reclassify_apply.apply_engine.spawn`). Ne JAMAIS patcher `threading.Thread`.
- `taxonomy._locks` est un `defaultdict(threading.Lock)` ; `TaxonomyError`/`CategoriesError` exposent `.status`.
- Task 2 est un refacto **behavior-preserving** : si un test refonte casse, c'est que l'extraction a changé un comportement — corriger l'extraction, pas le test.
- Le `vision_cache.compute_cache_key` dépend de `model`/`n_pages`/contenu du fichier : dans les fixtures, écrire le `profile.yaml` avec `llm.model` et `defaults.pages` cohérents avec ce que la fonction lit (model="M", pages=2).
