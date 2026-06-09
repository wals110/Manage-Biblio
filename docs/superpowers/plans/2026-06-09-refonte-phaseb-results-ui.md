# Restitution Phase B — UI outil de décision · Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal :** Transformer la restitution Phase B en outil de décision : 3 onglets (① Verdict & Risque, ② Arbre proposé, ③ Plan complet) avec triage de la zone de doute et icicle de la taxo proposée.

**Architecture :** Une couche d'agrégation **pure-Python** testable (`dashboard/refonte_results.py`) qui lit `reclassify-projection.csv` (via `csv.DictReader`, comme l'existant) et calcule matrice de risque / fichiers douteux / arbre / provenance. 4 nouveaux endpoints lecture seule. Frontend dans `agent_refonte_panel.html` (icicle HTML/CSS — pas de D3 car absent de la page standalone). Triage ✓/✗ **éphémère** (client-side, pas de persistance).

**Tech Stack :** Python 3.13, FastAPI, Jinja2, `csv.DictReader`, HTML/CSS/JS vanilla, unittest.

**Spec :** [docs/superpowers/specs/2026-06-09-refonte-phaseb-results-ui-design.md](../specs/2026-06-09-refonte-phaseb-results-ui-design.md)

---

## Note d'architecture (déviation assumée vs spec)

La spec suggérait DuckDB. On utilise **`csv.DictReader` + agrégation pure-Python** à la place, pour deux raisons : (1) la bucketisation de `source` et le `risk_score` sont des fonctions Python — les dupliquer en SQL CASE casserait le DRY ; (2) c'est déjà le pattern de `get_proposition_simulation` (`dashboard/agent_refonte.py:232`). Le CSV fait ~18 425 lignes : lecture + agrégation Python côté serveur reste < 100 ms, et les sorties renvoyées au client sont minuscules (matrice 5×4, zone de doute paginée). La contrainte "≤ 2 000 lignes côté client" est respectée par la pagination serveur.

## File Structure

| Fichier | Rôle |
|---|---|
| `dashboard/refonte_results.py` (créer) | Fonctions pures d'agrégation : `bucket_source`, `confidence_band`, `is_inter_discipline_jump`, `risk_score`, `build_risk_matrix`, `enrich_row`, `select_doubt_files`, `build_proposed_tree`, `folder_provenance` + lecteurs `read_projection_rows`, `load_creations`, `load_proposed_folders`. |
| `dashboard/agent_refonte.py` (modifier) | 4 helpers qui assemblent un run_dir + appellent `refonte_results`. |
| `dashboard/app.py` (modifier) | 4 routes lecture seule + réordonnancement onglet par défaut côté template (aucun). |
| `dashboard/templates/partials/agent_refonte_panel.html` (modifier) | Restructure 3 onglets + JS Verdict/Arbre. |
| `dashboard/static/style.css` (modifier) | Classes `.rr-*` (risk-matrix, budget, icicle, provenance). |
| `tests/auto/test_refonte_results.py` (créer) | Unitaires des fonctions pures. |
| `tests/auto/test_agent_refonte_results_api.py` (créer) | Intégration HTTP des 4 endpoints. |

---

## Task 1 : `bucket_source`

**Files :**
- Create : `dashboard/refonte_results.py`
- Test : `tests/auto/test_refonte_results.py` (create)

- [ ] **Step 1 : Write the failing test**

Create `tests/auto/test_refonte_results.py` :

```python
#!/usr/bin/env python3
"""Tests des agrégations pures de la restitution Phase B (refonte_results)."""

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from dashboard.refonte_results import bucket_source  # noqa: E402


class TestBucketSource(unittest.TestCase):

    def test_p1_refined_before_p1_theme(self):
        # "LLM (theme→refined)" doit matcher refined AVANT theme (ordre important)
        self.assertEqual(bucket_source("LLM (theme→refined)"), "p1_refined")
        self.assertEqual(bucket_source("LLM (theme→N3-refined)"), "p1_refined")

    def test_p1_theme(self):
        self.assertEqual(bucket_source("LLM (theme)"), "p1_theme")
        self.assertEqual(bucket_source("LLM (theme-generic)"), "p1_theme")

    def test_keyword(self):
        self.assertEqual(bucket_source("Keyword (logic)"), "keyword")
        self.assertEqual(bucket_source("Keyword ()"), "keyword")

    def test_fallback(self):
        self.assertEqual(bucket_source("LLM (fallback)"), "fallback")

    def test_failed_and_empty(self):
        self.assertEqual(bucket_source("FAILED"), "failed")
        self.assertEqual(bucket_source(""), "failed")
        self.assertEqual(bucket_source(None), "failed")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2 : Run test to verify it fails**

```bash
uv run python -m unittest tests.auto.test_refonte_results.TestBucketSource -v
```
Expected : `ModuleNotFoundError: No module named 'dashboard.refonte_results'`

- [ ] **Step 3 : Create the module + function**

Create `dashboard/refonte_results.py` :

```python
"""Agrégations pour la restitution Phase B de l'agent Refonte (outil de
décision). Lecture seule sur les artefacts d'un run de proposition
(reclassify-projection.csv, changes.json, tree-proposed.yaml).

Fonctions pures (testables sans I/O) + lecteurs fins. Pas de DuckDB :
la bucketisation de la source et le risk-score sont des fonctions Python,
donc on lit le CSV avec csv.DictReader et on agrège en Python (DRY).
"""

from __future__ import annotations


def bucket_source(source: str | None) -> str:
    """Bucketise le label `source` brut du CSV en classe de cascade.

    Ordre important : refined doit être testé AVANT theme (le label
    "LLM (theme→refined)" contient "theme").
    """
    s = (source or "").strip()
    if not s:
        return "failed"
    if s == "FAILED":
        return "failed"
    if "fallback" in s:
        return "fallback"
    if s.startswith("Keyword"):
        return "keyword"
    if "theme→" in s or "theme-" in s and "refined" in s:
        return "p1_refined"
    if "refined" in s:
        return "p1_refined"
    if s.startswith("LLM (theme"):
        return "p1_theme"
    return "p1_theme"
```

- [ ] **Step 4 : Run test to verify it passes**

```bash
uv run python -m unittest tests.auto.test_refonte_results.TestBucketSource -v
```
Expected : `Ran 5 tests ... OK`

- [ ] **Step 5 : Commit**

```bash
git add dashboard/refonte_results.py tests/auto/test_refonte_results.py
git commit -m "feat(refonte-ui): bucket_source — classe de cascade depuis le label CSV"
```

---

## Task 2 : `confidence_band`

**Files :**
- Modify : `dashboard/refonte_results.py`
- Modify : `tests/auto/test_refonte_results.py`

- [ ] **Step 1 : Write the failing test**

Add to `tests/auto/test_refonte_results.py` :

```python
from dashboard.refonte_results import confidence_band  # noqa: E402


class TestConfidenceBand(unittest.TestCase):

    def test_bands(self):
        self.assertEqual(confidence_band(0.0), "0-0.5")
        self.assertEqual(confidence_band(0.49), "0-0.5")
        self.assertEqual(confidence_band(0.5), "0.5-0.7")
        self.assertEqual(confidence_band(0.69), "0.5-0.7")
        self.assertEqual(confidence_band(0.7), "0.7-0.9")
        self.assertEqual(confidence_band(0.89), "0.7-0.9")
        self.assertEqual(confidence_band(0.9), "0.9-1.0")
        self.assertEqual(confidence_band(1.0), "0.9-1.0")

    def test_none_is_lowest(self):
        self.assertEqual(confidence_band(None), "0-0.5")
```

- [ ] **Step 2 : Run test to verify it fails**

```bash
uv run python -m unittest tests.auto.test_refonte_results.TestConfidenceBand -v
```
Expected : `ImportError: cannot import name 'confidence_band'`

- [ ] **Step 3 : Implement**

Add to `dashboard/refonte_results.py` :

```python
CONF_BANDS = ("0-0.5", "0.5-0.7", "0.7-0.9", "0.9-1.0")


def confidence_band(conf: float | None) -> str:
    """Mappe une confiance (0-1) sur une bande discrète."""
    c = float(conf) if conf is not None else 0.0
    if c < 0.5:
        return "0-0.5"
    if c < 0.7:
        return "0.5-0.7"
    if c < 0.9:
        return "0.7-0.9"
    return "0.9-1.0"
