# Dashboard Tests Fonctionnels — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local web dashboard (FastAPI + HTMX + Chart.js) for visualizing, executing, and monitoring Klodo's functional tests.

**Architecture:** Single FastAPI app in `dashboard/` directory. Jinja2 templates with a shared base layout (sidebar + header). HTMX for interactivity (run tests, close issues). Chart.js for graphs. All data read from existing JSON/CSV/YAML files — no database.

**Tech Stack:** Python 3.13, FastAPI, uvicorn, Jinja2, HTMX (CDN), Chart.js (CDN)

**Spec:** `docs/superpowers/specs/2026-04-07-dashboard-design.md`

---

## File Structure

```
dashboard/
├── app.py                 # FastAPI app, routes, data loading
├── data.py                # Data access layer (read JSON/CSV/YAML)
├── templates/
│   ├── base.html          # Layout: sidebar + header + content block
│   ├── overview.html      # KPIs, heatmap, release gate
│   ├── tests.html         # Test list, run buttons, details
│   ├── partials/
│   │   ├── series.html    # Single series row (for HTMX swap)
│   │   └── issue.html     # Issue badge (for HTMX swap)
│   ├── rapports.html      # CSV viewer
│   ├── comparer.html      # Diff view
│   ├── metriques.html     # Quality metrics + charts
│   ├── historique.html    # History charts + timeline
│   └── suggestions.html   # Read-only suggestions viewer
└── static/
    └── style.css          # Dark theme CSS
```

---

### Task 1: Foundation — FastAPI app + base layout + Overview page

**Files:**
- Create: `dashboard/app.py`
- Create: `dashboard/data.py`
- Create: `dashboard/templates/base.html`
- Create: `dashboard/templates/overview.html`
- Create: `dashboard/static/style.css`
- Modify: `pyproject.toml` (add dependencies)
- Modify: `klodo.py` (add `dashboard` subcommand)

This task produces a working dashboard with the Overview page.

- [ ] **Step 1: Add dependencies to pyproject.toml**

Add `fastapi`, `uvicorn`, `jinja2` to dependencies in `pyproject.toml`.

- [ ] **Step 2: Create `dashboard/data.py` — data access layer**

Functions to load all data sources:
- `get_latest_report()` → dict (latest report_*.json)
- `get_all_reports()` → list[dict] (all reports sorted by date)
- `get_tests_yaml()` → dict (tests.yaml content)
- `get_csv_files(type)` → list[str] (available CSV files)
- `load_csv(path)` → list[dict] (parsed CSV)
- `get_suggestions()` → dict (suggestions.yaml)
- `get_open_issues()` → list[dict] (via `gh issue list`)

All functions read from the filesystem — no caching, no DB.

- [ ] **Step 3: Create `dashboard/static/style.css` — dark theme**

Dark theme based on the mockup colors:
- Background: `#0f172a`, sidebar: `#1e293b`, cards: `#1e293b`
- Text: `#e2e8f0`, muted: `#64748b`
- Colors: green `#4ade80`, red `#f87171`, yellow `#fbbf24`, blue `#38bdf8`, purple `#a78bfa`
- Sidebar: 180px fixed, nav items with hover/active states
- Cards with border-left accent colors
- Tables with alternating rows and hover
- Responsive: min-width 1024px

- [ ] **Step 4: Create `dashboard/templates/base.html` — layout**

Jinja2 template with:
- `<head>`: Chart.js CDN, HTMX CDN, style.css
- Sidebar with 7 nav links (Overview, Tests, Rapports, Comparer, Métriques, Historique, Suggestions)
- Active page highlighted
- Content block `{% block content %}{% endblock %}`
- Footer with version + profile info

- [ ] **Step 5: Create `dashboard/templates/overview.html` — Overview page**

Extends base.html. Shows:
- KPI cards: Pass, Fail, Skip, Pass Rate, Duration
- Buttons: Run All, Run Failures
- Phase heatmap: each phase row with colored squares per series
- Release Gate: READY/BLOCKED status + open issues list

- [ ] **Step 6: Create `dashboard/app.py` — FastAPI routes**

FastAPI app with:
- `GET /` → render overview.html with data from `get_latest_report()`
- Static files mount for `/static`
- Jinja2 template configuration

- [ ] **Step 7: Add `dashboard` subcommand to klodo.py**

Add argparse subcommand:
```python
p_dashboard = subparsers.add_parser('dashboard', help='Dashboard tests fonctionnels')
p_dashboard.add_argument('--port', type=int, default=8080, help='Port (défaut: 8080)')
```

