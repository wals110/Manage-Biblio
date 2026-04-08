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
    """Return all functional test reports sorted by date descending."""
    reports_dir = get_project_root() / "tests" / "functional" / "reports"
    if not reports_dir.exists():
        return []

    report_files = sorted(reports_dir.glob("report_*.json"), reverse=True)
    results: list[dict] = []
    for f in report_files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            data["_filename"] = f.name
            results.append(data)
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

    result = dict(report) if report else {"duration_seconds": 0}
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
