# Agent Onboarding — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bootstrap d'un profil Klodo depuis un répertoire brut : Vision LLM sur tout le corpus → proposition d'une taxonomie de départ (tree + theme_mapping + categories) → dry-run de couverture, puis handoff vers les outils Taxonomie/Apply existants.

**Architecture:** Un **pipeline** (pas un agent ReAct/LangGraph conversationnel) dans `agents/onboarding/` : fonctions pures + 2 appels LLM ciblés (propose-hiérarchie + categories réutilisé). Un wrapper dashboard `agent_onboarding.py` (thread daemon + polling `status.json`, calqué sur `agent_refonte.py`). UI atomique : page assistant + handoff vers l'onglet Mappings. Profil **brouillon** créé tôt (flag `onboarding_draft`).

**Tech Stack:** Python 3.13 / FastAPI / unittest (mock LLM & Vision, zéro SSD réel) / langchain_openai (via la factory `agents.llm.get_agent_llm`) / vanilla JS.

**Branche:** `feature/agents-onboarding` (déjà créée depuis `develop`, spec + maquette commitées).

**Spec:** `docs/superpowers/specs/2026-06-14-onboarding-agent-design.md`.

**Conventions projet:** commits anglais, doc/UI français, `uv run python -m unittest`, jamais de Vision/LLM réel en test (mock), jamais de test sur `/Volumes/ExtSSD`. **Noms d'étapes explicites** (« Scan & estimation », « Analyse & proposition »), jamais « Phase A/B ».

---

## Contexte codebase (à lire avant Task 1)

Briques **réutilisées** (signatures vérifiées) :

- **Vision** — `lib/vision.py:analyze_cover(pdf_path, api_key='', endpoint='', model=DEFAULT_MODEL, n_pages=1, ...) -> dict|None` retourne `{title, author, theme, themes:[{theme,confidence,reason}], language, confidence}`.
- **Cache Vision** — `lib/vision_cache.py` : `compute_cache_key(pdf_path, model, n_pages) -> str|None` ; `lookup(cache, key) -> dict|None` (retourne `entry['result']`) ; `store(cache, key, result, model)` (en mémoire) ; `save_cache(cache_path, cache)` (disque, atomique, merge-safe). Entrée = `{result:{...}, model, prompt_version, cached_at}`.
- **Clustering** — `lib/theme_canon.py:extract_themes_from_vision_cache(profile) -> dict[str,int]` ({raw_theme: count}) ; `lib/theme_normalizer.py:cluster_themes(raw_themes, threshold=90, top_k=15) -> list[dict]` (chaque cluster = `{"canonical_forms":[...], "raw_members":[...]}`, trié taille décroissante).
- **Factory LLM** — `agents/llm.py:get_agent_llm() -> ChatOpenAI` (modèle `zai-org/GLM-4.7`, SiliconFlow, `streaming=True`, lève `RuntimeError` si pas de `SILICONFLOW_API_KEY`).
- **Catégories LLM** — `agents/refonte/categories_llm.py:propose_keywords_for_new_folders(llm, creations, existing_groupes, groupe_inference, sample_entries) -> list[{chemin,groupe,priorite,mots_cles}]`. `creations = [{"path":str,"rationale":str}]`. `agents/refonte/proposition_tools.py:_groupe_from_path_prefix(path, existing_cats) -> str`.
- **Dry-run couverture** — `dashboard/taxonomy.py:reclassify_dryrun(profile, sample_size=50, include_step2=True) -> {ok, stats:{n_in_lib, n_with_prediction, n_no_prediction, n_moving, n_stable, n_via_step1, n_via_step2}, by_destination, sample_moves, limits}`.
- **Profil** — `lib/profile.py:init_profile(name, target) -> Profile` (skeleton : `profile.yaml`, `tree.yaml:{folders:[]}`, `theme_mapping.yaml:{}`, `refinement.yaml`, `categories.yaml:{}`). Classe `Profile` (charge `name/target/llm_model/llm_endpoint/cache_dir`).
- **Patron dashboard** — `dashboard/agent_refonte.py` : `_runs_dir(profile)` (`profiles/<p>/.cache/refonte`), `_run_dir`, `_status_path`, `_write_status(profile, run_id, payload)`, `_read_status`, `start_diagnostic` (UUID + thread daemon + status initial), `_run_diagnostic` (try/finally). `agents/refonte/agent_journal.append_entry(profile, *, tool, ...)`, `agent_backup.create_backup(profile, batch_id)`.
- **UI** — `dashboard/templates/base.html` (sidebar 7 onglets + footer profil **texte statique**), `dashboard/templates/agent_refonte.html` (page standalone : extends base + inclut partial), `agent_refonte_panel.html` (poll `setInterval(2000)`), `dashboard/templates/taxonomy.html` (bandeau `.tax-cat-info-banner`, haut du panneau Mappings).

`profile.yaml` : clés top-level `name/description/target/inbox/fallback` + bloc `llm:{provider,model,endpoint}` + bloc `defaults:{workers,delay,cost_per_call,pages,...}`. On ajoute un flag top-level **`onboarding_draft: true|false`**.

## File structure

| Fichier | Action | Responsabilité |
| --- | --- | --- |
| `lib/profile.py` | Modify | charger `onboarding_draft` + `create_draft_profile()` + `set_onboarding_draft()` |
| `agents/onboarding/__init__.py` | Create | exports du pipeline |
| `agents/onboarding/scan.py` | Create | `scan_directory` + `estimate_cost` (léger, read-only) |
| `agents/onboarding/taxonomy_llm.py` | Create | `propose_taxonomy` (appel LLM « hiérarchie depuis clusters » + conventions) |
| `agents/onboarding/proposition.py` | Create | orchestration : vision → cluster → propose → categories → write → dry-run |
| `dashboard/agent_onboarding.py` | Create | wrapper thread+poll (scan sync, onboarding thread, status, finalize) |
| `dashboard/app.py` | Modify | 4 routes `/api/agent/onboarding/*` + page `GET /onboarding` |
| `dashboard/templates/onboarding.html` + partial | Create | page assistant (stepper) |
| `dashboard/templates/base.html` | Modify | entrée « ➕ Nouveau profil » + badge brouillon |
| `dashboard/templates/taxonomy.html` + `static/style.css` | Modify | bandeau brouillon + CSS |
| `dashboard/CLAUDE.md`, `agents/CLAUDE.md` | Modify | doc |
| `tests/auto/test_agent_onboarding.py` | Create | tests (Vision/LLM mockés) |

---

### Task 1: Profil brouillon — flag `onboarding_draft` + `create_draft_profile`

**Files:**
- Modify: `lib/profile.py`
- Test: `tests/auto/test_agent_onboarding.py` (nouveau)

- [ ] **Step 1: Write the failing tests** — créer `tests/auto/test_agent_onboarding.py` :

