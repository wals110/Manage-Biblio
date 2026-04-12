# Changelog

All notable changes to Klodo are documented in this file.

## [1.0.0-dev] — 2026-04-10

### Code review refactoring (17 steps)

All 17 improvement steps from the code review session have been applied:

1. Python 3.13 type hints across 20 files
2. `lib/constants.py` — centralized constants
3. Removed `organiser/` → `lib/keyword_classifier.py`
4. Removed `renommage/` → `lib/renamer.py`
5. `lib/exceptions.py` — custom exception hierarchy
6. Cleaned up unused imports (ruff F401)
7. Full ruff lint pass (F401, F841, E741, E402)
8. `sanitize_for_prompt()` — LLM prompt injection protection
9. CHANGELOG.md added
10. Functional test plan + `scripts/flatten_to_inbox.sh`
11. Step 12 (externalize GENERIC_TITLES) — skipped (not useful)
12. Step 15 (TF-IDF persistence) — skipped (LLM Mapper is superior)

### Dashboard (tests fonctionnels)
- FastAPI + Jinja2 + HTMX + Chart.js dashboard (8 pages, dark theme)
- DuckDB backend for test result historization (runs, series, checks, manual validations)
- Random run naming (adjective-noun: "cosmic-koala") with git branch/commit tracking
- Real-time test monitoring via stdout pipe + background thread
- Manual validation widgets (Oui/Non + revise) with DuckDB persistence
- Run comparison page (check-by-check diff between two runs)
- Metrics page linked to runs, using DuckDB read_csv_auto() for SQL analytics
- Admin page: API key management (.env), cleanup, system stats, runs CRUD
- Profile selector (Cloud/Local) with command text live update
- Variable resolution in displayed commands (${VAR} replaced with values)

### Ollama Local LLM
- Add test-local profile (Ollama qwen3-vl:32b, cost $0)
- LLM_TIMEOUT 30s to 120s, LLM_VISION_MAX_TOKENS 300 to 800 for thinking models
- .env auto-loading in klodo.sh (without overwriting existing vars) and runner

### Wordcheck (filename validation)
- Add lib/wordcheck.py: dictionary validation using pyspellchecker (EN+FR) + technical whitelist
- Integrated into is_name_clean() and _is_good_title() in renamer.py
- Rejects gibberish filenames like 'fh&itei.pdf (0% real words) while keeping 'Algorithms.pdf'

### Functional Testing
- Skill 1 (functional-test-plan): add UPDATE mode (incremental, preserves existing IDs)
- Skill 2 (functional-test-runner): REMOVED — runner is maintained code, not generated
- Runner loads .env at startup for API key availability
- Dashboard auto-includes phase 0 when running single phase (dependency fix)
- T0.4a checks correct LLM provider (SiliconFlow vs Ollama)
- Skip reasons show full detail ("Unmet requires: api_ready")

### Documentation
- 10 new SVG diagrams (architecture, runner flow, monitoring, data flow, etc.)
- Full documentation: docs/functional-testing-and-dashboard.md
- Steel-man analysis skill created

### Testing
- 281 total tests (unit + integration + 33 dashboard + 35 wordcheck)
- 123 functional checks (11 phases, 45 series)

---

## [1.0.0-dev initial] — 2026-04-05

### Cache & Storage
- Move `progress.json` and `isbn_cache.json` from `logs/` to `profiles/<name>/.cache/`
- Each profile now has its own isolated cache directory
- `logs/` now only contains horodated rapport CSV files

### Features
- Add `clean` subcommand (`./klodo.sh clean progress|isbn|logs|all --execute`)
- Add `scripts/clean_logs.sh` for standalone CSV cleanup

### Code Quality
- Modernize all type hints for Python 3.13 (`X | None`, `list[str]`, `dict[str, int]`)
- Centralize magic numbers into `lib/constants.py`
- Add custom exception hierarchy: `KlodoError` → `ConfigError`, `LLMError`, `ClassificationError`, `SafetyError`
- Fix all ruff lint warnings (F401, F841, E741, E402)
- Remove unused imports across the codebase

### Architecture
- Extract shared LLM client (`lib/llm_client.py`) with connection pooling (`requests.Session`)
- Split monolithic `klodo.py` into `commands/` sub-modules (1225 → 234 lines)
- Move `klodo_organizer.py` → `lib/keyword_classifier.py` (direct import, no `sys.path` hack)
- Move `klodo_renamer.py` → `lib/renamer.py` (direct import, no `sys.path` hack)
- Delete legacy `organiser/` and `renommage/` directories
- Unify version number — single source in `lib/__init__.py`

### Features
- Rename project from "biblio" to **Klodo**
- Add Refiner v2 — recursive 3-level scan with LLM fallback (`--llm`)
- Add LLM Vision escalation for classifier (`--vision`)
- Add LLM Vision multi-page support for renamer (`--pages N`)
- Add progress bar for LLM pass 2

### Security
- Remove `--api-key` from CLI (use environment variable only)
- Separate personal profiles from public template (`.gitignore`)

### Testing
- Add 22 tests for LLM client (retry, backoff, 429, timeout)
- Add integration tests (classify → copy → fallback)
- 191 total tests (unit + integration)

### Infrastructure
- Migrate to `uv` + Python 3.13, drop pip/conda
- Add GitHub Actions CI workflow
- Add ruff linter configuration in `pyproject.toml`

## [0.4.0] — 2026-04-01

### Features
- Unified CLI with full pipeline: rename → classify → copy → refine
- LLM Mapper with auto-learning and folder suggestions
- 4-level classification cascade: theme mapping → keywords → LLM mapper → suggestions
- Professional documentation with logo and community README

## [0.2.0] — 2026-03-30

### Features
- Phase 2: thematic classification of 18,500+ PDFs
- Keyword-based classifier with TF-IDF scoring
- YAML-driven category and theme mapping

## [0.1.0] — 2026-03-29

### Features
- Initial release — PDF Renamer v2.0
- ISBN-based metadata lookup
- PDF text extraction for title detection
- Mermaid flowchart documentation of renaming pipeline
