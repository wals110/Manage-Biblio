"""Klodo Dashboard — FastAPI application."""

import os
import re
import subprocess
import threading

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from dashboard import data

app = FastAPI(title="Klodo Dashboard")
app.mount("/static", StaticFiles(directory="dashboard/static"), name="static")
templates = Jinja2Templates(directory="dashboard/templates")


@app.get("/")
async def overview(request: Request):
    """Overview page with KPIs, heatmap, and release gate."""
    report = data.get_latest_report()
    tests = data.get_tests_yaml()
    issues = data.get_open_issues()
    return templates.TemplateResponse(
        request,
        "overview.html",
        {"report": report, "tests": tests, "issues": issues,
         "api_key_set": bool(os.environ.get("SILICONFLOW_API_KEY"))},
    )


@app.get("/tests")
async def tests_page(
    request: Request,
    phase: str | None = None,
    status: str | None = None,
    run: str | None = None,
):
    """Tests page — list, expand, run tests."""
    phase_int = int(phase) if phase and phase.isdigit() else None
    status_val = status if status else None
    tests_yaml = data.get_tests_yaml()
    available_runs = data.get_available_runs()

    # Load specific run from DuckDB, or latest
    db_report = data.get_run_from_db(run)
    if db_report:
        # Enrich with setup/description from tests.yaml
        report = data.get_merged_test_view(db_report, tests_yaml)
    else:
        # Fallback: read from JSON files
        report = data.get_merged_test_view(data.get_latest_report(), tests_yaml)

    return templates.TemplateResponse(
        request,
        "tests.html",
        {
            "report": report,
            "tests": tests_yaml,
            "phase": phase_int,
            "status": status_val,
            "active": "tests",
            "api_key_set": bool(os.environ.get("SILICONFLOW_API_KEY")),
            "available_runs": available_runs,
            "selected_run": run or "",
            "current_run_id": report.get("_run_id", run or "") if report else "",
        },
    )


# Track running process and current series
_running_process: subprocess.Popen | None = None
_current_series: str | None = None
_run_output_lines: list[str] = []


_completed_series: dict[str, str] = {}  # series_id → status (pass/fail/skip)
_completed_checks: dict[str, str] = {}  # check_id → status (pass/fail/skip)


def _monitor_output():
    """Read subprocess stdout in background to track current series and checks."""
    global _current_series
    if _running_process and _running_process.stdout:
        while True:
            line = _running_process.stdout.readline()
            if not line:
                break
            _run_output_lines.append(line)
            # Detect series start: T0.1 (not T0.1a)
            m = re.search(r'(T\d+\.\d+)', line)
            if m:
                sid = m.group(1)
                if not re.search(r'T\d+\.\d+[a-z]', line):
                    _current_series = sid
                    print(f"[monitor] Current series: {sid}")
            # Detect check result: "[T0.1a] ... ✓ PASS" or "✗ FAIL" or "⊘ SKIP"
            cm = re.search(r'\[(T\d+\.\d+[a-z])\].*?(PASS|FAIL|SKIP)', line, re.IGNORECASE)
            if cm:
                cid = cm.group(1)
                status = cm.group(2).lower()
                _completed_checks[cid] = status
            # Detect series result: "└─ T0.1: ✓ PASS"
            if '└' in line:
                m2 = re.search(r'(T\d+\.\d+).*?(PASS|FAIL|SKIP)', line, re.IGNORECASE)
                if m2:
                    sid = m2.group(1)
                    status = m2.group(2).lower()
                    _completed_series[sid] = status
                    print(f"[monitor] Completed: {sid} → {status}")


@app.get("/api/status")
async def run_status():
    """Check if a test is currently running, and which one."""
    from fastapi.responses import JSONResponse
    running = _running_process is not None and _running_process.poll() is None
    return JSONResponse({
        "running": running,
        "current_series": _current_series if running else None,
        "completed": dict(_completed_series) if running else {},
        "checks": dict(_completed_checks) if running else {},
    })


