"""Klodo Dashboard — FastAPI application."""

import asyncio
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from dashboard import agent_refonte, baseline, data, taxonomy

# Ensure functional test db module is importable
_func_dir = str(data.get_project_root() / "tests" / "functional")
if _func_dir not in sys.path:
    sys.path.insert(0, _func_dir)

app = FastAPI(title="Klodo Dashboard")


def _is_safe_file_path(file_path: str) -> bool:
    """Validate that a file path is inside allowed directories (prevent path traversal)."""
    if not file_path:
        return False
    resolved = Path(file_path).resolve()
    root = data.get_project_root().resolve()
    allowed = [
        root / "logs",
        root / "tests" / "functional" / "logs",
    ]
    return any(str(resolved).startswith(str(d)) for d in allowed)
app.mount("/static", StaticFiles(directory="dashboard/static"), name="static")
templates = Jinja2Templates(directory="dashboard/templates")

# Make project root available in all templates
_project_root = str(data.get_project_root())
templates.env.globals["project_root"] = _project_root


@app.get("/")
async def overview(request: Request):
    """Overview page with KPIs, heatmap, and release gate."""
    report = data.get_run_from_db() or data.get_latest_report()
    tests = data.get_tests_yaml()
    issues = data.get_open_issues()
    llm_config = data.get_llm_config()
    api_keys = _get_api_keys_status()
    api_key_set = any(k["set"] for k in api_keys if k["name"] == "SILICONFLOW_API_KEY")
    return templates.TemplateResponse(
        request,
        "overview.html",
        {"report": report, "tests": tests, "issues": issues,
         "api_key_set": api_key_set,
         "llm_config": llm_config},
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
        # Merge with tests.yaml: keep DuckDB statuses, add missing series as not_run
        report = data.get_merged_test_view(db_report, tests_yaml)
        # Override checks with DuckDB values (preserves manual validations)
        data.apply_db_statuses(report, db_report)
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
            "api_key_set": any(k["set"] for k in _get_api_keys_status() if k["name"] == "SILICONFLOW_API_KEY"),
            "available_runs": available_runs,
            "selected_run": run or "",
            "current_run_id": report.get("_run_id", run or "") if report else "",
            "available_profiles": data.get_available_profiles(),
            "current_profile": "test",
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


@app.get("/api/events")
async def sse_events():
    """Server-Sent Events stream for test run progress.

    Lit les dicts module-level (_current_series, _completed_series, _completed_checks)
    mis à jour par le thread _monitor_output, et émet des événements JSON
    à chaque changement d'état. Le client utilise EventSource() côté navigateur.
    """
    async def generate():
        last_series = None
        sent_series: set[str] = set()
        sent_checks: set[str] = set()

        # Événement initial immédiat (pour débloquer les clients)
        yield 'data: {"type": "connected"}\n\n'

        while True:
            running = _running_process is not None and _running_process.poll() is None

            if not running:
                if last_series is not None or sent_series or sent_checks:
                    # Le run vient de se terminer
                    yield 'data: {"type": "run_finished"}\n\n'
                    break
                # Pas de run actif : keep-alive périodique
                yield 'data: {"type": "idle"}\n\n'
                await asyncio.sleep(2)
                continue

            # Nouvelle série en cours
            current = _current_series
            if current and current != last_series:
                last_series = current
                yield f"data: {json.dumps({'type': 'series_started', 'id': current})}\n\n"

            # Séries terminées
            for sid, status in list(_completed_series.items()):
                if sid not in sent_series:
                    sent_series.add(sid)
                    yield f"data: {json.dumps({'type': 'series_completed', 'id': sid, 'status': status})}\n\n"

            # Checks terminés
            for cid, status in list(_completed_checks.items()):
                if cid not in sent_checks:
                    sent_checks.add(cid)
                    yield f"data: {json.dumps({'type': 'check_completed', 'id': cid, 'status': status})}\n\n"

            await asyncio.sleep(0.5)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


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
    profile: str | None = None,
):
    """Run functional tests and return updated HTML."""
    global _running_process

    cmd = ["uv", "run", "python", "tests/functional/run_functional.py"]
    if profile and profile != "test":
        cmd += ["--var", f"PROF={profile}"]
    if no_history:
        cmd.append("--no-history")
    if series:
        # Support comma-separated series: "T0.1,T0.2,T1.1"
        series_list = [s.strip() for s in series.split(",") if s.strip()]
        cmd += ["--series"] + series_list
        print(f"[dashboard] Running series: {series_list}")
    elif phase is not None:
        # Always include phase 0 (prerequisites) to satisfy dependencies
        if phase != 0:
            cmd += ["--phase", "0", str(phase)]
        else:
            cmd += ["--phase", "0"]
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
    # Load .env into subprocess environment
    env_file = data.get_project_root() / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env.setdefault(k.strip(), v.strip())
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

    # Path traversal protection
    if selected_file and not _is_safe_file_path(selected_file):
        selected_file = csv_files_filtered[0]["path"] if csv_files_filtered else None

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


