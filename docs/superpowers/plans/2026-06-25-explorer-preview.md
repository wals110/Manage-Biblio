# Explorateur « Maintenant / Après » — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Un onglet **Explorateur** qui montre la bibliothèque en deux modes basculables — **Maintenant** (disque réel) et **Après** (où chaque fichier irait au reclassify) — sans rien déplacer ; l'apply devient explicite depuis l'Explorateur et l'onboarding ne déplace plus automatiquement.

**Architecture:** Une projection par fichier (calculée par `taxonomy._scan_and_classify`, P1+P2, sans LLM Mapper) est construite **en tâche de fond** (~27 s/18k fichiers) et **mise en cache** (invalidée par un hash config+Vision). Le front la charge une fois, construit les deux arbres et bascule côté client. L'apply réutilise `reclassify_apply` avec un **flag de classification unifié** (`taxonomy.RECLASSIFY_INCLUDE_KEYWORD`) pour garantir « Après » == apply.

**Tech Stack:** Python 3.13 (`uv`), FastAPI + Jinja2 + vanilla JS, `lib/classifier.classify_combined`, pattern daemon-thread + status.json (`apply_engine.spawn`, comme `reclassify_apply`).

**Spec:** `docs/superpowers/specs/2026-06-25-explorer-preview-design.md`
**Branche:** `feature/explorer-preview` (déjà créée, spec committée).

---

## Contexte de code (à connaître)

- `dashboard/taxonomy.py` :
  - `_scan_and_classify(profile, *, include_step2) -> list[dict]` (l.915-995) : scan FS read-only + classement par fichier via `classify_combined(..., llm_mapper=None)`. Retourne par fichier `{rel_path, current_folder, predicted_folder, source, score, top_theme, top_confidence}`. **Pas de cache.** Boucle finale : `with ThreadPoolExecutor(max_workers=8) as ex: return list(ex.map(_process, file_list))`.
  - `build_reclassify_projection(profile, include_keyword) -> list[dict]` (l.1123-1149) : **moves seulement** ; `signal = "p2" if source.startswith("Keyword") else "p1"`.
  - `reset_cache(profile)`, `_check_lock_free(profile)` (lève `TaxonomyError`), `_lock_file(profile)` (= `.cache/taxonomy.lock`), `_locks[profile]` (mutex), `vision_cache` importé.
- `dashboard/reclassify_apply.py` : `_config_hash(profile)` (l.94-102, hash theme_mapping+categories+tree+theme-canon), `_write_progress`/`_read_progress` (status.json), `get_status`, `build_preview(profile, include_keyword)`, `start_execute`, `start_undo`. Daemon via `apply_engine.spawn(fn, args, name)`.
- `dashboard/app.py` : routes apply existantes l.2095-2139 (`/api/taxonomy/reclassify/apply/{preview,pending,status,execute,undo}`). `preview` accepte `?keyword=bool` (défaut **False**).
- `dashboard/templates/base.html` : sidebar l.14-26 (motif `<li><a href="/x" class="{% if active == 'x' %}active{% endif %}">…</a></li>`).
- `dashboard/templates/onboarding.html` : bouton `onb-move-btn` (l.163) + flux `moveClassified()`/`_rcaPollDone()`/`_moveShow()` (l.502-562) couplé aux globales `profileName`/`_rcaBase` et au DOM `onb-move-status`.

**Conventions** : Python 3.13, `uv`, ruff-clean, type hints modernes (`X | None`, `from collections.abc import Callable`), jamais de Vision/LLM réel en test (mock), messages/UI en français, commits en anglais, trailer `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

## File Structure

| Fichier | Responsabilité | Tâches |
|---|---|---|
| `dashboard/taxonomy.py` | Constante `RECLASSIFY_INCLUDE_KEYWORD` + flag `analyzed` + callback `on_progress` dans `_scan_and_classify` | 1 |
| `dashboard/explorer.py` (nouveau) | `build_projection` (mapping+summary) + cache/freshness + build de fond (`get_projection`/`get_build_status`/`refresh`) | 2, 3 |
| `dashboard/app.py` | 3 routes `/api/explorer/{projection,status,refresh}` + page `/explorer` | 4, 6 |
| `dashboard/static/js/reclassify_apply.js` (nouveau) | `applyReclassify({...})` — flux preview→execute→poll→undo générique | 5 |
| `dashboard/templates/explorer.html` + `static/js/explorer.js` + `base.html` | Page Explorateur (arbre, toggle, fichiers, badges, virtualisation, apply) + entrée nav | 6 |
| `dashboard/templates/onboarding.html` | Remplacer `onb-move-btn` par lien Explorateur | 7 |
| `tests/auto/test_explorer.py` (nouveau) | Tests backend + routes + rendu | 1-7 |

---

## Task 1 : `RECLASSIFY_INCLUDE_KEYWORD` + flag `analyzed` + progression dans `_scan_and_classify`

**Files:**
- Modify: `dashboard/taxonomy.py`
- Test: `tests/auto/test_explorer.py` (nouveau)

- [ ] **Step 1 : Écrire les tests qui échouent**

Créer `tests/auto/test_explorer.py` avec un helper de profil + 3 tests :

```python
#!/usr/bin/env python3
"""Tests Explorateur (Vision/LLM mockés, zéro SSD réel)."""
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