```python
#!/usr/bin/env python3
"""Tests de l'agent Onboarding (Vision/LLM mockés, zéro SSD réel)."""

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


class TestDraftProfile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="klodo-onb-")
        self.root = Path(self.tmp)
        self.target = self.root / "RAW"
        self.target.mkdir(parents=True)
        self.patch = mock.patch("lib.profile.get_project_root", return_value=self.root)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_create_draft_profile_sets_flag(self):
        from lib import profile as prof
        p = prof.create_draft_profile("perso-2026", str(self.target))
        self.assertTrue(p.onboarding_draft)
        cfg = yaml.safe_load((self.root / "profiles" / "perso-2026" / "profile.yaml").read_text())
        self.assertTrue(cfg["onboarding_draft"])
        self.assertEqual(cfg["target"], str(self.target))

    def test_normal_profile_has_flag_false(self):
        from lib import profile as prof
        prof.init_profile("normal", str(self.target))
        p = prof.Profile("normal")
        self.assertFalse(p.onboarding_draft)

    def test_set_onboarding_draft_toggles(self):
        from lib import profile as prof
        prof.create_draft_profile("perso-2026", str(self.target))
        prof.set_onboarding_draft("perso-2026", False)
        cfg = yaml.safe_load((self.root / "profiles" / "perso-2026" / "profile.yaml").read_text())
        self.assertFalse(cfg["onboarding_draft"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run → FAIL** : `uv run python -m unittest tests.auto.test_agent_onboarding.TestDraftProfile -v` → `AttributeError: module 'lib.profile' has no attribute 'create_draft_profile'`.

- [ ] **Step 3: Implement** dans `lib/profile.py` :

(a) Dans `Profile._load_profile_yaml` (la méthode qui lit `profile.yaml`), après le chargement de `target`, ajouter :
```python
        self.onboarding_draft = bool(data.get("onboarding_draft", False))
```
(Repère `_load_profile_yaml` (~ligne 137) et la variable `data` du `yaml.safe_load` ; ajoute l'attribut à côté des autres `self.xxx`.)

(b) Ajouter après `init_profile` :
```python
def create_draft_profile(name: str, target: str) -> "Profile":
    """Crée un profil brouillon (skeleton vide + flag onboarding_draft=true).

    L'onboarding y écrira ensuite les 3 YAMLs proposés. Le flag marque le
    profil comme incomplet jusqu'à finalisation.
    """
    p = init_profile(name, target)
    set_onboarding_draft(name, True)
    return Profile(name)


def set_onboarding_draft(name: str, value: bool) -> None:
    """Écrit/retire le flag onboarding_draft dans profile.yaml (préserve le reste)."""
    path = get_project_root() / PROFILES_DIR / name / "profile.yaml"
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg["onboarding_draft"] = bool(value)
    path.write_text(
        yaml.safe_dump(cfg, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8")
```
(Vérifie le nom de la constante du dossier profils dans `lib/profile.py` — `PROFILES_DIR` — et le helper racine — `get_project_root`. Adapte si différents.)

- [ ] **Step 4: Run → PASS** : `uv run python -m unittest tests.auto.test_agent_onboarding.TestDraftProfile -v` → 3 verts. Non-régression : `uv run python -m unittest tests.auto.test_compile tests.auto.test_imports -q 2>&1 | tail -3`.

- [ ] **Step 5: Commit**
```bash
uv run ruff check lib/profile.py tests/auto/test_agent_onboarding.py
git add lib/profile.py tests/auto/test_agent_onboarding.py
git commit -m "feat(onboarding): draft profile (onboarding_draft flag + create_draft_profile)"
```

---

### Task 2: Scan & estimation

**Files:**
- Create: `agents/onboarding/__init__.py`, `agents/onboarding/scan.py`
- Test: `tests/auto/test_agent_onboarding.py`

- [ ] **Step 1: Write the failing tests** — ajouter :

```python
class TestScanEstimate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="klodo-scan-")
        self.d = Path(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _f(self, rel):
        p = self.d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"%PDF-1.4 x")

    def test_scan_flat(self):
        from agents.onboarding import scan
        for n in ("a.pdf", "b.pdf", "c.epub", "notes.txt"):
            self._f(n)
        r = scan.scan_directory(str(self.d))
        self.assertEqual(r["n_files"], 3)            # pdf+epub, pas le .txt
        self.assertEqual(r["by_format"]["pdf"], 2)
        self.assertEqual(r["by_format"]["epub"], 1)
        self.assertFalse(r["has_subfolders"])        # plat

    def test_scan_preorg(self):
        from agents.onboarding import scan
        for n in ("Prog/a.pdf", "Prog/b.pdf", "Sci/c.pdf"):
            self._f(n)
        r = scan.scan_directory(str(self.d))
        self.assertTrue(r["has_subfolders"])
        self.assertEqual(set(r["top_folders"]), {"Prog", "Sci"})

    def test_estimate_cost(self):
        from agents.onboarding import scan
        e = scan.estimate_cost(n_files=1000, cost_per_call=0.00034, n_pages=2)
        self.assertEqual(e["n_calls"], 1000)
        self.assertAlmostEqual(e["usd"], 0.34, places=2)
        self.assertIn("eta_min", e)
```

- [ ] **Step 2: Run → FAIL** (`No module named 'agents.onboarding'`).

- [ ] **Step 3: Implement**

`agents/onboarding/__init__.py` :
```python
"""Agent Onboarding — bootstrap d'un profil depuis un répertoire brut.

Pipeline (pas un agent ReAct) : Scan & estimation → Analyse & proposition.
Voir docs/superpowers/specs/2026-06-14-onboarding-agent-design.md
"""
```

`agents/onboarding/scan.py` :
```python
"""Scan & estimation — léger, read-only, aucun appel LLM."""

from __future__ import annotations

import os

_EXTS = (".pdf", ".epub")


def scan_directory(path: str) -> dict:
    """Compte les fichiers classables + formats + détecte une pré-organisation."""
    n_files = 0
    by_format: dict[str, int] = {}
    top_folders: set[str] = set()
    has_sub = False
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        rel = os.path.relpath(root, path)
        if rel != ".":
            has_sub = True
            top_folders.add(rel.split(os.sep)[0])
        for f in files:
            if f.startswith(".") or not f.lower().endswith(_EXTS):
                continue
            n_files += 1
            ext = f.lower().rsplit(".", 1)[-1]
            by_format[ext] = by_format.get(ext, 0) + 1
    return {
        "path": path,
        "n_files": n_files,
        "by_format": by_format,
        "has_subfolders": has_sub,
        "top_folders": sorted(top_folders),
    }


def estimate_cost(n_files: int, cost_per_call: float, n_pages: int = 2,
                  sec_per_call: float = 1.1) -> dict:
    """Estimation Vision : 1 appel/fichier. Coût + durée approximatifs."""
    n_calls = n_files
    return {
        "n_calls": n_calls,
        "usd": round(n_calls * cost_per_call, 2),
        "eta_min": round(n_calls * sec_per_call / 60, 1),
    }
```

- [ ] **Step 4: Run → PASS** (3 tests).
- [ ] **Step 5: Commit**
```bash
uv run ruff check agents/onboarding/ tests/auto/test_agent_onboarding.py
git add agents/onboarding/__init__.py agents/onboarding/scan.py tests/auto/test_agent_onboarding.py
git commit -m "feat(onboarding): scan_directory + estimate_cost"
```

---

### Task 3: Vision sur tout le corpus (reprenable)

**Files:**
- Create: `agents/onboarding/proposition.py`
- Test: `tests/auto/test_agent_onboarding.py`

- [ ] **Step 1: Write the failing tests** :

```python
class TestRunVision(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="klodo-vis-")
        self.root = Path(self.tmp)
        self.target = self.root / "RAW"
        for n in ("a.pdf", "b.pdf"):
            p = self.target / n
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"%PDF-1.4 " + n.encode())
        self.prof = self.root / "profiles" / "perso"
        self.prof.mkdir(parents=True)
        (self.prof / "profile.yaml").write_text(yaml.safe_dump({
            "target": str(self.target), "llm": {"model": "M",
            "endpoint": "https://e"}, "defaults": {"workers": 1, "pages": 2}}),
            encoding="utf-8")
        self.patch = mock.patch("lib.profile.get_project_root", return_value=self.root)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_run_vision_populates_cache_and_progress(self):
        from agents.onboarding import proposition
        seen = []
        fake = lambda path, **kw: {"title": "T", "theme": "Deep Learning",
            "themes": [{"theme": "Deep Learning", "confidence": 0.9}], "confidence": 0.9}
        with mock.patch("agents.onboarding.proposition.analyze_cover", side_effect=fake):
            r = proposition.run_vision("perso", on_progress=lambda d, t: seen.append((d, t)))
        self.assertEqual(r["n_total"], 2)
        self.assertEqual(r["n_analyzed"], 2)
        cache = json.loads((self.prof / ".cache" / "vision_cache.json").read_text())
        self.assertEqual(len(cache), 2)
        self.assertEqual(seen[-1], (2, 2))            # progression finale

    def test_run_vision_resumes_from_cache(self):
        from agents.onboarding import proposition
        import lib.vision_cache as vc
        # pré-remplir le cache pour a.pdf → doit être skippé
        cache = {}
        key = vc.compute_cache_key(str(self.target / "a.pdf"), model="M", n_pages=2)
        vc.store(cache, key, {"theme": "X", "themes": [], "confidence": 0.5}, "M")
        (self.prof / ".cache").mkdir(exist_ok=True)
        vc.save_cache(self.prof / ".cache" / "vision_cache.json", cache)
        calls = []
        fake = lambda path, **kw: calls.append(path) or {"theme": "Y", "themes": [], "confidence": 0.8}
        with mock.patch("agents.onboarding.proposition.analyze_cover", side_effect=fake):
            r = proposition.run_vision("perso", on_progress=lambda d, t: None)
        self.assertEqual(r["n_analyzed"], 1)           # seul b.pdf analysé
        self.assertEqual(len(calls), 1)