@app.get("/logs")
async def logs_page(
    request: Request,
    type: str = "all",
    file: str | None = None,
    search: str | None = None,
    status: str | None = None,
    sort: str | None = None,
    order: str = "asc",
    page: int = 1,
):
    """Logs page — viewer for user reports in logs/."""
    log_files = data.get_user_log_files(type)
    selected_file = file or (log_files[0]["path"] if log_files else None)

    # Path traversal protection
    if selected_file and not _is_safe_file_path(selected_file):
        selected_file = log_files[0]["path"] if log_files else None

    headers: list[str] = []
    rows: list[dict] = []
    stats: dict = {}
    statuses: list[str] = []
    sections: list[str] = []
    total_pages = 1
    total_rows = 0

    # Determine effective type for the selected file
    effective_type = type
    if selected_file and type == "all":
        for lf in log_files:
            if lf["path"] == selected_file:
                effective_type = lf["log_type"]
                break

    if selected_file:
        headers, rows = data.load_csv(selected_file)
        stats = data.compute_csv_stats(rows, effective_type)
        statuses = data.get_csv_statuses(rows, effective_type)
        sections = data.get_csv_sections(rows)

        if search:
            rows = [r for r in rows if search.lower() in str(r).lower()]
        if status:
            status_key = "action" if effective_type == "rename" else "status"
            rows = [r for r in rows if r.get(status_key, "") == status]

        if sort and sort in headers:
            reverse = order == "desc"
            rows.sort(key=lambda r: r.get(sort, ""), reverse=reverse)

        per_page = 50
        total_rows = len(rows)
        total_pages = max(1, (total_rows + per_page - 1) // per_page)
        page = max(1, min(page, total_pages))
        rows = rows[(page - 1) * per_page : page * per_page]

    return templates.TemplateResponse(
        request,
        "logs.html",
        {
            "active": "logs",
            "log_type": type,
            "effective_type": effective_type,
            "log_files": log_files,
            "selected_file": selected_file or "",
            "headers": headers,
            "rows": rows,
            "stats": stats,
            "statuses": statuses,
            "sections": sections,
            "search": search or "",
            "status_filter": status or "",
            "sort": sort or "",
            "order": order,
            "page": page,
            "total_pages": total_pages,
            "total_rows": total_rows,
        },
    )


@app.get("/comparer")
async def comparer_page(
    request: Request,
    old_run: str | None = None,
    series_filter: str | None = None,
):
    """Comparer page — diff between current run and an older run."""
    available_runs = data.get_available_runs()

    # Current = most recent run
    current_run = data.get_run_from_db() if available_runs else None
    # Old = selected older run
    old_run_data = data.get_run_from_db(old_run) if old_run else None

    # Build comparison data
    diff = None
    common_series: list[dict] = []

    if current_run and old_run_data:
        diff = data.compare_runs(current_run, old_run_data, series_filter)
        # Find series in common
        current_series_ids = {
            s["id"] for p in current_run.get("phases", []) for s in p.get("series", [])
        }
        old_series_ids = {
            s["id"] for p in old_run_data.get("phases", []) for s in p.get("series", [])
        }
        common_ids = current_series_ids & old_series_ids
        # Build list with names
        for p in current_run.get("phases", []):
            for s in p.get("series", []):
                if s["id"] in common_ids:
                    common_series.append({"id": s["id"], "name": s.get("name", s["id"])})
        common_series.sort(key=lambda x: x["id"])

    # Exclude current run from "old runs" dropdown
    old_runs = [r for r in available_runs if r.get("id") != (current_run or {}).get("_run_id")]

    return templates.TemplateResponse(
        request,
        "comparer.html",
        {
            "active": "comparer",
            "current_run": current_run,
            "old_run_data": old_run_data,
            "old_runs": old_runs,
            "selected_old_run": old_run or "",
            "common_series": common_series,
            "series_filter": series_filter or "",
            "diff": diff,
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
    run: str | None = None,
):
    """Metriques page — quality metrics for a specific run."""
    available_runs = data.get_available_runs()

    # Get CSV files for selected run (or latest)
    run_csvs = data.get_run_csv_files(run)
    selected_run = run or ""

    # Find which run is actually loaded
    current_run = data.get_run_from_db(run)

    classify_metrics = None
    rename_metrics = None

    classify_paths = run_csvs.get("classify", []) + run_csvs.get("process", [])
    rename_paths = run_csvs.get("rename", [])

    if classify_paths:
        classify_metrics = data.compute_aggregated_metrics(
            classify_paths, data.compute_classification_metrics
        )

    if rename_paths:
        rename_metrics = data.compute_aggregated_metrics(
            rename_paths, data.compute_rename_metrics
        )

    return templates.TemplateResponse(
        request,
        "metriques.html",
        {
            "active": "metriques",
            "classify_metrics": classify_metrics,
            "rename_metrics": rename_metrics,
            "available_runs": available_runs,
            "selected_run": selected_run,
            "current_run": current_run,
            "classify_count": len(classify_paths),
            "rename_count": len(rename_paths),
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
    import sys

    from fastapi.responses import HTMLResponse
    func_dir = str(data.get_project_root() / "tests" / "functional")
    if func_dir not in sys.path:
        sys.path.insert(0, func_dir)
    from db import validate_check
    validate_check(run_id, check_id, status)
    # Return updated widget HTML
    icon = "✓" if status == "pass" else "✗"
    color = "green" if status == "pass" else "red"
    validated_at = datetime.now().strftime("%d/%m %H:%M")
    html = f'''<span class="manual-validated text-{color}">
        {icon} Valid&eacute; ({status.upper()}) le {validated_at}
    </span>'''
    return HTMLResponse(html)


# ═══════════════════════════════════════════════════════════════════════════
#  Viewer — visualisation des fichiers de l'INBOX
# ═══════════════════════════════════════════════════════════════════════════

# État de génération en masse (similaire à _running_process)
_thumbnail_gen_state: dict = {
    "running": False,
    "profile": None,
    "total": 0,
    "done": 0,
    "current": "",
    "errors": 0,
    "force": False,
}


def _is_safe_thumbnail_path(target: Path, cache_dir: Path) -> bool:
    """Path traversal guard pour les fichiers du cache thumbnails."""
    try:
        target_resolved = target.resolve()
        cache_resolved = cache_dir.resolve()
    except OSError:
        return False
    return str(target_resolved).startswith(str(cache_resolved))


def _profile_cache_stats(profile: str) -> dict:
    """Helper : récupère les stats du cache thumbnails d'un profil."""
    from lib.thumbnail import get_cache_stats
    try:
        cache_dir = data.get_thumbnail_cache_dir(profile)
        return get_cache_stats(cache_dir)
    except Exception:
        return {"count": 0, "size_bytes": 0}


@app.get("/viewer")
async def viewer_page(
    request: Request,
    source_profile: str = "default",
    dest_profile: str = "test",
):
    """Page principale du viewer (double panneau source + destination)."""
    all_profiles = data.get_available_profiles(include_all=True)
    dest_profiles = [p for p in all_profiles if p["name"] in data.CURATION_DEST_PROFILES]

    source_files = data.list_inbox_files(source_profile) if source_profile else []
    dest_files = data.list_inbox_files(dest_profile) if dest_profile else []

    source_cache = _profile_cache_stats(source_profile) if source_profile else {"count": 0, "size_bytes": 0}
    dest_cache = _profile_cache_stats(dest_profile) if dest_profile else {"count": 0, "size_bytes": 0}

    return templates.TemplateResponse(
        request,
        "viewer.html",
        {
            "active": "viewer",
            "all_profiles": all_profiles,
            "dest_profiles": dest_profiles,
            "source_profile": source_profile,
            "dest_profile": dest_profile,
            "source_files": source_files,
            "dest_files": dest_files,
            "source_cache": source_cache,
            "dest_cache": dest_cache,
            "source_cache_mo": round(source_cache["size_bytes"] / 1024 / 1024, 1),
            "dest_cache_mo": round(dest_cache["size_bytes"] / 1024 / 1024, 1),
        },
    )


@app.get("/viewer-mockup")
async def viewer_mockup_page(request: Request):
    """Mockup statique du viewer double-panneau pour validation visuelle."""
    source_files = [
        {"name": "Clean Code - Robert Martin.pdf", "cached": True, "marked": True},
        {"name": "Algorithms - Thomas Cormen.pdf", "cached": True, "marked": True},
        {"name": "Python Cookbook - David Beazley.pdf", "cached": True, "marked": True},
        {"name": "Deep Learning - Ian Goodfellow.pdf", "cached": False, "marked": False},
        {"name": "Practical Statistics.epub", "cached": True, "marked": False},
        {"name": "Refactoring - Martin Fowler.pdf", "cached": True, "marked": False},
        {"name": "The Pragmatic Programmer.pdf", "cached": False, "marked": False},
        {"name": "Designing Data-Intensive Apps.pdf", "cached": True, "marked": False},
        {"name": "Effective Java.pdf", "cached": False, "marked": False},
        {"name": "Head First Design Patterns.pdf", "cached": True, "marked": False},
    ]
    dest_files = [
        {"name": "Clean Code - Robert Martin.pdf", "cached": False},
        {"name": "Algorithms - Thomas Cormen.pdf", "cached": False},
        {"name": "Python Cookbook - David Beazley.pdf", "cached": True},
    ]
    return templates.TemplateResponse(request, "viewer_mockup.html", {
        "active": "viewer",
        "source_files": source_files,
        "dest_files": dest_files,
    })


@app.get("/api/viewer/files")
async def viewer_files(profile: str):
    """Renvoie la liste des fichiers de l'INBOX avec leur état de cache."""
    from fastapi.responses import JSONResponse
    return JSONResponse(data.list_inbox_files(profile))


@app.get("/api/viewer/thumbnail")
async def viewer_thumbnail(profile: str, filename: str, page: int = 1):
    """Sert un thumbnail JPEG depuis le cache.

    page: numéro de page (1 par défaut). Cherche cache_dir/{stem}/{page}.jpg
    """
    from fastapi.responses import FileResponse, JSONResponse
    try:
        cache_dir = data.get_thumbnail_cache_dir(profile)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    safe_stem = Path(filename).stem
    doc_dir = cache_dir / safe_stem
    target = doc_dir / f"{page}.jpg"

    if not _is_safe_thumbnail_path(target, cache_dir):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if not target.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(target, media_type="image/jpeg")


@app.get("/api/viewer/pages")
async def viewer_pages(profile: str, filename: str):
    """Retourne le nombre de pages en cache pour un document."""
    from fastapi.responses import JSONResponse

    from lib.thumbnail import count_pages
    try:
        cache_dir = data.get_thumbnail_cache_dir(profile)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    safe_stem = Path(filename).stem
    return JSONResponse({"count": count_pages(cache_dir, safe_stem)})


@app.get("/api/viewer/cache-stats")
async def viewer_cache_stats(profile: str):
    """Retourne les stats du cache thumbnails pour un profil."""
    from fastapi.responses import JSONResponse
    stats = _profile_cache_stats(profile)
    stats["size_mo"] = round(stats["size_bytes"] / 1024 / 1024, 1)
    return JSONResponse(stats)


@app.post("/api/viewer/generate")
async def viewer_generate_one(
    profile: str, filename: str, n_pages: int = 1, complete: bool = False,
):
    """Génère N thumbnails pour un fichier unique.

    Args:
        n_pages: nombre de pages à générer (1-5)
        complete: si True, complète à partir de la première page manquante
                  (utile pour "Compléter jusqu'à N" de l'option B)
    """
    from fastapi.responses import JSONResponse

    from lib.profile import Profile
    from lib.thumbnail import count_pages, generate_thumbnail
    try:
        profile_obj = Profile(profile)
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)

    source = Path(profile_obj.inbox) / filename
    if not source.exists():
        return JSONResponse({"success": False, "error": "source not found"}, status_code=404)

    cache_dir = data.get_thumbnail_cache_dir(profile)
    safe_stem = Path(filename).stem
    doc_dir = cache_dir / safe_stem

    if not _is_safe_thumbnail_path(doc_dir, cache_dir):
        return JSONResponse({"success": False, "error": "forbidden"}, status_code=403)

    if complete:
        existing = count_pages(cache_dir, safe_stem)
        start = existing + 1
        remaining = max(0, n_pages - existing)
        if remaining == 0:
            return JSONResponse({"success": True, "filename": filename, "generated": 0})
        generated = generate_thumbnail(source, doc_dir, n_pages=remaining, start_page=start)
    else:
        generated = generate_thumbnail(source, doc_dir, n_pages=n_pages, start_page=1)

    return JSONResponse({
        "success": generated > 0,
        "filename": filename,
        "generated": generated,
    })


_THUMBNAIL_BATCH_WORKERS = 4
_thumbnail_state_lock = threading.Lock()


def _process_one_file(
    f: Path, cache_dir: Path, force: bool, n_pages: int,
) -> tuple[bool, bool]:
    """Traite un seul fichier (utilisé par le pool de threads).

    Returns:
        (was_processed, had_error)
        - was_processed: False si on a skip (déjà cache complet, mode "complete")
        - had_error: True si la génération a échoué
    """
    import shutil

    from lib.thumbnail import count_pages, generate_thumbnail

    doc_dir = cache_dir / f.stem
    existing = count_pages(cache_dir, f.stem)

    if force:
        if doc_dir.exists():
            try:
                shutil.rmtree(doc_dir)
            except OSError:
                pass
        start = 1
        remaining = n_pages
    else:
        if existing >= n_pages:
            return (False, False)
        start = existing + 1
        remaining = n_pages - existing

    try:
        generated = generate_thumbnail(f, doc_dir, n_pages=remaining, start_page=start)
        return (True, generated == 0)
    except Exception:
        return (True, True)


def _generate_batch_thread(profile: str, force: bool, n_pages: int):
    """Worker thread principal — orchestre un ThreadPoolExecutor parallèle.

    Stratégies :
    - force=True : régénère tout (efface puis crée n_pages pages par fichier)
    - force=False : "compléter jusqu'à n_pages" pour chaque fichier
    """
    from concurrent.futures import ProcessPoolExecutor, as_completed

    from lib.profile import Profile

    global _thumbnail_gen_state
    try:
        profile_obj = Profile(profile)
    except Exception as e:
        _thumbnail_gen_state.update({"running": False, "current": f"Erreur: {e}"})
        return

    inbox = Path(profile_obj.inbox)
    cache_dir = data.get_thumbnail_cache_dir(profile)

    files = sorted(
        f for f in inbox.iterdir()
        if f.is_file() and f.suffix.lower() in data.VIEWER_SUPPORTED_EXTS
    )

    _thumbnail_gen_state.update({
        "running": True,
        "profile": profile,
        "total": len(files),
        "done": 0,
        "current": "",
        "errors": 0,
        "force": force,
    })

    if not files:
        _thumbnail_gen_state["running"] = False
        return

    with ProcessPoolExecutor(max_workers=_THUMBNAIL_BATCH_WORKERS) as executor:
        future_to_file = {
            executor.submit(_process_one_file, f, cache_dir, force, n_pages): f
            for f in files
        }
        for future in as_completed(future_to_file):
            f = future_to_file[future]
            try:
                _, had_error = future.result()
            except Exception:
                had_error = True
            with _thumbnail_state_lock:
                _thumbnail_gen_state["current"] = f.name
                _thumbnail_gen_state["done"] += 1
                if had_error:
                    _thumbnail_gen_state["errors"] += 1

    _thumbnail_gen_state["running"] = False
    _thumbnail_gen_state["current"] = ""


@app.post("/api/viewer/generate-batch")
async def viewer_generate_batch(profile: str, force: bool = False, n_pages: int = 1):
    """Lance la génération en masse en arrière-plan.

    n_pages: nombre de pages à générer/compléter par document (1-5)
    force: True = régénération totale, False = complète jusqu'à n_pages
    """
    from fastapi.responses import JSONResponse
    if _thumbnail_gen_state["running"]:
        return JSONResponse({"started": False, "reason": "already running"}, status_code=409)

    n_pages = max(1, min(5, n_pages))
    thread = threading.Thread(
        target=_generate_batch_thread, args=(profile, force, n_pages), daemon=True)
    thread.start()
    return JSONResponse({"started": True})


@app.get("/api/viewer/events")
async def viewer_events():
    """SSE pour la progression de la génération en masse."""
    async def generate():
        yield 'data: {"type": "connected"}\n\n'

        # Attendre que le thread worker démarre (max 3s)
        waited = 0.0
        while not _thumbnail_gen_state["running"] and waited < 3.0:
            await asyncio.sleep(0.1)
            waited += 0.1

        last_done = -1
        last_current = None

        while True:
            done = _thumbnail_gen_state["done"]
            total = _thumbnail_gen_state["total"]
            current = _thumbnail_gen_state["current"]
            running = _thumbnail_gen_state["running"]

            if done != last_done or current != last_current:
                payload = {
                    "type": "progress",
                    "done": done,
                    "total": total,
                    "current": current,
                    "errors": _thumbnail_gen_state["errors"],
                }
                yield f"data: {json.dumps(payload)}\n\n"
                last_done = done
                last_current = current

            # Sortir quand le thread a fini ET qu'on a rapporté le dernier état
            if not running and done >= total:
                break
            if not running and total == 0:
                break

            await asyncio.sleep(0.2)

        final = {
            "type": "batch_finished",
            "done": _thumbnail_gen_state["done"],
            "total": _thumbnail_gen_state["total"],
            "errors": _thumbnail_gen_state["errors"],
        }
        yield f"data: {json.dumps(final)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/api/viewer/clear-cache")
async def viewer_clear_cache(profile: str):
    """Vide le cache des thumbnails pour un profil."""
    from fastapi.responses import JSONResponse

    from lib.thumbnail import clear_cache
    try:
        cache_dir = data.get_thumbnail_cache_dir(profile)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    count, freed = clear_cache(cache_dir)
    return JSONResponse({"count": count, "freed_bytes": freed})


@app.post("/api/viewer/copy")
async def viewer_copy(source_profile: str, dest_profile: str, filenames: str):
    """Copie des fichiers de l'INBOX source vers l'INBOX destination.

    filenames est une liste séparée par des virgules.
    """
    from fastapi.responses import JSONResponse
    names = [f.strip() for f in filenames.split(",") if f.strip()]
    if not names:
        return JSONResponse({"error": "no filenames provided"}, status_code=400)
    try:
        result = data.copy_files_between_profiles(source_profile, dest_profile, names)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse(result)


@app.post("/api/viewer/clear-destination")
async def viewer_clear_destination(profile: str):
    """Supprime tous les .pdf/.epub de l'INBOX du profil destination."""
    from fastapi.responses import JSONResponse
    try:
        result = data.clear_destination_inbox(profile)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse(result)


# ═══════════════════════════════════════════════════════════════════════════
#  Admin
# ═══════════════════════════════════════════════════════════════════════════

def _get_env_path():
    """Return path to the project .env file."""
    return data.get_project_root() / ".env"


def _load_env_keys() -> dict[str, str]:
    """Load key=value pairs from .env file."""
    env_path = _get_env_path()
    result = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                result[k.strip()] = v.strip()
    return result


def _save_env_keys(keys: dict[str, str]):
    """Save key=value pairs to .env file, preserving comments."""
    env_path = _get_env_path()
    existing_lines = []
    if env_path.exists():
        existing_lines = env_path.read_text().splitlines()
    # Update existing keys, track which ones were updated
    updated = set()
    new_lines = []
    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _, _ = stripped.partition("=")
            k = k.strip()
            if k in keys:
                new_lines.append(f"{k}={keys[k]}")
                updated.add(k)
                continue
        new_lines.append(line)
    # Add new keys
    for k, v in keys.items():
        if k not in updated:
            new_lines.append(f"{k}={v}")
    env_path.write_text("\n".join(new_lines) + "\n")


def _get_api_keys_status() -> list[dict]:
    """Return status of known API keys."""
    env_keys = _load_env_keys()
    known = [
        ("SILICONFLOW_API_KEY", "SiliconFlow (LLM Vision)"),
    ]
    result = []
    for key_name, label in known:
        env_val = env_keys.get(key_name, "")
        runtime_val = os.environ.get(key_name, "")
        val = env_val or runtime_val
        result.append({
            "name": key_name,
            "label": label,
            "set": bool(val),
            "masked": f"{val[:8]}...{val[-4:]}" if val and len(val) > 12 else ("***" if val else ""),
            "source": "env" if runtime_val and not env_val else ("file" if env_val else ""),
        })
    return result


def _get_admin_stats() -> dict:
    """Compute admin stats."""
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
            with duckdb.connect(str(db_path), read_only=True) as con:
                db_runs = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
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
    api_keys = _get_api_keys_status()
    return templates.TemplateResponse(request, "admin.html", {
        "active": "admin", "stats": stats, "runs": runs, "api_keys": api_keys,
    })


@app.post("/api/admin/save-api-key")
async def save_api_key(key_name: str, key_value: str):
    """Save an API key to .env and update runtime environment."""
    from fastapi.responses import JSONResponse
    allowed = {"SILICONFLOW_API_KEY"}
    if key_name not in allowed:
        return JSONResponse({"message": f"Clé inconnue: {key_name}"}, status_code=400)
    _save_env_keys({key_name: key_value})
    os.environ[key_name] = key_value
    return JSONResponse({"message": f"{key_name} sauvegardée dans .env"})


@app.post("/api/admin/delete-api-key")
async def delete_api_key(key_name: str):
    """Remove an API key from .env and runtime environment."""
    from fastapi.responses import JSONResponse
    env_path = _get_env_path()
    if env_path.exists():
        lines = env_path.read_text().splitlines()
        lines = [line for line in lines if not line.strip().startswith(f"{key_name}=")]
        env_path.write_text("\n".join(lines) + "\n")
    os.environ.pop(key_name, None)
    return JSONResponse({"message": f"{key_name} supprimée"})


@app.post("/api/admin/clean-logs")
async def clean_logs():
    """Delete all files in logs/."""

    from fastapi.responses import JSONResponse
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
    import shutil

    from fastapi.responses import JSONResponse
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
    import shutil

    from fastapi.responses import JSONResponse
    root = data.get_project_root()
    count = 0
    # Logs
    for f in (root / "logs").iterdir() if (root / "logs").exists() else []:
        if f.is_file():
            f.unlink()
            count += 1
    # Test logs
    test_logs = root / "tests" / "functional" / "logs"
    if test_logs.exists():
        shutil.rmtree(test_logs)
        test_logs.mkdir()
        count += 1
    # Reports
    reports = root / "tests" / "functional" / "reports"
    if reports.exists():
        shutil.rmtree(reports)
        reports.mkdir()
        count += 1
    # DB
    db = root / "tests" / "functional" / "results.db"
    if db.exists():
        db.unlink()
        count += 1
    # History
    h = root / "tests" / "functional" / "history.json"
    if h.exists():
        h.unlink()
        count += 1
    # Progress
    cache = root / "profiles" / "test" / ".cache"
    if cache.exists():
        for f in cache.glob("progress*.json"):
            f.unlink()
            count += 1
    return JSONResponse({"message": f"Tout nettoyé ({count} éléments)"})


@app.post("/api/admin/delete-run")
async def delete_run(id: str):
    """Delete a specific run from DuckDB."""
    from fastapi.responses import JSONResponse
    db_path = data.get_project_root() / "tests" / "functional" / "results.db"
    if db_path.exists():
        try:
            import duckdb
            with duckdb.connect(str(db_path)) as con:
                con.execute("DELETE FROM check_results WHERE run_id = ?", [id])
                con.execute("DELETE FROM series_results WHERE run_id = ?", [id])
                con.execute("DELETE FROM runs WHERE id = ?", [id])
        except Exception as e:
            return JSONResponse({"message": f"Erreur: {e}"})
    # Also delete report directory
    import shutil
    report_dir = data.get_project_root() / "tests" / "functional" / "reports" / id
    if report_dir.exists():
        shutil.rmtree(report_dir)
    return JSONResponse({"message": f"Run {id} supprimé"})



# ═══════════════════════════════════════════════════════════════════════════
#  Baseline classification — adjudicate disagreements between current Klodo
#  predictions and the current SSD layout. Run-based, multi-profile.
# ═══════════════════════════════════════════════════════════════════════════


@app.get("/baseline")
async def baseline_page(
    request: Request,
    profile: str | None = None,
    run_id: str | None = None,
):
    """Validation UI for the latest baseline run of a profile.

    Smart default: profile with the most recent run, else 'default'.
    """
    if not profile:
        profile = baseline.smart_default_profile()

    available_profiles = [
        p["name"] if isinstance(p, dict) else p
        for p in data.get_available_profiles()
    ]

    runs = baseline.list_runs(profile)
    selected_run = baseline.get_run(profile, run_id)
    if selected_run is None and runs:
        selected_run = runs[0]

    folders = baseline.list_target_folders(profile)
    record = None
    stats = None
    if selected_run:
        rid = selected_run["run_id"]
        record = baseline.next_record(profile, rid)
        stats = baseline.stats(profile, rid)

    return templates.TemplateResponse(
        request,
        "baseline.html",
        {
            "active": "baseline",
            "profile": profile,
            "available_profiles": available_profiles,
            "runs": runs,
            "selected_run": selected_run,
            "record": record,
            "folders": folders,
            "stats": stats,
        },
    )


@app.get("/api/baseline/runs")
async def baseline_runs_api(profile: str):
    """List baseline runs for a profile (latest first)."""
    from fastapi.responses import JSONResponse
    return JSONResponse({"runs": baseline.list_runs(profile)})


@app.get("/api/baseline/next")
async def baseline_next_api(profile: str, run_id: str | None = None):
    """Return the next unvalidated disagreement for a run."""
    from fastapi.responses import JSONResponse
    run = baseline.get_run(profile, run_id)
    if run is None:
        return JSONResponse({"error": "no run available"}, status_code=404)
    rid = run["run_id"]
    record = baseline.next_record(profile, rid)
    return JSONResponse({
        "done": record is None,
        "record": record,
        "run_id": rid,
        "stats": baseline.stats(profile, rid),
    })


@app.get("/api/baseline/record")
async def baseline_record_api(profile: str, file_id: str, run_id: str | None = None):
    """Return a specific record by file_id (used for going back / editing)."""
    from fastapi.responses import JSONResponse
    run = baseline.get_run(profile, run_id)
    if run is None:
        return JSONResponse({"error": "no run available"}, status_code=404)
    rid = run["run_id"]
    record = baseline.get_record(profile, rid, file_id)
    if record is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"record": record, "run_id": rid,
                         "stats": baseline.stats(profile, rid)})