def _make_profile(root: Path, profile: str, files: dict, mapping: dict, categories: str = "{}"):
    """files: {nom_fichier: theme|None}. None → pas d'entrée vision_cache (non analysé)."""
    import lib.vision_cache as vc
    prof = root / "profiles" / profile
    (prof / ".cache").mkdir(parents=True)
    target = root / "LIB"
    target.mkdir(exist_ok=True)
    (prof / "profile.yaml").write_text(yaml.safe_dump(
        {"target": str(target), "llm": {"model": "M"}, "defaults": {"pages": 2}}), encoding="utf-8")
    (prof / "theme_mapping.yaml").write_text(yaml.safe_dump(mapping), encoding="utf-8")
    (prof / "categories.yaml").write_text(categories, encoding="utf-8")
    (prof / "tree.yaml").write_text(yaml.safe_dump({"folders": list(set(mapping.values()))}), encoding="utf-8")
    cache: dict = {}
    for fname, theme in files.items():
        fp = target / fname
        fp.write_bytes(b"%PDF-1.4 " + fname.encode())
        if theme is not None:
            key = vc.compute_cache_key(str(fp), model="M", n_pages=2)
            vc.store(cache, key, {"theme": theme,
                                  "themes": [{"theme": theme, "confidence": 0.9}],
                                  "confidence": 0.9}, "M")
    vc.save_cache(prof / ".cache" / "vision_cache.json", cache)
    return target


class TestScanAnalyzedAndProgress(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="klodo-expl-")
        self.root = Path(self.tmp)
        # a.pdf a un cache Vision (thème → mappé) ; b.pdf n'en a pas (non analysé)
        _make_profile(self.root, "p",
                      {"a.pdf": "Deep Learning", "b.pdf": None},
                      mapping={"deep learning": "01-Info/ML"})
        self.patch = mock.patch("dashboard.data.get_project_root", return_value=self.root)
        self.patch.start()

    def tearDown(self):
        mock.patch.stopall()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reclassify_include_keyword_constant(self):
        from dashboard import taxonomy
        self.assertEqual(taxonomy.RECLASSIFY_INCLUDE_KEYWORD, True)

    def test_scan_marks_analyzed_per_file(self):
        from dashboard import taxonomy
        rows = {r["rel_path"]: r for r in taxonomy._scan_and_classify("p", include_step2=True)}
        self.assertTrue(rows["a.pdf"]["analyzed"])      # a un cache Vision
        self.assertFalse(rows["b.pdf"]["analyzed"])     # pas de cache

    def test_scan_reports_progress(self):
        from dashboard import taxonomy
        seen = []
        taxonomy._scan_and_classify("p", include_step2=True,
                                    on_progress=lambda d, t: seen.append((d, t)))
        self.assertTrue(seen)
        self.assertEqual(seen[-1], (2, 2))              # 2 fichiers, dernier rapport (2,2)


if __name__ == "__main__":
    unittest.main()
```

> NB : si la patch `dashboard.data.get_project_root` ne suffit pas (taxonomy résout la racine autrement), regarde un test existant de `reclassify_apply`/`taxonomy` et ajoute la même patch (ex. `dashboard.taxonomy.get_project_root` si importée directement). Adapte `_make_profile` au besoin.

- [ ] **Step 2 : Vérifier l'échec**

Run : `uv run python -m unittest tests.auto.test_explorer.TestScanAnalyzedAndProgress -v`
Expected : FAIL (`AttributeError: ... RECLASSIFY_INCLUDE_KEYWORD` ; `analyzed`/`on_progress` absents).

- [ ] **Step 3 : Implémenter dans `dashboard/taxonomy.py`**

(a) Constante de module (près des autres constantes du haut de fichier) :
```python
# Flag de classification unifié du reclassify (P1 + P2 mots-clés). Consommé par
# l'Explorateur ET son bouton Appliquer → garantit « Après » == apply déclenché
# depuis l'Explorateur. Voir docs/.../2026-06-25-explorer-preview-design.md.
RECLASSIFY_INCLUDE_KEYWORD = True
```

(b) Signature de `_scan_and_classify` (ajouter `on_progress`, importer `Callable` si absent en haut du fichier : `from collections.abc import Callable`) :
```python
def _scan_and_classify(profile: str, *, include_step2: bool,
                       on_progress: "Callable[[int, int], None] | None" = None) -> list[dict]:
```

(c) Dans `_process`, exposer `analyzed` (remplacer les lignes qui font `result = vision_cache.lookup(...)` puis `if not isinstance(result, dict): result = {}`) :
```python
        key = vision_cache.compute_cache_key(abs_path, model=model, n_pages=n_pages)
        raw = vision_cache.lookup(cache, key) if key else None
        analyzed = isinstance(raw, dict)
        result = raw if analyzed else {}
```
et ajouter `"analyzed": analyzed` au dict retourné par `_process` :
```python
        return {"rel_path": rel, "current_folder": current_folder,
                "predicted_folder": dest, "source": source,
                "score": float(score) if score else 0.0,
                "top_theme": top_theme, "top_confidence": round(top_conf, 3),
                "analyzed": analyzed}
```

(d) Remplacer la boucle finale `with ThreadPoolExecutor(...) as ex: return list(ex.map(_process, file_list))` par une version qui rapporte la progression (l'ordre n'importe pas aux appelants) :
```python
    total = len(file_list)
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for done, r in enumerate(ex.map(_process, file_list), start=1):
            results.append(r)
            if on_progress:
                on_progress(done, total)
    return results
