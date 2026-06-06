# Overview Cockpit Biblio Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refondre l'onglet Overview du dashboard Klodo en cockpit Bibliothèque profile-aware avec 12 cartes dynamiques + 4 cartes générales statiques, server-render avec cache mémoire 30 s.

**Architecture:** Nouveau module Python `dashboard/overview.py` qui centralise 16 fonctions de cartes (1 par carte) + 2 builders + cache TTL. L'endpoint `/` lit `?profile=` + `?refresh=`, dispatche vers les builders et rend `overview.html` (shell minimal) qui include 2 partials. 100 % server-render, pas de JS fetch ni HTMX.

**Tech Stack:** Python 3.13 + FastAPI + Jinja2 + Chart.js + DuckDB (via helpers existants). Tests : `unittest` + `FastAPI TestClient`. Lint : `ruff`.

**Spec:** [docs/superpowers/specs/2026-06-05-overview-cockpit-biblio-design.md](../specs/2026-06-05-overview-cockpit-biblio-design.md)

---

## File Structure

| Fichier | Responsabilité | Action |
|---|---|---|
| `dashboard/overview.py` | Cache TTL + 16 fonctions de carte + 2 builders (`build_profile_snapshot`, `build_general_snapshot`) | Create |
| `dashboard/app.py` | Endpoint `/` réécrit pour dispatcher vers `overview.build_*` | Modify |
| `dashboard/templates/overview.html` | Shell minimal qui include les 2 partials | Rewrite |
| `dashboard/templates/partials/overview_profile.html` | Layout B moitié haute (4 KPI vedettes + 2 grilles) | Create |
| `dashboard/templates/partials/overview_general.html` | Grille 4×1 statique | Create |
| `dashboard/templates/macros/widgets.html` | +3 macros : `kpi_card_big`, `mini_bar`, `activity_timeline` | Modify (append) |
| `dashboard/static/style.css` | +CSS classes `.dash-overview-*`, `.dash-kpi-big*`, `.dash-grid-*`, `.dash-card`, `.dash-mini-bar*`, `.dash-activity*` | Modify (append) |
| `tests/auto/test_overview.py` | 30+ unit tests pour les cartes + cache + macros + `OverviewTestBase` | Create |
| `tests/auto/test_dashboard.py` | +7 tests routing pour `/` et `?profile=` / `?refresh=` | Modify |

---

## Pre-flight : convention de commits

Branche : `feature/overview-cockpit` depuis `develop`. Commits **frequent** (un par task ou par sous-bloc cohérent). Messages : préfixe `feat(overview):`, `test(overview):`, `refactor(overview):`, `style(overview):` selon nature.

---

### Task 1: Branch + Scaffold module overview.py + Test base

**Files:**

- Create: `dashboard/overview.py` (squelette avec imports + `reset_cache()`)
- Create: `tests/auto/test_overview.py` (base class + 1 test sanity)

- [ ] **Step 1: Créer la branche feature**

```bash
git checkout develop
git pull --ff-only
git checkout -b feature/overview-cockpit
```

- [ ] **Step 2: Créer le squelette `dashboard/overview.py`**

Contenu initial — juste imports + cache + `reset_cache()` pour les tests :

```python
"""Cockpit Overview — agrège des métriques par profil + métriques globales.

Cache mémoire 30 s par profil (snapshot) — invalidé par `?refresh=1` côté
endpoint ou `reset_cache()` côté tests.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import yaml

from dashboard import data

# ─── Cache ────────────────────────────────────────────────────────
_CACHE_TTL_SECONDS = 30
_cache_lock = threading.Lock()
_overview_cache: dict[str, tuple[float, dict]] = {}


def reset_cache(profile: str | None = None) -> None:
    """Vide le cache (un profil ou tout). Utilisé par les tests."""
    with _cache_lock:
        if profile is None:
            _overview_cache.clear()
        else:
            _overview_cache.pop(profile, None)
```

- [ ] **Step 3: Créer le squelette `tests/auto/test_overview.py`**

```python
#!/usr/bin/env python3
"""Tests pour dashboard/overview.py — cockpit Biblio."""

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

from dashboard import data, overview  # noqa: E402


class OverviewTestBase(unittest.TestCase):
    """Pose un profil de test isolé : profile.yaml + dossier target."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="klodo-ov-test-"))
        self.profiles_root = self.tmpdir / "profiles"
        self.target = self.tmpdir / "library"
        self.target.mkdir(parents=True, exist_ok=True)
        self.profile_name = "test"
        self.profile_dir = self.profiles_root / self.profile_name
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        (self.profile_dir / "profile.yaml").write_text(yaml.safe_dump({
            "name": "test",
            "description": "Test profile",
            "target": str(self.target),
            "inbox": str(self.tmpdir / "inbox"),
            "fallback": "_A-TRIER",
            "llm": {"provider": "siliconflow",
                    "model": "Qwen/Qwen3-VL-32B",
                    "endpoint": "https://api.siliconflow.com/v1"},
            "defaults": {"cost_per_call": 0.0003, "workers": 5},
        }))
        (self.profile_dir / ".cache").mkdir(parents=True, exist_ok=True)
        self._patcher = mock.patch.object(
            data, "get_project_root", return_value=self.tmpdir,
        )
        self._patcher.start()
        overview.reset_cache()

    def tearDown(self):
        self._patcher.stop()
        overview.reset_cache()
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class TestReset(OverviewTestBase):

    def test_reset_cache_clears_all(self):
        from dashboard import overview as ov
        ov._overview_cache["foo"] = (0.0, {"x": 1})
        ov.reset_cache()
        self.assertEqual(ov._overview_cache, {})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 4: Vérifier que le test sanity passe**

```bash
uv run python -m unittest tests.auto.test_overview -v
```

Expected: `OK · Ran 1 test in 0.00Xs`

- [ ] **Step 5: Commit**

```bash
git add dashboard/overview.py tests/auto/test_overview.py
git commit -m "feat(overview): scaffold module + test base class"
```

---

### Task 2: card_files_count

**Files:**

- Modify: `dashboard/overview.py` (append `card_files_count`)
- Test: `tests/auto/test_overview.py` (append `TestCardFilesCount`)

- [ ] **Step 1: Écrire les tests qui échouent**

Append à `tests/auto/test_overview.py` avant `if __name__` :

```python
class TestCardFilesCount(OverviewTestBase):

    def _make_files(self, files: dict[str, int]):
        """files = {rel_path: size_bytes}"""
        for rel, size in files.items():
            p = self.target / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x" * size)

    def test_happy_counts_pdf_and_epub(self):
        self._make_files({
            "a.pdf": 1024, "b.pdf": 2048, "c.epub": 512,
            "subdir/d.pdf": 4096, "subdir/notes.txt": 100,
        })
        r = overview.card_files_count(self.profile_name)
        self.assertEqual(r["total"], 4)
        self.assertEqual(r["by_ext"]["pdf"], 3)
        self.assertEqual(r["by_ext"]["epub"], 1)
        self.assertNotIn("error", r)

    def test_target_missing_returns_error(self):
        shutil.rmtree(self.target)
        r = overview.card_files_count(self.profile_name)
        self.assertEqual(r["total"], 0)
        self.assertEqual(r["error"], "target_missing")

    def test_profile_missing_returns_error(self):
        r = overview.card_files_count("does-not-exist")
        self.assertEqual(r["total"], 0)
        self.assertEqual(r["error"], "profile_missing")

    def test_empty_target_returns_zero(self):
        r = overview.card_files_count(self.profile_name)
        self.assertEqual(r["total"], 0)
        self.assertEqual(r["size_gb"], 0.0)
```

- [ ] **Step 2: Lancer pour confirmer l'échec**

```bash
uv run python -m unittest tests.auto.test_overview.TestCardFilesCount -v
```

Expected: 4 tests FAIL avec `AttributeError: module 'dashboard.overview' has no attribute 'card_files_count'`

- [ ] **Step 3: Implémenter `card_files_count` dans `dashboard/overview.py`**

```python
def _profile_yaml_path(profile: str) -> Path:
    return data.get_project_root() / "profiles" / profile / "profile.yaml"


def _load_profile_config(profile: str) -> dict | None:
    """Lit profile.yaml. None si absent ou invalide."""
    p = _profile_yaml_path(profile)
    if not p.exists():
        return None
    try:
        cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None
    return cfg if isinstance(cfg, dict) else None


def card_files_count(profile: str) -> dict:
    """Compte les fichiers PDF/EPUB dans target + taille totale.

    Retourne {total, size_gb, by_ext: {pdf, epub}, error?: str} où error =
    'profile_missing' | 'target_missing' (sentinelle pour l'UI tooltip).
    """
    cfg = _load_profile_config(profile)
    if cfg is None:
        return {"total": 0, "size_gb": 0.0,
                "by_ext": {"pdf": 0, "epub": 0}, "error": "profile_missing"}
    target = Path(str(cfg.get("target") or ""))
    if not target.exists():
        return {"total": 0, "size_gb": 0.0,
                "by_ext": {"pdf": 0, "epub": 0}, "error": "target_missing"}
    n_pdf = n_epub = 0
    size_bytes = 0
    for root, _dirs, files in os.walk(str(target)):
        for f in files:
            ext = f.lower().rsplit(".", 1)[-1] if "." in f else ""
            if ext == "pdf":
                n_pdf += 1
            elif ext == "epub":
                n_epub += 1
            else:
                continue
            try:
                size_bytes += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return {
        "total": n_pdf + n_epub,
        "size_gb": round(size_bytes / 1024**3, 2),
        "by_ext": {"pdf": n_pdf, "epub": n_epub},
    }
```

- [ ] **Step 4: Vérifier que les tests passent**

```bash
uv run python -m unittest tests.auto.test_overview.TestCardFilesCount -v
```

Expected: `OK · Ran 4 tests`

- [ ] **Step 5: Commit**

```bash
git add dashboard/overview.py tests/auto/test_overview.py
git commit -m "feat(overview): card_files_count + tests"
```

---

### Task 3: card_classified_rate

**Files:**

- Modify: `dashboard/overview.py` (append `card_classified_rate`)
- Test: `tests/auto/test_overview.py` (append `TestCardClassifiedRate`)

- [ ] **Step 1: Écrire les tests qui échouent**

```python
class TestCardClassifiedRate(OverviewTestBase):

    def _make(self, files: list[str]):
        for rel in files:
            p = self.target / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")

    def test_all_classified(self):
        self._make(["folder1/a.pdf", "folder1/b.pdf", "folder2/c.epub"])
        r = overview.card_classified_rate(self.profile_name)
        self.assertEqual(r["classified"], 3)
        self.assertEqual(r["unclassified"], 0)
        self.assertEqual(r["rate"], 100.0)
        self.assertEqual(r["fallback_count"], 0)

    def test_some_in_root_and_fallback(self):
        self._make([
            "folder/ok.pdf",
            "folder/sub/ok2.pdf",
            "in-root.pdf",
            "_A-TRIER/orphan.epub",
            "_A-TRIER/nested/orphan2.pdf",
        ])
        r = overview.card_classified_rate(self.profile_name)
        # classified = 2 (folder/ok + folder/sub/ok2)
        # root = 1, fallback = 2 → unclassified = 3
        self.assertEqual(r["classified"], 2)
        self.assertEqual(r["unclassified"], 3)
        self.assertEqual(r["fallback_count"], 2)
        self.assertEqual(r["rate"], 40.0)

    def test_empty_returns_zero(self):
        r = overview.card_classified_rate(self.profile_name)
        self.assertEqual(r["rate"], 0.0)

    def test_target_missing(self):
        shutil.rmtree(self.target)
        r = overview.card_classified_rate(self.profile_name)
        self.assertEqual(r["rate"], 0.0)
