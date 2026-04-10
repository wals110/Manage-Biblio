"""Data access layer for the Klodo dashboard."""

import csv
import json
import subprocess
from datetime import datetime
from pathlib import Path

import yaml


def get_project_root() -> Path:
    """Return the project root directory."""
    return Path(__file__).resolve().parent.parent


def get_latest_report() -> dict | None:
    """Return the most recent functional test report, or None."""
    reports = get_all_reports()
    return reports[0] if reports else None


def get_all_reports() -> list[dict]:
    """Return all functional test reports sorted by date descending.

    Supports both new structure (run_*/report.json) and old (report_*.json).
    """
    reports_dir = get_project_root() / "tests" / "functional" / "reports"
    if not reports_dir.exists():
        return []

    results: list[dict] = []

    # New structure: run_*/report.json
    for run_dir in sorted(reports_dir.glob("run_*"), reverse=True):
        report_file = run_dir / "report.json"
        if report_file.exists():
            try:
                report_data = json.loads(report_file.read_text(encoding="utf-8"))
                report_data["_filename"] = run_dir.name
                report_data["_run_dir"] = str(run_dir)
                results.append(report_data)
            except (json.JSONDecodeError, OSError):
                continue

    # Old structure: report_*.json (backward compat)
    for f in sorted(reports_dir.glob("report_*.json"), reverse=True):
        try:
            report_data = json.loads(f.read_text(encoding="utf-8"))
            report_data["_filename"] = f.name
            results.append(report_data)
        except (json.JSONDecodeError, OSError):
            continue

    return results


def get_tests_yaml() -> dict | None:
    """Return parsed tests.yaml, or None if not found."""
    path = get_project_root() / "tests" / "functional" / "tests.yaml"
    if not path.exists():
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return None


def get_llm_config() -> dict | None:
    """Read LLM configuration from profile.yaml."""
    for profile_name in ("test", "default"):
        path = get_project_root() / "profiles" / profile_name / "profile.yaml"
        if path.exists():
            try:
                profile = yaml.safe_load(path.read_text(encoding="utf-8"))
                llm = profile.get("llm", {})
                defaults = profile.get("defaults", {})
                return {
                    "provider": llm.get("provider", ""),
                    "model": llm.get("model", ""),
                    "endpoint": llm.get("endpoint", ""),
                    "cost_per_call": defaults.get("cost_per_call", 0),
                    "profile": profile_name,
                }
            except (yaml.YAMLError, OSError):
                continue
    return None