In dispatch, start uvicorn:
```python
'dashboard': lambda args, profile: __import__('uvicorn').run(
    'dashboard.app:app', host='127.0.0.1', port=args.port, reload=False)
```

- [ ] **Step 8: Test the dashboard**

```bash
./klodo.sh dashboard
# Open http://localhost:8080
# Verify: sidebar visible, Overview page shows KPIs and heatmap
```

- [ ] **Step 9: Commit**

```bash
git add dashboard/ pyproject.toml klodo.py
git commit -m "feat: add dashboard foundation — FastAPI + Overview page"
```

---

### Task 2: Tests page — list, expand, run

**Files:**
- Create: `dashboard/templates/tests.html`
- Create: `dashboard/templates/partials/series.html`
- Modify: `dashboard/app.py` (add routes)

- [ ] **Step 1: Create `dashboard/templates/tests.html`**

Page with:
- Filter dropdowns (phase, status, priority)
- Phases as accordions, series as rows
- Each series: status dot, ID, name, pass/total, duration, Run/Details/Issue buttons
- FAIL series expanded with check details

- [ ] **Step 2: Create `dashboard/templates/partials/series.html`**

HTMX partial for a single series row — used for swap after Run.

- [ ] **Step 3: Add routes to app.py**

```python
@app.get("/tests")
async def tests_page(request, phase=None, status=None):
    ...

@app.post("/api/run")
async def run_test(series=None, phase=None, all=False, failures=False):
    # subprocess.run(f"uv run python tests/functional/run_functional.py --series {series}", ...)
    # Return HTML partial
    ...

@app.post("/api/issue/close")
async def close_issue(number: int, comment: str = "PASS"):
    # subprocess.run(f"gh issue close {number} --comment '{comment}'", ...)
    ...
```

- [ ] **Step 4: Test**

```bash
./klodo.sh dashboard
# Open http://localhost:8080/tests
# Click "Run" on T0.1 → should execute and show result
# Click "Details" → should expand checks
```

- [ ] **Step 5: Commit**

```bash
git add dashboard/
git commit -m "feat: add Tests page with run/expand/close-issue"
```

---

### Task 3: Rapports page — CSV viewer

**Files:**
- Create: `dashboard/templates/rapports.html`
- Modify: `dashboard/app.py`
- Modify: `dashboard/data.py`

- [ ] **Step 1: Add CSV loading to data.py**

```python
def get_csv_files(report_type: str) -> list[str]:
    """List CSV files by type (classify, rename, refine, process)."""
    patterns = {
        'classify': 'rapport_classify_*.csv',
        'rename': 'rapport_rename_*.csv',
        'refine': 'refine_*.csv',
        'process': 'rapport_process_*.csv',
    }
    ...

def load_csv(path: str) -> tuple[list[str], list[dict]]:
    """Load CSV, return (headers, rows)."""
    ...

def compute_csv_stats(rows: list[dict]) -> dict:
    """Compute stats from CSV rows (total, classified, confidence, etc.)."""
    ...
```

- [ ] **Step 2: Create `dashboard/templates/rapports.html`**

Page with:
- Type selector (classify/rename/refine/process)
- File selector dropdown
- Link to associated test series
- Stats bar (total, classified, confidence, cost)
- Search bar + status/section filters
- Sortable table with pagination
- Colored badges per status

- [ ] **Step 3: Add routes**

```python
@app.get("/rapports")
async def rapports_page(request, type="classify", file=None, search=None, status=None, page=1):
    ...
```

HTMX: filters reload table via `hx-get`.

- [ ] **Step 4: Test**

```bash
./klodo.sh dashboard
# Open http://localhost:8080/rapports
# Select "rename" → table shows rename report
# Search "Einstein" → filtered results
```

- [ ] **Step 5: Commit**

```bash
git add dashboard/
git commit -m "feat: add Rapports page — CSV viewer with filters"
```

---

### Task 4: Comparer page — diff between reports

**Files:**
- Create: `dashboard/templates/comparer.html`
- Modify: `dashboard/app.py`
- Modify: `dashboard/data.py`

- [ ] **Step 1: Add diff logic to data.py**

```python
def diff_reports(csv_a: list[dict], csv_b: list[dict], key: str = 'fichier') -> dict:
    """Compare two CSV reports, return diff stats and changed rows."""
    # Returns: {new: [], regressions: [], changed: [], unchanged: [], stats: {}}
    ...
```

- [ ] **Step 2: Create `dashboard/templates/comparer.html`**