```

- [ ] **Step 2: Run → FAIL** (`No module named 'agents.onboarding.proposition'`).

- [ ] **Step 3: Implement** `agents/onboarding/proposition.py` (début) :

```python
"""Analyse & proposition — orchestration du pipeline onboarding."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import yaml

from lib import profile as _profilelib
from lib import vision_cache
from lib.vision import analyze_cover

_EXTS = (".pdf", ".epub")


def _profile_dir(profile: str) -> Path:
    # Accès par attribut de module (pas `from ... import get_project_root`) pour
    # que `mock.patch("lib.profile.get_project_root")` soit effectif en test.
    return _profilelib.get_project_root() / "profiles" / profile


def _load_profile_cfg(profile: str) -> dict:
    p = _profile_dir(profile) / "profile.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def run_vision(profile: str, on_progress: Callable[[int, int], None]) -> dict:
    """Vision LLM sur TOUT le corpus du profil → peuple vision_cache.json.
    Reprenable : skippe les fichiers déjà en cache. Sauvegarde incrémentale.
    """
    cfg = _load_profile_cfg(profile)
    target = Path(str(cfg.get("target") or ""))
    model = (cfg.get("llm") or {}).get("model") or "Qwen/Qwen3-VL-8B-Instruct"
    endpoint = (cfg.get("llm") or {}).get("endpoint") or ""
    n_pages = int((cfg.get("defaults") or {}).get("pages") or 2)
    api_key = os.environ.get("SILICONFLOW_API_KEY", "")

    cache_path = _profile_dir(profile) / ".cache" / "vision_cache.json"
    cache: dict = {}
    if cache_path.exists():
        import json
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}

    files: list[str] = []
    for root, dirs, fs in os.walk(str(target)):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in fs:
            if not f.startswith(".") and f.lower().endswith(_EXTS):
                files.append(os.path.join(root, f))

    n_total = len(files)
    n_analyzed = 0
    for i, path in enumerate(files, start=1):
        key = vision_cache.compute_cache_key(path, model=model, n_pages=n_pages)
        if key and vision_cache.lookup(cache, key) is not None:
            on_progress(i, n_total)
            continue
        result = analyze_cover(path, api_key, endpoint, model, n_pages=n_pages)
        if key and isinstance(result, dict):
            vision_cache.store(cache, key, result, model)
            n_analyzed += 1
            if n_analyzed % 25 == 0:
                vision_cache.save_cache(cache_path, cache)
        on_progress(i, n_total)
    vision_cache.save_cache(cache_path, cache)
    return {"n_total": n_total, "n_analyzed": n_analyzed}
```

- [ ] **Step 4: Run → PASS** (2 tests).
- [ ] **Step 5: Commit**
```bash
uv run ruff check agents/onboarding/proposition.py tests/auto/test_agent_onboarding.py
git add agents/onboarding/proposition.py tests/auto/test_agent_onboarding.py
git commit -m "feat(onboarding): run_vision — full-corpus Vision into cache, resumable"
```

---

### Task 4: Clustering du corpus

**Files:**
- Modify: `agents/onboarding/proposition.py`
- Test: `tests/auto/test_agent_onboarding.py`

- [ ] **Step 1: Write the failing test** :

```python
class TestClusterCorpus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="klodo-clu-")
        self.root = Path(self.tmp)
        self.prof = self.root / "profiles" / "perso" / ".cache"
        self.prof.mkdir(parents=True)
        import lib.vision_cache as vc
        cache = {}
        for i, theme in enumerate(["Deep Learning", "deep learning", "Astronomy"]):
            cache[f"k{i}"] = {"result": {"theme": theme,
                "themes": [{"theme": theme, "confidence": 0.9}], "confidence": 0.9},
                "model": "M", "prompt_version": "v3", "cached_at": "t"}
        (self.prof / "vision_cache.json").write_text(json.dumps(cache), encoding="utf-8")
        self.patch = mock.patch("lib.theme_canon.get_project_root", return_value=self.root)
        self.patch.start()
        self.patch2 = mock.patch("agents.onboarding.proposition.get_project_root", return_value=self.root)
        self.patch2.start()

    def tearDown(self):
        self.patch.stop(); self.patch2.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cluster_corpus_groups_and_counts(self):
        from agents.onboarding import proposition
        clusters = proposition.cluster_corpus("perso")
        # "Deep Learning" + "deep learning" fusionnent (count 2), "Astronomy" seul (count 1)
        by_count = {c["count"] for c in clusters}
        self.assertIn(2, by_count)
        self.assertIn(1, by_count)
        for c in clusters:
            self.assertIn("canonical", c)
            self.assertIn("raw_members", c)
            self.assertIn("count", c)
```

(NB : `extract_themes_from_vision_cache` lit `get_project_root` dans `lib.theme_canon` — d'où le double patch. Vérifie le vrai chemin du helper dans `theme_canon.py` si le mock échoue.)

- [ ] **Step 2: Run → FAIL** (`cluster_corpus` manquant).

- [ ] **Step 3: Implement** — ajouter à `proposition.py` :

```python
from lib.theme_canon import extract_themes_from_vision_cache
from lib.theme_normalizer import cluster_themes


def cluster_corpus(profile: str) -> list[dict]:
    """Clusterise tous les thèmes du vision_cache → clusters enrichis de counts.

    Retourne [{canonical, canonical_forms, raw_members, count}], trié par
    count décroissant. `canonical` = 1ère forme canonique du cluster.
    """
    themes = extract_themes_from_vision_cache(profile)   # {raw: count}
    clusters = cluster_themes(themes.keys())             # [{canonical_forms, raw_members}]
    out: list[dict] = []
    for c in clusters:
        count = sum(int(themes.get(m, 0)) for m in c["raw_members"])
        out.append({
            "canonical": (c["canonical_forms"] or c["raw_members"] or [""])[0],
            "canonical_forms": c["canonical_forms"],
            "raw_members": c["raw_members"],
            "count": count,
        })
    out.sort(key=lambda c: c["count"], reverse=True)
    return out
```

- [ ] **Step 4: Run → PASS**. Si le mock de `get_project_root` ne couvre pas `extract_themes_from_vision_cache`, lis comment `theme_canon._vision_cache_path` résout le chemin et ajuste le patch (mocker `dashboard.data.get_project_root` ou le helper réel).
- [ ] **Step 5: Commit**
```bash
git add agents/onboarding/proposition.py tests/auto/test_agent_onboarding.py
git commit -m "feat(onboarding): cluster_corpus — themes → clusters with counts"
```

---

### Task 5: Proposition de hiérarchie (appel LLM — la nouvelle intelligence)

**Files:**
- Create: `agents/onboarding/taxonomy_llm.py`
- Test: `tests/auto/test_agent_onboarding.py`

- [ ] **Step 1: Write the failing test** (LLM mocké) :

```python
class TestProposeTaxonomy(unittest.TestCase):
    def test_propose_tree_and_mapping(self):
        from agents.onboarding import taxonomy_llm
        clusters = [
            {"canonical": "deep learning", "raw_members": ["Deep Learning", "deep learning"], "count": 40},
            {"canonical": "astronomy", "raw_members": ["Astronomy"], "count": 12},
        ]
        # LLM mocké : retourne le schéma Pydantic attendu
        fake_out = taxonomy_llm._ProposedTaxonomy(
            sections=[
                taxonomy_llm._Section(folder="02-INFORMATIQUE/Deep-Learning",
                                      cluster_canonicals=["deep learning"]),
                taxonomy_llm._Section(folder="01-SCIENCES/Astronomie",
                                      cluster_canonicals=["astronomy"]),
            ])
        fake_llm = mock.Mock()
        fake_llm.with_structured_output.return_value.invoke.return_value = fake_out
        tree, mapping = taxonomy_llm.propose_taxonomy(fake_llm, clusters)
        # tree contient les dossiers + parents implicites + _A-TRIER
        self.assertIn("02-INFORMATIQUE", tree)
        self.assertIn("02-INFORMATIQUE/Deep-Learning", tree)
        self.assertIn("_A-TRIER", tree)
        # mapping : chaque raw_member du cluster → son dossier
        self.assertEqual(mapping["Deep Learning"], "02-INFORMATIQUE/Deep-Learning")
        self.assertEqual(mapping["deep learning"], "02-INFORMATIQUE/Deep-Learning")
        self.assertEqual(mapping["Astronomy"], "01-SCIENCES/Astronomie")

    def test_assignment_to_unknown_cluster_skipped(self):
        from agents.onboarding import taxonomy_llm
        clusters = [{"canonical": "a", "raw_members": ["A"], "count": 1}]
        fake_out = taxonomy_llm._ProposedTaxonomy(sections=[
            taxonomy_llm._Section(folder="X/Y", cluster_canonicals=["zzz-inexistant"])])
        fake_llm = mock.Mock()
        fake_llm.with_structured_output.return_value.invoke.return_value = fake_out
        tree, mapping = taxonomy_llm.propose_taxonomy(fake_llm, clusters)
        self.assertEqual(mapping, {})                 # cluster inconnu → ignoré
        self.assertIn("_A-TRIER", tree)
```

- [ ] **Step 2: Run → FAIL** (`No module named 'agents.onboarding.taxonomy_llm'`).

- [ ] **Step 3: Implement** `agents/onboarding/taxonomy_llm.py` :

```python
"""Proposition de hiérarchie depuis les clusters (1 appel LLM).