@app.post("/api/baseline/verdict")
async def baseline_verdict_api(request: Request):
    """Persist a verdict for one disagreement.

    Body JSON: {profile, run_id, file_id, verdict, ground_truth?, notes?}
    """
    from fastapi.responses import JSONResponse
    body = await request.json()
    profile = body.get("profile")
    run_id = body.get("run_id")
    file_id = body.get("file_id")
    verdict = body.get("verdict")
    ground_truth = body.get("ground_truth")
    notes = body.get("notes")

    if not profile or not run_id or not file_id or not verdict:
        return JSONResponse({"error": "missing fields"}, status_code=400)

    try:
        ok = baseline.save_verdict(
            profile, run_id, file_id, verdict,
            ground_truth=ground_truth, notes=notes,
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    if not ok:
        return JSONResponse({"error": "file_id not found"}, status_code=404)

    next_rec = baseline.next_record(profile, run_id)
    return JSONResponse({
        "ok": True,
        "stats": baseline.stats(profile, run_id),
        "next": next_rec,
        "done": next_rec is None,
    })


@app.get("/api/baseline/thumbnail/{file_id}")
async def baseline_thumbnail_api(
    file_id: str, profile: str, run_id: str | None = None,
):
    """Serve a JPEG thumbnail for a file_id, generating on demand."""
    from fastapi.responses import FileResponse, JSONResponse

    from lib.thumbnail import generate_thumbnail
    run = baseline.get_run(profile, run_id)
    if run is None:
        return JSONResponse({"error": "no run"}, status_code=404)
    record = baseline.get_record(profile, run["run_id"], file_id)
    if record is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    source = baseline.resolve_source_path(profile, record["rel_path"])
    if source is None:
        return JSONResponse({"error": "source missing"}, status_code=404)

    doc_dir = baseline.baseline_thumbnail_dir(profile, file_id)
    target = doc_dir / "1.jpg"
    if not target.exists():
        try:
            generate_thumbnail(source, doc_dir, n_pages=1, start_page=1)
        except Exception as e:
            return JSONResponse({"error": f"thumbnail failed: {e}"}, status_code=500)
    if not target.exists():
        return JSONResponse({"error": "unavailable"}, status_code=500)
    return FileResponse(target, media_type="image/jpeg")


@app.get("/api/baseline/folders")
async def baseline_folders_api(profile: str):
    from fastapi.responses import JSONResponse
    return JSONResponse({"folders": baseline.list_target_folders(profile)})


@app.get("/api/baseline/stats")
async def baseline_stats_api(profile: str, run_id: str | None = None):
    from fastapi.responses import JSONResponse
    run = baseline.get_run(profile, run_id)
    if run is None:
        return JSONResponse({"exists": False})
    return JSONResponse(baseline.stats(profile, run["run_id"]))


# ─── Taxonomy ────────────────────────────────────────────────────────────


@app.get("/taxonomy")
async def taxonomy_page(request: Request, profile: str | None = None):
    """Interactive viewer of tree.yaml + theme_mapping.yaml + LLM themes."""
    profiles = taxonomy.list_profiles()
    if not profile and profiles:
        profile = profiles[0]["name"]
    return templates.TemplateResponse(
        request,
        "taxonomy.html",
        {
            "active": "taxonomy",
            "profile": profile,
            "profiles": profiles,
        },
    )


@app.get("/api/categories/snapshot")
async def categories_snapshot_api(profile: str, force: bool = False):
    """Return the parsed + aggregated view of categories.yaml for the UI."""
    from fastapi.responses import JSONResponse

    from dashboard import categories
    return JSONResponse(categories.build_snapshot(profile, force_reload=force))


def _categories_err(e: "object"):  # typing.TYPE_CHECKING-safe
    from fastapi.responses import JSONResponse
    return JSONResponse({"error": str(e)}, status_code=getattr(e, "status", 400))


@app.post("/api/categories/entry")
async def categories_entry_add_api(request: Request):
    """Create a new entry under a group in categories.yaml."""
    from fastapi.responses import JSONResponse

    from dashboard import categories
    body = await request.json()
    try:
        return JSONResponse(categories.add_entry(
            body.get("profile"),
            body.get("group"),
            body.get("chemin"),
            body.get("priorite", 5),
            body.get("mots_cles") or [],
        ))
    except categories.CategoriesError as e:
        return _categories_err(e)


@app.patch("/api/categories/entry")
async def categories_entry_update_api(request: Request):
    """Update an existing entry's path and/or priority."""
    from fastapi.responses import JSONResponse

    from dashboard import categories
    body = await request.json()
    try:
        return JSONResponse(categories.update_entry(
            body.get("profile"),
            body.get("group"),
            body.get("chemin"),
            new_chemin=body.get("new_chemin"),
            new_priorite=body.get("new_priorite"),
        ))
    except categories.CategoriesError as e:
        return _categories_err(e)


@app.delete("/api/categories/entry")
async def categories_entry_delete_api(request: Request):
    """Remove an entry."""
    from fastapi.responses import JSONResponse

    from dashboard import categories
    body = await request.json()
    try:
        return JSONResponse(categories.delete_entry(
            body.get("profile"),
            body.get("group"),
            body.get("chemin"),
        ))
    except categories.CategoriesError as e:
        return _categories_err(e)


@app.post("/api/categories/entry/keyword")
async def categories_keyword_add_api(request: Request):
    """Append a keyword to an entry's mots_cles list (dedups)."""
    from fastapi.responses import JSONResponse

    from dashboard import categories
    body = await request.json()
    try:
        return JSONResponse(categories.add_keyword(
            body.get("profile"),
            body.get("group"),
            body.get("chemin"),
            body.get("keyword"),
        ))
    except categories.CategoriesError as e:
        return _categories_err(e)


@app.delete("/api/categories/entry/keyword")
async def categories_keyword_delete_api(request: Request):
    """Remove a keyword from an entry (case-insensitive match)."""
    from fastapi.responses import JSONResponse

    from dashboard import categories
    body = await request.json()
    try:
        return JSONResponse(categories.delete_keyword(
            body.get("profile"),
            body.get("group"),
            body.get("chemin"),
            body.get("keyword"),
        ))
    except categories.CategoriesError as e:
        return _categories_err(e)


@app.post("/api/categories/undo")
async def categories_undo_api(request: Request):
    """Restore categories.yaml from the most recent backup."""
    from fastapi.responses import JSONResponse

    from dashboard import categories
    body = await request.json()
    try:
        return JSONResponse(categories.undo(body.get("profile")))
    except categories.CategoriesError as e:
        return _categories_err(e)


@app.get("/api/rename/audit")
async def rename_audit_api(profile: str, force: bool = False):
    """Scan the lib + suggest a name per file using the profile's
    rename template. Read-only. Cached in memory.
    Optional `force=true` bypasses the cache."""
    from fastapi.responses import JSONResponse

    from dashboard import rename as rename_audit_module
    return JSONResponse(
        rename_audit_module.rename_audit(profile, force_reload=force))


@app.post("/api/rename/file")
async def rename_file_api(request: Request):
    """Apply a single rename. Body: {profile, rel_path, new_name, batch_id?}.
    On success the rename is committed on disk + journaled.
    Returns the new rel_path so the caller can navigate / select it."""
    from fastapi.responses import JSONResponse

    from dashboard import rename as rename_mod
    body = await request.json()
    try:
        result = rename_mod.commit_rename(
            profile=body.get("profile"),
            rel_path=body.get("rel_path"),
            new_name=body.get("new_name"),
            batch_id=body.get("batch_id") or "",
        )
        return JSONResponse(result)
    except rename_mod.RenameError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)