```

- [ ] **Step 2: Confirmer l'échec**

```bash
uv run python -m unittest tests.auto.test_overview.TestCardClassifiedRate -v
```

Expected: 4 FAIL.

- [ ] **Step 3: Implémenter**

```python
def card_classified_rate(profile: str) -> dict:
    """% de fichiers rangés vs racine ou fallback. Retourne
    {classified, unclassified, rate, fallback_count}."""
    cfg = _load_profile_config(profile)
    if cfg is None:
        return {"classified": 0, "unclassified": 0, "rate": 0.0,
                "fallback_count": 0}
    target = Path(str(cfg.get("target") or ""))
    fallback = str(cfg.get("fallback") or "_A-TRIER")
    if not target.exists():
        return {"classified": 0, "unclassified": 0, "rate": 0.0,
                "fallback_count": 0}
    classified = root_count = fallback_count = 0
    for root, _dirs, files in os.walk(str(target)):
        rel = Path(root).relative_to(target).as_posix()
        candidates = [f for f in files if f.lower().endswith((".pdf", ".epub"))]
        if not candidates:
            continue
        if rel == ".":
            root_count += len(candidates)
        elif rel.split("/", 1)[0] == fallback:
            fallback_count += len(candidates)
        else:
            classified += len(candidates)
    total = classified + root_count + fallback_count
    return {
        "classified": classified,
        "unclassified": root_count + fallback_count,
        "rate": round(classified / total * 100, 1) if total else 0.0,
        "fallback_count": fallback_count,
    }
```

- [ ] **Step 4: Verify**

```bash
uv run python -m unittest tests.auto.test_overview.TestCardClassifiedRate -v
```

Expected: `OK · Ran 4 tests`

- [ ] **Step 5: Commit**

```bash
git add dashboard/overview.py tests/auto/test_overview.py
git commit -m "feat(overview): card_classified_rate + tests"
```

---

### Task 4: card_folders_count

**Files:**

- Modify: `dashboard/overview.py` (append)
- Test: `tests/auto/test_overview.py` (append `TestCardFoldersCount`)

- [ ] **Step 1: Tests**

```python
class TestCardFoldersCount(OverviewTestBase):

    def _write_tree(self, folders: list[str]):
        (self.profile_dir / "tree.yaml").write_text(
            yaml.safe_dump({"folders": folders}, sort_keys=False)
        )

    def test_counts_total_and_max_depth(self):
        self._write_tree([
            "01-SCIENCES",
            "01-SCIENCES/MATH",
            "01-SCIENCES/MATH/ALG",
            "02-INFO",
        ])
        r = overview.card_folders_count(self.profile_name)
        self.assertEqual(r["total"], 4)
        self.assertEqual(r["max_depth"], 3)

    def test_no_tree_yaml_returns_error(self):
        r = overview.card_folders_count(self.profile_name)
        self.assertEqual(r["total"], 0)
        self.assertEqual(r["max_depth"], 0)
        self.assertEqual(r["error"], "tree_missing")

    def test_recently_modified_picks_3_newest(self):
        self._write_tree(["A", "A/B", "A/C", "D"])
        r = overview.card_folders_count(self.profile_name)
        self.assertIn("recently_modified", r)
        self.assertIsInstance(r["recently_modified"], list)
```

- [ ] **Step 2: Confirmer FAIL**

```bash
uv run python -m unittest tests.auto.test_overview.TestCardFoldersCount -v
```

- [ ] **Step 3: Implémenter**

```python
def card_folders_count(profile: str) -> dict:
    """Statistiques sur tree.yaml du profil. Retourne {total, max_depth,
    recently_modified, error?}."""
    tree_path = data.get_project_root() / "profiles" / profile / "tree.yaml"
    if not tree_path.exists():
        return {"total": 0, "max_depth": 0,
                "recently_modified": [], "error": "tree_missing"}
    try:
        d = yaml.safe_load(tree_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {"total": 0, "max_depth": 0,
                "recently_modified": [], "error": "tree_invalid"}
    folders = [str(f).strip() for f in (d.get("folders") or []) if f]
    max_depth = max((f.count("/") + 1 for f in folders), default=0)
    # recently_modified : 3 folders dont le dossier sur le disque a la
    # mtime la plus récente. Lookup via target. Best-effort : silent skip
    # si le dossier FS n'existe pas.
    cfg = _load_profile_config(profile)
    target = Path(str(cfg.get("target") or "")) if cfg else None
    rec: list[tuple[float, str]] = []
    if target and target.exists():
        for f in folders:
            fp = target / f
            try:
                rec.append((fp.stat().st_mtime, f))
            except OSError:
                pass
    rec.sort(reverse=True)
    return {
        "total": len(folders),
        "max_depth": max_depth,
        "recently_modified": [name for _, name in rec[:3]],
    }
```

- [ ] **Step 4: Verify**

```bash
uv run python -m unittest tests.auto.test_overview.TestCardFoldersCount -v
```

- [ ] **Step 5: Commit**

```bash
git add dashboard/overview.py tests/auto/test_overview.py
git commit -m "feat(overview): card_folders_count + tests"
```

---

### Task 5: card_llm_cost + card_vision_cache

Ces deux cartes lisent `vision_cache.json` — patterns identiques, traitées ensemble.

**Files:** `dashboard/overview.py`, `tests/auto/test_overview.py`

- [ ] **Step 1: Tests**

```python
class TestCardLlmCost(OverviewTestBase):

    def _write_cache(self, n_entries: int):
        cache = self.profile_dir / ".cache" / "vision_cache.json"
        cache.write_text(json.dumps({
            f"key{i}": {"result": {"title": f"book{i}"}}
            for i in range(n_entries)
        }))

    def test_cost_proportional_to_entries(self):
        self._write_cache(10)
        r = overview.card_llm_cost(self.profile_name)
        self.assertEqual(r["n_calls"], 10)
        self.assertAlmostEqual(r["cost_usd"], 0.003, places=4)  # 10 * 0.0003
        self.assertAlmostEqual(r["cost_per_call"], 0.0003)

    def test_corrupt_json_returns_zero(self):
        (self.profile_dir / ".cache" / "vision_cache.json").write_text("not json")
        r = overview.card_llm_cost(self.profile_name)
        self.assertEqual(r["n_calls"], 0)
        self.assertEqual(r["cost_usd"], 0.0)

    def test_no_cache_returns_zero(self):
        r = overview.card_llm_cost(self.profile_name)
        self.assertEqual(r["n_calls"], 0)


class TestCardVisionCache(OverviewTestBase):

    def _write_cache(self, payload: dict):
        (self.profile_dir / ".cache" / "vision_cache.json").write_text(
            json.dumps(payload)
        )

    def test_counts_entries_and_size(self):
        self._write_cache({
            "k1": {"result": {"title": "ok"}},
            "k2": {"result": {"title": "ok"}},
            "k3": {"result": None},  # raté
        })
        r = overview.card_vision_cache(self.profile_name)
        self.assertEqual(r["n_entries"], 3)
        self.assertEqual(r["n_successful"], 2)
        self.assertGreater(r["size_kb"], 0)
        self.assertIsNotNone(r["last_modified"])

    def test_no_cache(self):
        r = overview.card_vision_cache(self.profile_name)
        self.assertEqual(r["n_entries"], 0)
        self.assertEqual(r["size_kb"], 0)
        self.assertIsNone(r["last_modified"])
```

- [ ] **Step 2: Confirmer FAIL**

```bash
uv run python -m unittest tests.auto.test_overview.TestCardLlmCost tests.auto.test_overview.TestCardVisionCache -v
```

- [ ] **Step 3: Implémenter**

```python
def _vision_cache_path(profile: str) -> Path:
    return (data.get_project_root() / "profiles" / profile
            / ".cache" / "vision_cache.json")


def _load_vision_cache(profile: str) -> dict | None:
    p = _vision_cache_path(profile)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def card_llm_cost(profile: str) -> dict:
    """Coût LLM estimé = n_entries × cost_per_call (profile.yaml)."""
    cfg = _load_profile_config(profile)
    cpc = float(((cfg or {}).get("defaults") or {}).get("cost_per_call") or 0)
    cache = _load_vision_cache(profile)
    n = len(cache) if cache else 0
    return {"cost_usd": round(n * cpc, 2), "n_calls": n, "cost_per_call": cpc}


def card_vision_cache(profile: str) -> dict:
    """Stats sur vision_cache.json. n_successful = entries avec result non vide."""
    p = _vision_cache_path(profile)
    cache = _load_vision_cache(profile)
    if cache is None:
        return {"n_entries": 0, "n_successful": 0, "size_kb": 0,
                "last_modified": None}
    n_ok = sum(1 for v in cache.values()
               if isinstance(v, dict) and v.get("result"))
    size_kb = round(p.stat().st_size / 1024, 1)
    mtime_iso = time.strftime(
        "%Y-%m-%dT%H:%M:%S", time.localtime(p.stat().st_mtime),
    )
    return {
        "n_entries": len(cache),
        "n_successful": n_ok,
        "size_kb": size_kb,
        "last_modified": mtime_iso,
    }
```

- [ ] **Step 4: Verify**

```bash
uv run python -m unittest tests.auto.test_overview.TestCardLlmCost tests.auto.test_overview.TestCardVisionCache -v
```

- [ ] **Step 5: Commit**

```bash
git add dashboard/overview.py tests/auto/test_overview.py
git commit -m "feat(overview): card_llm_cost + card_vision_cache + tests"
```

---

### Task 6: card_inbox

**Files:** `dashboard/overview.py`, `tests/auto/test_overview.py`

- [ ] **Step 1: Tests**

```python
class TestCardInbox(OverviewTestBase):

    def test_inbox_with_files(self):
        inbox = self.tmpdir / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "a.pdf").write_bytes(b"x" * 1024)
        (inbox / "b.epub").write_bytes(b"x" * 512)
        r = overview.card_inbox(self.profile_name)
        self.assertEqual(r["n_files"], 2)
        self.assertTrue(r["exists"])
        self.assertGreater(r["size_mb"], 0)
        self.assertIsNotNone(r["oldest_iso"])

    def test_inbox_unconfigured(self):
        # Réécrit profile.yaml sans inbox:
        (self.profile_dir / "profile.yaml").write_text(yaml.safe_dump({
            "name": "test", "target": str(self.target),
            "defaults": {"cost_per_call": 0.0003},
        }))
        r = overview.card_inbox(self.profile_name)
        self.assertFalse(r["exists"])
        self.assertEqual(r["n_files"], 0)

    def test_inbox_configured_but_missing(self):
        # inbox dans yaml mais dossier absent
        r = overview.card_inbox(self.profile_name)
        self.assertFalse(r["exists"])