Les SECTIONS viennent du contenu (clusters) ; la FORME suit des conventions
imposées (cf. spec § Forme de l'arbre) : ≤ 2 niveaux, sections 1er niveau
numérotées en MAJUSCULES (01-SCIENCES), sous-dossiers TitleCase, bucket
résiduel _A-TRIER. Pas de template imposé.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)
_RESIDUAL = "_A-TRIER"

_SYSTEM = (
    "Tu proposes une arborescence de dossiers pour classer une bibliothèque, "
    "à partir de clusters de thèmes observés (avec leur volume). RÈGLES DE FORME "
    "STRICTES : profondeur 2 niveaux MAX ; sections de 1er niveau préfixées et en "
    "MAJUSCULES (ex. 01-SCIENCES, 02-INFORMATIQUE) ; sous-dossiers en TitleCase "
    "(ex. Astronomie, Deep-Learning, sans espace, tirets autorisés) ; n'invente pas "
    "de thème — chaque section couvre un ou plusieurs clusters fournis ; regroupe les "
    "petits clusters proches. Tu assignes chaque cluster (par sa forme canonique) à "
    "EXACTEMENT un dossier feuille."
)


class _Section(BaseModel):
    folder: str = Field(description="Chemin du dossier feuille, ex '02-INFORMATIQUE/Deep-Learning'")
    cluster_canonicals: list[str] = Field(description="Canoniques des clusters classés ici")


class _ProposedTaxonomy(BaseModel):
    sections: list[_Section]


def propose_taxonomy(llm: Any, clusters: list[dict]) -> tuple[list[str], dict[str, str]]:
    """Retourne (tree_folders, theme_mapping). theme_mapping = {raw_theme: folder}.

    Robuste : un cluster assigné à un canonical inconnu est ignoré ; _A-TRIER est
    toujours présent dans l'arbre (bucket résiduel).
    """
    by_canon = {c["canonical"]: c for c in clusters}
    payload = "\n".join(
        f"- canonical={c['canonical']!r} volume={c['count']} variantes={c['raw_members'][:4]}"
        for c in clusters[:200]
    )
    structured = llm.with_structured_output(_ProposedTaxonomy)
    try:
        result = structured.invoke(
            [{"role": "system", "content": _SYSTEM},
             {"role": "user", "content": f"Clusters observés :\n{payload}"}])
    except Exception as exc:  # noqa: BLE001 — frontière LLM
        log.warning("propose_taxonomy LLM failed: %s — fallback _A-TRIER seul", exc)
        return [_RESIDUAL], {}

    folders: set[str] = {_RESIDUAL}
    mapping: dict[str, str] = {}
    for sec in result.sections:
        folder = _sanitize_folder(sec.folder)
        if not folder:
            continue
        # parents implicites
        parts = folder.split("/")
        for i in range(1, len(parts) + 1):
            folders.add("/".join(parts[:i]))
        for canon in sec.cluster_canonicals:
            c = by_canon.get(canon)
            if not c:
                continue  # canonical halluciné → ignoré
            for raw in c["raw_members"]:
                mapping[raw] = folder
    return sorted(folders), mapping