@app.get("/api/rename/journal")
async def rename_journal_api(profile: str, limit: int = 200):
    """Return the rename journal entries (newest first) + batch groups.
    Read-only — used by the 📜 Renommages modal."""
    from fastapi.responses import JSONResponse

    from dashboard import rename as rename_mod
    return JSONResponse(rename_mod.get_journal(profile, limit=limit))


@app.post("/api/rename/undo/record")
async def rename_undo_record_api(request: Request):
    """Undo one rename. Body: {profile, ts, old, new}.
    The record must exist in the journal and both paths must be under
    the profile's target. Returns the inverse journal entry on success."""
    from fastapi.responses import JSONResponse

    from dashboard import rename as rename_mod
    body = await request.json()
    try:
        result = rename_mod.undo_single_rename(
            profile=body.get("profile"),
            ts=body.get("ts"),
            old_abs=body.get("old"),
            new_abs=body.get("new"),
        )
        return JSONResponse(result)
    except rename_mod.RenameError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)


@app.post("/api/rename/bulk")
async def rename_bulk_api(request: Request):
    """Apply N renames as a single batch.
    Body: {profile, items: [{rel_path, new_name}, ...], batch_id?}.
    Best-effort: per-item failures are surfaced in `errors[]`; the
    successful renames go through with a shared batch_id so they can
    be undone together via /api/rename/undo/batch."""
    from fastapi.responses import JSONResponse

    from dashboard import rename as rename_mod
    body = await request.json()
    try:
        result = rename_mod.commit_rename_bulk(
            profile=body.get("profile"),
            items=body.get("items") or [],
            batch_id=body.get("batch_id") or "",
        )
        return JSONResponse(result)
    except rename_mod.RenameError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)


