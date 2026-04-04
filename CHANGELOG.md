# Changelog

All notable changes to Klodo are documented in this file.

## [1.0.0-dev] — 2026-04-05

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