def _sanitize_folder(path: str) -> str:
    """Nettoie un chemin de dossier proposé (segments FS-safe, ≤ 2 niveaux)."""
    import re
    segs = [s.strip() for s in (path or "").strip().strip("/").split("/") if s.strip()]
    segs = segs[:2]   # profondeur 2 max
    safe = []
    for s in segs:
        if s in (".", "..") or s.startswith("."):
            return ""
        if not re.match(r"^[A-Za-zÀ-ÿ0-9 _.\-&()]+$", s):
            return ""
        safe.append(s)
    return "/".join(safe)
```

- [ ] **Step 4: Run → PASS** (2 tests).
- [ ] **Step 5: Commit**
```bash
uv run ruff check agents/onboarding/taxonomy_llm.py tests/auto/test_agent_onboarding.py
git add agents/onboarding/taxonomy_llm.py tests/auto/test_agent_onboarding.py
git commit -m "feat(onboarding): propose_taxonomy — LLM hierarchy from clusters with shape conventions"
```

---

### Task 6: Proposition des catégories (réutilise categories_llm)

**Files:**
- Modify: `agents/onboarding/proposition.py`
- Test: `tests/auto/test_agent_onboarding.py`

- [ ] **Step 1: Write the failing test** :

```python
class TestProposeCategories(unittest.TestCase):
    def test_build_categories_from_tree(self):
        from agents.onboarding import proposition
        tree = ["02-INFORMATIQUE", "02-INFORMATIQUE/Deep-Learning", "_A-TRIER"]
        fake_llm = mock.Mock()
        with mock.patch("agents.onboarding.proposition.get_agent_llm", return_value=fake_llm), \
             mock.patch("agents.onboarding.proposition.propose_keywords_for_new_folders",
                        return_value=[{"chemin": "02-INFORMATIQUE/Deep-Learning",
                                       "groupe": "informatique", "priorite": 5,
                                       "mots_cles": ["neural", "deep learning"]}]) as pk:
            cats = proposition.propose_categories(tree)
        # categories.yaml groupé par 'groupe'
        self.assertIn("informatique", cats)
        self.assertEqual(cats["informatique"][0]["chemin"], "02-INFORMATIQUE/Deep-Learning")
        # creations passées = dossiers feuilles (pas _A-TRIER, pas la racine de section)
        creations = pk.call_args.kwargs["creations"]
        paths = {c["path"] for c in creations}
        self.assertIn("02-INFORMATIQUE/Deep-Learning", paths)
        self.assertNotIn("_A-TRIER", paths)
```

- [ ] **Step 2: Run → FAIL** (`propose_categories` manquant).

- [ ] **Step 3: Implement** — ajouter à `proposition.py` :

```python
from agents.llm import get_agent_llm
from agents.refonte.categories_llm import propose_keywords_for_new_folders
from agents.refonte.proposition_tools import _groupe_from_path_prefix


def propose_categories(tree_folders: list[str]) -> dict:
    """Génère categories.yaml (P2) pour les dossiers feuilles via categories_llm,
    ancré sur le contenu. Retourne la structure {groupe: [entries]}.
    """
    # Dossiers feuilles (≥ 1 '/' = sous-dossier), hors résiduel/_INBOX
    leaves = [f for f in tree_folders
              if "/" in f and not f.startswith("_")]
    if not leaves:
        return {}
    creations = [{"path": f, "rationale": "dossier de la taxonomie d'onboarding"}
                 for f in leaves]
    # Pas de categories existantes au bootstrap → groupes inférés depuis les
    # préfixes des dossiers proposés eux-mêmes.
    existing_cats: dict = {}
    groupe_inference = {f: _groupe_from_path_prefix(f, existing_cats) for f in leaves}
    try:
        entries = propose_keywords_for_new_folders(
            llm=get_agent_llm(), creations=creations,
            existing_groupes=[], groupe_inference=groupe_inference, sample_entries={})
    except Exception:  # noqa: BLE001
        entries = []
    cats: dict[str, list[dict]] = {}
    for e in entries:
        g = e.get("groupe") or "autres"
        cats.setdefault(g, []).append(
            {"chemin": e["chemin"], "priorite": e.get("priorite", 5),
             "mots_cles": list(e.get("mots_cles", []))})
    return cats
```

(NB : `_groupe_from_path_prefix` avec `existing_cats={}` retournera « autres » pour tout — acceptable au bootstrap ; le raffinage des groupes se fait ensuite. Si tu préfères inférer le groupe depuis le 1er segment du chemin, documente-le.)

- [ ] **Step 4: Run → PASS**.
- [ ] **Step 5: Commit**
```bash
git add agents/onboarding/proposition.py tests/auto/test_agent_onboarding.py
git commit -m "feat(onboarding): propose_categories — reuse categories_llm on proposed tree"
```

---

### Task 7: Orchestration — écrire les 3 YAMLs + dry-run de couverture

**Files:**
- Modify: `agents/onboarding/proposition.py`, `agents/onboarding/__init__.py`
- Test: `tests/auto/test_agent_onboarding.py`

- [ ] **Step 1: Write the failing test** :

```python
class TestBuildProposal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="klodo-prop-")
        self.root = Path(self.tmp)
        self.target = self.root / "RAW"
        (self.target / "a.pdf").parent.mkdir(parents=True, exist_ok=True)
        (self.target / "a.pdf").write_bytes(b"%PDF-1.4 a")
        self.prof = self.root / "profiles" / "perso"
        (self.prof / ".cache").mkdir(parents=True)
        (self.prof / "profile.yaml").write_text(yaml.safe_dump({
            "target": str(self.target), "fallback": "_A-TRIER",
            "llm": {"model": "M"}, "defaults": {"pages": 2}}), encoding="utf-8")
        (self.prof / "tree.yaml").write_text(yaml.safe_dump({"folders": []}), encoding="utf-8")
        (self.prof / "theme_mapping.yaml").write_text("{}", encoding="utf-8")
        (self.prof / "categories.yaml").write_text("{}", encoding="utf-8")
        for m in ("lib.profile", "agents.onboarding.proposition",
                  "dashboard.data", "lib.theme_canon", "dashboard.taxonomy"):
            try:
                mock.patch(f"{m}.get_project_root", return_value=self.root).start()
            except Exception:
                pass

    def tearDown(self):
        mock.patch.stopall()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_proposal_writes_3_yamls_and_coverage(self):
        from agents.onboarding import proposition
        tree = ["02-INFO", "02-INFO/DL", "_A-TRIER"]
        mapping = {"Deep Learning": "02-INFO/DL"}
        cats = {"autres": [{"chemin": "02-INFO/DL", "priorite": 5, "mots_cles": ["dl"]}]}
        cov = proposition.write_proposal("perso", tree, mapping, cats)
        wrote = yaml.safe_load((self.prof / "tree.yaml").read_text())
        self.assertIn("02-INFO/DL", wrote["folders"])
        self.assertEqual(yaml.safe_load((self.prof / "theme_mapping.yaml").read_text()),
                         {"Deep Learning": "02-INFO/DL"})
        self.assertIn("coverage", cov)
        self.assertIn("stats", cov)