```

- [ ] **Step 2: Confirmer FAIL**

```bash
uv run python -m unittest tests.auto.test_overview.TestCardInbox -v
```

- [ ] **Step 3: Implémenter**

```python
def card_inbox(profile: str) -> dict:
    """Stats sur le dossier inbox du profil."""
    cfg = _load_profile_config(profile)
    inbox_path = (cfg or {}).get("inbox")
    if not inbox_path:
        return {"n_files": 0, "size_mb": 0.0,
                "oldest_iso": None, "exists": False}
    inbox = Path(str(inbox_path))
    if not inbox.exists() or not inbox.is_dir():
        return {"n_files": 0, "size_mb": 0.0,
                "oldest_iso": None, "exists": False}
    n = 0
    size_bytes = 0
    oldest_mtime: float | None = None
    for f in inbox.iterdir():
        if not f.is_file():
            continue
        if f.suffix.lower() not in (".pdf", ".epub"):
            continue
        n += 1
        try:
            st = f.stat()
            size_bytes += st.st_size
            if oldest_mtime is None or st.st_mtime < oldest_mtime:
                oldest_mtime = st.st_mtime
        except OSError:
            pass
    return {
        "n_files": n,
        "size_mb": round(size_bytes / 1024**2, 2),
        "oldest_iso": (time.strftime("%Y-%m-%dT%H:%M:%S",
                                     time.localtime(oldest_mtime))
                       if oldest_mtime else None),
        "exists": True,
    }
```

- [ ] **Step 4: Verify**

```bash
uv run python -m unittest tests.auto.test_overview.TestCardInbox -v
```

- [ ] **Step 5: Commit**

```bash
git add dashboard/overview.py tests/auto/test_overview.py
git commit -m "feat(overview): card_inbox + tests"
```

---

### Task 7: card_baseline_runs + card_agent_sessions

Deux cartes qui délèguent à des sources existantes (`baseline.list_runs`) ou lisent un `status.json` simple.

**Files:** `dashboard/overview.py`, `tests/auto/test_overview.py`

- [ ] **Step 1: Tests**

```python
class TestCardBaselineRuns(OverviewTestBase):

    def test_with_runs_via_mock(self):
        from dashboard import baseline
        fake_runs = [
            {"run_id": "r2", "created_at": "2026-06-04T10:00:00",
             "n_files": 100, "n_disagreements": 20, "klodo_version": "1.0"},
            {"run_id": "r1", "created_at": "2026-06-01T08:00:00",
             "n_files": 80, "n_disagreements": 30, "klodo_version": "1.0"},
        ]
        with mock.patch.object(baseline, "list_runs", return_value=fake_runs):
            r = overview.card_baseline_runs(self.profile_name)
        self.assertEqual(r["n_runs"], 2)
        self.assertEqual(r["last_run_iso"], "2026-06-04T10:00:00")

    def test_no_runs(self):
        from dashboard import baseline
        with mock.patch.object(baseline, "list_runs", return_value=[]):
            r = overview.card_baseline_runs(self.profile_name)
        self.assertEqual(r["n_runs"], 0)
        self.assertIsNone(r["last_run_iso"])


class TestCardAgentSessions(OverviewTestBase):

    def _write_status(self, agent: str, status: str):
        d = self.profile_dir / ".cache" / agent
        d.mkdir(parents=True, exist_ok=True)
        (d / "status.json").write_text(json.dumps({"status": status}))

    def _make_batches(self, agent: str, n: int):
        for i in range(n):
            d = self.profile_dir / ".cache" / agent / "batches" / f"batch-{i:03d}"
            d.mkdir(parents=True, exist_ok=True)

    def test_counts_batches_per_agent(self):
        self._make_batches("refonte", 3)
        self._make_batches("dedupli", 1)
        self._write_status("refonte", "idle")
        self._write_status("dedupli", "running")
        r = overview.card_agent_sessions(self.profile_name)
        self.assertEqual(r["refonte"]["n_batches"], 3)
        self.assertEqual(r["refonte"]["status"], "idle")
        self.assertEqual(r["dedupli"]["n_batches"], 1)
        self.assertEqual(r["dedupli"]["status"], "running")

    def test_missing_agents_default(self):
        r = overview.card_agent_sessions(self.profile_name)
        self.assertEqual(r["refonte"]["n_batches"], 0)
        self.assertEqual(r["refonte"]["status"], "missing")
        self.assertEqual(r["dedupli"]["status"], "missing")
```

- [ ] **Step 2: Confirmer FAIL**

```bash
uv run python -m unittest tests.auto.test_overview.TestCardBaselineRuns tests.auto.test_overview.TestCardAgentSessions -v
```

- [ ] **Step 3: Implémenter**

```python
def card_baseline_runs(profile: str) -> dict:
    """Délègue à baseline.list_runs. Retourne {n_runs, last_run_iso}."""
    from dashboard import baseline
    try:
        runs = baseline.list_runs(profile)
    except Exception:  # noqa: BLE001 — baseline peut crasher sur profil incomplet
        runs = []
    if not runs:
        return {"n_runs": 0, "last_run_iso": None}
    # baseline.list_runs trie déjà du plus récent au plus ancien
    return {"n_runs": len(runs), "last_run_iso": runs[0].get("created_at")}


def _agent_info(profile: str, agent: str) -> dict:
    """Lit .cache/<agent>/batches/ + status.json. status ∈ {idle, running,
    error, missing}."""
    base = data.get_project_root() / "profiles" / profile / ".cache" / agent
    if not base.exists():
        return {"n_batches": 0, "status": "missing"}
    batches_dir = base / "batches"
    n = 0
    if batches_dir.exists():
        n = sum(1 for d in batches_dir.iterdir() if d.is_dir())
    status_file = base / "status.json"
    status = "missing"
    if status_file.exists():
        try:
            data_ = json.loads(status_file.read_text(encoding="utf-8"))
            status = str(data_.get("status") or "idle")
        except (json.JSONDecodeError, OSError):
            status = "error"
    return {"n_batches": n, "status": status}


def card_agent_sessions(profile: str) -> dict:
    """Sessions des agents refonte + dedupli."""
    return {
        "refonte": _agent_info(profile, "refonte"),
        "dedupli": _agent_info(profile, "dedupli"),
    }
```

- [ ] **Step 4: Verify**

```bash
uv run python -m unittest tests.auto.test_overview.TestCardBaselineRuns tests.auto.test_overview.TestCardAgentSessions -v
```

- [ ] **Step 5: Commit**

```bash
git add dashboard/overview.py tests/auto/test_overview.py
git commit -m "feat(overview): card_baseline_runs + card_agent_sessions + tests"
```

---

### Task 8: card_health (orphans + locks)

**Files:** `dashboard/overview.py`, `tests/auto/test_overview.py`

- [ ] **Step 1: Tests**

```python
class TestCardHealth(OverviewTestBase):

    def _write_tree(self, folders: list[str]):
        (self.profile_dir / "tree.yaml").write_text(
            yaml.safe_dump({"folders": folders})
        )

    def _write_mapping(self, mapping: dict):
        (self.profile_dir / "theme_mapping.yaml").write_text(
            yaml.safe_dump(mapping, sort_keys=False)
        )

    def test_clean_state_returns_zero_orphans(self):
        self._write_tree(["A", "A/B"])
        self._write_mapping({"theme1": "A", "theme2": "A/B"})
        r = overview.card_health(self.profile_name)
        self.assertEqual(r["n_orphans_total"], 0)
        self.assertEqual(r["locks_active"], [])

    def test_mapping_orphan_counted(self):
        self._write_tree(["A"])
        self._write_mapping({"good": "A", "orphan": "DOES-NOT-EXIST"})
        r = overview.card_health(self.profile_name)
        self.assertEqual(r["n_orphans_mappings"], 1)
        self.assertEqual(r["n_orphans_total"], 1)

    def test_active_lock_detected(self):
        self._write_tree(["A"])
        self._write_mapping({})
        lock = self.profile_dir / ".cache" / ".taxonomy.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("")
        r = overview.card_health(self.profile_name)
        self.assertEqual(len(r["locks_active"]), 1)
        self.assertEqual(r["locks_active"][0]["name"], ".taxonomy.lock")
        self.assertIn("age_seconds", r["locks_active"][0])

    def test_stale_lock_flagged(self):
        self._write_tree(["A"])
        self._write_mapping({})
        lock = self.profile_dir / ".cache" / ".dedupli.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("")
        # Force mtime > 1 h ago
        old = lock.stat().st_mtime - 3700
        os.utime(lock, (old, old))
        r = overview.card_health(self.profile_name)
        self.assertTrue(r["locks_active"][0]["stale"])
```

- [ ] **Step 2: Confirmer FAIL**

```bash
uv run python -m unittest tests.auto.test_overview.TestCardHealth -v
```

- [ ] **Step 3: Implémenter**

```python
_STALE_LOCK_SECONDS = 3600


def card_health(profile: str) -> dict:
    """Compte les orphelins (mappings vers folder absent, categories orphelins)
    + locks actifs. {n_orphans_mappings, n_orphans_categories,
    n_orphans_total, locks_active: [{name, age_seconds, stale}]}."""
    from dashboard import taxonomy as _tax

    # Orphans côté theme_mapping : mapping val absente de tree
    tree_set = set()
    try:
        tree_set = set(_tax._load_tree(profile))
    except Exception:  # noqa: BLE001
        pass
    mapping = {}
    try:
        mapping = _tax._load_mapping(profile)
    except Exception:  # noqa: BLE001
        pass
    n_map_orphans = sum(1 for v in mapping.values() if v and v not in tree_set)

    # Orphans côté categories : snapshot.stats.n_orphans
    n_cat_orphans = 0
    try:
        from dashboard import categories as _cat
        snap = _cat.build_snapshot(profile, force_reload=False)
        n_cat_orphans = int((snap.get("stats") or {}).get("n_orphans") or 0)
    except Exception:  # noqa: BLE001
        pass

    # Locks actifs : glob .cache/.*.lock
    locks: list[dict] = []
    cache_dir = data.get_project_root() / "profiles" / profile / ".cache"
    if cache_dir.exists():
        now = time.time()
        for lock in cache_dir.glob(".*.lock"):
            try:
                age = now - lock.stat().st_mtime
            except OSError:
                continue
            locks.append({
                "name": lock.name,
                "age_seconds": int(age),
                "stale": age > _STALE_LOCK_SECONDS,
            })

    return {
        "n_orphans_mappings": n_map_orphans,
        "n_orphans_categories": n_cat_orphans,
        "n_orphans_total": n_map_orphans + n_cat_orphans,
        "locks_active": locks,
    }