```

- [ ] **Step 4 : Vérifier le succès + non-régression**

Run : `uv run python -m unittest tests.auto.test_explorer.TestScanAnalyzedAndProgress -v` → PASS.
Run : `uv run python -m unittest tests.auto.test_reclassify_apply tests.auto.test_taxonomy 2>&1 | tail -3` → OK (l'ajout de `analyzed` est additif, `on_progress` défaut None → comportement inchangé).
Run : `uv run ruff check dashboard/taxonomy.py tests/auto/test_explorer.py` → clean.

- [ ] **Step 5 : Commit**
```bash
git add dashboard/taxonomy.py tests/auto/test_explorer.py
git commit -m "feat(explorer): RECLASSIFY_INCLUDE_KEYWORD + flag analyzed + progression _scan_and_classify

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2 : `explorer.build_projection` (mapping + summary)

**Files:**
- Create: `dashboard/explorer.py`
- Test: `tests/auto/test_explorer.py`

- [ ] **Step 1 : Écrire les tests qui échouent**

Ajouter à `tests/auto/test_explorer.py` :
```python
class TestBuildProjection(unittest.TestCase):
    def _rows(self):
        return [
            {"rel_path": "a.pdf", "current_folder": "_INBOX", "predicted_folder": "01-Info/ML",
             "source": "LLM (theme)", "score": 0.9, "top_theme": "Deep Learning",
             "top_confidence": 0.9, "analyzed": True},                       # bouge, p1
            {"rel_path": "b.pdf", "current_folder": "01-Info/ML", "predicted_folder": "01-Info/ML",
             "source": "Keyword (x)", "score": 0.5, "top_theme": "ml",
             "top_confidence": 0.5, "analyzed": True},                       # stable, p2
            {"rel_path": "c.pdf", "current_folder": "_INBOX", "predicted_folder": "",
             "source": "", "score": 0.0, "top_theme": "", "top_confidence": 0.0,
             "analyzed": True},                                             # orphelin
            {"rel_path": "d.pdf", "current_folder": "_INBOX", "predicted_folder": "02-Maths",
             "source": "Keyword (nom)", "score": 0.4, "top_theme": "", "top_confidence": 0.0,
             "analyzed": False},                                            # non analysé MAIS bouge (P2 nom)
        ]

    def test_build_projection_maps_fields_and_summary(self):
        from dashboard import explorer
        with mock.patch("dashboard.taxonomy._scan_and_classify", return_value=self._rows()):
            out = explorer.build_projection("p")
        self.assertTrue(out["ok"])
        f = {x["rel_path"]: x for x in out["files"]}
        self.assertEqual(f["a.pdf"]["signal"], "p1")
        self.assertEqual(f["b.pdf"]["signal"], "p2")
        self.assertIsNone(f["c.pdf"]["signal"])                # pas de prédiction → null
        self.assertEqual(f["a.pdf"]["confidence"], 0.9)        # mappé depuis top_confidence
        self.assertEqual(f["d.pdf"]["analyzed"], False)
        s = out["summary"]
        self.assertEqual(s["n_total"], 4)
        self.assertEqual(s["n_moving"], 2)                     # a + d
        self.assertEqual(s["n_stable"], 1)                     # b
        self.assertEqual(s["n_no_prediction"], 1)              # c (analysé, sans pred)
        self.assertEqual(s["n_unanalyzed"], 1)                 # d
        self.assertEqual(out["flag_keyword"], True)
```

- [ ] **Step 2 : Vérifier l'échec**
Run : `uv run python -m unittest tests.auto.test_explorer.TestBuildProjection -v` → FAIL (`No module named 'dashboard.explorer'`).

- [ ] **Step 3 : Implémenter `dashboard/explorer.py`**
```python
"""Explorateur Maintenant/Après — projection lecture seule du reclassify.

Spec : docs/superpowers/specs/2026-06-25-explorer-preview-design.md
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dashboard import apply_engine, data, reclassify_apply, taxonomy


def build_projection(profile: str) -> dict[str, Any]:
    """Projection par fichier (TOUS les fichiers), lecture seule. Réutilise le
    MÊME classifieur P1+P2 que l'apply (taxonomy._scan_and_classify, llm_mapper
    None). Coûteux → appelé en tâche de fond par _build_job.
    """
    def _prog(done: int, total: int) -> None:
        _write_status(profile, {"status": "building", "n_done": done,
                                "n_total": total, "error": None})

    rows = taxonomy._scan_and_classify(
        profile, include_step2=taxonomy.RECLASSIFY_INCLUDE_KEYWORD, on_progress=_prog)
    files: list[dict] = []
    n_moving = n_stable = n_no_pred = n_unan = 0
    for r in rows:
        pred, cur = r["predicted_folder"], r["current_folder"]
        analyzed = bool(r.get("analyzed", True))
        signal = None
        if pred:
            signal = "p2" if str(r["source"]).startswith("Keyword") else "p1"
        if not analyzed:
            n_unan += 1
        if pred and pred != cur:
            n_moving += 1
        elif pred and pred == cur:
            n_stable += 1
        elif analyzed:
            n_no_pred += 1
        files.append({"rel_path": r["rel_path"], "current_folder": cur,
                      "predicted_folder": pred, "source": r["source"],
                      "signal": signal, "confidence": r["top_confidence"],
                      "top_theme": r["top_theme"], "analyzed": analyzed})
    return {"ok": True, "files": files,
            "summary": {"n_total": len(rows), "n_moving": n_moving,
                        "n_stable": n_stable, "n_no_prediction": n_no_pred,
                        "n_unanalyzed": n_unan},
            "flag_keyword": taxonomy.RECLASSIFY_INCLUDE_KEYWORD}
```
(Les fonctions `_write_status`/cache/build de fond arrivent en Task 3 ; pour que ce module importe, ajoute un stub minimal de `_write_status` que Task 3 remplacera — ou implémente `_write_status` dès maintenant comme en Task 3 §a. **Implémente `_write_status`/`_status_path`/`_explorer_dir` maintenant** pour éviter un NameError :)
```python
def _explorer_dir(profile: str) -> Path:
    return data.get_project_root() / "profiles" / profile / ".cache" / "explorer"


def _write_status(profile: str, payload: dict[str, Any]) -> None:
    d = _explorer_dir(profile)
    d.mkdir(parents=True, exist_ok=True)
    (d / "status.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
```