def get_open_issues() -> list[dict]:
    """Return open GitHub issues with the functional-test label."""
    try:
        result = subprocess.run(
            [
                "gh", "issue", "list",
                "--label", "functional-test",
                "--state", "open",
                "--json", "number,title",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(get_project_root()),
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return []


# ── CSV Report Functions ──


CSV_PATTERNS: dict[str, str] = {
    "classify": "rapport_classify_*.csv",
    "rename": "rapport_rename_*.csv",
    "refine": "refine_*.csv",
    "process": "rapport_process_*.csv",
}


def _parse_csv_date(name: str) -> str:
    """Extract a human-readable date from a CSV filename like rapport_classify_20260407_195746.csv."""
    parts = name.replace(".csv", "").split("_")
    # Find the date part (8 digits) and optional time part (6 digits)
    for i, p in enumerate(parts):
        if len(p) == 8 and p.isdigit():
            try:
                dt = datetime.strptime(p, "%Y%m%d")
                time_str = ""
                if i + 1 < len(parts) and len(parts[i + 1]) == 6 and parts[i + 1].isdigit():
                    time_str = f" {parts[i + 1][:2]}:{parts[i + 1][2:4]}:{parts[i + 1][4:]}"
                return dt.strftime("%Y-%m-%d") + time_str
            except ValueError:
                pass
    return ""


def get_csv_files(report_type: str, tests_yaml: dict | None = None) -> list[dict]:
    """List available CSV files for a given type, sorted newest first.

    Scans both logs/ (live reports) and tests/functional/logs/ (archived by runner).
    Returns list of {name, path, date, size, test_id, test_name}.
    """
    root = get_project_root()
    scan_dirs = [
        root / "tests" / "functional" / "logs",
    ]

    if tests_yaml is None:
        tests_yaml = get_tests_yaml()

    if report_type == "all":
        patterns = list(CSV_PATTERNS.values())
    else:
        p = CSV_PATTERNS.get(report_type)
        if not p:
            return []
        patterns = [p]

    all_files = []
    seen_paths: set[str] = set()
    for scan_dir in scan_dirs:
        if not scan_dir.exists():
            continue
        for pattern in patterns:
            # Use rglob to find CSVs in subdirectories (phase_N/T*.*)
            for f in scan_dir.rglob(pattern):
                fpath = str(f)
                if fpath not in seen_paths:
                    seen_paths.add(fpath)
                    all_files.append(f)

    files = sorted(all_files, key=lambda f: f.name, reverse=True)
    results: list[dict] = []
    for f in files:
        stat = f.stat()
        test = find_test_for_report(f.name, tests_yaml)
        # Derive test_id from parent directory if in structured logs (e.g. phase_2/T2.1/)
        parent_test_id = None
        parent_test_name = None
        if f.parent.name.startswith("T") and not test:
            parent_test_id = f.parent.name
            # Look up series name from tests_yaml
            if tests_yaml:
                for phase in tests_yaml.get("phases", []):
                    for series in phase.get("series", []):
                        if series.get("id") == parent_test_id:
                            parent_test_name = series.get("name", "")
                            break
                    if parent_test_name:
                        break
        results.append({
            "name": f.name,
            "path": str(f),
            "date": _parse_csv_date(f.name),
            "size": stat.st_size,
            "test_id": test["id"] if test else parent_test_id,
            "test_name": test["name"] if test else parent_test_name,
        })
    # Sort by test_id (numeric) then by date desc within each test
    results.sort(key=lambda r: (_sort_test_id(r["test_id"] or ""), r["name"]))
    return results


def load_csv(path: str) -> tuple[list[str], list[dict]]:
    """Load a CSV file. Returns (headers, rows as list of dicts)."""
    try:
        p = Path(path)
        if not p.exists() or not p.suffix == ".csv":
            return [], []
        with p.open(encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                return [], []
            headers = list(reader.fieldnames)
            rows = list(reader)
        return headers, rows
    except (OSError, csv.Error):
        return [], []


def compute_csv_stats(rows: list[dict], report_type: str) -> dict:
    """Compute stats from CSV rows based on report type."""
    total = len(rows)

    if report_type in ("classify", "process"):
        classified = sum(1 for r in rows if r.get("status") == "classifié")
        not_classified = sum(1 for r in rows if r.get("status") == "non_classifié")
        errors = sum(1 for r in rows if (r.get("status") or "").startswith("erreur"))
        confidences = []
        for r in rows:
            try:
                confidences.append(float(r.get("confiance", 0)))
            except (ValueError, TypeError):
                pass
        avg_conf = sum(confidences) / len(confidences) if confidences else 0.0
        return {
            "total": total,
            "classified": classified,
            "not_classified": not_classified,
            "errors": errors,
            "avg_confidence": round(avg_conf, 2),
        }

    if report_type == "rename":
        renamed = sum(1 for r in rows if r.get("action") == "RENOMME")
        unchanged = sum(1 for r in rows if r.get("action") == "INCHANGE")
        errors = sum(1 for r in rows if r.get("action") == "ECHEC")
        return {
            "total": total,
            "renamed": renamed,
            "unchanged": unchanged,
            "errors": errors,
        }

    if report_type == "refine":
        moved = sum(1 for r in rows if r.get("status") == "déplacé")
        not_moved = sum(1 for r in rows if r.get("status") != "déplacé")
        return {
            "total": total,
            "moved": moved,
            "not_moved": not_moved,
        }

    return {"total": total}


def get_csv_statuses(rows: list[dict], report_type: str) -> list[str]:
    """Extract unique status/action values from rows for filter dropdown."""
    if report_type in ("classify", "process", "refine"):
        key = "status"
    elif report_type == "rename":
        key = "action"
    else:
        return []

    values = sorted({r.get(key, "") for r in rows if r.get(key, "")})
    return values


def get_csv_sections(rows: list[dict]) -> list[str]:
    """Extract unique top-level sections from destination column."""
    sections: set[str] = set()
    for r in rows:
        dest = r.get("destination", "")
        if dest:
            top = dest.split("/")[0]
            if top:
                sections.add(top)
    return sorted(sections)


# ── Diff / Compare Functions ──


def diff_reports(rows_a: list[dict], rows_b: list[dict], key_col: str) -> dict:
    """Compare two CSV reports row by row.

    Returns {
        'new': [...],        # in B classified but not in A (or was non_classifié)
        'regressions': [...], # was classified in A, not in B
        'changed': [...],    # same file, different destination
        'unchanged': [...],  # same key, same status and destination
        'stats': {
            'new_count': N, 'regression_count': N, 'changed_count': N,
            'unchanged_count': N, 'delta_confidence': float
        }
    }
    """
    # Index rows by key
    index_a: dict[str, dict] = {}
    for r in rows_a:
        key = r.get(key_col, "")
        if key:
            index_a[key] = r

    index_b: dict[str, dict] = {}
    for r in rows_b:
        key = r.get(key_col, "")
        if key:
            index_b[key] = r

    new: list[dict] = []
    regressions: list[dict] = []
    changed: list[dict] = []
    unchanged: list[dict] = []

    # Helper to check if a row is "classified"
    def _is_classified(row: dict) -> bool:
        status = row.get("status", "")
        action = row.get("action", "")
        return status in ("classifié", "déplacé", "renommé") or action == "RENOMME"

    # Check all keys in B
    all_keys = set(index_a.keys()) | set(index_b.keys())

    conf_a_vals: list[float] = []
    conf_b_vals: list[float] = []

    for key in sorted(all_keys):
        row_a = index_a.get(key)
        row_b = index_b.get(key)

        if row_b and not row_a:
            # File only in B
            new.append({"key": key, "row_a": None, "row_b": row_b})
            continue

        if row_a and not row_b:
            # File only in A — regression (disappeared)
            if _is_classified(row_a):
                regressions.append({"key": key, "row_a": row_a, "row_b": None})
            continue

        # Both exist
        assert row_a is not None and row_b is not None
        a_classified = _is_classified(row_a)
        b_classified = _is_classified(row_b)

        # Collect confidence values
        try:
            conf_a_vals.append(float(row_a.get("confiance", 0)))
        except (ValueError, TypeError):
            pass
        try:
            conf_b_vals.append(float(row_b.get("confiance", 0)))
        except (ValueError, TypeError):
            pass

        if not a_classified and b_classified:
            # Was not classified, now is → new
            new.append({"key": key, "row_a": row_a, "row_b": row_b})
        elif a_classified and not b_classified:
            # Was classified, now isn't → regression
            regressions.append({"key": key, "row_a": row_a, "row_b": row_b})
        elif a_classified and b_classified:
            dest_a = row_a.get("destination", row_a.get("nouveau_nom", ""))
            dest_b = row_b.get("destination", row_b.get("nouveau_nom", ""))
            if dest_a != dest_b:
                changed.append({"key": key, "row_a": row_a, "row_b": row_b})
            else:
                unchanged.append({"key": key, "row_a": row_a, "row_b": row_b})
        else:
            # Both not classified
            unchanged.append({"key": key, "row_a": row_a, "row_b": row_b})

    avg_a = sum(conf_a_vals) / len(conf_a_vals) if conf_a_vals else 0.0
    avg_b = sum(conf_b_vals) / len(conf_b_vals) if conf_b_vals else 0.0

    return {
        "new": new,
        "regressions": regressions,
        "changed": changed,
        "unchanged": unchanged,
        "stats": {
            "new_count": len(new),
            "regression_count": len(regressions),
            "changed_count": len(changed),
            "unchanged_count": len(unchanged),
            "delta_confidence": round(avg_b - avg_a, 2),
        },
    }


def enrich_with_yaml(report: dict, tests_yaml: dict | None) -> dict:
    """Enrich a DuckDB report with setup/pre_run/mode/prompt from tests.yaml.

    Unlike get_merged_test_view, this does NOT re-merge with other reports.
    It preserves the exact statuses from DuckDB.
    """
    if not tests_yaml or not tests_yaml.get("phases"):
        return report

    # Build yaml lookup
    yaml_series: dict[str, dict] = {}
    yaml_checks: dict[str, dict] = {}
    for tp in tests_yaml.get("phases", []):
        for ts in tp.get("series", []):
            sid = ts.get("id", "")
            yaml_series[sid] = {
                "setup": ts.get("setup", []),
                "pre_run": ts.get("pre_run", []),
                "description": ts.get("description", ""),
                "github_issue": ts.get("github_issue"),
            }
            for ck in ts.get("checks", []):
                yaml_checks[ck.get("id", "")] = {
                    "mode": ck.get("mode", "auto"),
                    "prompt": ck.get("assert", {}).get("prompt", ""),
                    "command": ck.get("command", ""),
                }

    # Enrich phases/series/checks
    for ph in report.get("phases", []):
        # Update phase name from yaml
        for tp in tests_yaml.get("phases", []):
            if tp.get("id") == ph.get("id"):
                ph["name"] = tp.get("name", ph.get("name", ""))
                break

        for sr in ph.get("series", []):
            sid = sr.get("id", "")
            ys = yaml_series.get(sid, {})
            for k, v in ys.items():
                if k not in sr or not sr[k]:
                    sr[k] = v

            for ck in sr.get("checks", []):
                yc = yaml_checks.get(ck.get("id", ""), {})
                if "mode" not in ck:
                    ck["mode"] = yc.get("mode", "auto")
                if "prompt" not in ck:
                    ck["prompt"] = yc.get("prompt", "")
                if "command" not in ck or not ck["command"]:
                    ck["command"] = yc.get("command", "")

    return report


def apply_db_statuses(merged: dict, db_report: dict):
    """Override check/series statuses in merged view with DuckDB values.

    This preserves manual validations that were stored in DuckDB
    but would be overwritten by the merge with JSON reports.
    """
    # Build lookup from DuckDB data
    db_checks: dict[str, str] = {}
    db_series: dict[str, str] = {}
    for ph in db_report.get("phases", []):
        for sr in ph.get("series", []):
            db_series[sr.get("id", "")] = sr.get("status", "")
            for ck in sr.get("checks", []):
                db_checks[ck.get("id", "")] = ck.get("status", "")

    # Apply to merged view
    for ph in merged.get("phases", []):
        for sr in ph.get("series", []):
            sid = sr.get("id", "")
            if sid in db_series:
                for ck in sr.get("checks", []):
                    cid = ck.get("id", "")
                    if cid in db_checks:
                        ck["status"] = db_checks[cid]
                # Recalculate series status
                statuses = [ck.get("status", "") for ck in sr.get("checks", [])]
                if all(s == "pass" for s in statuses):
                    sr["status"] = "pass"
                elif any(s == "fail" for s in statuses):
                    sr["status"] = "fail"
                elif any(s == "skip" or s == "not_run" for s in statuses):
                    sr["status"] = "skip"


def _resolve_variables(text: str, variables: dict[str, str]) -> str:
    """Replace ${VAR} placeholders with their values, handling chained refs."""
    # Resolve variables themselves first (e.g. INBOX depends on BIBLIO_TEST)
    resolved = dict(variables)
    for _ in range(5):  # max depth
        changed = False
        for k, v in resolved.items():
            new_v = v
            for vk, vv in resolved.items():
                new_v = new_v.replace(f"${{{vk}}}", vv)
            if new_v != resolved[k]:
                resolved[k] = new_v
                changed = True
        if not changed:
            break
    # Apply to text
    for k, v in resolved.items():
        text = text.replace(f"${{{k}}}", v)
    return text


def _resolve_in_series(series: dict, variables: dict[str, str]):
    """Resolve variables in setup commands and check commands of a series."""
    if not variables:
        return
    if "setup" in series:
        series["setup"] = [_resolve_variables(c, variables) for c in series["setup"]]
    if "pre_run" in series:
        series["pre_run"] = [_resolve_variables(c, variables) for c in series["pre_run"]]
    for check in series.get("checks", []):
        if "command" in check and check["command"]:
            check["command"] = _resolve_variables(check["command"], variables)


def get_merged_test_view(report: dict | None, tests_yaml: dict | None) -> dict | None:
    """Merge tests.yaml definitions with the best result from ALL reports.

    For each series, takes the most recent result across all reports.
    This handles the case where tests are run individually (each run creates
    a separate report with only the tested series).
    """
    if not tests_yaml or not tests_yaml.get("phases"):
        return report

    variables = tests_yaml.get("variables", {})

    # Build lookup: series_id → best series result from ALL reports
    # Priority: pass > fail > skip > not_run
    # Among same priority, take the most recent
    report_series: dict[str, dict] = {}
    report_phases: dict[str, dict] = {}
    all_reports = get_all_reports()
    for rep in all_reports:
        for rp in rep.get("phases", []):
            if rp.get("id", "") not in report_phases:
                report_phases[rp.get("id", "")] = rp
            for sr in rp.get("series", []):
                sid = sr.get("id", "")
                if sid not in report_series:
                    # First time seeing this series — take it
                    report_series[sid] = sr
                else:
                    # Already have a result — keep the better one
                    existing = report_series[sid]
                    priority = {"pass": 3, "fail": 2, "skip": 1, "not_run": 0}
                    if priority.get(sr.get("status"), 0) > priority.get(existing.get("status"), 0):
                        report_series[sid] = sr

    merged_phases = []
    for tp in tests_yaml["phases"]:
        phase_id = tp.get("id", "")
        rp = report_phases.get(phase_id, {})
        merged_series = []
        for ts in tp.get("series", []):
            sid = ts.get("id", "")
            # Enrich with setup/pre_run from tests.yaml
            yaml_info = {
                "setup": ts.get("setup", []),
                "pre_run": ts.get("pre_run", []),
                "description": ts.get("description", ""),
                "github_issue": ts.get("github_issue"),
            }
            # Build check info from yaml (mode, prompt)
            yaml_checks = {}
            for ck in ts.get("checks", []):
                yaml_checks[ck.get("id", "")] = {
                    "mode": ck.get("mode", "auto"),
                    "prompt": ck.get("assert", {}).get("prompt", ""),
                }

            if sid in report_series:
                entry = dict(report_series[sid])
                entry.update({k: v for k, v in yaml_info.items() if k not in entry})
                # Enrich checks with mode/prompt from yaml
                for ck in entry.get("checks", []):
                    yc = yaml_checks.get(ck.get("id", ""), {})
                    if "mode" not in ck:
                        ck["mode"] = yc.get("mode", "auto")
                    if "prompt" not in ck:
                        ck["prompt"] = yc.get("prompt", "")
                _resolve_in_series(entry, variables)
                merged_series.append(entry)
            else:
                # Series not in report — show as not_run
                not_run_entry = {
                    "id": sid,
                    "name": ts.get("name", ""),
                    "status": "not_run",
                    "duration_ms": 0,
                    "setup": list(ts.get("setup", [])),
                    "pre_run": list(ts.get("pre_run", [])),
                    "description": ts.get("description", ""),
                    "github_issue": ts.get("github_issue"),
                    "checks": [
                        {
                            "id": ck.get("id", ""),
                            "description": ck.get("description", ""),
                            "status": "not_run",
                            "command": ck.get("command", ""),
                        }
                        for ck in ts.get("checks", [])
                    ],
                }
                _resolve_in_series(not_run_entry, variables)
                merged_series.append(not_run_entry)
        merged_phases.append({
            "id": phase_id,
            "name": tp.get("name", rp.get("name", "")),
            "series": merged_series,
        })

    # Recompute summary from merged data
    summary = {"total": 0, "pass": 0, "fail": 0, "skip": 0}
    for mp in merged_phases:
        for sr in mp.get("series", []):
            for ck in sr.get("checks", []):
                summary["total"] += 1
                st = ck.get("status", "not_run")
                if st == "pass":
                    summary["pass"] += 1
                elif st == "fail":
                    summary["fail"] += 1
                elif st in ("skip", "not_run"):
                    summary["skip"] += 1

    result = dict(report) if report else {"duration_seconds": 0, "run_at": ""}
    result["summary"] = summary
    result["phases"] = merged_phases
    if report:
        result["run_at"] = report.get("run_at", "")
    return result


def _sort_test_id(test_id: str) -> tuple[int, int]:
    """Sort key for test IDs like T0.1, T1.2, T10.4 — numeric order."""
    import re as _re
    m = _re.match(r'T(\d+)\.(\d+)', test_id)
    if m:
        return int(m.group(1)), int(m.group(2))
    return 999, 999


def get_available_tests(csv_files: list[dict]) -> list[dict]:
    """Extract unique tests from csv_files list, sorted by ID (T0.1, T1.1, ..., T10.4)."""
    seen: dict[str, str] = {}
    for f in csv_files:
        tid = f.get("test_id")
        if tid and tid not in seen:
            seen[tid] = f.get("test_name", "")
    tests = [{"id": tid, "name": name} for tid, name in seen.items()]
    tests.sort(key=lambda t: _sort_test_id(t["id"]))
    return tests


def get_run_csv_files(run_id: str | None = None) -> dict[str, list[str]]:
    """Get CSV report files associated with a specific run.

    Returns: {"classify": [paths], "rename": [paths], "process": [paths], "refine": [paths]}
    """
    root = get_project_root()
    reports_dir = root / "tests" / "functional" / "reports"

    # Find the report.json for this run
    if run_id:
        report_file = reports_dir / run_id / "report.json"
    else:
        latest = reports_dir / "latest" / "report.json"
        report_file = latest if latest.exists() else None

    result: dict[str, list[str]] = {"classify": [], "rename": [], "process": [], "refine": []}
    if not report_file or not report_file.exists():
        return result

    try:
        report = json.loads(report_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return result

    for phase in report.get("phases", []):
        for series in phase.get("series", []):
            for log_path in series.get("logs", []):
                full_path = str(root / log_path)
                if "rapport_classify" in log_path:
                    result["classify"].append(full_path)
                elif "rapport_rename" in log_path:
                    result["rename"].append(full_path)
                elif "rapport_process" in log_path:
                    result["process"].append(full_path)
                elif "refine_" in log_path:
                    result["refine"].append(full_path)

    return result


def _duckdb_query(sql: str) -> list[tuple]:
    """Execute a DuckDB SQL query and return raw rows."""
    import duckdb
    con = duckdb.connect()
    try:
        result = con.execute(sql)
        return result.fetchall()
    except Exception:
        return []
    finally:
        con.close()


def _csv_glob_to_list(paths: list[str]) -> str:
    """Convert a list of CSV paths to a DuckDB read_csv_auto() argument."""
    escaped = [p.replace("'", "''") for p in paths if Path(p).exists()]
    if not escaped:
        return ""
    if len(escaped) == 1:
        return f"'{escaped[0]}'"
    return "[" + ", ".join(f"'{p}'" for p in escaped) + "]"


def compute_aggregated_metrics(csv_paths: list[str], metric_func) -> dict | None:
    """Aggregate metrics from multiple CSV files using DuckDB read_csv_auto()."""
    src = _csv_glob_to_list(csv_paths)
    if not src:
        return None
    return metric_func(src)


def compute_classification_metrics(csv_source: str) -> dict:
    """Compute classification quality metrics using DuckDB.

    csv_source: a single path ('path.csv') or a list (['a.csv', 'b.csv'])
    suitable for read_csv_auto().

    Returns: {total, classified, not_classified, errors, rate, avg_confidence,
              cost, by_section, top_themes, suggestions}
    """
    # Main aggregates in one query
    rows = _duckdb_query(f"""
        SELECT
            COUNT(*) as total,
            COUNT(*) FILTER (WHERE status = 'classifié') as classified,
            COUNT(*) FILTER (WHERE status = 'non_classifié') as not_classified,
            COUNT(*) FILTER (WHERE status LIKE 'erreur%') as errors,
            COUNT(*) FILTER (WHERE status = 'suggestion') as suggestions,
            COUNT(*) FILTER (WHERE mot_cle LIKE 'llm%' OR mot_cle LIKE 'LLM%') as llm_calls,
            AVG(CASE WHEN TRY_CAST(confiance AS DOUBLE) > 0
                 THEN TRY_CAST(confiance AS DOUBLE) END) as avg_conf
        FROM read_csv_auto({csv_source}, header=true, ignore_errors=true)
    """)

    if not rows or not rows[0] or rows[0][0] == 0:
        return {
            "total": 0, "classified": 0, "not_classified": 0, "errors": 0,
            "rate": 0.0, "avg_confidence": 0.0, "cost": 0.0,
            "by_section": {}, "top_themes": [], "suggestions": 0,
        }

    total, classified, not_classified, errors, suggestions, llm_calls, avg_conf = rows[0]
    rate = round(classified / total * 100, 1) if total else 0.0
    avg_confidence = round(avg_conf or 0.0, 2)
    cost = round((llm_calls or 0) * 0.0003, 4)

    # By section
    section_rows = _duckdb_query(f"""
        SELECT split_part(destination, '/', 1) as section, COUNT(*) as n
        FROM read_csv_auto({csv_source}, header=true, ignore_errors=true)
        WHERE destination IS NOT NULL AND destination != ''
        GROUP BY 1 ORDER BY 1
    """)
    by_section = {r[0]: r[1] for r in section_rows if r[0]}

    # Top themes
    theme_rows = _duckdb_query(f"""
        SELECT theme_detecte, COUNT(*) as n
        FROM read_csv_auto({csv_source}, header=true, ignore_errors=true)
        WHERE theme_detecte IS NOT NULL AND trim(theme_detecte) != ''
        GROUP BY 1 ORDER BY 2 DESC LIMIT 10
    """)
    top_themes = [(r[0], r[1]) for r in theme_rows]

    return {
        "total": total,
        "classified": classified,
        "not_classified": not_classified,
        "errors": errors,
        "suggestions": suggestions,
        "rate": rate,
        "avg_confidence": avg_confidence,
        "cost": cost,
        "by_section": by_section,
        "top_themes": top_themes,
    }


def compute_rename_metrics(csv_source: str) -> dict:
    """Compute rename quality metrics using DuckDB.

    csv_source: a path or list suitable for read_csv_auto().
    Returns: {total, renamed, unchanged, errors, llm_used, rate}
    """
    rows = _duckdb_query(f"""
        SELECT
            COUNT(*) as total,
            COUNT(*) FILTER (WHERE action = 'RENOMME') as renamed,
            COUNT(*) FILTER (WHERE action = 'INCHANGE') as unchanged,
            COUNT(*) FILTER (WHERE action = 'ECHEC') as errors,
            COUNT(*) FILTER (WHERE lower(source) IN ('llm', 'llm_vision', 'vision')) as llm_used
        FROM read_csv_auto({csv_source}, header=true, ignore_errors=true)
    """)

    if not rows or not rows[0] or rows[0][0] == 0:
        return {"total": 0, "renamed": 0, "unchanged": 0, "errors": 0, "llm_used": 0, "rate": 0.0}

    total, renamed, unchanged, errors, llm_used = rows[0]
    rate = round(renamed / total * 100, 1) if total else 0.0

    return {
        "total": total,
        "renamed": renamed,
        "unchanged": unchanged,
        "errors": errors,
        "llm_used": llm_used,
        "rate": rate,
    }


def find_problematic_files(all_reports: list[dict]) -> list[dict]:
    """Find files that fail/skip across multiple test runs.

    Returns: [{name, problem, confidence, recurrence: "N/M runs"}]
    """
    if not all_reports:
        return []

    # Track failures per file across reports
    file_issues: dict[str, list[dict]] = {}
    total_runs = len(all_reports)

    for report in all_reports:
        for phase in report.get("phases", []):
            for series in phase.get("series", []):
                for check in series.get("checks", []):
                    status = check.get("status", "")
                    if status in ("fail", "skip"):
                        name = check.get("description", check.get("id", ""))
                        if name not in file_issues:
                            file_issues[name] = []
                        file_issues[name].append({
                            "status": status,
                            "error": check.get("error", ""),
                            "series": series.get("name", ""),
                        })

    # Build result — only files failing in more than one report
    results: list[dict] = []
    for name, issues in sorted(file_issues.items(), key=lambda x: len(x[1]), reverse=True):
        count = len(issues)
        if count < 1:
            continue
        latest = issues[0]
        results.append({
            "name": name,
            "problem": latest.get("error", latest.get("status", "")).strip()[:120],
            "series": latest.get("series", ""),
            "recurrence": f"{count}/{total_runs}",
        })

    return results[:30]


def get_history_data() -> list[dict]:
    """Load history from DuckDB (primary) + report_*.json (fallback).

    Returns list of {date, date_short, pass, fail, skip, total, rate, duration, filename, run_type}
    sorted by date ascending (oldest first for charts).
    """
    seen_dates: set[str] = set()
    results: list[dict] = []

    def _parse_date(run_at: str) -> tuple[str, str]:
        try:
            dt = datetime.fromisoformat(run_at)
            return dt.strftime("%Y-%m-%d %H:%M"), dt.strftime("%m/%d %H:%M")
        except (ValueError, TypeError):
            return run_at, run_at

    # 1. Load from DuckDB (primary source)
    db_path = get_project_root() / "tests" / "functional" / "results.db"
    if db_path.exists():
        try:
            import duckdb
            con = duckdb.connect(str(db_path), read_only=True)
            rows = con.execute("""
                SELECT id, date, duration, total, pass, fail, skip, rate, run_type
                FROM runs ORDER BY date ASC
            """).fetchall()
            con.close()
            for row in rows:
                date_str, date_short = _parse_date(row[1])
                if date_str in seen_dates:
                    continue
                seen_dates.add(date_str)
                results.append({
                    "date": date_str,
                    "date_short": date_short,
                    "pass": row[4],
                    "fail": row[5],
                    "skip": row[6],
                    "total": row[3],
                    "rate": row[7],
                    "duration": row[2],
                    "filename": row[0],
                    "run_type": row[8] or "unknown",
                })
        except Exception:
            pass

    # 2. Load report_*.json (recent, may not be in history.json yet)
    for report in get_all_reports():
        summary = report.get("summary", {})
        run_at = report.get("run_at", "")
        date_str, date_short = _parse_date(run_at)
        if date_str in seen_dates:
            continue
        seen_dates.add(date_str)
        total = summary.get("total", 0)
        pass_count = summary.get("pass", 0)
        results.append({
            "date": date_str,
            "date_short": date_short,
            "pass": pass_count,
            "fail": summary.get("fail", 0),
            "skip": summary.get("skip", 0),
            "total": total,
            "rate": round(pass_count / total * 100, 1) if total > 0 else 0.0,
            "duration": report.get("duration_seconds", 0),
            "filename": report.get("_filename", ""),
            "run_type": "unknown",
        })

    # Sort oldest first for charts
    results.sort(key=lambda r: r["date"])
    return results


def get_series_logs(phase_id: str, series_id: str) -> list[dict]:
    """Get log files for a specific series from the structured logs directory.

    Returns list of {name, path, type} where type is 'csv', 'setup_output', 'bench'.
    """
    series_dir = get_project_root() / "tests" / "functional" / "logs" / phase_id / series_id
    if not series_dir.exists():
        return []

    results: list[dict] = []
    for f in sorted(series_dir.iterdir()):
        if f.is_file():
            ftype = "setup_output" if f.name.startswith(".setup") else "bench" if f.name.startswith(".bench") else "csv"
            results.append({
                "name": f.name,
                "path": str(f),
                "type": ftype,
                "size": f.stat().st_size,
            })
    return results


def get_all_test_logs() -> dict[str, dict[str, list[dict]]]:
    """Get all structured test logs organized by phase/series.

    Returns {phase_id: {series_id: [log files]}}
    """
    logs_root = get_project_root() / "tests" / "functional" / "logs"
    if not logs_root.exists():
        return {}

    result: dict[str, dict[str, list[dict]]] = {}
    for phase_dir in sorted(logs_root.iterdir()):
        if phase_dir.is_dir() and phase_dir.name.startswith("phase_"):
            phase_logs: dict[str, list[dict]] = {}
            for series_dir in sorted(phase_dir.iterdir()):
                if series_dir.is_dir():
                    phase_logs[series_dir.name] = get_series_logs(phase_dir.name, series_dir.name)
            if phase_logs:
                result[phase_dir.name] = phase_logs
    return result


def get_run_from_db(run_id: str | None = None) -> dict | None:
    """Load a specific run from DuckDB, or the latest if run_id is None.

    Returns a report-like dict with phases/series/checks.
    """
    import sys
    func_dir = str(get_project_root() / "tests" / "functional")
    if func_dir not in sys.path:
        sys.path.insert(0, func_dir)
    try:
        from db import get_run_detail, get_runs
        if run_id is None:
            runs = get_runs(limit=1)
            if not runs:
                return None
            run_id = runs[0]["id"]
        return get_run_detail(run_id)
    except Exception:
        return None


def get_available_runs() -> list[dict]:
    """Get list of available runs from DuckDB for the run selector."""
    import sys
    func_dir = str(get_project_root() / "tests" / "functional")
    if func_dir not in sys.path:
        sys.path.insert(0, func_dir)
    try:
        from db import get_runs
        return get_runs(limit=50)
    except Exception:
        return []


def get_suggestions() -> list[dict]:
    """Return suggestions from logs/suggestions.yaml, or empty list."""
    path = get_project_root() / "logs" / "suggestions.yaml"
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        return data
    except (yaml.YAMLError, OSError):
        return []


def find_test_for_report(csv_filename: str, tests_yaml: dict | None) -> dict | None:
    """Find which test series produced a given CSV report.

    Checks logs/.setup_output_<series>.txt files for the report filename.
    Returns {'id': 'T2.2', 'name': 'Classify --execute'} or None.
    """
    if not tests_yaml or not csv_filename:
        return None

    logs_dir = get_project_root() / "logs"

    # Strategy 1: Check setup output files for the CSV filename
    for setup_file in logs_dir.glob(".setup_output_*.txt"):
        try:
            content = setup_file.read_text(encoding="utf-8", errors="replace")
            if csv_filename in content:
                # Extract series ID from filename: .setup_output_T2.2.txt
                series_id = setup_file.stem.replace(".setup_output_", "")
                # Look up the series name in tests.yaml
                for phase in tests_yaml.get("phases", []):
                    for series in phase.get("series", []):
                        if series.get("id") == series_id:
                            return {
                                "id": series_id,
                                "name": series.get("name", ""),
                            }
                # Found in setup output but series not in yaml
                return {"id": series_id, "name": ""}
        except OSError:
            continue

    # Strategy 2: Check report_*.json for series with matching logs
    reports_dir = get_project_root() / "tests" / "functional" / "reports"
    if reports_dir.exists():
        for report_file in sorted(reports_dir.glob("report_*.json"), reverse=True):
            try:
                report_data = json.loads(report_file.read_text(encoding="utf-8"))
                for rp in report_data.get("phases", []):
                    for s in rp.get("series", []):
                        series_logs = s.get("logs", [])
                        for log_path in series_logs:
                            if csv_filename in log_path or Path(log_path).name == csv_filename:
                                return {
                                    "id": s.get("id", ""),
                                    "name": s.get("name", ""),
                                }
            except (json.JSONDecodeError, OSError):
                continue

    return None


def compare_runs(
    current: dict, old: dict, series_filter: str | None = None
) -> dict:
    """Compare two runs check-by-check.

    Returns {
        series: [{id, name, checks: [{id, desc, old_status, new_status, change}]}],
        stats: {improved, regressed, unchanged, new, removed}
    }
    """
    # Build check lookup: check_id → status for each run
    def _build_check_map(run: dict) -> dict[str, dict]:
        result = {}
        for phase in run.get("phases", []):
            for series in phase.get("series", []):
                for check in series.get("checks", []):
                    result[check["id"]] = {
                        "status": check.get("status", "not_run"),
                        "description": check.get("description", ""),
                        "series_id": series["id"],
                        "series_name": series.get("name", ""),
                    }
        return result

    old_checks = _build_check_map(old)
    new_checks = _build_check_map(current)

    all_check_ids = sorted(set(old_checks) | set(new_checks))

    # Group by series
    series_map: dict[str, dict] = {}
    stats = {"improved": 0, "regressed": 0, "unchanged": 0, "new": 0, "removed": 0}

    for cid in all_check_ids:
        old_ck = old_checks.get(cid)
        new_ck = new_checks.get(cid)
        sid = (new_ck or old_ck)["series_id"]
        sname = (new_ck or old_ck)["series_name"]

        if series_filter and sid != series_filter:
            continue

        if sid not in series_map:
            series_map[sid] = {"id": sid, "name": sname, "checks": []}

        old_st = old_ck["status"] if old_ck else None
        new_st = new_ck["status"] if new_ck else None

        # Determine change type
        priority = {"pass": 3, "fail": 2, "skip": 1, "not_run": 0}
        if old_st is None:
            change = "new"
            stats["new"] += 1
        elif new_st is None:
            change = "removed"
            stats["removed"] += 1
        elif old_st == new_st:
            change = "unchanged"
            stats["unchanged"] += 1
        elif priority.get(new_st, 0) > priority.get(old_st, 0):
            change = "improved"
            stats["improved"] += 1
        else:
            change = "regressed"
            stats["regressed"] += 1

        series_map[sid]["checks"].append({
            "id": cid,
            "description": (new_ck or old_ck)["description"],
            "old_status": old_st,
            "new_status": new_st,
            "change": change,
        })

    return {
        "series": sorted(series_map.values(), key=lambda s: s["id"]),
        "stats": stats,
    }