```

- [ ] **Step 2: Run → FAIL** (`write_proposal` manquant).

- [ ] **Step 3: Implement** — ajouter à `proposition.py` :

```python
def write_proposal(profile: str, tree_folders: list[str], theme_mapping: dict[str, str],
                   categories: dict) -> dict:
    """Écrit les 3 YAMLs proposés dans le profil (après backup) puis lance le
    dry-run de couverture. Retourne {coverage, stats, by_destination}.
    """
    from agents.refonte import agent_backup
    from dashboard import taxonomy
    pdir = _profile_dir(profile)
    try:
        agent_backup.create_backup(profile, batch_id=f"onboarding-{profile}")
    except Exception:  # noqa: BLE001 — pas de YAML à snapshoter au tout 1er run
        pass
    _atomic_yaml(pdir / "tree.yaml", {"folders": sorted(set(tree_folders))})
    _atomic_yaml(pdir / "theme_mapping.yaml", dict(theme_mapping))
    _atomic_yaml(pdir / "categories.yaml", dict(categories))
    taxonomy.reset_cache(profile)
    dry = taxonomy.reclassify_dryrun(profile)
    stats = dry.get("stats", {})
    n_lib = max(1, int(stats.get("n_in_lib", 0)))
    coverage = round(100 * int(stats.get("n_with_prediction", 0)) / n_lib, 1)
    return {"coverage": coverage, "stats": stats,
            "by_destination": dry.get("by_destination", [])}


def _atomic_yaml(path: Path, payload: dict) -> None:
    import yaml as _y
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(_y.safe_dump(payload, allow_unicode=True, sort_keys=False),
                   encoding="utf-8")
    tmp.replace(path)


def build_proposal(profile: str, on_progress: Callable[[int, int], None]) -> dict:
    """Pipeline complet « Analyse & proposition » : vision → cluster → propose →
    categories → write 3 YAMLs → dry-run. Retourne le rapport de couverture."""
    run_vision(profile, on_progress)
    clusters = cluster_corpus(profile)
    tree, mapping = propose_taxonomy(get_agent_llm(), clusters)
    cats = propose_categories(tree)
    return write_proposal(profile, tree, mapping, cats)
```

Ajouter à `agents/onboarding/__init__.py` :
```python
from agents.onboarding.proposition import build_proposal, write_proposal  # noqa: F401
from agents.onboarding.scan import estimate_cost, scan_directory  # noqa: F401
```

- [ ] **Step 4: Run → PASS** (le dry-run sur 1 fichier sans theme matching → couverture calculée). Lance toute la classe.
- [ ] **Step 5: Commit**
```bash
uv run ruff check agents/onboarding/ tests/auto/test_agent_onboarding.py
git add agents/onboarding/ tests/auto/test_agent_onboarding.py
git commit -m "feat(onboarding): write_proposal (3 YAMLs + dry-run) + build_proposal orchestration"
```

---

### Task 8: Wrapper dashboard (thread + polling)

**Files:**
- Create: `dashboard/agent_onboarding.py`
- Test: `tests/auto/test_agent_onboarding.py`

- [ ] **Step 1: Write the failing tests** :

```python
class TestAgentOnboardingWrapper(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="klodo-wrap-")
        self.root = Path(self.tmp)
        self.target = self.root / "RAW"
        (self.target / "a.pdf").parent.mkdir(parents=True, exist_ok=True)
        (self.target / "a.pdf").write_bytes(b"%PDF-1.4 a")
        self.patch = mock.patch("dashboard.data.get_project_root", return_value=self.root)
        self.patch.start()
        self.patch2 = mock.patch("lib.profile.get_project_root", return_value=self.root)
        self.patch2.start()

    def tearDown(self):
        mock.patch.stopall()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scan_endpoint_logic(self):
        from dashboard import agent_onboarding as ao
        r = ao.scan(str(self.target))
        self.assertEqual(r["n_files"], 1)
        self.assertIn("estimate", r)

    def test_start_then_status_done(self):
        from dashboard import agent_onboarding as ao
        # exécution synchrone : on patche _spawn pour appeler la cible direct
        with mock.patch.object(ao, "_spawn", lambda fn, args, name: fn(*args)), \
             mock.patch("agents.onboarding.proposition.build_proposal",
                        return_value={"coverage": 80.0, "stats": {"n_in_lib": 10}}):
            res = ao.start_onboarding("perso-2026", str(self.target))
            run_id = res["run_id"]
            st = ao.get_status("perso-2026", run_id)
        self.assertEqual(st["status"], "done")
        self.assertEqual(st["coverage"], 80.0)
        # profil brouillon créé
        cfg = yaml.safe_load((self.root / "profiles" / "perso-2026" / "profile.yaml").read_text())
        self.assertTrue(cfg["onboarding_draft"])

    def test_finalize_clears_flag(self):
        from dashboard import agent_onboarding as ao
        from lib import profile as prof
        prof.create_draft_profile("perso-2026", str(self.target))
        ao.finalize("perso-2026")
        cfg = yaml.safe_load((self.root / "profiles" / "perso-2026" / "profile.yaml").read_text())
        self.assertFalse(cfg["onboarding_draft"])
```

- [ ] **Step 2: Run → FAIL** (`No module named 'dashboard.agent_onboarding'`).

- [ ] **Step 3: Implement** `dashboard/agent_onboarding.py` (calqué sur `agent_refonte.py`) :

```python
"""Wrapper dashboard de l'agent Onboarding : scan synchrone + analyse/proposition
en thread daemon + polling status.json. Calqué sur dashboard/agent_refonte.py.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dashboard import data


def _runs_dir(profile: str) -> Path:
    base = data.get_project_root() / "profiles" / profile / ".cache" / "onboarding"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _status_path(profile: str, run_id: str) -> Path:
    d = _runs_dir(profile) / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d / "status.json"


def _write_status(profile: str, run_id: str, payload: dict[str, Any]) -> None:
    _status_path(profile, run_id).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def get_status(profile: str, run_id: str) -> dict[str, Any] | None:
    p = _status_path(profile, run_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _spawn(target: Callable, args: tuple, name: str) -> None:
    threading.Thread(target=target, args=args, daemon=True, name=name).start()


def scan(inbox_path: str) -> dict[str, Any]:
    """Scan & estimation (synchrone, pas de LLM)."""
    from agents.onboarding import scan as _scan
    r = _scan.scan_directory(inbox_path)
    r["estimate"] = _scan.estimate_cost(r["n_files"], cost_per_call=0.00034, n_pages=2)
    return r


def _run(profile: str, run_id: str) -> None:
    from agents.onboarding import proposition
    try:
        def on_progress(done: int, total: int) -> None:
            _write_status(profile, run_id, {
                "run_id": run_id, "profile": profile, "status": "running",
                "phase": "vision", "n_done": done, "n_total": total, "error": None})
        cov = proposition.build_proposal(profile, on_progress)
        _write_status(profile, run_id, {
            "run_id": run_id, "profile": profile, "status": "done",
            "completed_at": datetime.now(UTC).isoformat(),
            "coverage": cov["coverage"], "stats": cov["stats"],
            "by_destination": cov.get("by_destination", []), "error": None})
    except Exception as exc:  # noqa: BLE001 — frontière de thread
        _write_status(profile, run_id, {
            "run_id": run_id, "profile": profile, "status": "error", "error": str(exc)})


def start_onboarding(profile_name: str, inbox_path: str) -> dict[str, Any]:
    """Crée le profil brouillon puis lance l'analyse/proposition en thread."""
    from lib import profile as _profile
    pdir = data.get_project_root() / "profiles" / profile_name
    if pdir.exists():
        raise FileExistsError(f"profile already exists: {profile_name}")
    _profile.create_draft_profile(profile_name, inbox_path)
    run_id = str(uuid.uuid4())
    _write_status(profile_name, run_id, {
        "run_id": run_id, "profile": profile_name, "status": "pending",
        "started_at": datetime.now(UTC).isoformat(), "error": None})
    _spawn(_run, (profile_name, run_id), f"onboarding-{run_id[:8]}")
    return {"run_id": run_id, "profile": profile_name, "status": "pending"}