- [ ] **Step 4 : Vérifier le succès**
Run : `uv run python -m unittest tests.auto.test_explorer.TestBuildProjection -v` → PASS.
Run : `uv run ruff check dashboard/explorer.py` → clean.

- [ ] **Step 5 : Commit**
```bash
git add dashboard/explorer.py tests/auto/test_explorer.py
git commit -m "feat(explorer): build_projection — mapping par fichier + summary

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3 : cache + freshness + build de fond

**Files:**
- Modify: `dashboard/explorer.py`
- Test: `tests/auto/test_explorer.py`

- [ ] **Step 1 : Écrire les tests qui échouent**
```python
class TestProjectionCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="klodo-expl-c-")
        self.root = Path(self.tmp)
        (self.root / "profiles" / "p" / ".cache").mkdir(parents=True)
        self.patch = mock.patch("dashboard.data.get_project_root", return_value=self.root)
        self.patch.start()

    def tearDown(self):
        from dashboard import explorer
        explorer._projection_cache.clear()
        mock.patch.stopall()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_build(self):
        return {"ok": True, "files": [{"rel_path": "a.pdf"}],
                "summary": {"n_total": 1, "n_moving": 0, "n_stable": 0,
                            "n_no_prediction": 0, "n_unanalyzed": 0},
                "flag_keyword": True}

    def test_get_projection_builds_then_serves_cache(self):
        from dashboard import explorer
        with mock.patch.object(explorer, "build_projection", return_value=self._fake_build()), \
             mock.patch.object(explorer.apply_engine, "spawn",
                               side_effect=lambda fn, args, name: fn(*args)), \
             mock.patch.object(explorer, "_fresh_hash", return_value="h1"):
            first = explorer.get_projection("p")      # déclenche le build (spawn synchrone)
            second = explorer.get_projection("p")     # servi par le cache
        self.assertEqual(second["status"], "ready")
        self.assertEqual(second["files"], [{"rel_path": "a.pdf"}])

    def test_stale_hash_triggers_rebuild(self):
        from dashboard import explorer
        explorer._projection_cache["p"] = {"fresh_hash": "OLD", "data": self._fake_build()}
        with mock.patch.object(explorer, "build_projection", return_value=self._fake_build()) as b, \
             mock.patch.object(explorer.apply_engine, "spawn",
                               side_effect=lambda fn, args, name: fn(*args)), \
             mock.patch.object(explorer, "_fresh_hash", return_value="NEW"):
            out = explorer.get_projection("p")
        b.assert_called()                              # hash différent → rebuild
        self.assertEqual(out["status"], "ready")
```

- [ ] **Step 2 : Vérifier l'échec**
Run : `uv run python -m unittest tests.auto.test_explorer.TestProjectionCache -v` → FAIL (`_fresh_hash`/`get_projection`/`_projection_cache` absents).

- [ ] **Step 3 : Implémenter (ajouts à `dashboard/explorer.py`)**
```python
_projection_cache: dict[str, dict] = {}   # profile -> {"fresh_hash": str, "data": dict}


def _status_path(profile: str) -> Path:
    return _explorer_dir(profile) / "status.json"


def _read_status(profile: str) -> dict[str, Any] | None:
    p = _status_path(profile)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _vision_sig(profile: str) -> str:
    p = data.get_project_root() / "profiles" / profile / ".cache" / "vision_cache.json"
    try:
        return str(int(p.stat().st_mtime))
    except OSError:
        return "0"


def _fresh_hash(profile: str) -> str:
    return reclassify_apply._config_hash(profile) + ":" + _vision_sig(profile)


def _build_job(profile: str) -> None:
    try:
        result = build_projection(profile)
        _projection_cache[profile] = {"fresh_hash": _fresh_hash(profile), "data": result}
        n = result["summary"]["n_total"]
        _write_status(profile, {"status": "ready", "n_done": n, "n_total": n, "error": None})
    except Exception as exc:  # noqa: BLE001 — frontière de thread
        _write_status(profile, {"status": "error", "n_done": 0, "n_total": 0, "error": str(exc)})


def _spawn_build(profile: str) -> None:
    _write_status(profile, {"status": "building", "n_done": 0, "n_total": 0, "error": None})
    apply_engine.spawn(_build_job, (profile,), f"explorer-build-{profile}")


def get_projection(profile: str) -> dict[str, Any]:
    """Cache frais → {status: ready, ...data} ; sinon lance un build de fond et
    renvoie {status: building}. Recalcul auto si le hash config+Vision a changé."""
    cached = _projection_cache.get(profile)
    if cached and cached["fresh_hash"] == _fresh_hash(profile):
        return {"status": "ready", **cached["data"]}
    status = _read_status(profile)
    if not (status and status.get("status") == "building"):
        _spawn_build(profile)
    return {"status": "building"}


def get_build_status(profile: str) -> dict[str, Any]:
    return _read_status(profile) or {"status": "idle"}


def refresh(profile: str) -> dict[str, Any]:
    _projection_cache.pop(profile, None)
    _spawn_build(profile)
    return {"ok": True}
