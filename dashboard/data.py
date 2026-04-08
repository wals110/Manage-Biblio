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

    Returns list of {name, path, date, size, test_id, test_name}.
    """
    logs_dir = get_project_root() / "logs"
    if not logs_dir.exists():
        return []

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
    for pattern in patterns:
        all_files.extend(logs_dir.glob(pattern))
    files = sorted(all_files, reverse=True)
    results: list[dict] = []
    for f in files:
        stat = f.stat()
        test = find_test_for_report(f.name, tests_yaml)
        results.append({
            "name": f.name,
            "path": str(f),
            "date": _parse_csv_date(f.name),
            "size": stat.st_size,
            "test_id": test["id"] if test else None,
            "test_name": test["name"] if test else None,
        })
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
        errors = sum(1 for r in rows if r.get("status") == "erreur")
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


def get_merged_test_view(report: dict | None, tests_yaml: dict | None) -> dict | None:
    """Merge tests.yaml definitions with the best result from ALL reports.

    For each series, takes the most recent result across all reports.
    This handles the case where tests are run individually (each run creates
    a separate report with only the tested series).
    """
    if not tests_yaml or not tests_yaml.get("phases"):
        return report

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
            if sid in report_series:
                entry = dict(report_series[sid])
                entry.update({k: v for k, v in yaml_info.items() if k not in entry})
                merged_series.append(entry)
            else:
                # Series not in report — show as not_run
                merged_series.append({
                    "id": sid,
                    "name": ts.get("name", ""),
                    "status": "not_run",
                    "duration_ms": 0,
                    "setup": ts.get("setup", []),
                    "pre_run": ts.get("pre_run", []),
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
                })
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


def compute_classification_metrics(csv_path: str) -> dict:
    """Compute classification quality metrics from a classify CSV.

    Returns: {total, classified, not_classified, errors, rate, avg_confidence,
              cost, by_section: {section: count}, top_themes: [(theme, count)]}
    """
    _, rows = load_csv(csv_path)
    total = len(rows)
    if total == 0:
        return {
            "total": 0, "classified": 0, "not_classified": 0, "errors": 0,
            "rate": 0.0, "avg_confidence": 0.0, "cost": 0.0,
            "by_section": {}, "top_themes": [], "suggestions": 0,
        }

    classified = sum(1 for r in rows if r.get("status") == "classifié")
    not_classified = sum(1 for r in rows if r.get("status") == "non_classifié")
    errors = sum(1 for r in rows if r.get("status") == "erreur")
    suggestions = sum(1 for r in rows if r.get("status") == "suggestion")
    rate = round(classified / total * 100, 1) if total else 0.0

    confidences: list[float] = []
    for r in rows:
        try:
            val = float(r.get("confiance", 0))
            if val > 0:
                confidences.append(val)
        except (ValueError, TypeError):
            pass
    avg_confidence = round(sum(confidences) / len(confidences), 2) if confidences else 0.0

    # Estimate API cost: ~$0.0003 per LLM call (SiliconFlow pricing)
    llm_calls = sum(1 for r in rows if r.get("mot_cle", "").startswith("llm"))
    cost = round(llm_calls * 0.0003, 4)

    # By section (top-level folder)
    by_section: dict[str, int] = {}
    for r in rows:
        dest = r.get("destination", "")
        if dest:
            section = dest.split("/")[0]
            if section:
                by_section[section] = by_section.get(section, 0) + 1

    # Top themes
    theme_counts: dict[str, int] = {}
    for r in rows:
        theme = r.get("theme_detecte", "").strip()
        if theme:
            theme_counts[theme] = theme_counts.get(theme, 0) + 1
    top_themes = sorted(theme_counts.items(), key=lambda x: x[1], reverse=True)[:10]

    return {
        "total": total,
        "classified": classified,
        "not_classified": not_classified,
        "errors": errors,
        "suggestions": suggestions,
        "rate": rate,
        "avg_confidence": avg_confidence,
        "cost": cost,
        "by_section": dict(sorted(by_section.items())),
        "top_themes": top_themes,
    }


def compute_rename_metrics(csv_path: str) -> dict:
    """Compute rename quality metrics from a rename CSV.

    Returns: {total, renamed, unchanged, errors, llm_used, rate}
    """
    _, rows = load_csv(csv_path)
    total = len(rows)
    if total == 0:
        return {"total": 0, "renamed": 0, "unchanged": 0, "errors": 0, "llm_used": 0, "rate": 0.0}

    renamed = sum(1 for r in rows if r.get("action") == "RENOMME")
    unchanged = sum(1 for r in rows if r.get("action") == "INCHANGE")
    errors = sum(1 for r in rows if r.get("action") == "ECHEC")
    llm_used = sum(1 for r in rows if r.get("source", "").lower() in ("llm", "llm_vision", "vision"))
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