def finalize(profile: str) -> dict[str, Any]:
    """Retire le flag onboarding_draft."""
    from lib import profile as _profile
    _profile.set_onboarding_draft(profile, False)
    return {"ok": True, "profile": profile}
```

(NB : `data.get_project_root` et `lib.profile.get_project_root` doivent pointer la même racine — d'où le double patch dans les tests.)

- [ ] **Step 4: Run → PASS** (3 tests).
- [ ] **Step 5: Commit**
```bash
uv run ruff check dashboard/agent_onboarding.py tests/auto/test_agent_onboarding.py
git add dashboard/agent_onboarding.py tests/auto/test_agent_onboarding.py
git commit -m "feat(onboarding): dashboard wrapper — scan, start (thread), status, finalize"
```

---

### Task 9: Routes API

**Files:**
- Modify: `dashboard/app.py`
- Test: `tests/auto/test_agent_onboarding.py`

- [ ] **Step 1: Write the failing tests** (TestClient, mock `ao._spawn` synchrone) :

```python
class TestOnboardingEndpoints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="klodo-ep-")
        self.root = Path(self.tmp)
        self.target = self.root / "RAW"
        (self.target / "a.pdf").parent.mkdir(parents=True, exist_ok=True)
        (self.target / "a.pdf").write_bytes(b"%PDF-1.4 a")
        mock.patch("dashboard.data.get_project_root", return_value=self.root).start()
        mock.patch("lib.profile.get_project_root", return_value=self.root).start()
        from fastapi.testclient import TestClient
        from dashboard.app import app
        self.client = TestClient(app)

    def tearDown(self):
        mock.patch.stopall()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scan_endpoint(self):
        r = self.client.post("/api/agent/onboarding/scan", json={"inbox_path": str(self.target)})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["n_files"], 1)

    def test_start_status_finalize_flow(self):
        from dashboard import agent_onboarding as ao
        sync = lambda fn, args, name: fn(*args)
        with mock.patch.object(ao, "_spawn", sync), \
             mock.patch("agents.onboarding.proposition.build_proposal",
                        return_value={"coverage": 75.0, "stats": {"n_in_lib": 4}}):
            s = self.client.post("/api/agent/onboarding/start",
                json={"profile_name": "perso", "inbox_path": str(self.target)})
        self.assertEqual(s.status_code, 200)
        run_id = s.json()["run_id"]
        st = self.client.get(f"/api/agent/onboarding/status?profile=perso&run_id={run_id}")
        self.assertEqual(st.json()["status"], "done")
        f = self.client.post("/api/agent/onboarding/finalize", json={"profile": "perso"})
        self.assertEqual(f.status_code, 200)

    def test_start_missing_fields_400(self):
        r = self.client.post("/api/agent/onboarding/start", json={"profile_name": "x"})
        self.assertEqual(r.status_code, 400)
```

- [ ] **Step 2: Run → FAIL** (404 sur les routes).

- [ ] **Step 3: Add routes** dans `dashboard/app.py`. En tête, importer `from dashboard import agent_onboarding`. Puis (près des routes refonte) :

```python
@app.post("/api/agent/onboarding/scan")
async def api_onboarding_scan(request: Request):
    from fastapi.responses import JSONResponse
    body = await request.json()
    inbox = (body.get("inbox_path") or "").strip()
    if not inbox:
        return JSONResponse({"error": "inbox_path requis"}, status_code=400)
    import os
    if not os.path.isdir(inbox):
        return JSONResponse({"error": "répertoire introuvable"}, status_code=400)
    return JSONResponse(agent_onboarding.scan(inbox))


@app.post("/api/agent/onboarding/start")
async def api_onboarding_start(request: Request):
    from fastapi.responses import JSONResponse
    body = await request.json()
    name = (body.get("profile_name") or "").strip()
    inbox = (body.get("inbox_path") or "").strip()
    if not name or not inbox:
        return JSONResponse({"error": "profile_name et inbox_path requis"}, status_code=400)
    try:
        return JSONResponse(agent_onboarding.start_onboarding(name, inbox))
    except FileExistsError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)


@app.get("/api/agent/onboarding/status")
async def api_onboarding_status(profile: str, run_id: str):
    from fastapi.responses import JSONResponse
    st = agent_onboarding.get_status(profile, run_id)
    if st is None:
        return JSONResponse({"error": "run introuvable"}, status_code=404)
    return JSONResponse(st)


@app.post("/api/agent/onboarding/finalize")
async def api_onboarding_finalize(request: Request):
    from fastapi.responses import JSONResponse
    body = await request.json()
    profile = (body.get("profile") or "").strip()
    if not profile:
        return JSONResponse({"error": "profile requis"}, status_code=400)
    return JSONResponse(agent_onboarding.finalize(profile))
```

- [ ] **Step 4: Run → PASS** + suite complète `uv run python -m unittest discover tests/auto -q 2>&1 | tail -3`.
- [ ] **Step 5: Commit**
```bash
uv run ruff check dashboard/app.py tests/auto/test_agent_onboarding.py
git add dashboard/app.py tests/auto/test_agent_onboarding.py
git commit -m "feat(onboarding): HTTP routes scan/start/status/finalize"
```

---

### Task 10: Page assistant + entrée « ➕ Nouveau profil »

**Files:**
- Create: `dashboard/templates/onboarding.html`
- Modify: `dashboard/app.py` (route `GET /onboarding`), `dashboard/templates/base.html`

Pas de test JS auto — vérif : `node --check` (sur le JS extrait) + la page se rend (suite Python intacte).

- [ ] **Step 1: Route GET** dans `dashboard/app.py` (modèle `/agent/refonte`) :
```python
@app.get("/onboarding")
async def onboarding_page(request: Request):
    available = [p["name"] if isinstance(p, dict) else p
                 for p in data.get_available_profiles(include_all=True)]
    return templates.TemplateResponse(request, "onboarding.html",
        {"active": "onboarding", "available_profiles": available})