```

- [ ] **Step 4 : Run test to verify it passes**

```bash
uv run python -m unittest tests.auto.test_refonte_results.TestConfidenceBand -v
```
Expected : OK

- [ ] **Step 5 : Commit**

```bash
git add dashboard/refonte_results.py tests/auto/test_refonte_results.py
git commit -m "feat(refonte-ui): confidence_band — bandes de confiance discrètes"
```

---

## Task 3 : `is_inter_discipline_jump`

**Files :**
- Modify : `dashboard/refonte_results.py`
- Modify : `tests/auto/test_refonte_results.py`

- [ ] **Step 1 : Write the failing test**

Add :

```python
from dashboard.refonte_results import is_inter_discipline_jump  # noqa: E402


class TestJump(unittest.TestCase):

    def test_jump_between_disciplines(self):
        # 1er segment différent → saut
        self.assertTrue(is_inter_discipline_jump(
            "04-SHS/03-HISTOIRE", "01-SCIENCES/02-PHYSIQUE"))

    def test_same_discipline_no_jump(self):
        self.assertFalse(is_inter_discipline_jump(
            "01-SCIENCES/PHYSIQUE", "01-SCIENCES/MATHEMATIQUES"))

    def test_inbox_origin_is_not_a_jump(self):
        # _INBOX n'est pas une discipline → classement frais, pas un saut
        self.assertFalse(is_inter_discipline_jump("_INBOX", "02-INFORMATIQUE/14-Web"))
        self.assertFalse(is_inter_discipline_jump("", "02-INFORMATIQUE/14-Web"))

    def test_empty_destination_no_jump(self):
        self.assertFalse(is_inter_discipline_jump("04-SHS", ""))
```

- [ ] **Step 2 : Run test to verify it fails**

```bash
uv run python -m unittest tests.auto.test_refonte_results.TestJump -v
```
Expected : ImportError

- [ ] **Step 3 : Implement**

Add to `dashboard/refonte_results.py` :

```python
def _top_segment(path: str) -> str:
    return (path or "").strip("/").split("/", 1)[0]


def is_inter_discipline_jump(current_folder: str, proposed_folder: str) -> bool:
    """True si le fichier change de discipline (1er segment de chemin).

    _INBOX / racine vide ne sont pas des disciplines : un fichier qui
    vient de l'inbox est un classement frais, pas un saut. Destination
    vide (no prediction) n'est pas un saut non plus.
    """
    src = _top_segment(current_folder)
    dst = _top_segment(proposed_folder)
    if not src or not dst:
        return False
    if src in ("_INBOX", "_A-TRIER"):
        return False
    return src != dst
```

- [ ] **Step 4 : Run test to verify it passes**

```bash
uv run python -m unittest tests.auto.test_refonte_results.TestJump -v
```
Expected : OK

- [ ] **Step 5 : Commit**

```bash
git add dashboard/refonte_results.py tests/auto/test_refonte_results.py
git commit -m "feat(refonte-ui): is_inter_discipline_jump — détection saut de discipline"
```

---

## Task 4 : `risk_score`

**Files :**
- Modify : `dashboard/refonte_results.py`
- Modify : `tests/auto/test_refonte_results.py`

- [ ] **Step 1 : Write the failing test**

Add :

```python
from dashboard.refonte_results import risk_score  # noqa: E402


class TestRiskScore(unittest.TestCase):

    def test_failed_is_max(self):
        r = risk_score("failed", confidence=0.0, is_jump=False, is_new_dest=False)
        self.assertGreater(r, 0.5)

    def test_p1_theme_high_conf_is_low(self):
        r = risk_score("p1_theme", confidence=1.0, is_jump=False, is_new_dest=False)
        self.assertEqual(r, 0.0)

    def test_jump_and_new_dest_add_risk(self):
        base = risk_score("keyword", confidence=0.9, is_jump=False, is_new_dest=False)
        more = risk_score("keyword", confidence=0.9, is_jump=True, is_new_dest=True)
        self.assertGreater(more, base)

    def test_ordering_fallback_gt_keyword_gt_p1(self):
        a = risk_score("fallback", 0.9, False, False)
        b = risk_score("keyword", 0.9, False, False)
        c = risk_score("p1_refined", 0.9, False, False)
        self.assertGreater(a, b)
        self.assertGreater(b, c)
```

- [ ] **Step 2 : Run test to verify it fails**

```bash
uv run python -m unittest tests.auto.test_refonte_results.TestRiskScore -v
```
Expected : ImportError

- [ ] **Step 3 : Implement**

Add to `dashboard/refonte_results.py` :

```python
# Poids du risk-score (constantes nommées, ajustables). Cf. spec §1.5.
_SRC_WEIGHT = {
    "failed": 1.0,
    "fallback": 0.7,
    "keyword": 0.6,
    "p1_refined": 0.1,
    "p1_theme": 0.0,
}
_W_SRC, _W_CONF, _W_JUMP, _W_NEW = 0.5, 0.2, 0.2, 0.1


def risk_score(source_class: str, confidence: float | None,
               is_jump: bool, is_new_dest: bool) -> float:
    """Score de risque composite (0..1+), sert le tri par défaut de la
    table de drill-down. Transparent : chaque terme est nommé."""
    sw = _SRC_WEIGHT.get(source_class, 0.6)
    conf = float(confidence) if confidence is not None else 0.0
    r = (_W_SRC * sw
         + _W_CONF * (1.0 - conf)
         + _W_JUMP * (1.0 if is_jump else 0.0)
         + _W_NEW * (1.0 if is_new_dest else 0.0))
    return round(r, 4)
```

- [ ] **Step 4 : Run test to verify it passes**

```bash
uv run python -m unittest tests.auto.test_refonte_results.TestRiskScore -v
```
Expected : OK

- [ ] **Step 5 : Commit**

```bash
git add dashboard/refonte_results.py tests/auto/test_refonte_results.py
git commit -m "feat(refonte-ui): risk_score — score composite transparent (poids nommés)"
```

---

## Task 5 : `build_risk_matrix`

**Files :**
- Modify : `dashboard/refonte_results.py`
- Modify : `tests/auto/test_refonte_results.py`

- [ ] **Step 1 : Write the failing test**

Add :

```python
from dashboard.refonte_results import build_risk_matrix  # noqa: E402


class TestRiskMatrix(unittest.TestCase):

    def _rows(self):
        return [
            {"source": "LLM (theme)", "confidence": "0.95"},
            {"source": "LLM (theme)", "confidence": "0.95"},
            {"source": "Keyword (x)", "confidence": "0.40"},
            {"source": "LLM (fallback)", "confidence": "0.95"},
            {"source": "FAILED", "confidence": "0.0"},
        ]

    def test_matrix_counts(self):
        out = build_risk_matrix(self._rows())
        # cellule (p1_theme, 0.9-1.0) = 2
        cell = next(c for c in out["matrix"]
                    if c["source_class"] == "p1_theme" and c["band"] == "0.9-1.0")
        self.assertEqual(cell["count"], 2)

    def test_budget_totals(self):
        out = build_risk_matrix(self._rows())
        budget = {b["source_class"]: b["count"] for b in out["budget"]}
        self.assertEqual(budget["p1_theme"], 2)
        self.assertEqual(budget["keyword"], 1)
        self.assertEqual(budget["fallback"], 1)
        self.assertEqual(budget["failed"], 1)

    def test_n_doubt_excludes_safe_p1(self):
        # doute = tout sauf P1 à conf >= 0.7. Ici : keyword(1) + fallback(1)
        # + failed(1) = 3 ; les 2 p1_theme à 0.95 sont sûrs.
        out = build_risk_matrix(self._rows())
        self.assertEqual(out["n_doubt"], 3)
```

- [ ] **Step 2 : Run test to verify it fails**

```bash
uv run python -m unittest tests.auto.test_refonte_results.TestRiskMatrix -v
```
Expected : ImportError

- [ ] **Step 3 : Implement**

Add to `dashboard/refonte_results.py` :

```python
SOURCE_CLASSES = ("p1_theme", "p1_refined", "keyword", "fallback", "failed")