```

- [ ] **Step 4 : Vérifier le succès**
Run : `uv run python -m unittest tests.auto.test_explorer.TestProjectionCache -v` → PASS.
Run : `uv run ruff check dashboard/explorer.py` → clean.

- [ ] **Step 5 : Commit**
```bash
git add dashboard/explorer.py tests/auto/test_explorer.py
git commit -m "feat(explorer): cache + freshness hash + build de fond

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4 : routes `/api/explorer/*`

**Files:**
- Modify: `dashboard/app.py`
- Test: `tests/auto/test_explorer.py`

- [ ] **Step 1 : Écrire les tests qui échouent**

Repère dans `tests/auto/test_dashboard.py` (ou `test_agent_onboarding.py`) le motif de `TestClient(app)` et réutilise-le. Ajoute :
```python
class TestExplorerRoutes(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from dashboard.app import app
        self.client = TestClient(app)

    def test_projection_route_building(self):
        from dashboard import explorer
        with mock.patch.object(explorer, "get_projection",
                               return_value={"status": "building"}) as g:
            r = self.client.get("/api/explorer/projection?profile=p")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "building")
        g.assert_called_once_with("p")

    def test_status_route(self):
        from dashboard import explorer
        with mock.patch.object(explorer, "get_build_status",
                               return_value={"status": "ready", "n_done": 3, "n_total": 3}):
            r = self.client.get("/api/explorer/status?profile=p")
        self.assertEqual(r.json()["n_total"], 3)

    def test_refresh_route(self):
        from dashboard import explorer
        with mock.patch.object(explorer, "refresh", return_value={"ok": True}) as rf:
            r = self.client.post("/api/explorer/refresh?profile=p")
        self.assertEqual(r.status_code, 200)
        rf.assert_called_once_with("p")
```

- [ ] **Step 2 : Vérifier l'échec**
Run : `uv run python -m unittest tests.auto.test_explorer.TestExplorerRoutes -v` → FAIL (404).

- [ ] **Step 3 : Implémenter dans `dashboard/app.py`**

Vérifie que `from dashboard import explorer` figure dans les imports du haut de `app.py` (ajoute-le près de `reclassify_apply`). Ajoute les 3 routes (près des routes reclassify, ~l.2140) :
```python
@app.get("/api/explorer/projection")
async def api_explorer_projection(profile: str):
    from fastapi.responses import JSONResponse
    return JSONResponse(explorer.get_projection(profile))


@app.get("/api/explorer/status")
async def api_explorer_status(profile: str):
    from fastapi.responses import JSONResponse
    return JSONResponse(explorer.get_build_status(profile))


@app.post("/api/explorer/refresh")
async def api_explorer_refresh(profile: str):
    from fastapi.responses import JSONResponse
    return JSONResponse(explorer.refresh(profile))
```

- [ ] **Step 4 : Vérifier le succès**
Run : `uv run python -m unittest tests.auto.test_explorer.TestExplorerRoutes -v` → PASS.
Run : `uv run ruff check dashboard/app.py` → clean.

- [ ] **Step 5 : Commit**
```bash
git add dashboard/app.py tests/auto/test_explorer.py
git commit -m "feat(explorer): routes /api/explorer/{projection,status,refresh}

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5 : module JS partagé `reclassify_apply.js`

**Files:**
- Create: `dashboard/static/js/reclassify_apply.js`
- Test: `tests/auto/test_explorer.py` (présence du script — voir Task 6)

> Pas de test unitaire JS (vanilla). La présence/utilisation est vérifiée par le rendu de page (Task 6) + smoke (Task 8). Le flux reclassify_apply backend est déjà couvert par `test_reclassify_apply`.

- [ ] **Step 1 : Créer `dashboard/static/js/reclassify_apply.js`**

Flux générique extrait de `onboarding.html` (preview → confirm → execute → poll → undo), paramétré, sans dépendance au DOM onboarding :
```javascript
// Flux Apply global du reclassify, réutilisable (onboarding, explorer).
// applyReclassify({profile, keyword, onStatus, confirmFn, undoConfirmFn}) -> Promise<void>
//   onStatus(msg)      : callback d'affichage (string)
//   confirmFn(n)       : retourne true/false (confirmer le déplacement de n fichiers)
//   undoConfirmFn(n)   : retourne true/false (proposer l'annulation après n déplacés)
window.applyReclassify = async function ({ profile, keyword, onStatus, confirmFn, undoConfirmFn }) {
  const base = '/api/taxonomy/reclassify/apply';
  const enc = encodeURIComponent(profile);
  const say = onStatus || (() => {});
  async function pollDone() {
    for (;;) {
      const st = await fetch(base + '/status?profile=' + enc).then((r) => r.json());
      const p = st.progress;
      if (p && (p.status === 'done' || p.status === 'error')) return p;
      if (p) say(`Déplacement… ${p.n_done || 0}/${p.n_total || '?'}`);
      await new Promise((res) => setTimeout(res, 1500));
    }
  }
  say('Calcul de ce qui bougerait…');
  const pv = await fetch(base + '/preview?profile=' + enc + '&keyword=' + (keyword ? 'true' : 'false'))
    .then((r) => r.json());
  if (pv.error) { say('✗ ' + pv.error); return; }
  const n = pv.n_moves || 0;
  if (!n) { say('Aucun fichier à déplacer.'); return; }
  if (!(confirmFn ? confirmFn(n) : window.confirm(`Déplacer ${n} fichier(s) ?`))) { say(''); return; }
  say('Déplacement en cours…');
  await fetch(base + '/execute', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ profile }) });
  const prog = await pollDone();
  if (prog.status === 'error') { say('✗ Échec : ' + (prog.error || 'erreur')); return; }
  const moved = prog.n_done || 0, failed = prog.n_failed || 0, skipped = prog.n_skipped || 0;
  say(`✓ ${moved} déplacé(s)` + (skipped ? ` · ${skipped} ignoré(s)` : '')
      + (failed ? ` · ${failed} erreur(s)` : ''));
  if (moved > 0 && (undoConfirmFn ? undoConfirmFn(moved)
      : window.confirm(`${moved} déplacé(s). Annuler ?`))) {
    say('Annulation…');
    await fetch(base + '/undo', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile }) });
    const u = await pollDone();
    say(u.status === 'error' ? ('✗ ' + (u.error || 'échec annulation')) : '↩ Déplacement annulé.');
  }
};
```

- [ ] **Step 2 : Sanity import dashboard**
Run : `uv run python -c "import dashboard.app"` → pas d'erreur (le fichier est statique, juste vérifier qu'on n'a rien cassé).

- [ ] **Step 3 : Commit**
```bash
git add dashboard/static/js/reclassify_apply.js
git commit -m "feat(explorer): module JS partagé applyReclassify (flux preview/execute/undo)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6 : page Explorateur (template + nav + JS) + route page

