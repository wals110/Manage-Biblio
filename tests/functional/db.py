"""DuckDB backend for functional test results."""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import duckdb

DB_PATH = Path(__file__).parent / "results.db"


def get_connection() -> duckdb.DuckDBPyConnection:
    """Get a DuckDB connection, creating tables if needed."""
    con = duckdb.connect(str(DB_PATH))
    con.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            date TEXT,
            duration INTEGER,
            total INTEGER,
            pass INTEGER,
            fail INTEGER,
            skip INTEGER,
            rate REAL,
            run_type TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS series_results (
            run_id TEXT,
            series_id TEXT,
            name TEXT,
            status TEXT,
            duration_ms REAL,
            phase_id TEXT,
            PRIMARY KEY (run_id, series_id)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS check_results (
            run_id TEXT,
            check_id TEXT,
            series_id TEXT,
            description TEXT,
            status TEXT,
            error TEXT,
            duration_ms REAL,
            PRIMARY KEY (run_id, check_id)
        )
    """)
    return con


def insert_run(report: dict, run_id: str, run_type: str = "full"):
    """Insert a complete run with all series and checks."""
    con = get_connection()
    sm = report.get("summary", {})
    total = sm.get("total", 0)
    pass_count = sm.get("pass", 0)

    # Insert run
    con.execute("""
        INSERT OR REPLACE INTO runs (id, date, duration, total, pass, fail, skip, rate, run_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        run_id,
        report.get("run_at", ""),
        report.get("duration_seconds", 0),
        total,
        pass_count,
        sm.get("fail", 0),
        sm.get("skip", 0),
        round(pass_count / total * 100, 1) if total else 0.0,
        run_type,
    ])

    # Insert series and checks
    for ph in report.get("phases", []):
        phase_id = ph.get("id", "")
        for sr in ph.get("series", []):
            sid = sr.get("id", "")
            con.execute("""
                INSERT OR REPLACE INTO series_results (run_id, series_id, name, status, duration_ms, phase_id)
                VALUES (?, ?, ?, ?, ?, ?)
            """, [run_id, sid, sr.get("name", ""), sr.get("status", ""), sr.get("duration_ms", 0), phase_id])

            for ck in sr.get("checks", []):
                con.execute("""
                    INSERT OR REPLACE INTO check_results (run_id, check_id, series_id, description, status, error, duration_ms)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, [
                    run_id, ck.get("id", ""), sid,
                    ck.get("description", ""), ck.get("status", ""),
                    ck.get("error", ck.get("detail", "")),
                    ck.get("duration_ms", 0),
                ])

    con.close()


def get_runs(limit: int = 50) -> list[dict]:
    """Get recent runs sorted by date desc."""
    con = get_connection()
    result = con.execute("""
        SELECT id, date, duration, total, pass, fail, skip, rate, run_type
        FROM runs ORDER BY date DESC LIMIT ?
    """, [limit]).fetchall()
    con.close()
    cols = ["id", "date", "duration", "total", "pass", "fail", "skip", "rate", "run_type"]
    return [dict(zip(cols, row)) for row in result]


def get_series_history(series_id: str, limit: int = 20) -> list[dict]:
    """Get history of a specific series across runs."""
    con = get_connection()
    result = con.execute("""
        SELECT r.id, r.date, s.status, s.duration_ms
        FROM series_results s
        JOIN runs r ON s.run_id = r.id
        WHERE s.series_id = ?
        ORDER BY r.date DESC LIMIT ?
    """, [series_id, limit]).fetchall()
    con.close()
    return [{"run_id": r[0], "date": r[1], "status": r[2], "duration_ms": r[3]} for r in result]


def get_check_history(check_id: str, limit: int = 20) -> list[dict]:
    """Get history of a specific check across runs."""
    con = get_connection()
    result = con.execute("""
        SELECT r.id, r.date, c.status, c.error, c.duration_ms
        FROM check_results c
        JOIN runs r ON c.run_id = r.id
        WHERE c.check_id = ?
        ORDER BY r.date DESC LIMIT ?
    """, [check_id, limit]).fetchall()
    con.close()
    return [{"run_id": r[0], "date": r[1], "status": r[2], "error": r[3], "duration_ms": r[4]} for r in result]


def get_failing_series(min_fails: int = 2) -> list[dict]:
    """Get series that fail frequently across runs."""
    con = get_connection()
    result = con.execute("""
        SELECT series_id, name,
               COUNT(*) as total_runs,
               SUM(CASE WHEN status = 'fail' THEN 1 ELSE 0 END) as fail_count,
               SUM(CASE WHEN status = 'pass' THEN 1 ELSE 0 END) as pass_count
        FROM series_results
        GROUP BY series_id, name
        HAVING fail_count >= ?
        ORDER BY fail_count DESC
    """, [min_fails]).fetchall()
    con.close()
    return [{"series_id": r[0], "name": r[1], "total_runs": r[2], "fail_count": r[3], "pass_count": r[4]} for r in result]


def query_csv(csv_pattern: str, sql: str | None = None) -> list[dict]:
    """Query CSV files directly with DuckDB.

    Example: query_csv('logs/rapport_classify_*.csv',
                        "SELECT theme_detecte, COUNT(*) as n GROUP BY 1 ORDER BY 2 DESC LIMIT 10")
    """
    con = get_connection()
    if sql:
        full_sql = f"SELECT * FROM read_csv_auto('{csv_pattern}') {sql}"
    else:
        full_sql = f"SELECT * FROM read_csv_auto('{csv_pattern}') LIMIT 100"
    try:
        result = con.execute(full_sql)
        cols = [desc[0] for desc in result.description]
        rows = result.fetchall()
        con.close()
        return [dict(zip(cols, row)) for row in rows]
    except Exception:
        con.close()
        return []


def import_history_json(history_path: str):
    """Import existing history.json into DuckDB (migration)."""
    if not os.path.exists(history_path):
        return 0
    try:
        with open(history_path, encoding="utf-8") as f:
            history = json.load(f)
    except (json.JSONDecodeError, OSError):
        return 0

    con = get_connection()
    count = 0
    for entry in history:
        run_id = entry.get("report", f"legacy_{count}")
        try:
            con.execute("""
                INSERT OR IGNORE INTO runs (id, date, duration, total, pass, fail, skip, rate, run_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                run_id,
                entry.get("date", ""),
                entry.get("duration", 0),
                entry.get("total", 0),
                entry.get("pass", 0),
                entry.get("fail", 0),
                entry.get("skip", 0),
                entry.get("rate", 0),
                entry.get("run_type", "unknown"),
            ])
            count += 1
        except Exception:
            continue
    con.close()
    return count