def build_risk_matrix(rows: list[dict]) -> dict:
    """Construit la matrice source_class × confidence_band + le budget
    (totaux par source) + n_doubt (zone de doute).

    Zone de doute = toutes les classes sauf P1 (theme/refined) à
    confiance >= 0.7. Autrement dit : keyword/fallback/failed + tout P1
    à confiance < 0.7.
    """
    from collections import defaultdict
    cells: dict = defaultdict(int)
    budget: dict = defaultdict(int)
    n_doubt = 0
    for r in rows:
        sc = bucket_source(r.get("source"))
        try:
            conf = float(r.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        band = confidence_band(conf)
        cells[(sc, band)] += 1
        budget[sc] += 1
        is_safe = sc in ("p1_theme", "p1_refined") and conf >= 0.7
        if not is_safe:
            n_doubt += 1
    matrix = [
        {"source_class": sc, "band": b, "count": cells.get((sc, b), 0)}
        for sc in SOURCE_CLASSES for b in CONF_BANDS
    ]
    budget_list = [
        {"source_class": sc, "count": budget.get(sc, 0)}
        for sc in SOURCE_CLASSES
    ]
    return {"matrix": matrix, "budget": budget_list, "n_doubt": n_doubt}
```

- [ ] **Step 4 : Run test to verify it passes**

```bash
uv run python -m unittest tests.auto.test_refonte_results.TestRiskMatrix -v
```
Expected : OK

- [ ] **Step 5 : Commit**

```bash
git add dashboard/refonte_results.py tests/auto/test_refonte_results.py
git commit -m "feat(refonte-ui): build_risk_matrix — matrice source×confiance + budget + n_doubt"
```

---

## Task 6 : `enrich_row` + `select_doubt_files` (filtre/tri/pagination)

**Files :**
- Modify : `dashboard/refonte_results.py`
- Modify : `tests/auto/test_refonte_results.py`

- [ ] **Step 1 : Write the failing test**

Add :

```python
from dashboard.refonte_results import enrich_row, select_doubt_files  # noqa: E402


class TestDoubtFiles(unittest.TestCase):

    def _rows(self):
        return [
            {"rel_path": "a.pdf", "current_folder": "_INBOX",
             "proposed_folder": "02-INFORMATIQUE/14-Web", "source": "LLM (theme)",
             "top_theme": "Web", "confidence": "0.95"},
            {"rel_path": "b.pdf", "current_folder": "04-SHS/03-HISTOIRE",
             "proposed_folder": "01-SCIENCES/02-PHYSIQUE/Astro", "source": "LLM (fallback)",
             "top_theme": "Astro", "confidence": "0.93"},
            {"rel_path": "c.pdf", "current_folder": "_INBOX",
             "proposed_folder": "", "source": "FAILED", "top_theme": "",
             "confidence": "0.0"},
        ]

    def test_enrich_flags(self):
        row = enrich_row(self._rows()[1], creations={"01-SCIENCES/02-PHYSIQUE/Astro"})
        self.assertEqual(row["source_class"], "fallback")
        self.assertTrue(row["is_jump"])       # SHS → SCIENCES
        self.assertTrue(row["is_new_dest"])   # destination créée
        self.assertIn("risk", row)

    def test_select_doubt_excludes_safe_p1(self):
        out = select_doubt_files(self._rows(), creations=set(), page=1, page_size=50)
        paths = [r["rel_path"] for r in out["rows"]]
        self.assertNotIn("a.pdf", paths)  # P1 conf 0.95 = sûr, exclu
        self.assertIn("b.pdf", paths)
        self.assertIn("c.pdf", paths)
        self.assertEqual(out["total"], 2)

    def test_sorted_by_risk_desc(self):
        out = select_doubt_files(self._rows(), creations=set(), page=1, page_size=50)
        risks = [r["risk"] for r in out["rows"]]
        self.assertEqual(risks, sorted(risks, reverse=True))

    def test_filter_by_source_class_and_band(self):
        out = select_doubt_files(self._rows(), creations=set(), page=1, page_size=50,
                                 source_class="failed")
        self.assertEqual([r["rel_path"] for r in out["rows"]], ["c.pdf"])

    def test_pagination(self):
        out = select_doubt_files(self._rows(), creations=set(), page=1, page_size=1)
        self.assertEqual(len(out["rows"]), 1)
        self.assertEqual(out["total"], 2)
        self.assertEqual(out["page"], 1)
```

- [ ] **Step 2 : Run test to verify it fails**

```bash
uv run python -m unittest tests.auto.test_refonte_results.TestDoubtFiles -v
```
Expected : ImportError

- [ ] **Step 3 : Implement**

Add to `dashboard/refonte_results.py` :

```python
def enrich_row(row: dict, creations: set[str]) -> dict:
    """Enrichit une ligne CSV avec source_class, band, is_jump,
    is_new_dest, risk. Retourne un nouveau dict (n'altère pas l'entrée)."""
    try:
        conf = float(row.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    sc = bucket_source(row.get("source"))
    cur = row.get("current_folder", "")
    prop = row.get("proposed_folder", "")
    jump = is_inter_discipline_jump(cur, prop)
    new_dest = bool(prop) and prop in creations
    return {
        "rel_path": row.get("rel_path", ""),
        "current_folder": cur,
        "proposed_folder": prop,
        "source": row.get("source", ""),
        "source_class": sc,
        "top_theme": row.get("top_theme", ""),
        "confidence": conf,
        "band": confidence_band(conf),
        "is_jump": jump,
        "is_new_dest": new_dest,
        "risk": risk_score(sc, conf, jump, new_dest),
    }


def _is_doubt(enriched: dict) -> bool:
    sc = enriched["source_class"]
    return not (sc in ("p1_theme", "p1_refined") and enriched["confidence"] >= 0.7)


def select_doubt_files(rows: list[dict], creations: set[str], *,
                       page: int = 1, page_size: int = 50,
                       source_class: str | None = None,
                       band: str | None = None) -> dict:
    """Zone de doute enrichie, filtrée (source_class/band), triée par
    risque décroissant, paginée côté serveur."""
    enriched = [enrich_row(r, creations) for r in rows]
    doubt = [e for e in enriched if _is_doubt(e)]
    if source_class:
        doubt = [e for e in doubt if e["source_class"] == source_class]
    if band:
        doubt = [e for e in doubt if e["band"] == band]
    doubt.sort(key=lambda e: e["risk"], reverse=True)
    total = len(doubt)
    page = max(1, page)
    start = (page - 1) * page_size
    return {
        "rows": doubt[start:start + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
```

- [ ] **Step 4 : Run test to verify it passes**

```bash
uv run python -m unittest tests.auto.test_refonte_results.TestDoubtFiles -v
```
Expected : OK

- [ ] **Step 5 : Commit**

```bash
git add dashboard/refonte_results.py tests/auto/test_refonte_results.py
git commit -m "feat(refonte-ui): enrich_row + select_doubt_files (filtre/tri risque/pagination)"
```

---

## Task 7 : `build_proposed_tree` (icicle avec entrants)

**Files :**
- Modify : `dashboard/refonte_results.py`
- Modify : `tests/auto/test_refonte_results.py`

- [ ] **Step 1 : Write the failing test**

Add :

```python
from dashboard.refonte_results import build_proposed_tree  # noqa: E402


class TestProposedTree(unittest.TestCase):

    def test_incoming_counts_and_creation_flag(self):
        folders = ["02-INFORMATIQUE/14-Web", "02-INFORMATIQUE/05-IA-ML/RAG"]
        rows = [
            {"proposed_folder": "02-INFORMATIQUE/14-Web"},
            {"proposed_folder": "02-INFORMATIQUE/14-Web"},
            {"proposed_folder": "02-INFORMATIQUE/05-IA-ML/RAG"},
        ]
        tree = build_proposed_tree(folders, rows,
                                   creations={"02-INFORMATIQUE/05-IA-ML/RAG"})
        # racine "02-INFORMATIQUE" agrège ses descendants (3 entrants)
        info = next(c for c in tree["children"] if c["name"] == "02-INFORMATIQUE")
        self.assertEqual(info["n_incoming"], 3)
        # le dossier RAG est marqué créé
        rag = _find(info, "RAG")
        self.assertTrue(rag["is_creation"])
        self.assertEqual(rag["n_incoming"], 1)


def _find(node, name):
    if node["name"] == name:
        return node
    for c in node.get("children", []):
        r = _find(c, name)
        if r:
            return r
    return None
```

- [ ] **Step 2 : Run test to verify it fails**

```bash
uv run python -m unittest tests.auto.test_refonte_results.TestProposedTree -v
```
Expected : ImportError

- [ ] **Step 3 : Implement**

Add to `dashboard/refonte_results.py` :

```python
def build_proposed_tree(folders: list[str], rows: list[dict],
                        creations: set[str]) -> dict:
    """Construit une hiérarchie {name, path, children, n_incoming,
    is_creation} depuis la liste plate des dossiers proposés. n_incoming
    par dossier = nb de lignes dont proposed_folder == ce dossier ;
    agrégé bottom-up sur les ancêtres."""
    from collections import Counter
    direct = Counter(r.get("proposed_folder", "") for r in rows)

    root = {"name": "(racine)", "path": "", "children": [], "n_incoming": 0,
            "is_creation": False}
    index: dict[str, dict] = {"": root}

    def _ensure(path: str) -> dict:
        if path in index:
            return index[path]
        parent_path, _, name = path.rpartition("/")
        parent = _ensure(parent_path)
        node = {"name": name, "path": path, "children": [], "n_incoming": 0,
                "is_creation": path in creations}
        parent["children"].append(node)
        index[path] = node
        return node

    for f in folders:
        _ensure(f)
    # entrants directs (un dossier non listé mais cible de fichiers est créé)
    for path, n in direct.items():
        if not path:
            continue
        _ensure(path)
    # agrégation bottom-up
    def _agg(node: dict) -> int:
        total = direct.get(node["path"], 0)
        for c in node["children"]:
            total += _agg(c)
        node["n_incoming"] = total
        return total

    _agg(root)
    return root
```

- [ ] **Step 4 : Run test to verify it passes**

```bash
uv run python -m unittest tests.auto.test_refonte_results.TestProposedTree -v
```
Expected : OK

- [ ] **Step 5 : Commit**

```bash
git add dashboard/refonte_results.py tests/auto/test_refonte_results.py
git commit -m "feat(refonte-ui): build_proposed_tree — hiérarchie + entrants agrégés + flag créations"
```

---

## Task 8 : `folder_provenance`

**Files :**
- Modify : `dashboard/refonte_results.py`
- Modify : `tests/auto/test_refonte_results.py`

- [ ] **Step 1 : Write the failing test**

Add :

```python
from dashboard.refonte_results import folder_provenance  # noqa: E402


class TestProvenance(unittest.TestCase):

    def test_top_origins_for_folder(self):
        rows = [
            {"proposed_folder": "X/Y", "current_folder": "_INBOX"},
            {"proposed_folder": "X/Y", "current_folder": "_INBOX"},
            {"proposed_folder": "X/Y", "current_folder": "04-SHS"},
            {"proposed_folder": "Z", "current_folder": "_INBOX"},
        ]
        out = folder_provenance(rows, "X/Y")
        self.assertEqual(out["origins"][0], {"folder": "_INBOX", "count": 2})
        self.assertEqual(out["origins"][1], {"folder": "04-SHS", "count": 1})

    def test_limit(self):
        rows = [{"proposed_folder": "X", "current_folder": f"o{i}"} for i in range(20)]
        out = folder_provenance(rows, "X", limit=8)
        self.assertEqual(len(out["origins"]), 8)
```

- [ ] **Step 2 : Run test to verify it fails**

```bash
uv run python -m unittest tests.auto.test_refonte_results.TestProvenance -v
```
Expected : ImportError

- [ ] **Step 3 : Implement**

Add to `dashboard/refonte_results.py` :

```python
def folder_provenance(rows: list[dict], folder: str, *, limit: int = 8) -> dict:
    """Top dossiers d'origine (current_folder) des fichiers entrants d'un
    proposed_folder donné, triés par compte décroissant."""
    from collections import Counter
    c = Counter(
        r.get("current_folder", "") or "(racine)"
        for r in rows if r.get("proposed_folder") == folder
    )
    origins = [{"folder": k, "count": n}
               for k, n in c.most_common(limit)]
    return {"folder": folder, "origins": origins}
```

- [ ] **Step 4 : Run test to verify it passes**

```bash
uv run python -m unittest tests.auto.test_refonte_results.TestProvenance -v
```
Expected : OK

- [ ] **Step 5 : Commit**

```bash
git add dashboard/refonte_results.py tests/auto/test_refonte_results.py
git commit -m "feat(refonte-ui): folder_provenance — top origines des entrants d'un dossier"
```

---

## Task 9 : Lecteurs d'artefacts (`read_projection_rows`, `load_creations`, `load_proposed_folders`)

**Files :**
- Modify : `dashboard/refonte_results.py`
- Modify : `tests/auto/test_refonte_results.py`

- [ ] **Step 1 : Write the failing test**

Add :

```python
import json
import shutil
import tempfile
from pathlib import Path

from dashboard.refonte_results import (  # noqa: E402
    read_projection_rows, load_creations, load_proposed_folders,
)


class TestReaders(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.run = Path(self.tmp) / "run1"
        (self.run / "proposed").mkdir(parents=True)
        (self.run / "simulation").mkdir(parents=True)
        (self.run / "simulation" / "reclassify-projection.csv").write_text(
            "rel_path,current_folder,proposed_folder,changed,source,top_theme,confidence,score\n"
            "a.pdf,_INBOX,02-INFO/Web,true,LLM (theme),Web,0.95,0.9\n",
            encoding="utf-8")
        (self.run / "proposed" / "changes.json").write_text(
            json.dumps({"creations": [{"path": "02-INFO/Web", "rationale": "x"}]}),
            encoding="utf-8")
        import yaml
        (self.run / "proposed" / "tree-proposed.yaml").write_text(
            yaml.safe_dump({"folders": ["02-INFO/Web", "02-INFO/IA"]}),
            encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_read_rows(self):
        rows = read_projection_rows(self.run)
        self.assertEqual(rows[0]["rel_path"], "a.pdf")
        self.assertEqual(rows[0]["proposed_folder"], "02-INFO/Web")

    def test_load_creations(self):
        self.assertEqual(load_creations(self.run), {"02-INFO/Web"})

    def test_load_proposed_folders(self):
        self.assertEqual(load_proposed_folders(self.run),
                         ["02-INFO/Web", "02-INFO/IA"])

    def test_missing_files_return_empty(self):
        empty = Path(self.tmp) / "nope"
        self.assertEqual(read_projection_rows(empty), [])
        self.assertEqual(load_creations(empty), set())
        self.assertEqual(load_proposed_folders(empty), [])
```

- [ ] **Step 2 : Run test to verify it fails**

```bash
uv run python -m unittest tests.auto.test_refonte_results.TestReaders -v
```
Expected : ImportError

- [ ] **Step 3 : Implement**

Add to `dashboard/refonte_results.py` :

```python
import csv
import json
from pathlib import Path

import yaml


def read_projection_rows(run_dir: Path | str) -> list[dict]:
    """Lit simulation/reclassify-projection.csv → list[dict]. [] si absent."""
    p = Path(run_dir) / "simulation" / "reclassify-projection.csv"
    if not p.exists():
        return []
    with p.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_creations(run_dir: Path | str) -> set[str]:
    """Ensemble des chemins de dossiers créés (changes.json:creations)."""
    p = Path(run_dir) / "proposed" / "changes.json"
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return {c.get("path", "") for c in (data.get("creations") or []) if c.get("path")}


def load_proposed_folders(run_dir: Path | str) -> list[str]:
    """Liste plate des dossiers de tree-proposed.yaml (clé 'folders')."""
    p = Path(run_dir) / "proposed" / "tree-proposed.yaml"
    if not p.exists():
        return []
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return []
    folders = data.get("folders") if isinstance(data, dict) else None
    return [str(f) for f in folders] if isinstance(folders, list) else []
```

Move the `import csv/json/Path/yaml` to the top of the module (after `from __future__`).

- [ ] **Step 4 : Run test to verify it passes**

```bash
uv run python -m unittest tests.auto.test_refonte_results.TestReaders -v
uv run python -m unittest tests.auto.test_refonte_results -v
```
Expected : tout OK

- [ ] **Step 5 : Commit**

```bash
git add dashboard/refonte_results.py tests/auto/test_refonte_results.py
git commit -m "feat(refonte-ui): lecteurs d'artefacts run (rows CSV, creations, folders)"
```

---

## Task 10 : Helper + route `risk-matrix`

**Files :**
- Modify : `dashboard/agent_refonte.py`
- Modify : `dashboard/app.py`
- Create : `tests/auto/test_agent_refonte_results_api.py`

- [ ] **Step 1 : Write the failing test**

Create `tests/auto/test_agent_refonte_results_api.py` :

```python
#!/usr/bin/env python3
"""Tests d'intégration HTTP des endpoints de restitution Phase B."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml
from fastapi.testclient import TestClient

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from dashboard.app import app  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.profiles = Path(self.tmp) / "profiles"
        run = self.profiles / "default" / ".cache" / "refonte" / "run1"
        (run / "proposed").mkdir(parents=True)
        (run / "simulation").mkdir(parents=True)
        (run / "simulation" / "reclassify-projection.csv").write_text(
            "rel_path,current_folder,proposed_folder,changed,source,top_theme,confidence,score\n"
            "a.pdf,_INBOX,02-INFO/Web,true,LLM (theme),Web,0.95,0.9\n"
            "b.pdf,04-SHS/HIST,01-SCI/Astro,true,LLM (fallback),Astro,0.93,0.5\n"
            "c.pdf,_INBOX,,false,FAILED,,0.0,0.0\n", encoding="utf-8")
        (run / "proposed" / "changes.json").write_text(
            json.dumps({"creations": [{"path": "01-SCI/Astro", "rationale": "x"}]}),
            encoding="utf-8")
        (run / "proposed" / "tree-proposed.yaml").write_text(
            yaml.safe_dump({"folders": ["02-INFO/Web", "01-SCI/Astro"]}),
            encoding="utf-8")
        self.patch = mock.patch("dashboard.data.get_project_root",
                                return_value=Path(self.tmp))
        self.patch.start()
        self.client = TestClient(app)

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestRiskMatrixAPI(_Base):
    def test_risk_matrix_endpoint(self):
        r = self.client.get(
            "/api/agent/refonte/proposition/run1/risk-matrix?profile=default")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertIn("matrix", d)
        self.assertIn("budget", d)
        self.assertEqual(d["n_doubt"], 2)  # fallback + failed


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2 : Run test to verify it fails**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_results_api.TestRiskMatrixAPI -v
```
Expected : 404 (route absente) → assertion 200 échoue

- [ ] **Step 3 : Implement helper + route**

In `dashboard/agent_refonte.py`, add a helper (after `get_proposition_simulation`). First locate the existing run-dir resolution: `get_proposition_simulation` builds the run dir from `profile` + `run_id` — reuse the same path logic. Add :

```python
def get_risk_matrix(profile: str, run_id: str) -> dict:
    """Matrice de risque + budget + n_doubt pour un run de proposition."""
    from dashboard import data, refonte_results
    run_dir = (data.get_project_root() / "profiles" / profile
               / ".cache" / "refonte" / run_id)
    rows = refonte_results.read_projection_rows(run_dir)
    return refonte_results.build_risk_matrix(rows)
```

In `dashboard/app.py`, near the existing `/simulation` route (~line 2759), add :

```python
@app.get("/api/agent/refonte/proposition/{run_id}/risk-matrix")
async def api_refonte_risk_matrix(run_id: str, profile: str):
    from fastapi.responses import JSONResponse
    return JSONResponse(agent_refonte.get_risk_matrix(profile, run_id))
```

- [ ] **Step 4 : Run test to verify it passes**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_results_api.TestRiskMatrixAPI -v
```
Expected : OK

- [ ] **Step 5 : Commit**

```bash
git add dashboard/agent_refonte.py dashboard/app.py tests/auto/test_agent_refonte_results_api.py
git commit -m "feat(refonte-ui): endpoint GET .../risk-matrix"
```

---

## Task 11 : Helper + route `doubt-files` (paginé/filtré)

**Files :**
- Modify : `dashboard/agent_refonte.py`
- Modify : `dashboard/app.py`
- Modify : `tests/auto/test_agent_refonte_results_api.py`

- [ ] **Step 1 : Write the failing test**

Add to `tests/auto/test_agent_refonte_results_api.py` :

```python
class TestDoubtFilesAPI(_Base):
    def test_doubt_files_excludes_safe_and_sorts(self):
        r = self.client.get(
            "/api/agent/refonte/proposition/run1/doubt-files?profile=default")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        paths = [x["rel_path"] for x in d["rows"]]
        self.assertNotIn("a.pdf", paths)        # P1 0.95 sûr
        self.assertEqual(d["total"], 2)
        # b.pdf a is_jump + is_new_dest → flags présents
        b = next(x for x in d["rows"] if x["rel_path"] == "b.pdf")
        self.assertTrue(b["is_jump"])
        self.assertTrue(b["is_new_dest"])

    def test_doubt_files_filter_source_class(self):
        r = self.client.get(
            "/api/agent/refonte/proposition/run1/doubt-files"
            "?profile=default&source_class=failed")
        d = r.json()
        self.assertEqual([x["rel_path"] for x in d["rows"]], ["c.pdf"])
```

- [ ] **Step 2 : Run test to verify it fails**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_results_api.TestDoubtFilesAPI -v
```
Expected : 404

- [ ] **Step 3 : Implement**

In `dashboard/agent_refonte.py` :

```python
def get_doubt_files(profile: str, run_id: str, *, page: int = 1,
                    page_size: int = 50, source_class: str | None = None,
                    band: str | None = None) -> dict:
    from dashboard import data, refonte_results
    run_dir = (data.get_project_root() / "profiles" / profile
               / ".cache" / "refonte" / run_id)
    rows = refonte_results.read_projection_rows(run_dir)
    creations = refonte_results.load_creations(run_dir)
    return refonte_results.select_doubt_files(
        rows, creations, page=page, page_size=page_size,
        source_class=source_class, band=band)
```

In `dashboard/app.py` :

```python
@app.get("/api/agent/refonte/proposition/{run_id}/doubt-files")
async def api_refonte_doubt_files(run_id: str, profile: str, page: int = 1,
                                  page_size: int = 50,
                                  source_class: str | None = None,
                                  band: str | None = None):
    from fastapi.responses import JSONResponse
    return JSONResponse(agent_refonte.get_doubt_files(
        profile, run_id, page=page, page_size=page_size,
        source_class=source_class, band=band))
```

- [ ] **Step 4 : Run test to verify it passes**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_results_api.TestDoubtFilesAPI -v
```
Expected : OK

- [ ] **Step 5 : Commit**

```bash
git add dashboard/agent_refonte.py dashboard/app.py tests/auto/test_agent_refonte_results_api.py
git commit -m "feat(refonte-ui): endpoint GET .../doubt-files (filtre/tri/pagination)"
```

---

## Task 12 : Helper + route `proposed-tree`

**Files :**
- Modify : `dashboard/agent_refonte.py`, `dashboard/app.py`, `tests/auto/test_agent_refonte_results_api.py`

- [ ] **Step 1 : Write the failing test**

Add :

```python
class TestProposedTreeAPI(_Base):
    def test_tree_endpoint(self):
        r = self.client.get(
            "/api/agent/refonte/proposition/run1/proposed-tree?profile=default")
        self.assertEqual(r.status_code, 200)
        tree = r.json()["tree"]
        names = [c["name"] for c in tree["children"]]
        self.assertIn("02-INFO", names)
        self.assertIn("01-SCI", names)
```

- [ ] **Step 2 : Run test to verify it fails**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_results_api.TestProposedTreeAPI -v
```
Expected : 404

- [ ] **Step 3 : Implement**

In `dashboard/agent_refonte.py` :

```python
def get_proposed_tree(profile: str, run_id: str) -> dict:
    from dashboard import data, refonte_results
    run_dir = (data.get_project_root() / "profiles" / profile
               / ".cache" / "refonte" / run_id)
    rows = refonte_results.read_projection_rows(run_dir)
    folders = refonte_results.load_proposed_folders(run_dir)
    creations = refonte_results.load_creations(run_dir)
    return {"tree": refonte_results.build_proposed_tree(folders, rows, creations)}
```

In `dashboard/app.py` :

```python
@app.get("/api/agent/refonte/proposition/{run_id}/proposed-tree")
async def api_refonte_proposed_tree(run_id: str, profile: str):
    from fastapi.responses import JSONResponse
    return JSONResponse(agent_refonte.get_proposed_tree(profile, run_id))
```

- [ ] **Step 4 : Run test to verify it passes**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_results_api.TestProposedTreeAPI -v
```
Expected : OK

- [ ] **Step 5 : Commit**

```bash
git add dashboard/agent_refonte.py dashboard/app.py tests/auto/test_agent_refonte_results_api.py
git commit -m "feat(refonte-ui): endpoint GET .../proposed-tree"
```

---

## Task 13 : Helper + route `folder-provenance`

**Files :**
- Modify : `dashboard/agent_refonte.py`, `dashboard/app.py`, `tests/auto/test_agent_refonte_results_api.py`

- [ ] **Step 1 : Write the failing test**

Add :

```python
class TestProvenanceAPI(_Base):
    def test_provenance_endpoint(self):
        r = self.client.get(
            "/api/agent/refonte/proposition/run1/folder-provenance"
            "?profile=default&folder=01-SCI/Astro")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["origins"], [{"folder": "04-SHS/HIST", "count": 1}])
```

- [ ] **Step 2 : Run test to verify it fails**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_results_api.TestProvenanceAPI -v
```
Expected : 404

- [ ] **Step 3 : Implement**

In `dashboard/agent_refonte.py` :

```python
def get_folder_provenance(profile: str, run_id: str, folder: str) -> dict:
    from dashboard import data, refonte_results
    run_dir = (data.get_project_root() / "profiles" / profile
               / ".cache" / "refonte" / run_id)
    rows = refonte_results.read_projection_rows(run_dir)
    return refonte_results.folder_provenance(rows, folder)
```

In `dashboard/app.py` :

```python
@app.get("/api/agent/refonte/proposition/{run_id}/folder-provenance")
async def api_refonte_folder_provenance(run_id: str, profile: str, folder: str):
    from fastapi.responses import JSONResponse
    return JSONResponse(agent_refonte.get_folder_provenance(profile, run_id, folder))
```

- [ ] **Step 4 : Run test to verify it passes**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_results_api -v
```
Expected : tous OK (4 classes API)

- [ ] **Step 5 : Commit**

```bash
git add dashboard/agent_refonte.py dashboard/app.py tests/auto/test_agent_refonte_results_api.py
git commit -m "feat(refonte-ui): endpoint GET .../folder-provenance"
```

---

## Task 14 : Restructure des 3 onglets (template + tab JS)

**Files :**
- Modify : `dashboard/templates/partials/agent_refonte_panel.html`
- Modify : `tests/auto/test_agent_refonte_results_api.py` (test de rendu)

Le template actuel a 3 onglets `rationale / diff / impact` (l.928-941) et `renderPhaseBReport` qui bind les listeners. On passe à `verdict / arbre / plan`, **verdict par défaut**.

- [ ] **Step 1 : Write the failing test (rendu)**

Add to `tests/auto/test_agent_refonte_results_api.py` :

```python
class TestPanelTabs(_Base):
    def test_panel_has_three_decision_tabs(self):
        r = self.client.get("/taxonomy?profile=default")
        self.assertEqual(r.status_code, 200)
        html = r.text
        self.assertIn('data-pane="verdict"', html)
        self.assertIn('data-pane="arbre"', html)
        self.assertIn('data-pane="plan"', html)
        self.assertIn("Verdict", html)
```

- [ ] **Step 2 : Run test to verify it fails**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_results_api.TestPanelTabs -v
```
Expected : `data-pane="verdict"` absent → échec

- [ ] **Step 3 : Modify the template tabs**

In `dashboard/templates/partials/agent_refonte_panel.html`, replace the Phase B tabs block (the `tax-refonte-phaseb-tabs` + the 3 panes, ~l.928-941) with :

```html
<div class="tax-refonte-phaseb-tabs" role="tablist">
  <button class="tax-refonte-phaseb-tab active" data-pane="verdict">🚦 Verdict &amp; Risque</button>
  <button class="tax-refonte-phaseb-tab" data-pane="arbre">🌳 Arbre proposé</button>
  <button class="tax-refonte-phaseb-tab" data-pane="plan">📋 Plan complet</button>
</div>
<div class="tax-refonte-phaseb-pane active" data-pane="verdict">
  <div class="rr-verdict" id="rr-verdict"><p class="muted small">Chargement…</p></div>
</div>
<div class="tax-refonte-phaseb-pane" data-pane="arbre">
  <div class="rr-arbre" id="rr-arbre"></div>
</div>
<div class="tax-refonte-phaseb-pane" data-pane="plan">
  <div class="tax-refonte-md" id="tax-refonte-rationale-md"></div>
</div>
```

In the JS `renderPhaseBReport` (~l.953), update the pane-switch + lazy-load wiring. Find where `loadSimulation`/`loadTreeDiff` are bound to `impact`/`diff` panes and replace with bindings to the new panes : `verdict` → `loadVerdict` (Task 15), `arbre` → `loadProposedTree` (Task 16), `plan` → render rationale markdown (existing `marked.parse` into `#tax-refonte-rationale-md`). Set `verdict` as the default active pane (load it immediately when the report renders). Keep the lazy-load flags pattern (`verdictLoaded`, `arbreLoaded`).

Replace the old `diffLoaded`/`impactLoaded` flags + their click handlers accordingly. The rationale markdown that was loaded into the old `rationale` pane now loads into the `plan` pane (same `#tax-refonte-rationale-md` id, just a different default order).

- [ ] **Step 4 : Run test to verify it passes**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_results_api.TestPanelTabs -v
```
Expected : OK

- [ ] **Step 5 : Commit**

```bash
git add dashboard/templates/partials/agent_refonte_panel.html tests/auto/test_agent_refonte_results_api.py
git commit -m "feat(refonte-ui): restructure 3 onglets Verdict/Arbre/Plan (verdict défaut)"
```

---

## Task 15 : Onglet Verdict & Risque (JS + rendu)

**Files :**
- Modify : `dashboard/templates/partials/agent_refonte_panel.html`

Adapter le JS de la maquette `06-combo-1-2.html` (déjà validée) au template réel, branché sur les vrais endpoints.

- [ ] **Step 1 : Add `loadVerdict` + render (no automated test — interactive JS, verified by smoke in Task 18)**

In `agent_refonte_panel.html`, inside the `<script>` of the panel, add functions :

```javascript
async function loadVerdict(runId, profile){
  const el = document.getElementById('rr-verdict');
  const r = await fetch('/api/agent/refonte/proposition/'+runId+'/risk-matrix?profile='+encodeURIComponent(profile));
  const d = await r.json();
  renderVerdict(d, runId, profile);
}
const RR_SRC = [
  {k:'p1_theme',name:'P1 mapping',col:'#4ade80'},
  {k:'p1_refined',name:'P1 affiné',col:'#22c55e'},
  {k:'keyword',name:'keyword P2',col:'#fbbf24'},
  {k:'fallback',name:'fallback P4',col:'#fb923c'},
  {k:'failed',name:'FAILED',col:'#f87171'},
];
const RR_BANDS = ['0-0.5','0.5-0.7','0.7-0.9','0.9-1.0'];
function rrCellClass(sc,band,n){
  if(n===0) return 'rr-c0';
  if(sc==='p1_theme'||sc==='p1_refined') return band==='0.9-1.0'||band==='0.7-0.9'?'rr-cg':'rr-cy';
  if(sc==='failed') return 'rr-cr';
  if(sc==='fallback') return band==='0.9-1.0'?'rr-co':'rr-cy';
  if(sc==='keyword') return band==='0-0.5'?'rr-cr':'rr-cy';
  return 'rr-cy';
}
function renderVerdict(d, runId, profile){
  const el = document.getElementById('rr-verdict');
  const bud = {}; d.budget.forEach(b=>bud[b.source_class]=b.count);
  const total = d.budget.reduce((s,b)=>s+b.count,0);
  const cellMap = {}; d.matrix.forEach(c=>cellMap[c.source_class+'|'+c.band]=c.count);
  let html = '<div class="rr-budget">';
  RR_SRC.forEach(s=>{const n=bud[s.k]||0; if(n>0) html+='<div style="flex:'+n+';background:'+s.col+'" title="'+s.name+' '+n+'">'+(n/total>0.05?n.toLocaleString('fr'):'')+'</div>';});
  html += '</div><div class="rr-doubt-kpi">⚠ '+d.n_doubt.toLocaleString('fr')+' fichiers à revoir</div>';
  // matrix
  html += '<div class="rr-mx"><div></div>'+RR_BANDS.map(b=>'<div class="rr-h">'+b+'</div>').join('');
  RR_SRC.forEach(s=>{html+='<div class="rr-rh">'+s.name+'</div>';
    RR_BANDS.forEach(b=>{const n=cellMap[s.k+'|'+b]||0;const cls=rrCellClass(s.k,b,n);
      const clickable=(cls==='rr-cr'||cls==='rr-co'||cls==='rr-cy')&&n>0;
      html+='<div class="rr-cell '+cls+'"'+(clickable?' data-sc="'+s.k+'" data-band="'+b+'"':'')+'>'+(n>0?n.toLocaleString('fr'):'·')+'</div>';});});
  html += '</div><div class="rr-table-host" id="rr-table-host"></div>';
  el.innerHTML = html;
  el.querySelectorAll('.rr-cell[data-sc]').forEach(c=>c.onclick=()=>{
    el.querySelectorAll('.rr-cell.sel').forEach(e=>e.classList.remove('sel')); c.classList.add('sel');
    loadDoubt(runId, profile, c.dataset.sc, c.dataset.band);
  });
}
async function loadDoubt(runId, profile, sc, band){
  const host = document.getElementById('rr-table-host');
  host.innerHTML = '<p class="muted small">Chargement…</p>';
  const r = await fetch('/api/agent/refonte/proposition/'+runId+'/doubt-files?profile='+encodeURIComponent(profile)+'&source_class='+sc+'&band='+band+'&page_size=50');
  const d = await r.json();
  let h = '<div class="rr-table-head">'+d.total+' fichier(s) · triés par risque ▼</div><table class="rr-ftab"><thead><tr><th>Fichier</th><th>Source</th><th>Déplacement</th><th>Conf.</th><th>Alerte</th><th>Triage</th></tr></thead><tbody>';
  d.rows.forEach(x=>{
    const gc = x.confidence>=0.7?'#4ade80':x.confidence>=0.5?'#fbbf24':'#f87171';
    h += '<tr><td>'+x.rel_path+'</td><td><span class="rr-badge">'+x.source_class+'</span></td>'+
      '<td class="rr-arrow">'+(x.current_folder||'_')+' → '+(x.proposed_folder||'(aucune)')+'</td>'+
      '<td><span class="rr-gauge"><i style="width:'+Math.round(x.confidence*100)+'%;background:'+gc+'"></i></span></td>'+
      '<td>'+(x.is_new_dest?'🆕':'')+(x.is_jump?'⇄':'')+'</td>'+
      '<td><span class="rr-act" onclick="rrVerdict(this,1)">✓</span><span class="rr-act" onclick="rrVerdict(this,0)">✗</span></td></tr>';
  });
  h += '</tbody></table>';
  host.innerHTML = h;
}
// triage éphémère (V1) : marque la ligne, aucune persistance
function rrVerdict(elm, ok){const tr=elm.closest('tr');tr.style.opacity=0.4;tr.style.background=ok?'#14532d22':'#3a141422';}
```

- [ ] **Step 2 : Wire `loadVerdict` into the default pane load**

In `renderPhaseBReport`, call `loadVerdict(runId, profile)` immediately (verdict is the default pane). Guard with `verdictLoaded`.

- [ ] **Step 3 : Manual smoke**

```bash
uv run python -m dashboard 8099 &
sleep 5
curl -s "http://localhost:8099/api/agent/refonte/proposition/<un-vrai-run>/risk-matrix?profile=default" | head -c 200
kill %1
```
Expected : JSON `{"matrix":[...],"budget":[...],"n_doubt":...}`. (Le rendu interactif sera validé en Task 18.)

- [ ] **Step 4 : Run the full suite to ensure no regression**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_results_api tests.auto.test_refonte_results 2>&1 | tail -3
```
Expected : OK

- [ ] **Step 5 : Commit**

```bash
git add dashboard/templates/partials/agent_refonte_panel.html
git commit -m "feat(refonte-ui): onglet Verdict — budget + matrice cliquable + table triage éphémère"
```

---

## Task 16 : Onglet Arbre proposé (icicle HTML/CSS + provenance)

**Files :**
- Modify : `dashboard/templates/partials/agent_refonte_panel.html`

Icicle en **HTML/CSS pur** (pas de D3 : absent de la page standalone `/agent/refonte`).

- [ ] **Step 1 : Add `loadProposedTree` + render + provenance**

In `agent_refonte_panel.html` `<script>` :

```javascript
async function loadProposedTree(runId, profile){
  const r = await fetch('/api/agent/refonte/proposition/'+runId+'/proposed-tree?profile='+encodeURIComponent(profile));
  const d = await r.json();
  renderIcicle(d.tree, runId, profile);
}
function renderIcicle(tree, runId, profile){
  const el = document.getElementById('rr-arbre');
  const sections = (tree.children||[]).slice().sort((a,b)=>b.n_incoming-a.n_incoming);
  const maxSec = Math.max(1, ...sections.map(s=>s.n_incoming));
  let h = '<div class="rr-ic-legend">🆕 = dossier créé · hauteur = fichiers entrants · clic = provenance</div><div class="rr-icicle">';
  // col 1 sections
  h += '<div class="rr-ic-col" style="width:160px">';
  sections.forEach(s=>{h+='<div class="rr-ic-node'+(s.is_creation?' rr-new':'')+'" style="flex:'+Math.max(1,s.n_incoming)+'" data-folder="'+s.path+'">'+s.name+'<small>'+s.n_incoming+'</small></div>';});
  h += '</div>';
  // col 2 = children of the biggest section
  const big = sections[0];
  h += '<div class="rr-ic-col" style="flex:1" id="rr-ic-sub"></div></div>';
  h += '<div class="rr-prov" id="rr-prov"><span class="muted small">Clique un dossier pour voir d\'où viennent ses fichiers.</span></div>';
  el.innerHTML = h;
  function showSub(node){
    const sub = document.getElementById('rr-ic-sub');
    const ch = (node.children||[]).slice().sort((a,b)=>b.n_incoming-a.n_incoming);
    sub.innerHTML = ch.map(c=>'<div class="rr-ic-node'+(c.is_creation?' rr-new':'')+'" style="flex:'+Math.max(1,c.n_incoming)+'" data-folder="'+c.path+'">'+(c.is_creation?'🆕 ':'')+c.name+'<small>'+c.n_incoming+'</small></div>').join('');
    sub.querySelectorAll('.rr-ic-node').forEach(n=>n.onclick=()=>loadProvenance(runId,profile,n.dataset.folder,n));
  }
  if(big) showSub(big);
  el.querySelectorAll('.rr-ic-col:first-child .rr-ic-node').forEach(n=>n.onclick=()=>{
    const node=sections.find(s=>s.path===n.dataset.folder); showSub(node); loadProvenance(runId,profile,n.dataset.folder,n);
  });
}
async function loadProvenance(runId, profile, folder, nodeEl){
  document.querySelectorAll('.rr-ic-node.sel').forEach(e=>e.classList.remove('sel'));
  if(nodeEl) nodeEl.classList.add('sel');
  const r = await fetch('/api/agent/refonte/proposition/'+runId+'/folder-provenance?profile='+encodeURIComponent(profile)+'&folder='+encodeURIComponent(folder));
  const d = await r.json();
  const max = Math.max(1, ...d.origins.map(o=>o.count));
  document.getElementById('rr-prov').innerHTML =
    '<h4 class="rr-prov-h">D\'où viennent les fichiers de « '+folder+' »</h4>'+
    (d.origins.length? d.origins.map(o=>'<div class="rr-flow"><span class="rr-flow-l">'+o.folder+'</span><span class="rr-flow-bar" style="width:'+Math.round(o.count/max*180)+'px"></span><span class="muted">'+o.count+'</span></div>').join('') : '<span class="muted small">Aucun fichier entrant.</span>');
}
```

- [ ] **Step 2 : Wire `loadProposedTree` to the `arbre` pane (lazy-load, `arbreLoaded` flag)**

In `renderPhaseBReport`, bind the `arbre` tab click → `if(!arbreLoaded){loadProposedTree(runId,profile);arbreLoaded=true;}`.

- [ ] **Step 3 : Manual smoke**

```bash
uv run python -m dashboard 8099 &
sleep 5
curl -s "http://localhost:8099/api/agent/refonte/proposition/<un-vrai-run>/proposed-tree?profile=default" | head -c 200
kill %1
```
Expected : JSON `{"tree":{"children":[...]}}`.

- [ ] **Step 4 : Run suite**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_results_api 2>&1 | tail -3
```
Expected : OK

- [ ] **Step 5 : Commit**

```bash
git add dashboard/templates/partials/agent_refonte_panel.html
git commit -m "feat(refonte-ui): onglet Arbre proposé — icicle HTML/CSS + provenance au clic"
```

---

## Task 17 : CSS des composants

**Files :**
- Modify : `dashboard/static/style.css`

- [ ] **Step 1 : Add the `.rr-*` styles**

Append to `dashboard/static/style.css` :

```css
/* ── Restitution Phase B (outil de décision) ─────────────────────────── */
.rr-budget{display:flex;height:26px;border-radius:6px;overflow:hidden;border:1px solid var(--border);margin:6px 0}
.rr-budget>div{display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;color:#0b1220;cursor:default}
.rr-doubt-kpi{color:#fbbf24;font-weight:700;margin:8px 0}
.rr-mx{display:grid;grid-template-columns:150px repeat(4,1fr);gap:4px;max-width:680px;margin-top:8px}
.rr-mx .rr-h{font-size:10px;color:var(--text-muted);text-align:center}
.rr-rh{font-size:11px;display:flex;align-items:center}
.rr-cell{border-radius:4px;padding:8px 2px;text-align:center;font-weight:700;font-size:12px;border:1px solid transparent}
.rr-cell[data-sc]{cursor:pointer}
.rr-cell[data-sc]:hover{border-color:#ffffff66}
.rr-cell.sel{border-color:#fff}
.rr-c0{background:#1e2a3d;color:#475569}
.rr-cg{background:#14532d;color:#86efac}
.rr-cy{background:#3b2f0c;color:#fbbf24}
.rr-co{background:#3a230c;color:#fb923c}
.rr-cr{background:#3a1414;color:#f87171}
.rr-ftab{width:100%;border-collapse:collapse;margin-top:10px;font-size:12px}
.rr-ftab th{text-align:left;color:var(--text-muted);padding:5px 8px;border-bottom:1px solid var(--border);font-size:10px;text-transform:uppercase}
.rr-ftab td{padding:6px 8px;border-bottom:1px solid #1b2536}
.rr-table-head{margin-top:12px;color:var(--text-muted);font-size:11px}
.rr-badge{font-size:10px;padding:2px 6px;border-radius:4px;font-weight:700;background:#263349;color:#cbd5e1}
.rr-arrow{font-family:monospace;font-size:11px;color:var(--text-muted)}
.rr-gauge{display:inline-block;width:42px;height:6px;background:#1b2536;border-radius:3px;overflow:hidden;vertical-align:middle}
.rr-gauge>i{display:block;height:100%}
.rr-act{cursor:pointer;padding:0 3px;opacity:.7}.rr-act:hover{opacity:1}
.rr-ic-legend{font-size:11px;color:var(--text-muted);margin-bottom:8px}
.rr-icicle{display:flex;gap:4px;height:360px}
.rr-ic-col{display:flex;flex-direction:column;gap:4px}
.rr-ic-node{border-radius:5px;padding:6px 8px;font-size:11px;cursor:pointer;overflow:hidden;display:flex;flex-direction:column;justify-content:center;background:#1e3a5f;color:#bfdbfe;border:1px solid transparent}
.rr-ic-node:hover{filter:brightness(1.18)}
.rr-ic-node.sel{outline:2px solid #fff;outline-offset:-2px}
.rr-ic-node small{opacity:.7;font-size:9.5px}
.rr-ic-node.rr-new{background:#3a2a0c;color:#fbbf24;border-color:#f59e0b;box-shadow:0 0 8px #f59e0b55}
.rr-prov{margin-top:14px;background:var(--bg-tertiary,#0b1220);border:1px solid var(--border);border-radius:8px;padding:12px 14px;min-height:60px}
.rr-prov-h{margin:0 0 8px;font-size:12px}
.rr-flow{display:flex;align-items:center;gap:8px;font-size:11px;margin-bottom:5px}
.rr-flow-l{width:230px}
.rr-flow-bar{height:9px;border-radius:3px;background:#38bdf8}
```

- [ ] **Step 2 : Verify CSS loads (route renders, no 500)**

```bash
uv run python -m unittest tests.auto.test_agent_refonte_results_api.TestPanelTabs -v
```
Expected : OK (template + CSS référencé sans erreur)

- [ ] **Step 3 : Commit**

```bash
git add dashboard/static/style.css
git commit -m "feat(refonte-ui): styles .rr-* (budget, matrice, table triage, icicle, provenance)"
```

---

## Task 18 : Smoke end-to-end + ouverture PR

**Files :** aucun fichier de code (vérification).

- [ ] **Step 1 : Lancer un vrai run Phase B (ou réutiliser un run existant)**

Vérifier qu'un run de proposition existe :

```bash
ls profiles/default/.cache/refonte/*/simulation/reclassify-projection.csv | head -1
```

- [ ] **Step 2 : Smoke des 4 endpoints sur un vrai run**

```bash
RUN=$(ls -d profiles/default/.cache/refonte/*/ | tail -1 | xargs basename)
uv run python -m dashboard 8099 &
sleep 5
for ep in risk-matrix proposed-tree; do
  echo "== $ep =="; curl -s "http://localhost:8099/api/agent/refonte/proposition/$RUN/$ep?profile=default" | head -c 160; echo
done
curl -s "http://localhost:8099/api/agent/refonte/proposition/$RUN/doubt-files?profile=default&page_size=3" | head -c 200; echo
kill %1
```
Expected : chaque endpoint renvoie du JSON non vide et cohérent.

- [ ] **Step 3 : Vérif visuelle dashboard**

Lancer `./klodo.sh dashboard`, aller Taxonomie → 🤖 Refonte → sélectionner le run B. Vérifier : onglet Verdict par défaut (budget + matrice), clic cellule rouge → table avec ✓/✗ ; onglet Arbre → icicle, clic dossier → provenance ; onglet Plan → markdown rationale.

- [ ] **Step 4 : Suite complète + ruff**

```bash
uv run python -m unittest tests.auto.test_refonte_results tests.auto.test_agent_refonte_results_api tests.auto.test_agent_refonte_simulator tests.auto.test_agent_refonte_proposition 2>&1 | tail -3
uv run ruff check dashboard/refonte_results.py dashboard/agent_refonte.py dashboard/app.py tests/auto/test_refonte_results.py tests/auto/test_agent_refonte_results_api.py
```
Expected : tests OK, ruff `All checks passed!`

- [ ] **Step 5 : Push + PR**

```bash
git push -u origin feature/refonte-phaseb-results-ui
gh pr create --base develop \
  --title "feat(refonte-ui): restitution Phase B — outil de décision (Triage + Arbre)" \
  --body "Cf. spec docs/superpowers/specs/2026-06-09-refonte-phaseb-results-ui-design.md et plan docs/superpowers/plans/2026-06-09-refonte-phaseb-results-ui.md.

3 onglets : Verdict & Risque (budget + matrice source×confiance + table triage éphémère) · Arbre proposé (icicle HTML/CSS + provenance) · Plan complet (rationale). 4 endpoints lecture seule, agrégations pures testées. Triage V1 éphémère (pas de persistance).

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

---

## Self-Review

**Spec coverage :**

| Requirement spec | Task |
|---|---|
| Restructure 3 onglets (Verdict/Arbre/Plan, verdict défaut) | Task 14 |
| §1.1 ligne de verdict (KPI dont n_doubt) | Task 5 (n_doubt) + Task 15 (rendu) |
| §1.2 budget de risque par source (cliquable) | Task 5 (budget) + Task 15 |
| §1.3 matrice source×confiance (cliquable) | Task 5 + Task 15 |
| §1.4 table drill-down (paginée serveur, triée risque, badges/flèche/jauge/alertes) | Task 6 + Task 11 + Task 15 |
| §1.5 risk-score transparent | Task 4 |
| zone de doute (non-P1 + P1 conf<0.7), P1 sûr plié | Task 5 (n_doubt) + Task 6 (_is_doubt) |
| §2.1 icicle taxo proposée (entrants, créations surbrillance) | Task 7 + Task 12 + Task 16 |
| §2.2 panneau provenance | Task 8 + Task 13 + Task 16 |
| §③ Plan complet (rationale relégué) | Task 14 |
| backend bucket source / band / jump / new-dest | Tasks 1,2,3,6 |
| endpoints risk-matrix / doubt-files / proposed-tree / folder-provenance | Tasks 10,11,12,13 |
| pagination serveur (≤2000 lignes client) | Task 6 + Task 11 |
| triage ✓/✗ éphémère (pas de persistance) | Task 15 (`rrVerdict` client-only) |
| CSS composants | Task 17 |
| tests unitaires agrégations + intégration endpoints | Tasks 1-13 |

Toutes les exigences ont une tâche. ✓

**Placeholder scan :** aucun TBD/TODO ; tout le code est exécutable ; les `<un-vrai-run>` dans les smokes sont des paramètres runtime explicitement résolus par la commande `ls` du Step 1 de Task 18 (pas des placeholders de code). ✓

**Type consistency :**
- `bucket_source` retourne les 5 classes utilisées partout (`SOURCE_CLASSES`). ✓
- `build_risk_matrix` retourne `{matrix:[{source_class,band,count}], budget:[{source_class,count}], n_doubt}` — consommé tel quel par `get_risk_matrix` (Task 10) et `renderVerdict` (Task 15). ✓
- `select_doubt_files` retourne `{rows, total, page, page_size}` ; chaque row a `rel_path/source/source_class/current_folder/proposed_folder/confidence/top_theme/band/is_jump/is_new_dest/risk` (via `enrich_row`) — consommé par `loadDoubt` (Task 15). ✓
- `build_proposed_tree` retourne `{name,path,children,n_incoming,is_creation}` — consommé par `renderIcicle` (Task 16). ✓
- `folder_provenance` retourne `{folder, origins:[{folder,count}]}` — consommé par `loadProvenance` (Task 16). ✓
- Endpoints : chemins `/api/agent/refonte/proposition/{run_id}/{risk-matrix|doubt-files|proposed-tree|folder-provenance}` cohérents entre routes (app.py) et fetch (JS). ✓

Cohérent de bout en bout.