```

- [ ] **Step 4: Verify**

```bash
uv run python -m unittest tests.auto.test_overview.TestCardHealth -v
```

- [ ] **Step 5: Commit**

```bash
git add dashboard/overview.py tests/auto/test_overview.py
git commit -m "feat(overview): card_health (orphans + locks) + tests"
```

---

### Task 9: card_top_themes

**Files:** `dashboard/overview.py`, `tests/auto/test_overview.py`

- [ ] **Step 1: Tests**

```python
class TestCardTopThemes(OverviewTestBase):

    def _write_cache(self, themes_per_file: list[list[tuple[str, float]]]):
        """themes_per_file = [[(theme, confidence), ...], ...]"""
        cache: dict = {}
        for i, themes in enumerate(themes_per_file):
            cache[f"k{i}"] = {"result": {
                "themes": [{"theme": t, "confidence": c} for t, c in themes]
            }}
        (self.profile_dir / ".cache" / "vision_cache.json").write_text(
            json.dumps(cache)
        )

    def test_aggregates_and_sorts_descending(self):
        self._write_cache([
            [("Python", 0.9), ("Linux", 0.8)],
            [("Python", 0.9)],
            [("Python", 0.9), ("Math", 0.7)],
        ])
        r = overview.card_top_themes(self.profile_name, limit=3)
        self.assertEqual(len(r), 3)
        self.assertEqual(r[0]["theme"], "Python")
        self.assertEqual(r[0]["count"], 3)
        # Pct = count / total occurrences = 3 / 5 = 60
        self.assertAlmostEqual(r[0]["pct"], 60.0, places=1)

    def test_filters_low_confidence(self):
        self._write_cache([[("Low", 0.3), ("Good", 0.9)]])
        r = overview.card_top_themes(self.profile_name)
        themes = [x["theme"] for x in r]
        self.assertNotIn("Low", themes)
        self.assertIn("Good", themes)

    def test_empty_cache(self):
        r = overview.card_top_themes(self.profile_name)
        self.assertEqual(r, [])
```

- [ ] **Step 2: Confirmer FAIL**

```bash
uv run python -m unittest tests.auto.test_overview.TestCardTopThemes -v
```

- [ ] **Step 3: Implémenter**

```python
_MIN_CONFIDENCE = 0.5


def card_top_themes(profile: str, limit: int = 10) -> list[dict]:
    """Top thèmes LLM (vision_cache) triés par count décroissant. Filtre
    confidence < _MIN_CONFIDENCE."""
    from dashboard import taxonomy as _tax
    cache = _load_vision_cache(profile)
    if not cache:
        return []
    counts: dict[str, int] = {}
    # Réutilise _iter_themes(cache) qui yield (theme, confidence)
    for theme, conf in _tax._iter_themes(cache):
        if conf < _MIN_CONFIDENCE:
            continue
        counts[theme] = counts.get(theme, 0) + 1
    if not counts:
        return []
    total_occ = sum(counts.values())
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [
        {"theme": t, "count": c, "pct": round(c / total_occ * 100, 1)}
        for t, c in items[:limit]
    ]
```

- [ ] **Step 4: Verify**

```bash
uv run python -m unittest tests.auto.test_overview.TestCardTopThemes -v
```

- [ ] **Step 5: Commit**

```bash
git add dashboard/overview.py tests/auto/test_overview.py
git commit -m "feat(overview): card_top_themes + tests"
```

---

### Task 10: card_top_folders

**Files:** `dashboard/overview.py`, `tests/auto/test_overview.py`

- [ ] **Step 1: Tests**

```python
class TestCardTopFolders(OverviewTestBase):

    def _make(self, files: dict[str, int]):
        for rel, n in files.items():
            d = self.target / rel
            d.mkdir(parents=True, exist_ok=True)
            for i in range(n):
                (d / f"f{i}.pdf").write_bytes(b"x")

    def test_top_folders_sorted_desc(self):
        self._make({"A": 5, "B/C": 2, "D": 10, "E": 1})
        r = overview.card_top_folders(self.profile_name, limit=3)
        paths = [x["path"] for x in r]
        self.assertEqual(paths, ["D", "A", "B/C"])
        self.assertEqual(r[0]["n_files"], 10)
        self.assertAlmostEqual(r[0]["pct"], 10 / 18 * 100, places=1)

    def test_empty(self):
        r = overview.card_top_folders(self.profile_name)
        self.assertEqual(r, [])

    def test_target_missing(self):
        shutil.rmtree(self.target)
        r = overview.card_top_folders(self.profile_name)
        self.assertEqual(r, [])
```

- [ ] **Step 2: Confirmer FAIL**

```bash
uv run python -m unittest tests.auto.test_overview.TestCardTopFolders -v
```

- [ ] **Step 3: Implémenter**

```python
def card_top_folders(profile: str, limit: int = 10) -> list[dict]:
    """Top N folders par nombre de fichiers (PDF/EPUB)."""
    cfg = _load_profile_config(profile)
    if cfg is None:
        return []
    target = Path(str(cfg.get("target") or ""))
    if not target.exists():
        return []
    counts: dict[str, int] = {}
    for root, _dirs, files in os.walk(str(target)):
        rel = Path(root).relative_to(target).as_posix()
        if rel == ".":
            continue  # racine = "non classifié" → exclu
        n = sum(1 for f in files if f.lower().endswith((".pdf", ".epub")))
        if n:
            counts[rel] = n
    if not counts:
        return []
    total = sum(counts.values())
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [
        {"path": p, "n_files": n, "pct": round(n / total * 100, 1)}
        for p, n in items[:limit]
    ]
```

- [ ] **Step 4: Verify**

```bash
uv run python -m unittest tests.auto.test_overview.TestCardTopFolders -v
```

- [ ] **Step 5: Commit**

```bash
git add dashboard/overview.py tests/auto/test_overview.py
git commit -m "feat(overview): card_top_folders + tests"
```

---

### Task 11: card_recent_activity

**Files:** `dashboard/overview.py`, `tests/auto/test_overview.py`

- [ ] **Step 1: Tests**

```python
class TestCardRecentActivity(OverviewTestBase):

    def test_merges_sources_and_sorts_recent_first(self):
        # 3 backups taxonomy à mtimes différentes
        bdir = self.profile_dir / ".cache" / "taxonomy-backups"
        bdir.mkdir(parents=True, exist_ok=True)
        f1 = bdir / "theme_mapping-20260101-100000-000000.yaml"
        f2 = bdir / "tree-20260102-100000-000000.yaml"
        f3 = bdir / "theme_mapping-20260103-100000-000000.yaml"
        for f in [f1, f2, f3]:
            f.write_text("")
        import os as _os
        _os.utime(f1, (1000, 1000))
        _os.utime(f2, (2000, 2000))
        _os.utime(f3, (3000, 3000))
        r = overview.card_recent_activity(self.profile_name, limit=5)
        self.assertEqual(len(r), 3)
        # Plus récent en premier
        self.assertEqual(r[0]["target"], f3.name)

    def test_empty(self):
        r = overview.card_recent_activity(self.profile_name)
        self.assertEqual(r, [])
```

- [ ] **Step 2: Confirmer FAIL**

```bash
uv run python -m unittest tests.auto.test_overview.TestCardRecentActivity -v
```

- [ ] **Step 3: Implémenter**

```python
def card_recent_activity(profile: str, limit: int = 5) -> list[dict]:
    """Liste les N mutations les plus récentes : backups taxonomy/categories
    + tail rename-journal. Retourne [{ts, relative_time, action, target}]."""
    cache_dir = data.get_project_root() / "profiles" / profile / ".cache"
    events: list[tuple[float, str, str]] = []  # (mtime, action, target)
    if not cache_dir.exists():
        return []
    # Backups
    for sub, action in (
        ("taxonomy-backups", "taxonomy"),
        ("categories-backups", "categories"),
    ):
        d = cache_dir / sub
        if not d.exists():
            continue
        for f in d.glob("*.yaml"):
            try:
                events.append((f.stat().st_mtime, action, f.name))
            except OSError:
                pass
    # Rename journal : on prend juste la mtime du fichier (un événement
    # synthétique). Détail par ligne hors scope.
    rj = cache_dir / "rename-journal.jsonl"
    if rj.exists():
        try:
            events.append((rj.stat().st_mtime, "rename",
                           "rename-journal.jsonl"))
        except OSError:
            pass
    events.sort(reverse=True)
    now = time.time()
    out: list[dict] = []
    for mtime, action, target in events[:limit]:
        out.append({
            "ts": mtime,
            "relative_time": _human_relative(now - mtime),
            "action": action,
            "target": target,
        })
    return out


def _human_relative(seconds: float) -> str:
    """3600 → '1 h', 60 → '1 min', etc."""
    s = int(seconds)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"il y a {s // 3600} h"
    return f"il y a {s // 86400} j"
```

- [ ] **Step 4: Verify**

```bash
uv run python -m unittest tests.auto.test_overview.TestCardRecentActivity -v
```

- [ ] **Step 5: Commit**

```bash
git add dashboard/overview.py tests/auto/test_overview.py
git commit -m "feat(overview): card_recent_activity + tests"
```

---

### Task 12: card_llm_models + card_api_keys + card_profiles_list

Cartes générales qui délèguent aux helpers existants de `data.py`.

**Files:** `dashboard/overview.py`, `tests/auto/test_overview.py`

- [ ] **Step 1: Tests**

```python
class TestCardLlmModels(OverviewTestBase):

    def test_returns_per_profile_config(self):
        # Crée un 2e profil
        p2 = self.profiles_root / "test-local"
        p2.mkdir(parents=True, exist_ok=True)
        (p2 / "profile.yaml").write_text(yaml.safe_dump({
            "name": "test-local", "target": str(self.target),
            "llm": {"provider": "ollama", "model": "qwen2-vl:7b",
                    "endpoint": "http://localhost:11434"},
        }))
        r = overview.card_llm_models()
        names = {x["profile"] for x in r}
        self.assertIn("test", names)
        self.assertIn("test-local", names)
        local = next(x for x in r if x["profile"] == "test-local")
        self.assertEqual(local["provider"], "ollama")