```
(Vérifie le helper réel des profils disponibles — `data.get_available_profiles` — et la signature `TemplateResponse` utilisée par les autres pages.)

- [ ] **Step 2: Template** `dashboard/templates/onboarding.html` — extends `base.html`, stepper 3 étapes (config → progression Vision → proposition) + JS de poll calqué sur `agent_refonte_panel.html`. Reprends la structure de la maquette `docs/mockups/2026-06-14-onboarding-mockup.html` (étapes, ids `onboarding_step_1/2/3`, barre de progression, bouton « Continuer dans Mappings » → `/taxonomy?profile=<name>`). Le JS :
  - `POST /api/agent/onboarding/scan` au changement de répertoire → affiche `n_files` + estimation.
  - « Analyser » → `POST /api/agent/onboarding/start` → `run_id` → `setInterval(poll, 2000)` sur `/api/agent/onboarding/status?profile=&run_id=` → met à jour la barre (`n_done/n_total`) puis affiche la couverture à `status:done`.
  - « Continuer dans Mappings » → `window.location = '/taxonomy?profile=' + name`.

(Le HTML/JS complet : calquer la maquette + le poll de `agent_refonte_panel.html`. C'est de l'assemblage de patterns existants — pas de logique métier neuve.)

- [ ] **Step 3: Entrée nav** dans `dashboard/templates/base.html` — ajouter dans la sidebar un lien **« ➕ Nouveau profil »** vers `/onboarding` (à côté des 7 onglets, ou dans le footer profil). Repère la structure de la sidebar (les `<a>` des onglets) et ajoute le lien dans le même style.

- [ ] **Step 4: Vérif** :
```bash
python3 -c "import re,subprocess,tempfile,pathlib;h=pathlib.Path('dashboard/templates/onboarding.html').read_text();s='\n'.join(re.findall(r'<script>(.*?)</script>',h,re.S));s=re.sub(r'{%.*?%}','',s);p=tempfile.NamedTemporaryFile(suffix='.js',delete=False,mode='w');p.write(s);p.close();print(subprocess.run(['node','--check',p.name],capture_output=True,text=True).returncode)"
uv run python -m unittest tests.auto.test_agent_onboarding tests.auto.test_dashboard -q 2>&1 | tail -3
```
Puis manuel : `./klodo.sh dashboard` → sidebar « ➕ Nouveau profil » → assistant.

- [ ] **Step 5: Commit**
```bash
git add dashboard/templates/onboarding.html dashboard/app.py dashboard/templates/base.html
git commit -m "feat(onboarding): wizard page + nav entry"
```

---

### Task 11: Badge brouillon + bandeau Taxonomie

**Files:**
- Modify: `dashboard/templates/base.html` (badge dans le footer profil), `dashboard/templates/taxonomy.html` (bandeau), `dashboard/static/style.css`

- [ ] **Step 1: Badge brouillon** — dans `base.html`, le footer profil affiche le profil actif ; ajouter, quand le profil a `onboarding_draft`, un badge « brouillon ». Cela nécessite que la vue passe l'info. Plus simple : un endpoint léger `GET /api/agent/onboarding/is-draft?profile=` (lit `Profile(profile).onboarding_draft`) appelé en JS, OU passer le flag dans le contexte de rendu des pages. Choisir le plus simple selon comment `base.html` connaît le profil actif (lis-le). Implémente le badge `<span class="onb-badge">brouillon</span>` conditionnel.

- [ ] **Step 2: Bandeau Taxonomie** — dans `taxonomy.html`, en haut du panneau Mappings (modèle `.tax-cat-info-banner`), ajouter un bandeau `.tax-onboarding-banner` affiché quand le profil actif est un brouillon : « 🟡 Profil en cours d'onboarding — raffine puis applique · [Finaliser] ». Le bouton Finaliser → `POST /api/agent/onboarding/finalize` puis reload. Conditionne l'affichage sur le flag (via le même mécanisme que le badge).

- [ ] **Step 3: CSS** — en fin de `style.css` :
```css
/* ── Onboarding (badge brouillon + bandeau) ────────────────────────── */
.onb-badge { font-size: 10px; padding: 1px 6px; border-radius: 8px; margin-left: 6px;
    background: rgba(224,168,0,.18); color: #e0a800; border: 1px solid rgba(224,168,0,.4); }
.tax-onboarding-banner { display: flex; align-items: center; gap: 12px; padding: 11px 14px;
    border-radius: 10px; background: rgba(224,168,0,.12); border: 1px solid rgba(224,168,0,.4);
    margin-bottom: 14px; }
```

- [ ] **Step 4: Vérif** : `node --check` (JS extrait) + suite Python verte.
- [ ] **Step 5: Commit**
```bash
git add dashboard/templates/base.html dashboard/templates/taxonomy.html dashboard/static/style.css dashboard/app.py
git commit -m "feat(onboarding): draft badge + Taxonomie banner + finalize"
```

---

### Task 12: Documentation

**Files:**
- Modify: `dashboard/CLAUDE.md`, `agents/CLAUDE.md` (s'il existe, sinon le créer minimal)

- [ ] **Step 1: `dashboard/CLAUDE.md`** — ajouter une section :
```markdown
### Agent Onboarding (bootstrap d'un profil)

- **`agents/onboarding/`** (pipeline, pas un agent ReAct) : `scan.py` (scan +
  estimation coût), `proposition.py` (run_vision full-corpus reprenable →
  cluster_corpus via theme_canon → propose_taxonomy → propose_categories via
  categories_llm → write_proposal : 3 YAMLs + dry-run couverture),
  `taxonomy_llm.py` (appel LLM « propose une hiérarchie depuis les clusters »,
  forme par conventions).
- **`dashboard/agent_onboarding.py`** : wrapper thread+poll (scan sync,
  start_onboarding crée le profil brouillon + lance l'analyse, status, finalize).
  État dans `profiles/<p>/.cache/onboarding/<run_id>/status.json`.
- Profil **brouillon** : `onboarding_draft: true` dans profile.yaml (badge +
  bandeau Taxonomie + Finaliser).
- Routes : `POST …/scan|start|finalize`, `GET …/status`. Page `/onboarding`.
- Réutilise : lib.vision, vision_cache, theme_canon, categories_llm,
  reclassify_dryrun, init_profile, l'éditeur Mappings + Apply global (handoff).
```
Mettre à jour le compte de tests si cité.

- [ ] **Step 2: `agents/CLAUDE.md`** — mentionner l'agent onboarding (le 2ᵉ agent, pipeline) à côté de Refonte.

- [ ] **Step 3: Commit**
```bash
uv run python -m unittest discover tests/auto -q 2>&1 | tail -3
git add dashboard/CLAUDE.md agents/CLAUDE.md
git commit -m "docs: document onboarding agent"
```

---

## Vérification end-to-end finale

1. `uv run python -m unittest discover tests/auto -q` → tous verts.
2. `uv run ruff check .` → clean.
3. **Smoke test** (règle projet — pipeline LLM touché) : sur un dossier de TEST de 3-5 PDFs réels, `agents.onboarding.proposition.build_proposal('<draft>', lambda d,t:None)` → vérifier que la Vision + la proposition ne sont pas vides (tree + mapping non vides), couverture calculée.
4. `./klodo.sh dashboard` → « ➕ Nouveau profil » → assistant : scan + estimation → analyser (progression) → couverture → « Continuer dans Mappings » → le profil brouillon apparaît avec bandeau → raffiner → Apply global → Finaliser.
5. PR vers `develop`, CI verte.

## Notes pour l'exécuteur

- **Jamais** d'appel Vision/LLM réel en test : mocker `analyze_cover` (Task 3), `get_agent_llm`/`propose_keywords_for_new_folders`/`propose_taxonomy` (Tasks 5-7), `_spawn` synchrone (Tasks 8-9).
- `data.get_project_root` (dashboard) et `lib.profile.get_project_root` doivent être mockés **ensemble** vers la même racine tmp.
- `get_agent_llm()` lève `RuntimeError` sans `SILICONFLOW_API_KEY` — toujours le mocker en test.
- Onboarding est un **pipeline** : pas de LangGraph/MessagesState, pas de ReAct. Les 2 appels LLM sont directs (`with_structured_output(...).invoke(...)`).
- L'« étape Raffinage & application » n'a **aucun code neuf** : c'est le handoff vers l'onglet Mappings + Apply global existants.
- Smoke test obligatoire avant tout run réel (le pipeline Vision est coûteux).