@app.post("/api/stop")
async def stop_test():
    """Stop the currently running test process."""
    global _running_process
    if _running_process and _running_process.poll() is None:
        _running_process.terminate()
        try:
            _running_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _running_process.kill()
        _running_process = None
        return {"status": "stopped"}
    return {"status": "not_running"}


@app.post("/api/run")
async def run_test(
    request: Request,
    series: str | None = None,
    phase: int | None = None,
    all: bool = False,
    failures: bool = False,
    no_history: bool = False,
):
    """Run functional tests and return updated HTML."""
    global _running_process

    cmd = ["uv", "run", "python", "tests/functional/run_functional.py"]
    if no_history:
        cmd.append("--no-history")
    if series:
        # Support comma-separated series: "T0.1,T0.2,T1.1"
        series_list = [s.strip() for s in series.split(",") if s.strip()]
        cmd += ["--series"] + series_list
        print(f"[dashboard] Running series: {series_list}")
    elif phase is not None:
        cmd += ["--phase", str(phase)]
    elif failures:
        cmd.append("--rerun-failures")
    # else: run all (no extra args)

    global _current_series
    _current_series = None
    _run_output_lines.clear()
    _completed_series.clear()
    _completed_checks.clear()

    print(f"[dashboard] CMD: {' '.join(cmd)}")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    _running_process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(data.get_project_root()),
        env=env,
    )

    # Start background thread to monitor stdout for current series
    monitor_thread = threading.Thread(target=_monitor_output, daemon=True)
    monitor_thread.start()

    # Wait in a non-blocking loop so other requests (like /api/stop) can be processed
    import asyncio
    while _running_process and _running_process.poll() is None:
        await asyncio.sleep(0.5)
    _running_process = None
    _current_series = None

    # Reload data and return updated HTML
    report = data.get_latest_report()
    tests_yaml = data.get_tests_yaml()
    merged = data.get_merged_test_view(report, tests_yaml)

    if series and merged:
        # Return just the updated series partial
        for rp in merged.get("phases", []):
            for s in rp.get("series", []):
                if s["id"] == series:
                    return templates.TemplateResponse(
                        request,
                        "partials/series.html",
                        {"series": s, "tests": tests_yaml},
                    )

    # Return simple JSON — the client JS will reload the page
    from fastapi.responses import JSONResponse
    return JSONResponse({"status": "done"})