**Files:**
- Create: `dashboard/templates/explorer.html`, `dashboard/static/js/explorer.js`
- Modify: `dashboard/templates/base.html` (nav), `dashboard/app.py` (route page `/explorer`)
- Test: `tests/auto/test_explorer.py`

- [ ] **Step 1 : Écrire les tests qui échouent**
```python
class TestExplorerPage(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from dashboard.app import app
        self.client = TestClient(app)

    def test_explorer_page_renders(self):
        r = self.client.get("/explorer")
        self.assertEqual(r.status_code, 200)
        body = r.text
        self.assertIn("onb-expl-toggle", body)            # interrupteur Maintenant/Après
        self.assertIn("explorer.js", body)
        self.assertIn("reclassify_apply.js", body)        # module apply partagé chargé

    def test_explorer_in_nav(self):
        r = self.client.get("/")
        self.assertIn('href="/explorer"', r.text)
```

- [ ] **Step 2 : Vérifier l'échec**
Run : `uv run python -m unittest tests.auto.test_explorer.TestExplorerPage -v` → FAIL (404 / chaînes absentes).

- [ ] **Step 3 : Implémenter**

(a) `dashboard/templates/base.html` — ajouter l'entrée nav après la ligne Taxonomie (l.19) :
```html
            <li><a href="/explorer" class="{% if active == 'explorer' %}active{% endif %}">Explorateur</a></li>
```

(b) Route page dans `dashboard/app.py` (mirror de la route `/onboarding` — cherche `def onboarding_page` pour le motif exact `templates.TemplateResponse`) :
```python
@app.get("/explorer")
async def explorer_page(request: Request):
    return templates.TemplateResponse("explorer.html", {"request": request, "active": "explorer"})
```

(c) `dashboard/templates/explorer.html` (hérite de `base.html` ; conteneurs + scripts) :
```html
{% extends "base.html" %}
{% block title %}Explorateur{% endblock %}
{% block content %}
<h1>Explorateur — Maintenant / Après</h1>
<p class="muted">Vois où iraient les fichiers au prochain reclassify, sans rien déplacer.</p>

<div id="onb-expl-toolbar" style="display:flex;gap:12px;align-items:center;margin:10px 0;">
  <div id="onb-expl-toggle" class="seg">
    <button data-mode="now" class="active" type="button">Maintenant</button>
    <button data-mode="after" type="button">Après</button>
  </div>
  <span id="expl-counter" class="muted small"></span>
  <button id="expl-refresh" class="btn-secondary" type="button">⟳ Rafraîchir</button>
  <button id="expl-apply" class="btn-primary" type="button" style="display:none;">Appliquer cette projection</button>
  <span id="expl-apply-status" class="muted small"></span>
</div>

<div id="expl-loading" class="onb-note" style="display:none;"></div>
<div id="expl-body" style="display:flex;gap:16px;">
  <div id="expl-tree" style="flex:0 0 320px;max-height:70vh;overflow:auto;"></div>
  <div id="expl-files" style="flex:1;max-height:70vh;overflow:auto;"></div>
</div>
{% endblock %}
{% block scripts %}
<script src="/static/js/reclassify_apply.js"></script>
<script src="/static/js/explorer.js"></script>
{% endblock %}
```
> Vérifie que `base.html` a bien un `{% block scripts %}{% endblock %}` ; sinon ajoute les `<script>` à la fin du `{% block content %}`.