@app.post("/api/rename/undo/batch")
async def rename_undo_batch_api(request: Request):
    """Reverse every rename tagged with `batch_id`.
    Body: {profile, batch_id}. Returns the per-record undone / errors
    summary from the journal."""
    from fastapi.responses import JSONResponse

    from dashboard import rename as rename_mod
    body = await request.json()
    try:
        result = rename_mod.undo_batch_for_profile(
            profile=body.get("profile"),
            batch_id=body.get("batch_id"),
        )
        return JSONResponse(result)
    except rename_mod.RenameError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)


@app.post("/api/rename/override")
async def rename_override_api(request: Request):
    """Set or clear user category overrides for the rename audit.
    Body: {profile, items: [{rel_path, category}], clear?}.
    When clear=true, drops the override for each rel_path (the
    category field is ignored). Otherwise sets the category — only
    `ok` is allowed in the MVP. Returns per-item successes + errors."""
    from fastapi.responses import JSONResponse

    from dashboard import rename as rename_mod
    body = await request.json()
    try:
        result = rename_mod.set_overrides(
            profile=body.get("profile"),
            items=body.get("items") or [],
            clear=bool(body.get("clear")),
        )
        return JSONResponse(result)
    except rename_mod.RenameError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)


@app.get("/api/categories/entry/files")
async def categories_entry_files_api(
    profile: str,
    group: str,
    chemin: str,
    limit: int = 50,
):
    """For a given category entry, return files matching its keywords
    (future) + files currently in its target folder (current)."""
    from fastapi.responses import JSONResponse

    from dashboard import categories
    limit = max(1, min(limit, 500))
    return JSONResponse(
        categories.entry_files(profile, group, chemin, limit=limit))