@app.get("/rapports")
async def rapports_page(
    request: Request,
    type: str = "classify",
    file: str | None = None,
    test: str | None = None,
    search: str | None = None,
    status: str | None = None,
    section: str | None = None,
    sort: str | None = None,
    order: str = "asc",
    page: int = 1,
):
    """Rapports page — CSV viewer with filters, search, pagination."""
    tests_yaml = data.get_tests_yaml()
    csv_files = data.get_csv_files(type, tests_yaml)
    available_tests = data.get_available_tests(csv_files)

    # Filter by test if specified
    if test:
        csv_files_filtered = [f for f in csv_files if f.get("test_id") == test]
    else:
        csv_files_filtered = csv_files

    # Use selected file or most recent from filtered list
    selected_file = file or (csv_files_filtered[0]["path"] if csv_files_filtered else None)

    headers: list[str] = []
    rows: list[dict] = []
    stats: dict = {}
    statuses: list[str] = []
    sections: list[str] = []
    total_pages = 1
    total_rows = 0

    associated_test = None

    if selected_file:
        headers, rows = data.load_csv(selected_file)
        stats = data.compute_csv_stats(rows, type)
        statuses = data.get_csv_statuses(rows, type)
        sections = data.get_csv_sections(rows)

        # Find associated test series
        tests_yaml = data.get_tests_yaml()
        associated_test = data.find_test_for_report(
            os.path.basename(selected_file), tests_yaml
        )

        # Apply filters
        if search:
            rows = [r for r in rows if search.lower() in str(r).lower()]
        if status:
            status_key = "action" if type == "rename" else "status"
            rows = [r for r in rows if r.get(status_key, "") == status]
        if section:
            rows = [r for r in rows if r.get("destination", "").startswith(section)]

        # Sort
        if sort and sort in headers:
            reverse = order == "desc"
            rows.sort(key=lambda r: r.get(sort, ""), reverse=reverse)

        # Paginate
        per_page = 50
        total_rows = len(rows)
        total_pages = max(1, (total_rows + per_page - 1) // per_page)
        page = max(1, min(page, total_pages))
        rows = rows[(page - 1) * per_page : page * per_page]

    return templates.TemplateResponse(
        request,
        "rapports.html",
        {
            "active": "rapports",
            "report_type": type,
            "csv_files": csv_files,
            "available_tests": available_tests,
            "test_filter": test or "",
            "selected_file": selected_file or "",
            "headers": headers,
            "rows": rows,
            "stats": stats,
            "statuses": statuses,
            "sections": sections,
            "search": search or "",
            "status_filter": status or "",
            "section_filter": section or "",
            "sort": sort or "",
            "order": order,
            "page": page,
            "total_pages": total_pages,
            "total_rows": total_rows,
            "associated_test": associated_test,
        },
    )


@app.get("/comparer")
async def comparer_page(
    request: Request,
    type: str = "classify",
    test: str | None = None,
    file_a: str | None = None,
    file_b: str | None = None,
    filter: str | None = None,
):
    """Comparer page — diff between two reports."""
    tests_yaml = data.get_tests_yaml()
    csv_files = data.get_csv_files(type, tests_yaml)
    available_tests = data.get_available_tests(csv_files)

    # Filter by test if specified
    if test:
        csv_files_filtered = [f for f in csv_files if f.get("test_id") == test]
    else:
        csv_files_filtered = csv_files

    diff = None
    test_a = test_b = None

    if file_a and file_b:
        _, rows_a = data.load_csv(file_a)
        _, rows_b = data.load_csv(file_b)
        key_col = "ancien_nom" if type == "rename" else "fichier"
        diff = data.diff_reports(rows_a, rows_b, key_col)

        test_a = data.find_test_for_report(os.path.basename(file_a), tests_yaml)
        test_b = data.find_test_for_report(os.path.basename(file_b), tests_yaml)

    return templates.TemplateResponse(
        request,
        "comparer.html",
        {
            "active": "comparer",
            "report_type": type,
            "csv_files": csv_files,
            "csv_files_filtered": csv_files_filtered,
            "available_tests": available_tests,
            "test_filter": test or "",
            "file_a": file_a or "",
            "file_b": file_b or "",
            "diff": diff,
            "test_a": test_a,
            "test_b": test_b,
            "filter": filter or "all",
        },
    )


@app.get("/historique")
async def historique_page(request: Request, run_type: str = "all"):
    """Historique page — evolution charts and timeline."""
    history = data.get_history_data()
    run_types = sorted({h.get("run_type", "unknown") for h in history})
    if run_type != "all":
        history = [h for h in history if h.get("run_type", "unknown") == run_type]
    return templates.TemplateResponse(
        request,
        "historique.html",
        {
            "active": "historique",
            "history": history,
            "run_type": run_type,
            "run_types": run_types,
        },
    )


@app.get("/metriques")
async def metriques_page(
    request: Request,
    classify_file: str | None = None,
    rename_file: str | None = None,
):
    """Metriques page — quality metrics with Chart.js charts."""
    tests_yaml = data.get_tests_yaml()
    classify_files = data.get_csv_files("classify", tests_yaml)
    rename_files = data.get_csv_files("rename", tests_yaml)

    classify_metrics = None
    rename_metrics = None

    if classify_files:
        selected = classify_file or classify_files[0]["path"]
        classify_metrics = data.compute_classification_metrics(selected)

    if rename_files:
        selected = rename_file or rename_files[0]["path"]
        rename_metrics = data.compute_rename_metrics(selected)

    problematic = data.find_problematic_files(data.get_all_reports())

    return templates.TemplateResponse(
        request,
        "metriques.html",
        {
            "active": "metriques",
            "classify_files": classify_files,
            "rename_files": rename_files,
            "classify_metrics": classify_metrics,
            "rename_metrics": rename_metrics,
            "problematic": problematic,
            "selected_classify": classify_file or (classify_files[0]["path"] if classify_files else ""),
            "selected_rename": rename_file or (rename_files[0]["path"] if rename_files else ""),
        },
    )


@app.get("/suggestions")
async def suggestions_page(request: Request):
    """Suggestions page — read-only viewer."""
    suggestions = data.get_suggestions()
    total_files = sum(len(s.get("files", [])) for s in suggestions)
    return templates.TemplateResponse(
        request,
        "suggestions.html",
        {
            "active": "suggestions",
            "suggestions": suggestions,
            "total_files": total_files,
        },
    )


@app.post("/api/issue/close")
async def close_issue(
    request: Request,
    number: int,
    comment: str = "PASS — test validé",
):
    """Close a GitHub issue and return updated badge."""
    subprocess.run(
        ["gh", "issue", "close", str(number), "--comment", comment],
        capture_output=True,
        text=True,
        cwd=str(data.get_project_root()),
        timeout=15,
    )
    return templates.TemplateResponse(
        request,
        "partials/issue_badge.html",
        {
            "issue_num": number,
            "issue": {"number": number, "state": "closed"},
        },
    )


# ═══════════════════════════════════════════════════════════════════════════
#  Manual Validation
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/api/validate-check")
async def validate_check_api(run_id: str, check_id: str, status: str):
    """Validate a manual check from the dashboard (pass/fail)."""
    from fastapi.responses import HTMLResponse
    import sys
    func_dir = str(data.get_project_root() / "tests" / "functional")
    if func_dir not in sys.path:
        sys.path.insert(0, func_dir)
    from db import validate_check
    success = validate_check(run_id, check_id, status)
    # Return updated widget HTML
    icon = "✓" if status == "pass" else "✗"
    color = "green" if status == "pass" else "red"
    validated_at = __import__("datetime").datetime.now().strftime("%d/%m %H:%M")
    html = f'''<span class="manual-validated text-{color}">
        {icon} Valid&eacute; ({status.upper()}) le {validated_at}
    </span>'''
    return HTMLResponse(html)


# ═══════════════════════════════════════════════════════════════════════════
#  Admin
# ═══════════════════════════════════════════════════════════════════════════

def _get_admin_stats() -> dict:
    """Compute admin stats."""
    import glob as G
    root = data.get_project_root()
    logs_count = len(list((root / "logs").glob("*"))) if (root / "logs").exists() else 0
    test_logs = root / "tests" / "functional" / "logs"
    test_logs_count = sum(1 for _ in test_logs.rglob("*") if _.is_file()) if test_logs.exists() else 0
    reports_dir = root / "tests" / "functional" / "reports"
    reports_count = len(list(reports_dir.glob("run_*"))) if reports_dir.exists() else 0
    db_path = root / "tests" / "functional" / "results.db"
    db_runs = 0
    db_size = "0 KB"
    if db_path.exists():
        db_size = f"{db_path.stat().st_size / 1024:.1f} KB"
        try:
            import duckdb
            con = duckdb.connect(str(db_path), read_only=True)
            db_runs = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            con.close()
        except Exception:
            pass
    return {
        "logs_count": logs_count,
        "test_logs_count": test_logs_count,
        "reports_count": reports_count,
        "db_runs": db_runs,
        "db_size": db_size,
    }


@app.get("/admin")
async def admin_page(request: Request):
    """Admin page — maintenance tools."""
    stats = _get_admin_stats()
    runs = data.get_available_runs()
    return templates.TemplateResponse(request, "admin.html", {
        "active": "admin", "stats": stats, "runs": runs,
    })


@app.post("/api/admin/clean-logs")
async def clean_logs():
    """Delete all files in logs/."""
    from fastapi.responses import JSONResponse
    import shutil
    logs_dir = data.get_project_root() / "logs"
    count = 0
    if logs_dir.exists():
        for f in logs_dir.iterdir():
            if f.is_file():
                f.unlink()
                count += 1
    return JSONResponse({"message": f"{count} fichiers supprimés dans logs/"})


@app.post("/api/admin/clean-reports")
async def clean_reports():
    """Delete all reports."""
    from fastapi.responses import JSONResponse
    import shutil
    reports_dir = data.get_project_root() / "tests" / "functional" / "reports"
    count = 0
    if reports_dir.exists():
        for d in reports_dir.iterdir():
            if d.is_dir() and d.name.startswith("run_"):
                shutil.rmtree(d)
                count += 1
        # Remove latest symlink
        latest = reports_dir / "latest"
        if latest.is_symlink():
            latest.unlink()
    return JSONResponse({"message": f"{count} rapports supprimés"})


@app.post("/api/admin/clean-db")
async def clean_db():
    """Delete the DuckDB database."""
    from fastapi.responses import JSONResponse
    db_path = data.get_project_root() / "tests" / "functional" / "results.db"
    if db_path.exists():
        db_path.unlink()
        return JSONResponse({"message": "Base DuckDB supprimée"})
    return JSONResponse({"message": "Pas de base à supprimer"})


@app.post("/api/admin/clean-progress")
async def clean_progress():
    """Delete progress/checkpoint files."""
    from fastapi.responses import JSONResponse
    cache_dir = data.get_project_root() / "profiles" / "test" / ".cache"
    count = 0
    if cache_dir.exists():
        for f in cache_dir.glob("progress*.json"):
            f.unlink()
            count += 1
    return JSONResponse({"message": f"{count} fichiers progress supprimés"})


@app.post("/api/admin/clean-all")
async def clean_all():
    """Clean everything: logs + reports + DB + progress + test logs."""
    from fastapi.responses import JSONResponse
    import shutil
    root = data.get_project_root()
    count = 0
    # Logs
    for f in (root / "logs").iterdir() if (root / "logs").exists() else []:
        if f.is_file(): f.unlink(); count += 1
    # Test logs
    test_logs = root / "tests" / "functional" / "logs"
    if test_logs.exists():
        shutil.rmtree(test_logs); test_logs.mkdir(); count += 1
    # Reports
    reports = root / "tests" / "functional" / "reports"
    if reports.exists():
        shutil.rmtree(reports); reports.mkdir(); count += 1
    # DB
    db = root / "tests" / "functional" / "results.db"
    if db.exists(): db.unlink(); count += 1
    # History
    h = root / "tests" / "functional" / "history.json"
    if h.exists(): h.unlink(); count += 1
    # Progress
    cache = root / "profiles" / "test" / ".cache"
    if cache.exists():
        for f in cache.glob("progress*.json"): f.unlink(); count += 1
    return JSONResponse({"message": f"Tout nettoyé ({count} éléments)"})


@app.post("/api/admin/delete-run")
async def delete_run(id: str):
    """Delete a specific run from DuckDB."""
    from fastapi.responses import JSONResponse
    db_path = data.get_project_root() / "tests" / "functional" / "results.db"
    if db_path.exists():
        try:
            import duckdb
            con = duckdb.connect(str(db_path))
            con.execute("DELETE FROM check_results WHERE run_id = ?", [id])
            con.execute("DELETE FROM series_results WHERE run_id = ?", [id])
            con.execute("DELETE FROM runs WHERE id = ?", [id])
            con.close()
        except Exception as e:
            return JSONResponse({"message": f"Erreur: {e}"})
    # Also delete report directory
    import shutil
    report_dir = data.get_project_root() / "tests" / "functional" / "reports" / id
    if report_dir.exists():
        shutil.rmtree(report_dir)
    return JSONResponse({"message": f"Run {id} supprimé"})