(d) `dashboard/static/js/explorer.js` — logique complète :
```javascript
(function () {
  const $ = (id) => document.getElementById(id);
  const profile = new URLSearchParams(location.search).get('profile') || 'default';
  let mode = (new URLSearchParams(location.search).get('mode') === 'after') ? 'after' : 'now';
  let projection = null;           // {files, summary, flag_keyword}
  let selected = null;             // dossier sélectionné

  function finalFolder(f) {
    return (f.predicted_folder && f.predicted_folder !== f.current_folder)
      ? f.predicted_folder : (f.predicted_folder || f.current_folder);
  }
  function folderOf(f) { return mode === 'now' ? f.current_folder : finalFolder(f); }

  function load() {
    $('expl-loading').style.display = '';
    $('expl-loading').textContent = 'Calcul de la projection…';
    fetch('/api/explorer/projection?profile=' + encodeURIComponent(profile))
      .then((r) => r.json()).then((d) => {
        if (d.status === 'ready') { projection = d; $('expl-loading').style.display = 'none'; render(); }
        else if (d.status === 'building') { poll(); }
        else { $('expl-loading').textContent = '✗ ' + (d.error || 'erreur'); }
      });
  }
  function poll() {
    fetch('/api/explorer/status?profile=' + encodeURIComponent(profile))
      .then((r) => r.json()).then((s) => {
        if (s.status === 'ready') { load(); }
        else if (s.status === 'error') { $('expl-loading').textContent = '✗ ' + (s.error || 'erreur'); }
        else {
          $('expl-loading').textContent = `Calcul… ${s.n_done || 0}/${s.n_total || '?'} fichiers`;
          setTimeout(poll, 1000);
        }
      });
  }

  function render() {
    if (!projection) return;
    const s = projection.summary;
    $('expl-counter').textContent = `${s.n_moving} bougeraient · ${s.n_total} fichiers`
      + (s.n_unanalyzed ? ` · ${s.n_unanalyzed} non analysés (Vision)` : '');
    $('expl-apply').style.display = (mode === 'after' && s.n_moving > 0) ? '' : 'none';
    // regrouper par dossier du mode courant + badges
    const byFolder = {}, incoming = {}, outgoing = {};
    for (const f of projection.files) {
      (byFolder[folderOf(f)] ||= []).push(f);
      if (f.predicted_folder && f.predicted_folder !== f.current_folder) {
        incoming[f.predicted_folder] = (incoming[f.predicted_folder] || 0) + 1;
        outgoing[f.current_folder] = (outgoing[f.current_folder] || 0) + 1;
      }
    }
    const folders = Object.keys(byFolder).sort();
    $('expl-tree').innerHTML = folders.map((p) => {
      const badge = mode === 'after' && incoming[p] ? ` <span class="muted">+${incoming[p]}</span>` : '';
      const out = mode === 'now' && outgoing[p] ? ` <span class="muted">−${outgoing[p]}</span>` : '';
      const sel = p === selected ? ' style="font-weight:600"' : '';
      return `<div class="expl-folder" data-folder="${encodeURIComponent(p)}"${sel}>`
        + `${p || '(racine)'} <span class="muted small">(${byFolder[p].length})</span>${badge}${out}</div>`;
    }).join('');
    $('expl-tree').querySelectorAll('.expl-folder').forEach((el) => {
      el.onclick = () => { selected = decodeURIComponent(el.dataset.folder); render(); renderFiles(byFolder); };
    });
    renderFiles(byFolder);
  }

  const PAGE = 200;
  function renderFiles(byFolder) {
    const list = (selected != null && byFolder[selected]) || [];
    const slice = list.slice(0, PAGE);   // virtualisation simple : 200 max, "+N autres"
    $('expl-files').innerHTML = `<div class="muted small">${selected || ''} — ${list.length} fichier(s)</div>`
      + slice.map((f) => {
        const moved = f.predicted_folder && f.predicted_folder !== f.current_folder;
        const prov = (mode === 'after' && moved) ? ` <span class="muted small">← ${f.current_folder || '(racine)'}</span>` : '';
        const sig = f.signal ? ` <span class="muted small">[${f.signal}]</span>` : '';
        return `<div class="expl-file">${f.rel_path.split('/').pop()}${prov}${sig}</div>`;
      }).join('')
      + (list.length > PAGE ? `<div class="muted small">+${list.length - PAGE} autres…</div>` : '');
  }

  $('onb-expl-toggle').querySelectorAll('button').forEach((b) => {
    b.onclick = () => {
      mode = b.dataset.mode;
      $('onb-expl-toggle').querySelectorAll('button').forEach((x) => x.classList.toggle('active', x === b));
      render();
    };
    if (b.dataset.mode === mode) b.classList.add('active'); else b.classList.remove('active');
  });
  $('expl-refresh').onclick = () => {
    fetch('/api/explorer/refresh?profile=' + encodeURIComponent(profile), { method: 'POST' })
      .then(() => { projection = null; load(); });
  };
  $('expl-apply').onclick = () => {
    if (!projection) return;
    window.applyReclassify({
      profile, keyword: !!projection.flag_keyword,
      onStatus: (m) => { $('expl-apply-status').textContent = m; },
    }).then(() => { projection = null; load(); });   // recharge après apply
  };

  load();
})();
```
> Style : réutilise les classes existantes (`btn-primary`/`btn-secondary`/`muted`/`small`). Pour `.seg` (segmented toggle), ajoute un petit style inline ou réutilise un style existant ; l'important pour les tests est l'id `onb-expl-toggle`.

- [ ] **Step 4 : Vérifier le succès**
Run : `uv run python -m unittest tests.auto.test_explorer.TestExplorerPage -v` → PASS.
Run : `uv run python -c "import dashboard.app"` → OK.
Run : `uv run ruff check dashboard/app.py` → clean.