@app.get("/api/categories/dormant")
async def categories_dormant_api(profile: str):
    """Identify keywords + entries that no file's text could ever trigger.
    Phase C audit endpoint."""
    from fastapi.responses import JSONResponse

    from dashboard import categories
    return JSONResponse(categories.dormant_audit(profile))


@app.post("/api/categories/keywords/bulk-delete")
async def categories_keywords_bulk_delete_api(request: Request):
    """Delete multiple keywords in one transaction (one backup)."""
    from fastapi.responses import JSONResponse

    from dashboard import categories
    body = await request.json()
    profile = body.get("profile")
    if not profile:
        return JSONResponse({"error": "profile requis"}, status_code=400)
    try:
        return JSONResponse(categories.delete_keywords_bulk(
            profile, body.get("items") or [],
        ))
    except categories.CategoriesError as e:
        return _categories_err(e)


@app.post("/api/categories/entries/bulk-delete")
async def categories_entries_bulk_delete_api(request: Request):
    """Delete multiple entries in one transaction (one backup)."""
    from fastapi.responses import JSONResponse

    from dashboard import categories
    body = await request.json()
    profile = body.get("profile")
    if not profile:
        return JSONResponse({"error": "profile requis"}, status_code=400)
    try:
        return JSONResponse(categories.delete_entries_bulk(
            profile, body.get("items") or [],
        ))
    except categories.CategoriesError as e:
        return _categories_err(e)


@app.get("/api/taxonomy/profiles")
async def taxonomy_profiles_api():
    from fastapi.responses import JSONResponse
    return JSONResponse({"profiles": taxonomy.list_profiles()})


@app.get("/api/taxonomy/snapshot")
async def taxonomy_snapshot_api(profile: str, force: bool = False):
    from fastapi.responses import JSONResponse
    try:
        snap = taxonomy.get_snapshot(profile, force_reload=force)
    except Exception as exc:  # pragma: no cover — defensive
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse(snap)


@app.get("/api/taxonomy/folder/files")
async def taxonomy_files_api(
    profile: str,
    path: str = "",
    offset: int = 0,
    limit: int = 50,
):
    from fastapi.responses import JSONResponse
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    return JSONResponse(taxonomy.list_files_in_folder(profile, path, offset, limit))


@app.get("/api/taxonomy/theme/files")
async def taxonomy_theme_files_api(
    profile: str,
    theme: str,
    limit: int = 50,
):
    """For a given theme, return:
      - future: files in vision_cache that carry this theme (potential
                reclassify targets)
      - current: files actually in the folder mapped to this theme now
    """
    from fastapi.responses import JSONResponse
    if not theme.strip():
        return JSONResponse({"error": "theme requis"}, status_code=400)
    limit = max(1, min(limit, 500))
    return JSONResponse(taxonomy.theme_files(profile, theme, limit=limit))


@app.get("/api/taxonomy/reclassify/dryrun")
async def taxonomy_reclassify_dryrun_api(profile: str, sample: int = 50):
    """Project what would move at the next `klodo classify --execute`.
    Read-only — uses the cached vision results + today's mapping."""
    from fastapi.responses import JSONResponse
    sample = max(0, min(sample, 500))
    return JSONResponse(taxonomy.reclassify_dryrun(profile, sample_size=sample))


@app.get("/api/taxonomy/backups")
async def taxonomy_backups_list_api(profile: str):
    """List theme_mapping + tree backups, newest first."""
    from fastapi.responses import JSONResponse
    return JSONResponse(taxonomy.list_taxonomy_backups(profile))


@app.post("/api/taxonomy/backups/restore")
async def taxonomy_backups_restore_api(request: Request):
    """Restore a specific taxonomy backup by filename."""
    from fastapi.responses import JSONResponse
    body = await request.json()
    profile = body.get("profile")
    filename = body.get("filename")
    if not profile or not filename:
        return JSONResponse(
            {"error": "profile + filename requis"}, status_code=400,
        )
    try:
        return JSONResponse(taxonomy.restore_taxonomy_backup(profile, filename))
    except taxonomy.TaxonomyError as e:
        return JSONResponse({"error": str(e)}, status_code=e.status)


@app.get("/api/categories/backups")
async def categories_backups_list_api(profile: str):
    """List categories backups for the profile, newest first."""
    from fastapi.responses import JSONResponse

    from dashboard import categories
    return JSONResponse(categories.list_categories_backups(profile))