Page with:
- Two dropdowns (Report A, Report B) with associated test links
- Compare button
- Diff stats (new, regressions, changed, delta confidence)
- Filter pills (All/New/Regressions/Changed/Unchanged)
- Diff table with colored rows

- [ ] **Step 3: Add routes**

```python
@app.get("/comparer")
async def comparer_page(request, type="classify", file_a=None, file_b=None, filter=None):
    ...
```

- [ ] **Step 4: Test and commit**

```bash
git add dashboard/
git commit -m "feat: add Comparer page — diff between reports"
```

---

### Task 5: Métriques page — quality charts

**Files:**
- Create: `dashboard/templates/metriques.html`
- Modify: `dashboard/app.py`
- Modify: `dashboard/data.py`

- [ ] **Step 1: Add metrics computation to data.py**

```python
def compute_classification_metrics(csv_rows: list[dict]) -> dict:
    """Compute: rate, confidence, cost, by_section, top_themes."""
    ...

def compute_rename_metrics(csv_rows: list[dict]) -> dict:
    """Compute: rate, clean, failures, llm_used."""
    ...

def find_problematic_files(all_reports: list[dict]) -> list[dict]:
    """Find files that fail across multiple runs."""
    ...
```

- [ ] **Step 2: Create `dashboard/templates/metriques.html`**

Page with:
- Classification section: KPIs + Chart.js donut (by section) + Chart.js bar (top themes)
- Rename section: KPIs
- Problematic files table (file, problem, confidence, recurrence)
- Chart.js scripts inline with data from Jinja2 variables

- [ ] **Step 3: Add routes**

```python
@app.get("/metriques")
async def metriques_page(request, report=None):
    ...
```

- [ ] **Step 4: Test and commit**

```bash
git add dashboard/
git commit -m "feat: add Métriques page — quality charts with Chart.js"
```

---

### Task 6: Historique page — evolution charts

**Files:**
- Create: `dashboard/templates/historique.html`
- Modify: `dashboard/app.py`
- Modify: `dashboard/data.py`

- [ ] **Step 1: Add history data to data.py**

```python
def get_history_data() -> list[dict]:
    """Load all reports, return list of {date, pass, fail, skip, rate, duration}."""
    ...
```

- [ ] **Step 2: Create `dashboard/templates/historique.html`**

Page with:
- Chart.js stacked bar: pass/fail/skip per run
- Chart.js line: pass rate % over time
- Chart.js bar: duration per run
- Timeline table: date, pass, fail, skip, rate, duration, delta, "Voir" link

- [ ] **Step 3: Add routes**

```python
@app.get("/historique")
async def historique_page(request):
    ...
```

- [ ] **Step 4: Test and commit**

```bash
git add dashboard/
git commit -m "feat: add Historique page — evolution charts"
```

---

### Task 7: Suggestions page (read-only)

**Files:**
- Create: `dashboard/templates/suggestions.html`
- Modify: `dashboard/app.py`

- [ ] **Step 1: Create `dashboard/templates/suggestions.html`**

Page with:
- Stats: pending count, files concerned
- List of suggestions: theme, proposed folder, reason, file list (expandable)
- No action buttons — read-only viewer

- [ ] **Step 2: Add route**

```python
@app.get("/suggestions")
async def suggestions_page(request):
    suggestions = data.get_suggestions()
    ...
```

- [ ] **Step 3: Test and commit**

```bash
git add dashboard/
git commit -m "feat: add Suggestions page — read-only viewer"
```

---

### Task 8: Integration — klodo.sh + CLAUDE.md + final polish

**Files:**
- Modify: `klodo.sh` (add dashboard launch)
- Modify: `.claude/CLAUDE.md` (document dashboard)

- [ ] **Step 1: Add dashboard to klodo.sh**

Add to the dispatch section:
```bash
if [ "$1" = "dashboard" ]; then
    shift
    uv run python -m dashboard.app "$@"
    exit $?
fi
```

- [ ] **Step 2: Update CLAUDE.md**

Add dashboard section to the documentation.

- [ ] **Step 3: Full test**

```bash
./klodo.sh dashboard
# Navigate all 7 pages
# Run a test from the Tests page
# Check charts render on Métriques and Historique
# Verify Suggestions shows data (or empty state)
```

- [ ] **Step 4: Final commit**

```bash
git add dashboard/ klodo.sh klodo.py .claude/CLAUDE.md
git commit -m "feat: complete dashboard — 7 pages, run tests, close issues, charts"
```