class TestCardApiKeys(OverviewTestBase):

    def test_siliconflow_status_reflects_env(self):
        with mock.patch.dict(os.environ, {"SILICONFLOW_API_KEY": "sk-test"}):
            r = overview.card_api_keys()
        sf = next((x for x in r if x["name"] == "SILICONFLOW_API_KEY"), None)
        self.assertIsNotNone(sf)
        self.assertTrue(sf["configured"])

    def test_siliconflow_missing(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            r = overview.card_api_keys()
        sf = next((x for x in r if x["name"] == "SILICONFLOW_API_KEY"), None)
        self.assertFalse(sf["configured"])


class TestCardProfilesList(OverviewTestBase):

    def test_returns_profiles_with_target_status(self):
        r = overview.card_profiles_list()
        names = {x["name"] for x in r}
        self.assertIn("test", names)
        item = next(x for x in r if x["name"] == "test")
        self.assertTrue(item["target_exists"])
```

- [ ] **Step 2: Confirmer FAIL**

```bash
uv run python -m unittest tests.auto.test_overview.TestCardLlmModels tests.auto.test_overview.TestCardApiKeys tests.auto.test_overview.TestCardProfilesList -v
```

- [ ] **Step 3: Implémenter**

```python
def card_llm_models() -> list[dict]:
    """Liste {profile, provider, model, endpoint} pour chaque profil."""
    out: list[dict] = []
    for p in data.get_available_profiles():
        name = p["name"] if isinstance(p, dict) else p
        cfg = _load_profile_config(name) or {}
        llm = cfg.get("llm") or {}
        out.append({
            "profile": name,
            "provider": llm.get("provider", ""),
            "model": llm.get("model", ""),
            "endpoint": llm.get("endpoint", ""),
        })
    return out


def card_api_keys() -> list[dict]:
    """Statut des API keys connues (SiliconFlow pour l'instant)."""
    known = ["SILICONFLOW_API_KEY"]
    return [
        {"name": k, "configured": bool(os.environ.get(k))}
        for k in known
    ]


def card_profiles_list() -> list[dict]:
    """Liste de tous les profils + état de leur target."""
    out: list[dict] = []
    for p in data.get_available_profiles():
        name = p["name"] if isinstance(p, dict) else p
        cfg = _load_profile_config(name) or {}
        target = str(cfg.get("target") or "")
        out.append({
            "name": name,
            "target_path": target,
            "target_exists": bool(target and Path(target).exists()),
        })
    return out
```

- [ ] **Step 4: Verify**

```bash
uv run python -m unittest tests.auto.test_overview.TestCardLlmModels tests.auto.test_overview.TestCardApiKeys tests.auto.test_overview.TestCardProfilesList -v
```

- [ ] **Step 5: Commit**

```bash
git add dashboard/overview.py tests/auto/test_overview.py
git commit -m "feat(overview): card_llm_models + card_api_keys + card_profiles_list + tests"
```

---

### Task 13: card_global_cost

**Files:** `dashboard/overview.py`, `tests/auto/test_overview.py`

- [ ] **Step 1: Tests**

```python
class TestCardGlobalCost(OverviewTestBase):

    def _seed_profile(self, name: str, n_entries: int, cpc: float = 0.0003):
        p = self.profiles_root / name
        p.mkdir(parents=True, exist_ok=True)
        (p / "profile.yaml").write_text(yaml.safe_dump({
            "name": name, "target": str(self.target),
            "defaults": {"cost_per_call": cpc},
        }))
        (p / ".cache").mkdir(parents=True, exist_ok=True)
        if n_entries:
            (p / ".cache" / "vision_cache.json").write_text(
                json.dumps({f"k{i}": {"result": {}} for i in range(n_entries)})
            )

    def test_sums_per_profile_costs(self):
        self._seed_profile("test", 100)         # 0.03
        self._seed_profile("test-local", 200)   # 0.06 (same cpc)
        r = overview.card_global_cost()
        self.assertAlmostEqual(r["total_usd"], 0.09, places=4)
        # Chaque profil présent avec son pct
        by_p = {x["profile"]: x for x in r["by_profile"]}
        self.assertIn("test", by_p)
        self.assertIn("test-local", by_p)

    def test_empty_when_no_calls(self):
        r = overview.card_global_cost()
        self.assertEqual(r["total_usd"], 0.0)
        self.assertEqual(r["by_profile"], [])

    def test_pct_drops_below_1_segments(self):
        # Profile 1 : 1000 entries, Profile 2 : 5 entries (~ 0.5%)
        self._seed_profile("big", 1000)
        self._seed_profile("tiny", 5)
        r = overview.card_global_cost()
        profiles = {x["profile"] for x in r["by_profile"]}
        self.assertIn("big", profiles)
        self.assertNotIn("tiny", profiles)  # < 1% drop
```

- [ ] **Step 2: Confirmer FAIL**

```bash
uv run python -m unittest tests.auto.test_overview.TestCardGlobalCost -v
```

- [ ] **Step 3: Implémenter**

```python
def card_global_cost() -> dict:
    """Somme des coûts LLM estimés tous profils. Pie segments < 1 % cachés."""
    by_profile_full: list[dict] = []
    for p in data.get_available_profiles():
        name = p["name"] if isinstance(p, dict) else p
        cost = card_llm_cost(name)
        if cost["n_calls"] > 0:
            by_profile_full.append({
                "profile": name,
                "cost_usd": cost["cost_usd"],
                "n_calls": cost["n_calls"],
            })
    total = round(sum(x["cost_usd"] for x in by_profile_full), 2)
    if total == 0:
        return {"total_usd": 0.0, "by_profile": []}
    # Calcule pct et filtre les segments < 1 %
    by_profile: list[dict] = []
    for x in by_profile_full:
        pct = round(x["cost_usd"] / total * 100, 1)
        if pct >= 1.0:
            by_profile.append({**x, "pct": pct})
    by_profile.sort(key=lambda x: -x["pct"])
    return {"total_usd": total, "by_profile": by_profile}
```

- [ ] **Step 4: Verify**

```bash
uv run python -m unittest tests.auto.test_overview.TestCardGlobalCost -v
```

- [ ] **Step 5: Commit**

```bash
git add dashboard/overview.py tests/auto/test_overview.py
git commit -m "feat(overview): card_global_cost + tests"
```

---

### Task 14: Builders `build_profile_snapshot` + `build_general_snapshot` + cache

**Files:** `dashboard/overview.py`, `tests/auto/test_overview.py`

- [ ] **Step 1: Tests**

```python
class TestBuildSnapshots(OverviewTestBase):

    def test_profile_snapshot_returns_all_keys(self):
        r = overview.build_profile_snapshot(self.profile_name)
        expected = {
            "files", "classified", "folders", "llm_cost", "health",
            "vision_cache", "inbox", "baseline_runs", "agent_sessions",
            "top_themes", "top_folders", "recent_activity",
        }
        self.assertEqual(set(r.keys()), expected)

    def test_general_snapshot_returns_all_keys(self):
        r = overview.build_general_snapshot()
        self.assertEqual(set(r.keys()),
                         {"llm_models", "api_keys", "profiles_list",
                          "global_cost"})

    def test_cache_hit_within_ttl(self):
        # Spy sur card_files_count : seul le 1er appel doit hit
        with mock.patch.object(overview, "card_files_count",
                               wraps=overview.card_files_count) as spy:
            overview.build_profile_snapshot(self.profile_name)
            overview.build_profile_snapshot(self.profile_name)
        self.assertEqual(spy.call_count, 1)

    def test_force_invalidates_cache(self):
        with mock.patch.object(overview, "card_files_count",
                               wraps=overview.card_files_count) as spy:
            overview.build_profile_snapshot(self.profile_name)
            overview.build_profile_snapshot(self.profile_name, force=True)
        self.assertEqual(spy.call_count, 2)

    def test_cache_miss_after_ttl(self):
        overview.build_profile_snapshot(self.profile_name)
        # Avance le ts dans le cache
        with overview._cache_lock:
            ts, snap = overview._overview_cache[self.profile_name]
            overview._overview_cache[self.profile_name] = (
                ts - 31, snap,
            )
        with mock.patch.object(overview, "card_files_count",
                               wraps=overview.card_files_count) as spy:
            overview.build_profile_snapshot(self.profile_name)
        self.assertEqual(spy.call_count, 1)

    def test_cache_per_profile_isolation(self):
        # Crée profile2
        (self.profiles_root / "other").mkdir(parents=True)
        (self.profiles_root / "other" / "profile.yaml").write_text(
            yaml.safe_dump({"name": "other", "target": str(self.target),
                            "defaults": {"cost_per_call": 0.0003}})
        )
        overview.build_profile_snapshot(self.profile_name)
        with mock.patch.object(overview, "card_files_count",
                               wraps=overview.card_files_count) as spy:
            overview.build_profile_snapshot("other")
        self.assertEqual(spy.call_count, 1)
```

- [ ] **Step 2: Confirmer FAIL**

```bash
uv run python -m unittest tests.auto.test_overview.TestBuildSnapshots -v
```

- [ ] **Step 3: Implémenter**

```python
def build_profile_snapshot(profile: str, force: bool = False) -> dict:
    """Construit le snapshot complet du profil. Cache 30 s sauf force=True."""
    if not force:
        with _cache_lock:
            cached = _overview_cache.get(profile)
            if cached is not None:
                ts, snap = cached
                if time.time() - ts < _CACHE_TTL_SECONDS:
                    return snap
    snap = {
        "files": card_files_count(profile),
        "classified": card_classified_rate(profile),
        "folders": card_folders_count(profile),
        "llm_cost": card_llm_cost(profile),
        "health": card_health(profile),
        "vision_cache": card_vision_cache(profile),
        "inbox": card_inbox(profile),
        "baseline_runs": card_baseline_runs(profile),
        "agent_sessions": card_agent_sessions(profile),
        "top_themes": card_top_themes(profile),
        "top_folders": card_top_folders(profile),
        "recent_activity": card_recent_activity(profile),
    }
    with _cache_lock:
        _overview_cache[profile] = (time.time(), snap)
    return snap


def build_general_snapshot() -> dict:
    """Snapshot statique tous-profils. Non caché (déjà rapide)."""
    return {
        "llm_models": card_llm_models(),
        "api_keys": card_api_keys(),
        "profiles_list": card_profiles_list(),
        "global_cost": card_global_cost(),
    }
```

- [ ] **Step 4: Verify**

```bash
uv run python -m unittest tests.auto.test_overview.TestBuildSnapshots -v
```

- [ ] **Step 5: Commit**

```bash
git add dashboard/overview.py tests/auto/test_overview.py
git commit -m "feat(overview): build_profile_snapshot + build_general_snapshot + cache TTL"
```

---

### Task 15: Macro `kpi_card_big` + CSS

**Files:**

- Modify: `dashboard/templates/macros/widgets.html` (append macro)
- Modify: `dashboard/static/style.css` (append CSS)
- Test: `tests/auto/test_overview.py` (append `TestKpiCardBigMacro`)

- [ ] **Step 1: Tests**

```python
class TestKpiCardBigMacro(unittest.TestCase):
    """Rend la macro dans un template inline + parse le résultat."""

    def _render(self, **kwargs) -> str:
        from jinja2 import Environment, FileSystemLoader
        env = Environment(
            loader=FileSystemLoader(
                os.path.join(PROJECT_ROOT, "dashboard", "templates"),
            ),
            autoescape=False,
        )
        tmpl = env.from_string(
            "{% from 'macros/widgets.html' import kpi_card_big %}"
            "{{ kpi_card_big(label, value, subtitle, variant) }}"
        )
        return tmpl.render(**kwargs)

    def test_renders_label_and_value(self):
        out = self._render(label="Fichiers", value="19250",
                           subtitle="2.1 Go", variant="default")
        self.assertIn("Fichiers", out)
        self.assertIn("19250", out)
        self.assertIn("2.1 Go", out)
        self.assertIn("dash-kpi-big-default", out)

    def test_warn_variant_class(self):
        out = self._render(label="Health", value="32",
                           subtitle="orphans", variant="warn")
        self.assertIn("dash-kpi-big-warn", out)
```

- [ ] **Step 2: Confirmer FAIL**

```bash
uv run python -m unittest tests.auto.test_overview.TestKpiCardBigMacro -v
```

- [ ] **Step 3: Ajouter la macro dans `dashboard/templates/macros/widgets.html`** (append en bas du fichier)

```jinja


{# kpi_card_big — KPI vedette pour le cockpit Overview.
   variant ∈ {default, ok, warn}. Le subtitle est optionnel. #}
{% macro kpi_card_big(label, value, subtitle="", variant="default") %}
<div class="dash-kpi-big dash-kpi-big-{{ variant }}">
  <div class="dash-kpi-label">{{ label }}</div>
  <div class="dash-kpi-value">{{ value }}</div>
  {% if subtitle %}<div class="dash-kpi-sub">{{ subtitle }}</div>{% endif %}
</div>
{% endmacro %}
```

- [ ] **Step 4: Ajouter le CSS dans `dashboard/static/style.css`** (append à la fin)

```css

/* ───────────────────────────────────────────────────────────────────
   Overview cockpit — KPI vedettes (Layout B rangée 1)
   ─────────────────────────────────────────────────────────────────── */
.dash-kpi-big {
    background: var(--bg-secondary, #1f1f1f);
    border: 1px solid var(--border, #30363d);
    border-radius: 6px;
    padding: 14px 16px;
    display: flex;
    flex-direction: column;
    gap: 4px;
    transition: background 0.15s;
}
.dash-kpi-big:hover { background: rgba(255,255,255,0.03); }
.dash-kpi-big .dash-kpi-label {
    font-size: 11.5px;
    color: var(--text-muted, #8b949e);
    font-weight: 500;
}
.dash-kpi-big .dash-kpi-value {
    font-size: 22px;
    font-weight: 700;
    color: var(--text, #c9d1d9);
    font-variant-numeric: tabular-nums;
}
.dash-kpi-big .dash-kpi-sub {
    font-size: 10.5px;
    color: var(--text-muted, #8b949e);
}
.dash-kpi-big-warn {
    border-color: rgba(248, 81, 73, 0.4);
    background: rgba(248, 81, 73, 0.05);
}
.dash-kpi-big-warn .dash-kpi-value { color: #ff8a82; }
.dash-kpi-big-ok {
    border-color: rgba(63, 185, 80, 0.4);
}
.dash-kpi-big-ok .dash-kpi-value { color: #7ee787; }
```

- [ ] **Step 5: Verify + Commit**

```bash
uv run python -m unittest tests.auto.test_overview.TestKpiCardBigMacro -v
```

Expected: `OK`

```bash
git add dashboard/templates/macros/widgets.html dashboard/static/style.css tests/auto/test_overview.py
git commit -m "feat(overview): macro kpi_card_big + CSS + tests"
```

---

### Task 16: Macro `mini_bar` + CSS

**Files:** mêmes que Task 15.

- [ ] **Step 1: Tests**

```python
class TestMiniBarMacro(unittest.TestCase):

    def _render(self, **kwargs) -> str:
        from jinja2 import Environment, FileSystemLoader
        env = Environment(
            loader=FileSystemLoader(
                os.path.join(PROJECT_ROOT, "dashboard", "templates"),
            ),
            autoescape=False,
        )
        tmpl = env.from_string(
            "{% from 'macros/widgets.html' import mini_bar %}"
            "{{ mini_bar(items, value_key=value_key, label_key=label_key,"
            "            empty=empty) }}"
        )
        return tmpl.render(**kwargs)

    def test_renders_items_with_proportional_widths(self):
        out = self._render(
            items=[{"theme": "Python", "count": 10},
                   {"theme": "Linux", "count": 5}],
            value_key="count", label_key="theme", empty="",
        )
        self.assertIn("Python", out)
        self.assertIn("Linux", out)
        self.assertIn("width: 100.0%", out)
        self.assertIn("width: 50.0%", out)

    def test_empty_uses_fallback_text(self):
        out = self._render(items=[], value_key="count",
                           label_key="theme",
                           empty="Aucun thème en cache.")
        self.assertIn("Aucun thème en cache.", out)
```

- [ ] **Step 2: Confirmer FAIL**

```bash
uv run python -m unittest tests.auto.test_overview.TestMiniBarMacro -v
```

- [ ] **Step 3: Ajouter la macro**

```jinja


{# mini_bar — liste de barres horizontales proportionnelles.
   items     : list[dict] — chaque élément a au moins value_key et label_key
   value_key : nom du champ numérique (count, n_files, …)
   label_key : nom du champ texte (theme, path, …)
   empty     : texte affiché si items est vide #}
{% macro mini_bar(items, value_key='count', label_key='label', empty="") %}
{% if items %}
  {% set vmax = (items | map(attribute=value_key) | max) or 1 %}
  <ul class="dash-mini-bars">
    {% for it in items %}
    <li class="dash-mini-bar-row" title="{{ it[label_key] }} · {{ it[value_key] }}">
      <span class="dash-mini-bar-label">{{ it[label_key] }}</span>
      <span class="dash-mini-bar-track">
        <span class="dash-mini-bar-fill"
              style="width: {{ (it[value_key] / vmax * 100) | round(1) }}%"></span>
      </span>
      <span class="dash-mini-bar-value">{{ it[value_key] }}</span>
    </li>
    {% endfor %}
  </ul>
{% else %}<p class="muted small">{{ empty }}</p>{% endif %}
{% endmacro %}
```

- [ ] **Step 4: Ajouter le CSS**

```css

/* Mini bars (top thèmes / top folders) */
.dash-mini-bars {
    list-style: none; padding: 0; margin: 0;
    display: flex; flex-direction: column; gap: 4px;
}
.dash-mini-bar-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 100px auto;
    align-items: center;
    gap: 8px;
    font-size: 11.5px;
}
.dash-mini-bar-label {
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    color: var(--text, #c9d1d9);
}
.dash-mini-bar-track {
    background: rgba(255,255,255,0.05); border-radius: 3px;
    height: 6px; overflow: hidden;
}
.dash-mini-bar-fill {
    background: var(--blue, #58a6ff); height: 100%; display: block;
}
.dash-mini-bar-value {
    color: var(--text-muted, #8b949e);
    font-variant-numeric: tabular-nums; font-size: 10.5px;
}
```

- [ ] **Step 5: Verify + Commit**

```bash
uv run python -m unittest tests.auto.test_overview.TestMiniBarMacro -v
```

```bash
git add dashboard/templates/macros/widgets.html dashboard/static/style.css tests/auto/test_overview.py
git commit -m "feat(overview): macro mini_bar + CSS + tests"
```

---

### Task 17: Macro `activity_timeline` + CSS

**Files:** mêmes.

- [ ] **Step 1: Tests**

```python
class TestActivityTimelineMacro(unittest.TestCase):

    def _render(self, events, empty=""):
        from jinja2 import Environment, FileSystemLoader
        env = Environment(
            loader=FileSystemLoader(
                os.path.join(PROJECT_ROOT, "dashboard", "templates"),
            ),
            autoescape=False,
        )
        tmpl = env.from_string(
            "{% from 'macros/widgets.html' import activity_timeline %}"
            "{{ activity_timeline(events, empty=empty) }}"
        )
        return tmpl.render(events=events, empty=empty)

    def test_renders_events(self):
        out = self._render([
            {"relative_time": "il y a 2 h",
             "action": "rename", "target": "folder X"},
        ])
        self.assertIn("il y a 2 h", out)
        self.assertIn("rename", out)
        self.assertIn("folder X", out)

    def test_empty(self):
        out = self._render([], empty="Aucune mutation.")
        self.assertIn("Aucune mutation.", out)
```

- [ ] **Step 2: FAIL**

```bash
uv run python -m unittest tests.auto.test_overview.TestActivityTimelineMacro -v
```

- [ ] **Step 3: Macro**

```jinja


{# activity_timeline — liste temporelle des mutations récentes. #}
{% macro activity_timeline(events, empty="") %}
{% if events %}
  <ul class="dash-activity">
    {% for e in events %}
    <li>
      <span class="dash-activity-time">{{ e.relative_time }}</span>
      <span class="dash-activity-action">{{ e.action }}</span>
      <span class="dash-activity-target">{{ e.target }}</span>
    </li>
    {% endfor %}
  </ul>
{% else %}<p class="muted small">{{ empty }}</p>{% endif %}
{% endmacro %}
```

- [ ] **Step 4: CSS**

```css

/* Activity timeline */
.dash-activity {
    list-style: none; padding: 0; margin: 0;
    display: flex; flex-direction: column; gap: 4px;
    font-size: 11px;
}
.dash-activity li {
    display: grid;
    grid-template-columns: auto auto 1fr;
    gap: 8px;
    padding: 3px 0;
    border-bottom: 1px dashed rgba(255,255,255,0.04);
}
.dash-activity-time {
    color: var(--text-muted, #8b949e);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
}
.dash-activity-action {
    color: var(--blue, #58a6ff);
    font-weight: 600;
}
.dash-activity-target {
    color: var(--text, #c9d1d9);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
```

- [ ] **Step 5: Verify + Commit**

```bash
uv run python -m unittest tests.auto.test_overview.TestActivityTimelineMacro -v
git add dashboard/templates/macros/widgets.html dashboard/static/style.css tests/auto/test_overview.py
git commit -m "feat(overview): macro activity_timeline + CSS + tests"
```

---

### Task 18: CSS du header + grilles (sans macros)

**Files:**

- Modify: `dashboard/static/style.css` (append CSS de structure)

Tâche sans test (CSS pur de structure — tests E2E couvriront la page rendue).

- [ ] **Step 1: Append le CSS de structure**

```css

/* ───────────────────────────────────────────────────────────────────
   Overview cockpit — header + grilles + cards génériques
   ─────────────────────────────────────────────────────────────────── */
.dash-overview-header {
    display: flex; justify-content: space-between; align-items: center;
    gap: 16px; margin-bottom: 16px;
}
.dash-overview-toolbar {
    display: flex; align-items: center; gap: 10px;
}
.dash-overview-toolbar label {
    color: var(--text-muted, #8b949e);
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.4px;
}
.dash-refresh-btn {
    background: var(--bg-secondary, #1f1f1f);
    border: 1px solid var(--border, #30363d);
    color: var(--text, #c9d1d9);
    width: 30px; height: 28px; border-radius: 5px;
    display: inline-flex; align-items: center; justify-content: center;
    text-decoration: none; font-size: 13px;
    transition: background 0.12s;
}
.dash-refresh-btn:hover { background: rgba(255,255,255,0.06); }
.dash-global-cost {
    background: rgba(56, 139, 253, 0.1);
    border: 1px solid rgba(56, 139, 253, 0.3);
    color: var(--text, #c9d1d9);
    padding: 4px 10px; border-radius: 5px;
    font-size: 12px;
}

/* Grilles Layout B */
.dash-kpi-row {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 10px;
    margin-bottom: 12px;
}
.dash-grid-2-bias-right {
    display: grid;
    grid-template-columns: 1fr 1.3fr;
    gap: 10px;
    margin-bottom: 12px;
}
.dash-grid-2-bias-left {
    display: grid;
    grid-template-columns: 1.3fr 1fr;
    gap: 10px;
    margin-bottom: 12px;
}
.dash-card {
    background: var(--bg-secondary, #1f1f1f);
    border: 1px solid var(--border, #30363d);
    border-radius: 6px;
    padding: 12px 14px;
}
.dash-card h3 {
    margin: 0 0 10px 0;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.4px;
    color: var(--text-muted, #8b949e);
}
.dash-details-list {
    list-style: none; padding: 0; margin: 0;
    display: flex; flex-direction: column; gap: 6px;
    font-size: 12px;
}
.dash-details-list .muted {
    color: var(--text-muted, #8b949e); font-size: 10.5px;
}

/* Section générale — grille 4×1 */
.dash-general-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 10px;
    margin-top: 20px;
}

/* Responsive */
@media (max-width: 1280px) {
    .dash-grid-2-bias-right,
    .dash-grid-2-bias-left {
        grid-template-columns: 1fr;
    }
}
@media (max-width: 900px) {
    .dash-kpi-row,
    .dash-general-grid {
        grid-template-columns: 1fr;
    }
    .dash-overview-header {
        flex-direction: column;
        align-items: flex-start;
    }
}
```

- [ ] **Step 2: Vérifier que le serveur démarre sans erreur CSS**

```bash
uv run python -c "from dashboard.app import app; print('imports ok')"
```

Expected: `imports ok`

- [ ] **Step 3: Commit**

```bash
git add dashboard/static/style.css
git commit -m "style(overview): header + grids + cards CSS for cockpit layout"
```

---

### Task 19: Partial `overview_profile.html` (Layout B)

**Files:**

- Create: `dashboard/templates/partials/overview_profile.html`

Pas de test isolé — sera couvert par les routing tests Task 22.

- [ ] **Step 1: Créer le fichier**

```jinja
{# Moitié haute du cockpit Overview — Layout B (hiérarchique).
   Contexte attendu : s (profile_snapshot). #}
{% from "macros/widgets.html" import kpi_card_big, mini_bar, activity_timeline %}

{# ── Rangée 1 : 4 KPI vedettes ── #}
<div class="dash-kpi-row">
  {% if s.files.get('error') %}
    {{ kpi_card_big("📁 Fichiers", "—",
                    "target manquant" if s.files.error == 'target_missing'
                    else "profil incomplet",
                    variant="warn") }}
  {% else %}
    {{ kpi_card_big("📁 Fichiers", s.files.total,
                    s.files.size_gb ~ ' Go') }}
  {% endif %}

  {% if s.files.total %}
    {{ kpi_card_big("📊 Classifiés", s.classified.rate ~ '%',
                    s.classified.classified ~ '/' ~ s.files.total) }}
  {% else %}
    {{ kpi_card_big("📊 Classifiés", "—", "pas de fichier à classer") }}
  {% endif %}

  {{ kpi_card_big("💰 Coût LLM", '$' ~ s.llm_cost.cost_usd,
                  s.llm_cost.n_calls ~ ' appel(s)') }}

  {% set health_variant = "warn" if s.health.n_orphans_total > 0
                         else ("ok" if s.health.locks_active|length == 0
                               else "default") %}
  {{ kpi_card_big("⚠ Health", s.health.n_orphans_total,
                  "orphan(s)" if s.health.n_orphans_total
                  else "tout propre",
                  variant=health_variant) }}
</div>

{# ── Rangée 2 : Détails (1fr) + Top thèmes (1.3fr) ── #}
<div class="dash-grid-2-bias-right">
  <div class="dash-card">
    <h3>Détails</h3>
    <ul class="dash-details-list">
      <li>🗂 {{ s.folders.total }} folders
        <span class="muted">· profondeur max {{ s.folders.max_depth }}</span></li>
      <li>👁 {{ s.vision_cache.n_entries }} analyses vision
        <span class="muted">· {{ s.vision_cache.size_kb }} Ko</span></li>
      <li>📥 {% if s.inbox.exists %}{{ s.inbox.n_files }} inbox
        <span class="muted">· {{ s.inbox.size_mb }} Mo</span>{% else %}— inbox{% endif %}</li>
      <li>🎯 {{ s.baseline_runs.n_runs }} runs baseline</li>
      <li>🤖 {{ s.agent_sessions.refonte.n_batches }} refonte
        · {{ s.agent_sessions.dedupli.n_batches }} dédupli</li>
    </ul>
  </div>
  <div class="dash-card">
    <h3>🔥 Top 10 thèmes LLM</h3>
    {{ mini_bar(s.top_themes, value_key='count', label_key='theme',
                empty="Aucun thème en cache.") }}
  </div>
</div>

{# ── Rangée 3 : Top folders (1.3fr) + Activité (1fr) ── #}
<div class="dash-grid-2-bias-left">
  <div class="dash-card">
    <h3>📈 Top 10 folders</h3>
    {{ mini_bar(s.top_folders, value_key='n_files', label_key='path',
                empty="Aucun folder peuplé.") }}
  </div>
  <div class="dash-card">
    <h3>🕒 Activité récente</h3>
    {{ activity_timeline(s.recent_activity,
                         empty="Aucune mutation enregistrée.") }}
  </div>
</div>
```

- [ ] **Step 2: Commit**

```bash
git add dashboard/templates/partials/overview_profile.html
git commit -m "feat(overview): partial overview_profile.html (Layout B)"
```

---

### Task 20: Partial `overview_general.html`

**Files:**

- Create: `dashboard/templates/partials/overview_general.html`

- [ ] **Step 1: Créer le fichier**

```jinja
{# Moitié basse du cockpit Overview — infos générales statiques.
   Contexte attendu : g (general_snapshot). #}
{% set g = general_snapshot %}

<h2 class="section-title" style="margin-top: 20px;">Infos générales</h2>

<div class="dash-general-grid">
  {# 🧠 LLM models par profil #}
  <div class="dash-card">
    <h3>🧠 Modèles LLM</h3>
    {% if g.llm_models %}
    <ul class="dash-details-list" style="font-size: 11px;">
      {% for m in g.llm_models %}
      <li>
        <strong>{{ m.profile }}</strong>
        <div class="muted">{{ m.provider }} · {{ m.model }}</div>
      </li>
      {% endfor %}
    </ul>
    {% else %}<p class="muted small">Aucun profil.</p>{% endif %}
  </div>

  {# 🔑 API keys #}
  <div class="dash-card">
    <h3>🔑 API keys</h3>
    <ul class="dash-details-list" style="font-size: 12px;">
      {% for k in g.api_keys %}
      <li>
        {% if k.configured %}
          <span style="color: #7ee787;">●</span>
        {% else %}
          <span style="color: #f85149;">○</span>
        {% endif %}
        {{ k.name }}
        <span class="muted">
          {{ 'configurée' if k.configured else 'non configurée' }}
        </span>
      </li>
      {% endfor %}
    </ul>
  </div>

  {# 👥 Profils existants #}
  <div class="dash-card">
    <h3>👥 Profils</h3>
    <ul class="dash-details-list" style="font-size: 12px;">
      {% for p in g.profiles_list %}
      <li>
        {% if p.target_exists %}
          <span style="color: #7ee787;">●</span>
        {% else %}
          <span style="color: #d29922;" title="target absent">●</span>
        {% endif %}
        {{ p.name }}
      </li>
      {% endfor %}
    </ul>
  </div>

  {# 💵 Coûts cumulés — pie chart Chart.js #}
  <div class="dash-card">
    <h3>💵 Coûts cumulés</h3>
    {% if g.global_cost.by_profile %}
    <canvas id="dash-cost-pie" height="140"></canvas>
    <script>
      (function() {
        var ctx = document.getElementById('dash-cost-pie');
        if (!ctx) return;
        var data = {{ g.global_cost.by_profile | tojson }};
        new Chart(ctx, {
          type: 'doughnut',
          data: {
            labels: data.map(function(d){return d.profile;}),
            datasets: [{
              data: data.map(function(d){return d.cost_usd;}),
              backgroundColor: ['#58a6ff','#7ee787','#d29922','#f85149','#a371f7'],
              borderColor: '#1f1f1f', borderWidth: 1,
            }]
          },
          options: {
            responsive: true, maintainAspectRatio: false, cutout: '55%',
            plugins: { legend: { position: 'bottom', labels: {
              color: '#8b949e', font: { size: 10 }, padding: 6,
              boxWidth: 10, usePointStyle: true } } }
          }
        });
      })();
    </script>
    {% else %}
    <p class="muted small">Aucun appel LLM enregistré.</p>
    {% endif %}
  </div>
</div>
```

- [ ] **Step 2: Commit**

```bash
git add dashboard/templates/partials/overview_general.html
git commit -m "feat(overview): partial overview_general.html (general info grid)"
```

---

### Task 21: Shell `overview.html` + endpoint refactor

**Files:**

- Rewrite: `dashboard/templates/overview.html`
- Modify: `dashboard/app.py` (endpoint `overview()`)

- [ ] **Step 1: Réécrire `dashboard/templates/overview.html`**

```jinja
{% extends "base.html" %}
{% set active = "overview" %}
{% block title %}Overview — {{ current_profile }}{% endblock %}
{% block content %}

<div class="dash-overview-header">
  <h1 class="page-title">📚 Overview</h1>
  <div class="dash-overview-toolbar">
    <label>Profil</label>
    <select class="filter-select"
            onchange="window.location.href='/?profile=' + encodeURIComponent(this.value)">
      {% if not available_profiles %}
      <option>(aucun profil)</option>
      {% endif %}
      {% for p in available_profiles %}
      <option value="{{ p.name }}"
              {% if p.name == current_profile %}selected{% endif %}>
        {{ p.name }}
      </option>
      {% endfor %}
    </select>
    <a href="/?profile={{ current_profile }}&refresh=1"
       class="dash-refresh-btn" title="Recalculer (force cache miss)">🔄</a>
    <div class="dash-global-cost">
      Coût total : <strong>${{ general_snapshot.global_cost.total_usd }}</strong>
    </div>
  </div>
</div>

{% set s = profile_snapshot %}
{% include "partials/overview_profile.html" %}
{% include "partials/overview_general.html" %}

{% endblock %}
```

- [ ] **Step 2: Réécrire l'endpoint `overview()` dans `dashboard/app.py`**

Trouve la fonction `overview()` actuelle (probablement vers la ligne 57) et remplace par :

```python
@app.get("/")
async def overview(
    request: Request,
    profile: str = "default",
    refresh: bool = False,
):
    """Cockpit Biblio — moitié haute profile-aware, moitié basse générale.

    Whitelist du paramètre `profile` : fallback transparent sur le 1er profil
    disponible si la valeur est inconnue. `?refresh=1` invalide le cache 30 s.
    """
    from dashboard import overview as ov
    available = data.get_available_profiles()
    names = {p["name"] if isinstance(p, dict) else p for p in available}
    if profile not in names:
        if available:
            first = available[0]
            profile = first["name"] if isinstance(first, dict) else first
        else:
            profile = "default"
    if refresh:
        ov.reset_cache(profile)
    return templates.TemplateResponse(request, "overview.html", {
        "active": "overview",
        "current_profile": profile,
        "available_profiles": available,
        "profile_snapshot": ov.build_profile_snapshot(profile, force=refresh),
        "general_snapshot": ov.build_general_snapshot(),
    })
```

- [ ] **Step 3: Vérifier que la page se rend sans erreur**

Démarre le serveur sur un port libre, hit `/`, attends 200 :

```bash
uv run python -m dashboard 8085 &
SERVER_PID=$!
sleep 4
curl -s -o /tmp/overview-out.html -w "code=%{http_code}\n" http://127.0.0.1:8085/
kill $SERVER_PID
```

Expected: `code=200`. Inspecte `/tmp/overview-out.html` — doit contenir `📚 Overview`, `dash-kpi-big`, `Coût total :`.

- [ ] **Step 4: Lint**

```bash
uv run ruff check dashboard/overview.py dashboard/app.py
```

Expected: `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add dashboard/templates/overview.html dashboard/app.py
git commit -m "feat(overview): shell overview.html + endpoint refactor"
```

---

### Task 22: Routing tests dans `test_dashboard.py`

**Files:**

- Modify: `tests/auto/test_dashboard.py` (append 7 tests)

- [ ] **Step 1: Ajouter les tests dans `TestDashboardRoutes`** (juste avant `def test_admin_page`)

```python
    # ─── Overview cockpit refactor 2026-06-05 ─────────────────────

    def test_overview_default_profile(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        # Le selector contient "default" comme option sélectionnée
        self.assertIn(b'value="default"', r.content)

    def test_overview_explicit_profile(self):
        r = self.client.get("/?profile=test")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"test", r.content)

    def test_overview_unknown_profile_falls_back(self):
        """?profile=foo → fallback transparent (pas de 404)."""
        r = self.client.get("/?profile=foo-does-not-exist")
        self.assertEqual(r.status_code, 200)

    def test_overview_refresh_param_returns_200(self):
        r = self.client.get("/?profile=default&refresh=1")
        self.assertEqual(r.status_code, 200)

    def test_overview_contains_global_cost_banner(self):
        r = self.client.get("/")
        self.assertIn(b"Co", r.content)  # "Coût" présent (UTF-8 ou HTML entity)
        self.assertIn(b"dash-global-cost", r.content)

    def test_overview_contains_kpi_grid(self):
        r = self.client.get("/")
        self.assertIn(b"dash-kpi-big", r.content)

    def test_overview_no_longer_contains_test_kpis(self):
        """Régression : Overview ne doit plus afficher Pass/Fail/Skip."""
        r = self.client.get("/")
        # L'ancienne page contenait "Heatmap par phase"
        self.assertNotIn(b"Heatmap par phase", r.content)
        # Et "Release Gate"
        self.assertNotIn(b"Release Gate", r.content)
```

- [ ] **Step 2: Vérifier que les 7 tests passent**

```bash
uv run python -m unittest tests.auto.test_dashboard.TestDashboardRoutes -v 2>&1 | grep -E "overview_|OK|FAIL"
```

Expected: 7 tests `overview_*` en `ok`.

- [ ] **Step 3: Commit**

```bash
git add tests/auto/test_dashboard.py
git commit -m "test(overview): routing tests for / hub (7 cases)"
```

---

### Task 23: Full suite + lint + smoke test manuel

**Files:** aucun changement de code prévu.

- [ ] **Step 1: Lancer la suite complète**

```bash
uv run python -m unittest discover -s tests/auto -t .
```

Expected: `OK · Ran NNNN tests in <60s` (visé : ~1280 tests verts).

Si un test inattendu échoue : investiguer et fixer (étape supplémentaire).

- [ ] **Step 2: Lint global**

```bash
uv run ruff check .
```

Expected: `All checks passed!`

- [ ] **Step 3: Smoke test manuel — 6 vérifications**

Démarre le serveur :

```bash
uv run python -m dashboard 8080
```

Dans le browser :

1. Ouvre `http://localhost:8080/` → page contient le header `📚 Overview`, dropdown profil défaut sur `default`, bandeau `Coût total`
2. 4 KPI vedettes visibles (Fichiers, Classifiés, Coût LLM, Health)
3. 2 grilles 2-cols (Détails + Top thèmes, puis Top folders + Activité)
4. Section générale 4×1 visible en bas (LLM models, API keys, Profils, Pie coûts)
5. Switch dropdown sur `test` → URL devient `/?profile=test`, données changent
6. Click 🔄 → URL gagne `&refresh=1`, page se recharge

- [ ] **Step 4: Push branche**

```bash
git push -u origin feature/overview-cockpit
```

---

### Task 24: PR vers develop

**Files:** aucun.

- [ ] **Step 1: Créer la PR**

```bash
gh pr create --base develop \
  --title "feat(overview): cockpit Biblio profile-aware (12 cartes + 4 générales)" \
  --body "$(cat <<'EOF'
## Summary

Refonte de l'onglet Overview en cockpit Biblio profile-aware. L'ancienne
page (KPIs tests Pass/Fail, heatmap par phase, release gate) devient
redondante depuis que Tests est un hub dédié (PR #166).

- Sélecteur de profil dropdown → URL `?profile=X`
- Bouton 🔄 refresh → URL `?refresh=1` invalide le cache 30 s
- 12 cartes profile-aware (Layout B hiérarchique)
- 4 cartes générales statiques (LLM models, API keys, profils, coûts)
- Nouveau module `dashboard/overview.py` (~400 LOC)
- 3 macros Jinja réutilisables (`kpi_card_big`, `mini_bar`, `activity_timeline`)
- ~30+ nouveaux tests unit + 7 routing tests

## Test plan

- [x] Suite complète verte (~1280 tests)
- [x] Lint ruff clean
- [x] Smoke manuel : page rendue, switch profil, bouton refresh, cohérence
      avec base.html nav-bar

Spec : [docs/superpowers/specs/2026-06-05-overview-cockpit-biblio-design.md](docs/superpowers/specs/2026-06-05-overview-cockpit-biblio-design.md)
Plan : [docs/superpowers/plans/2026-06-05-overview-cockpit-biblio.md](docs/superpowers/plans/2026-06-05-overview-cockpit-biblio.md)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 2: Vérifier la PR créée**

```bash
gh pr view --json url,number,baseRefName,mergeable
```

Expected: `mergeable=MERGEABLE`, `baseRefName=develop`.

---

## Self-review

**1. Spec coverage** : ✓

- 12 cartes profil → Tasks 2-11 (`files_count`, `classified_rate`, `folders_count`, `llm_cost`, `vision_cache`, `inbox`, `baseline_runs`, `agent_sessions`, `health`, `top_themes`, `top_folders`, `recent_activity`)
- 4 cartes générales → Tasks 12-13 (`llm_models`, `api_keys`, `profiles_list`, `global_cost`)
- Cache TTL 30 s → Task 14
- Macros + CSS → Tasks 15-18
- Partials + shell + endpoint → Tasks 19-21
- Routing tests → Task 22
- Smoke + PR → Tasks 23-24
- Empty states (`—` + tooltip) → géré dans Task 19 (le partial) via guards `s.files.get('error')` et `if s.inbox.exists`
- Refresh button → Task 21 (`?refresh=1` reset cache + force snapshot)

**2. Placeholder scan** : ✓ — chaque step contient le code complet. Aucun "TBD" / "TODO" / "similar to". Les sentinelles d'erreur (`error: 'target_missing'`, `error: 'profile_missing'`, `error: 'tree_missing'`) sont des strings concrètes et testées.

**3. Type consistency** : ✓ — vérification croisée :

- `card_files_count` → `{total, size_gb, by_ext, error?}` utilisé dans Task 19 via `s.files.total`, `s.files.size_gb`, `s.files.get('error')`
- `card_classified_rate` → `{classified, unclassified, rate, fallback_count}` utilisé via `s.classified.rate`, `s.classified.classified`
- `card_health` → `{n_orphans_total, locks_active, ...}` utilisé via `s.health.n_orphans_total`, `s.health.locks_active|length`
- `card_inbox` → `{n_files, size_mb, oldest_iso, exists}` utilisé via `s.inbox.exists`, `s.inbox.n_files`
- `card_top_themes` retourne `list[dict]` avec clés `theme/count/pct` — utilisé par `mini_bar` (value_key='count', label_key='theme'). ✓
- `card_top_folders` retourne `list[dict]` avec `path/n_files/pct` — `mini_bar` (value_key='n_files', label_key='path'). ✓
- `card_recent_activity` retourne `{ts, relative_time, action, target}` — `activity_timeline` lit `e.relative_time`, `e.action`, `e.target`. ✓
- `card_global_cost` → `{total_usd, by_profile: [{profile, cost_usd, n_calls, pct}]}` utilisé dans Task 20 (pie chart map sur `d.profile`, `d.cost_usd`) et header (`general_snapshot.global_cost.total_usd`). ✓
- `card_agent_sessions` → `{refonte: {n_batches, status}, dedupli: {n_batches, status}}` utilisé via `s.agent_sessions.refonte.n_batches`. ✓
- `card_baseline_runs` → `{n_runs, last_run_iso}` utilisé via `s.baseline_runs.n_runs`. ✓

Toutes les méthodes et noms de champs sont cohérents bout-à-bout.