@app.post("/api/categories/backups/restore")
async def categories_backups_restore_api(request: Request):
    """Restore a specific categories backup by filename."""
    from fastapi.responses import JSONResponse

    from dashboard import categories
    body = await request.json()
    profile = body.get("profile")
    filename = body.get("filename")
    if not profile or not filename:
        return JSONResponse(
            {"error": "profile + filename requis"}, status_code=400,
        )
    try:
        return JSONResponse(
            categories.restore_categories_backup(profile, filename))
    except categories.CategoriesError as e:
        return _categories_err(e)


@app.get("/api/taxonomy/mapping-conflicts")
async def taxonomy_mapping_conflicts_api(profile: str):
    """Extended audit: substring eclipses + same-folder duplicates.
    Pure analysis, no writes."""
    from fastapi.responses import JSONResponse
    return JSONResponse(taxonomy.mapping_conflicts(profile))


@app.get("/api/taxonomy/dormant-mappings")
async def taxonomy_dormant_mappings_api(profile: str):
    """List mapping keys that no file's top theme resolves through.
    Removing them wouldn't change any file's classification."""
    from fastapi.responses import JSONResponse
    return JSONResponse(taxonomy.dormant_mappings(profile))


@app.post("/api/taxonomy/mappings/bulk-delete")
async def taxonomy_mappings_bulk_delete_api(request: Request):
    """Delete multiple mappings in a single transaction (one backup)."""
    from fastapi.responses import JSONResponse
    body = await request.json()
    profile = body.get("profile")
    keys = body.get("keys")
    if not profile:
        return JSONResponse({"error": "profile requis"}, status_code=400)
    try:
        return JSONResponse(taxonomy.delete_mappings_bulk(profile, keys or []))
    except taxonomy.TaxonomyError as e:
        return JSONResponse({"error": str(e)}, status_code=e.status)


@app.post("/api/taxonomy/mapping")
async def taxonomy_mapping_add_api(request: Request):
    from fastapi.responses import JSONResponse
    body = await request.json()
    profile = (body.get("profile") or "").strip()
    theme = body.get("theme") or ""
    folder = body.get("folder") or ""
    if not profile:
        return JSONResponse({"error": "profile manquant"}, status_code=400)
    try:
        result = taxonomy.add_mapping(profile, theme, folder)
    except taxonomy.TaxonomyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)
    return JSONResponse(result)


@app.patch("/api/taxonomy/mapping")
async def taxonomy_mapping_update_api(request: Request):
    from fastapi.responses import JSONResponse
    body = await request.json()
    profile = (body.get("profile") or "").strip()
    theme = body.get("theme") or ""
    folder = body.get("folder") or ""
    if not profile:
        return JSONResponse({"error": "profile manquant"}, status_code=400)
    try:
        result = taxonomy.update_mapping(profile, theme, folder)
    except taxonomy.TaxonomyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)
    return JSONResponse(result)


@app.delete("/api/taxonomy/mapping")
async def taxonomy_mapping_delete_api(request: Request):
    from fastapi.responses import JSONResponse
    body = await request.json()
    profile = (body.get("profile") or "").strip()
    theme = body.get("theme") or ""
    if not profile:
        return JSONResponse({"error": "profile manquant"}, status_code=400)
    try:
        result = taxonomy.delete_mapping(profile, theme)
    except taxonomy.TaxonomyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)
    return JSONResponse(result)


@app.post("/api/taxonomy/undo")
async def taxonomy_undo_api(request: Request):
    from fastapi.responses import JSONResponse
    body = await request.json()
    profile = (body.get("profile") or "").strip()
    if not profile:
        return JSONResponse({"error": "profile manquant"}, status_code=400)
    try:
        result = taxonomy.restore_last_backup(profile)
    except taxonomy.TaxonomyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)
    return JSONResponse(result)


@app.post("/api/taxonomy/folder")
async def taxonomy_folder_create_api(request: Request):
    from fastapi.responses import JSONResponse
    body = await request.json()
    profile = (body.get("profile") or "").strip()
    parent = body.get("parent") or ""
    name = body.get("name") or ""
    if not profile:
        return JSONResponse({"error": "profile manquant"}, status_code=400)
    try:
        result = taxonomy.create_folder(profile, parent, name)
    except taxonomy.TaxonomyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)
    return JSONResponse(result)


@app.patch("/api/taxonomy/folder")
async def taxonomy_folder_rename_api(request: Request):
    from fastapi.responses import JSONResponse
    body = await request.json()
    profile = (body.get("profile") or "").strip()
    old_path = body.get("path") or ""
    new_name = body.get("new_name") or ""
    if not profile:
        return JSONResponse({"error": "profile manquant"}, status_code=400)
    try:
        result = taxonomy.rename_folder(profile, old_path, new_name)
    except taxonomy.TaxonomyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)
    return JSONResponse(result)


@app.post("/api/taxonomy/folder/move")
async def taxonomy_folder_move_api(request: Request):
    """Move a folder under a different parent (basename unchanged)."""
    from fastapi.responses import JSONResponse
    body = await request.json()
    profile = (body.get("profile") or "").strip()
    old_path = body.get("path") or ""
    new_parent = body.get("new_parent") or ""
    if not profile:
        return JSONResponse({"error": "profile manquant"}, status_code=400)
    try:
        result = taxonomy.move_folder(profile, old_path, new_parent)
    except taxonomy.TaxonomyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)
    return JSONResponse(result)


@app.get("/api/taxonomy/folder/delete-preview")
async def taxonomy_folder_delete_preview_api(profile: str, path: str):
    """Read-only: what would `delete_folder(path)` remove ?"""
    from fastapi.responses import JSONResponse
    try:
        return JSONResponse(taxonomy.delete_folder_preview(profile, path))
    except taxonomy.TaxonomyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)


@app.delete("/api/taxonomy/folder")
async def taxonomy_folder_delete_api(request: Request):
    """Delete a folder (force=True required for non-empty)."""
    from fastapi.responses import JSONResponse
    body = await request.json()
    profile = (body.get("profile") or "").strip()
    path = body.get("path") or ""
    force = bool(body.get("force") or False)
    if not profile:
        return JSONResponse({"error": "profile manquant"}, status_code=400)
    try:
        result = taxonomy.delete_folder(profile, path, force=force)
    except taxonomy.TaxonomyError as exc:
        payload = {"error": str(exc)}
        # Surface the preview stats when refusing for non-empty so the
        # client can show its confirm modal without a second round-trip.
        if hasattr(exc, "preview"):
            payload["preview"] = exc.preview  # type: ignore[attr-defined]
        return JSONResponse(payload, status_code=exc.status)
    return JSONResponse(result)


@app.post("/api/taxonomy/mapping/preview")
async def taxonomy_mapping_preview_api(request: Request):
    """Dry-run: simulate the effect of an add/update/delete on the cache
    without writing anything. Returns counts + sample examples."""
    from fastapi.responses import JSONResponse
    body = await request.json()
    profile = (body.get("profile") or "").strip()
    theme = body.get("theme") or ""
    folder = body.get("folder")  # None for delete
    action = body.get("action") or "add"
    if not profile:
        return JSONResponse({"error": "profile manquant"}, status_code=400)
    try:
        result = taxonomy.preview_mapping_impact(profile, theme, folder, action)
    except taxonomy.TaxonomyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)
    return JSONResponse(result)


@app.get("/api/taxonomy/file/metadata")
async def taxonomy_file_metadata_api(profile: str, path: str):
    from fastapi.responses import JSONResponse
    meta = taxonomy.get_file_metadata(profile, path)
    meta["file"]["page_count_estimate"] = taxonomy.get_file_page_count(profile, path)
    return JSONResponse(meta)


@app.get("/api/taxonomy/file/thumbnail")
async def taxonomy_file_thumbnail_api(profile: str, path: str, page: int = 1):
    from fastapi.responses import FileResponse, JSONResponse
    img, mime = taxonomy.get_thumbnail(profile, path, page)
    if img is None:
        return JSONResponse({"error": mime or "indisponible"}, status_code=404)
    return FileResponse(img, media_type=mime or "image/jpeg")