- [ ] **Step 5 : Commit**
```bash
git add dashboard/templates/explorer.html dashboard/static/js/explorer.js dashboard/templates/base.html dashboard/app.py tests/auto/test_explorer.py
git commit -m "feat(explorer): page Explorateur (arbre, toggle Maintenant/Après, apply) + nav

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7 : onboarding — remplacer l'auto-move par un lien Explorateur

**Files:**
- Modify: `dashboard/templates/onboarding.html`
- Test: `tests/auto/test_explorer.py`

- [ ] **Step 1 : Écrire les tests qui échouent**
```python
class TestOnboardingHandoff(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from dashboard.app import app
        self.client = TestClient(app)

    def test_onboarding_no_longer_auto_moves(self):
        body = self.client.get("/onboarding").text
        self.assertNotIn("moveClassified", body)          # l'ancien flux a disparu
        self.assertIn("/explorer?profile=", body)         # handoff vers l'Explorateur
```

- [ ] **Step 2 : Vérifier l'échec**
Run : `uv run python -m unittest tests.auto.test_explorer.TestOnboardingHandoff -v` → FAIL (`moveClassified` encore présent).

- [ ] **Step 3 : Implémenter dans `dashboard/templates/onboarding.html`**

(a) Remplacer le bouton `onb-move-btn` (l.163, « 🚀 Déplacer les fichiers classés ») par un lien/bouton vers l'Explorateur (garde l'id pour le handler) :
```html
<button class="btn-secondary" id="onb-move-btn" type="button">🔍 Voir dans l'Explorateur</button>
```
(b) **Supprimer** tout le bloc JS `_moveShow`/`_rcaPollDone`/`moveClassified` + `$('onb-move-btn').addEventListener('click', moveClassified)` (l.502-562) et le remplacer par :
```javascript
    // ── Voir dans l'Explorateur (mode Après) — plus de déplacement automatique ──
    $('onb-move-btn').addEventListener('click', () => {
        window.location = '/explorer?profile=' + encodeURIComponent(profileName) + '&mode=after';
    });
```
(c) Si l'élément `onb-move-status` n'est plus référencé, le laisser (inerte) ou le retirer du HTML — au choix, sans casser le reste.

- [ ] **Step 4 : Vérifier le succès**
Run : `uv run python -m unittest tests.auto.test_explorer.TestOnboardingHandoff -v` → PASS.
Run : `uv run python -m unittest tests.auto.test_agent_onboarding 2>&1 | tail -3` → OK (pas de régression sur les autres tests onboarding).
Run : `uv run python -c "import dashboard.app"` → OK.

- [ ] **Step 5 : Commit**
```bash
git add dashboard/templates/onboarding.html tests/auto/test_explorer.py
git commit -m "feat(onboarding): remplacer l'auto-move par un lien vers l'Explorateur

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8 : suite complète + lint + smoke live (manuel)

**Files:** —

- [ ] **Step 1 : Suite complète + lint**
Run : `uv run python -m unittest discover -s tests/auto 2>&1 | tail -3` → OK.
Run : `uv run ruff check dashboard/ tests/auto/test_explorer.py` → `All checks passed!`.

- [ ] **Step 2 : Smoke live (opérateur, nécessite SILICONFLOW_API_KEY + un profil scanné)**
```bash
pkill -f "python -m dashboard"; sleep 1
./scripts/test_onboarding.sh dash_start   # ou ./klodo.sh dashboard
# Ouvre http://localhost:8080/explorer?profile=test-local (ou un profil avec vision_cache)
```
Attendu : barre de progression pendant le build (~quelques s sur un petit profil, ~27 s sur la grosse biblio), puis l'arbre s'affiche ; bascule **Maintenant/Après** instantanée ; en Après, badges +N + provenance « ← venait de X » ; le compteur « N bougeraient » est cohérent. Cliquer **Appliquer** → preview/confirm/déplacement/annulation (moteur existant). Éditer un mapping dans Taxonomie puis revenir → le build se relance (hash changé).

- [ ] **Step 3 : Vérif handoff onboarding** : finir un onboarding → bouton « 🔍 Voir dans l'Explorateur » ouvre `/explorer?...&mode=after` ; **aucun déplacement automatique**.

---

## Self-review (auteur)

**Couverture spec :**
- §3 flag unifié `RECLASSIFY_INCLUDE_KEYWORD` → Task 1 (constante) + Task 6 (`flag_keyword` propagé au bouton Appliquer). ✓
- §4.1 `build_projection` (mapping confidence←top_confidence, signal dérivé, analyzed) + summary → Task 2 ; cache+freshness+build de fond → Task 3. ✓
- §4.2 routes → Task 4. ✓
- §4.3 page (arbre, toggle, fichiers, badges, provenance, virtualisation, loading) → Task 6. ✓
- §4.4 module JS partagé `applyReclassify` → Task 5. ✓
- §4.5 handoff onboarding → Task 7. ✓
- §5 cas limites : `analyzed` (Task 1/2), non-analysé peut bouger via P2 (compteurs n_unanalyzed ≠ moving, Task 2), lock 423 (réutilisé via reclassify_apply, aucun code neuf), péremption (freshness hash, Task 3). ✓
- §6 tests : analyzed/progress (T1), mapping/summary + anti-dérive de signal (T2), cache/freshness (T3), routes (T4), rendu page + nav + script partagé (T6), handoff (T7). ✓ Verrou anti-dérive `build_projection` vs `build_reclassify_projection(RECLASSIFY_INCLUDE_KEYWORD)` : couvert par le partage de `_scan_and_classify` + la même constante ; un test explicite peut être ajouté en T2 si souhaité.
- §8 risques (27 s build, 62 % bougent, péremption) → build de fond + progression (T1/T3/T6), affordance compacte + virtualisation (T6). ✓

**Placeholders :** aucun — code complet à chaque step, commandes exactes.

**Cohérence des noms :** `RECLASSIFY_INCLUDE_KEYWORD`, `build_projection`/`get_projection`/`get_build_status`/`refresh`/`_fresh_hash`/`_build_job`/`_write_status`/`_projection_cache`, `applyReclassify`, ids `onb-expl-toggle`/`expl-*` — cohérents d'une tâche à l'autre.