@app.get("/api/taxonomy/file/full_pipeline")
async def taxonomy_file_full_pipeline_api(profile: str, path: str):
    """Recompute the prediction with the full classifier (KeywordClassifier
    included). Doesn't call the LLM Mapper to avoid token spending."""
    from fastapi.responses import JSONResponse
    return JSONResponse(taxonomy.get_file_metadata_full_pipeline(profile, path))


@app.post("/api/taxonomy/file/delete")
async def taxonomy_file_delete_api(request: Request):
    """Soft-delete a file: move it to <target>/.trash/<ts>/<basename>.
    Body: {profile, rel_path}. Reversible via Finder. Refuses to
    re-delete files already inside .trash/."""
    from fastapi.responses import JSONResponse
    body = await request.json()
    try:
        result = taxonomy.soft_delete_file(
            profile=body.get("profile"),
            rel_path=body.get("rel_path"),
        )
        return JSONResponse(result)
    except taxonomy.TaxonomyFileError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)


@app.post("/api/taxonomy/file/delete-bulk")
async def taxonomy_file_delete_bulk_api(request: Request):
    """Bulk soft-delete: move N files to <target>/.trash/<ts>/ in one
    call. Body: {profile, rel_paths: [...]}. Best-effort — per-item
    failures surface in errors[]. Single cache invalidation at the
    end so a 100-file deletion doesn't pay 100× the overhead."""
    from fastapi.responses import JSONResponse
    body = await request.json()
    try:
        result = taxonomy.soft_delete_bulk(
            profile=body.get("profile"),
            rel_paths=body.get("rel_paths") or [],
        )
        return JSONResponse(result)
    except taxonomy.TaxonomyFileError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)


@app.get("/api/taxonomy/file/move-impact")
async def taxonomy_file_move_impact_api(
    profile: str, path: str, dest: str,
):
    """Preview the impact of moving a file to ``dest``. Returns the
    classifier's predicted folder so the UI can warn the user if the
    chosen destination diverges (= the file would be proposed for
    return at the next reclassify)."""
    from fastapi.responses import JSONResponse
    try:
        result = taxonomy.compute_move_impact(profile, path, dest)
        return JSONResponse(result)
    except taxonomy.TaxonomyFileError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)


@app.post("/api/taxonomy/file/move")
async def taxonomy_file_move_api(request: Request):
    """Move a file to ``dest_folder`` (kept basename).
    Body: {profile, rel_path, dest_folder}. Invalidates the taxonomy /
    rename / categories caches so the next reads see the new state."""
    from fastapi.responses import JSONResponse
    body = await request.json()
    try:
        result = taxonomy.move_file(
            profile=body.get("profile"),
            rel_path=body.get("rel_path"),
            dest_folder=body.get("dest_folder"),
        )
        return JSONResponse(result)
    except taxonomy.TaxonomyFileError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)


# ════════════════════════════════════════════════════════════════════════
#  Agent Refonte — Phase A : Diagnostic
# ════════════════════════════════════════════════════════════════════════


@app.get("/agent/refonte")
async def agent_refonte_page(request: Request, profile: str = "default"):
    """Page dédiée à l'agent Refonte (Phase A — Diagnostic)."""
    available = [
        p["name"] if isinstance(p, dict) else p
        for p in data.get_available_profiles(include_all=True)
    ]
    if profile not in available:
        profile = available[0] if available else "default"
    runs = agent_refonte.list_runs(profile, limit=20)
    return templates.TemplateResponse(
        request,
        "agent_refonte.html",
        {
            "active": "agent_refonte",
            "profile": profile,
            "available_profiles": available,
            "runs": runs,
        },
    )


@app.post("/api/agent/refonte/diagnostic")
async def api_agent_refonte_start(request: Request):
    """Démarre un diagnostic en arrière-plan. Retourne run_id pour polling."""
    from fastapi.responses import JSONResponse
    body = await request.json()
    profile = (body.get("profile") or "").strip()
    max_llm_calls = int(body.get("max_llm_calls") or 5)
    if max_llm_calls < 1 or max_llm_calls > 20:
        return JSONResponse({"error": "max_llm_calls must be between 1 and 20"}, status_code=400)
    try:
        result = agent_refonte.start_diagnostic(profile, max_llm_calls=max_llm_calls)
        return JSONResponse(result)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except FileNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


@app.get("/api/agent/refonte/diagnostic/{run_id}")
async def api_agent_refonte_status(run_id: str, profile: str):
    """Lit le status d'un run (polling depuis le frontend)."""
    from fastapi.responses import JSONResponse
    if not profile:
        return JSONResponse({"error": "profile query param is required"}, status_code=400)
    status = agent_refonte.get_status(profile, run_id)
    if status is None:
        return JSONResponse({"error": f"run not found: {run_id}"}, status_code=404)
    return JSONResponse(status)


@app.get("/api/agent/refonte/runs")
async def api_agent_refonte_runs(profile: str, limit: int = 20):
    """Liste les runs récents pour un profil (utilisé par le sous-onglet Refonte
    de Taxonomie pour recharger l'historique sans full page reload)."""
    from fastapi.responses import JSONResponse
    if not profile:
        return JSONResponse({"error": "profile query param is required"}, status_code=400)
    limit = max(1, min(int(limit), 100))
    runs = agent_refonte.list_runs(profile, limit=limit)
    return JSONResponse({"profile": profile, "runs": runs})


@app.delete("/api/agent/refonte/runs/{run_id}")
async def api_agent_refonte_delete_run(run_id: str, profile: str):
    """Supprime un run (et tous ses artefacts) du cache local."""
    from fastapi.responses import JSONResponse
    if not profile:
        return JSONResponse({"error": "profile query param is required"}, status_code=400)
    try:
        result = agent_refonte.delete_run(profile, run_id)
        return JSONResponse(result)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except FileNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except agent_refonte.RunBusyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)


# ──────────── Phase B : Proposition ────────────


@app.get("/api/agent/refonte/proposition/{run_id}/tree-diff")
async def api_agent_refonte_tree_diff(run_id: str, profile: str):
    """Diff visuel du tree.yaml courant vs tree-proposed.yaml du run B."""
    from fastapi.responses import JSONResponse
    if not profile:
        return JSONResponse({"error": "profile query param is required"}, status_code=400)
    diff = agent_refonte.get_proposition_tree_diff(profile, run_id)
    return JSONResponse(diff)


@app.get("/api/agent/refonte/proposition/{run_id}/simulation")
async def api_agent_refonte_simulation(run_id: str, profile: str, sample_limit: int = 50):
    """Lit le simulation-summary.json + N premières lignes de la projection CSV."""
    from fastapi.responses import JSONResponse
    if not profile:
        return JSONResponse({"error": "profile query param is required"}, status_code=400)
    sample_limit = max(1, min(int(sample_limit), 500))
    result = agent_refonte.get_proposition_simulation(profile, run_id, sample_limit=sample_limit)
    if result is None:
        return JSONResponse(
            {"error": f"simulation not found for run {run_id} (probably Phase A run, or simulation failed)"},
            status_code=404,
        )
    return JSONResponse(result)


@app.post("/api/agent/refonte/proposition")
async def api_agent_refonte_proposition_start(request: Request):
    """Démarre un run Phase B basé sur un diagnostic Phase A existant.

    Body: {profile, diagnostic_run_id, max_llm_calls?}.
    """
    from fastapi.responses import JSONResponse
    body = await request.json()
    profile = (body.get("profile") or "").strip()
    diagnostic_run_id = (body.get("diagnostic_run_id") or "").strip()
    max_llm_calls = int(body.get("max_llm_calls") or 8)
    if max_llm_calls < 1 or max_llm_calls > 20:
        return JSONResponse({"error": "max_llm_calls must be between 1 and 20"}, status_code=400)
    try:
        result = agent_refonte.start_proposition(
            profile=profile,
            diagnostic_run_id=diagnostic_run_id,
            max_llm_calls=max_llm_calls,
        )
        return JSONResponse(result)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except FileNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
