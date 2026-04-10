#!/usr/bin/env python3
"""Klodo Functional Test Runner — reads tests.yaml, executes, reports."""
from __future__ import annotations

import argparse
import glob as G
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime

try:
    import yaml
except ImportError:
    print("ERROR: pip install pyyaml"); sys.exit(1)

# ── Paths ──────────────────────────────────────────────────────────────────
SD = os.path.dirname(os.path.abspath(__file__))
PR = os.path.dirname(os.path.dirname(SD))

# ── Load .env if present ──────────────────────────────────────────────────
_env_file = os.path.join(PR, ".env")
if os.path.exists(_env_file):
    with open(_env_file, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip())
TY = os.path.join(SD, "tests.yaml")
RD = os.path.join(SD, "reports")
TO = 600  # 10 minutes default timeout

# ── Display ────────────────────────────────────────────────────────────────
W = 60
CO = sys.stdout.isatty()

def _c(c, t): return f"\033[{c}m{t}\033[0m" if CO else t
def grn(t): return _c("32", t)
def red(t): return _c("31", t)
def ylw(t): return _c("33", t)
def cyn(t): return _c("36", t)
def bld(t): return _c("1", t)
def dim(t): return _c("2", t)
def sfmt(s): return {"pass": grn("✓ PASS"), "fail": red("✗ FAIL"), "skip": ylw("⊘ SKIP")}.get(s, s)
def hline(): print(f"  {'─' * W}")
def dline(): print(f"  {'═' * W}")
def banner(text): dline(); print(f"  {bld(text)}"); dline()
def dotfill(left, right, width=54):
    raw = re.sub(r'\033\[[0-9;]*m', '', left)
    dots = max(2, width - len(raw))
    return f"{left} {'·' * dots} {right}"

# ── Variables ──────────────────────────────────────────────────────────────

def resolve_vars(variables: dict, overrides: dict) -> dict:
    r = dict(variables); r.update(overrides)
    for _ in range(10):
        changed = False
        for k, v in r.items():
            nv = re.sub(r"\$\{(\w+)\}", lambda m: r.get(m.group(1), m.group(0)), v)
            if nv != v: r[k] = nv; changed = True
        if not changed: break
    for k, v in r.items():
        bad = re.findall(r"\$\{(\w+)\}", v)
        if bad: raise ValueError(f"Unresolved in {k}: {', '.join(bad)}")
    return r

def sub(text: str, v: dict) -> str:
    if not isinstance(text, str): return text
    return re.sub(r"\$\{(\w+)\}", lambda m: v.get(m.group(1), m.group(0)), text)

def sub_deep(obj, v: dict):
    if isinstance(obj, str): return sub(obj, v)
    if isinstance(obj, list): return [sub_deep(i, v) for i in obj]
    if isinstance(obj, dict): return {k: sub_deep(val, v) for k, val in obj.items()}
    return obj

# ── Dependencies ───────────────────────────────────────────────────────────

def resolve_deps(phases: list) -> tuple[dict, dict]:
    prov_map: dict[str, str] = {}
    req_map: dict[str, list[str]] = {}
    for ph in phases:
        for s in ph.get("series", []):
            sid = s["id"]
            for cap in s.get("provides", []): prov_map[cap] = sid
            req_map[sid] = s.get("requires", [])
    graph: dict[str, set[str]] = defaultdict(set)
    for sid, reqs in req_map.items():
        for r in reqs:
            if r in prov_map: graph[sid].add(prov_map[r])
    visited: set[str] = set()
    in_stack: set[str] = set()
    def dfs(node: str) -> str | None:
        if node in in_stack: return node
        if node in visited: return None
        visited.add(node); in_stack.add(node)
        for nb in graph.get(node, set()):
            c = dfs(nb)
            if c: return c
        in_stack.discard(node); return None
    for sid in req_map:
        c = dfs(sid)
        if c: raise ValueError(f"Circular dependency involving {c}")
    return req_map, prov_map

# ── Execution ──────────────────────────────────────────────────────────────

def run_cmd(cmd: str, timeout: int = TO) -> tuple[int, str, str, float]:
    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, cwd=PR)
        return r.returncode, r.stdout, r.stderr, (time.monotonic() - t0) * 1000
    except subprocess.TimeoutExpired:
        return -1, "", f"TIMEOUT after {timeout}s", (time.monotonic() - t0) * 1000

def run_cmds(cmds: list[str], label: str) -> bool:
    for cmd in cmds:
        print(f"  {dim('$')} {dim(cmd)}")
        code, _, stderr, _ = run_cmd(cmd)
        if code != 0:
            print(f"  {red('✗')} {label}: exit {code}")
            if stderr.strip():
                for line in stderr.strip().split("\n")[:3]:
                    print(f"    {dim(line)}")
            return False
    return True

# ── Assertions ─────────────────────────────────────────────────────────────

def evaluate(assertion: dict, exit_code: int, stdout: str, stderr: str,
             duration_ms: float, interactive: bool) -> tuple[str, str]:
    atype = assertion.get("type", "exit_code")

    if atype == "output_equals":
        expected = str(assertion["expected"]); actual = stdout.strip()
        return ("pass", "") if actual == expected else ("fail", f"Expected '{expected}', got '{actual}'")

    if atype == "output_contains":
        missing = [s for s in assertion["expected"] if s not in stdout]
        return ("pass", "") if not missing else ("fail", f"Missing: {missing}")

    if atype == "output_not_contains":
        found = [s for s in assertion["expected"] if s in stdout]
        return ("pass", "") if not found else ("fail", f"Found (unexpected): {found}")

    if atype == "exit_code":
        expected = int(assertion["expected"])
        return ("pass", "") if exit_code == expected else ("fail", f"Exit {exit_code} != {expected}")

    if atype == "file_exists":
        matches = G.glob(assertion["pattern"])
        return ("pass", f"Found: {matches[0]}") if matches else ("fail", f"No match: {assertion['pattern']}")

    if atype == "file_not_exists":
        matches = G.glob(assertion["pattern"])
        return ("pass", "") if not matches else ("fail", f"Exists: {matches}")

    if atype == "line_count":
        fp, op, value = assertion["file"], assertion["op"], int(assertion["value"])
        try:
            with open(fp) as fh: count = sum(1 for _ in fh)
        except FileNotFoundError: return "fail", f"File not found: {fp}"
        ops = {">": count > value, "<": count < value, ">=": count >= value,
               "<=": count <= value, "==": count == value}
        return ("pass", f"{count} {op} {value}") if ops.get(op) else ("fail", f"{count} not {op} {value}")

    if atype == "manual_check":
        if not interactive: return "skip", "Manual (auto mode)"
        print(f"\n  │  {cyn('Manual:')} {assertion.get('prompt', '?')}")
        while True:
            ans = input(f"  │  [{grn('P')}]ass / [{red('F')}]ail / [{ylw('S')}]kip ? ").strip().lower()
            if ans in ("p", ""): return "pass", "User confirmed"
            if ans == "f": return "fail", "User rejected"
            if ans == "s": return "skip", "User skipped"

    if atype == "duration_under":
        mx = float(assertion["seconds"]); ac = duration_ms / 1000
        return ("pass", f"{ac:.1f}s < {mx}s") if ac < mx else ("fail", f"{ac:.1f}s >= {mx}s (too slow)")

    return "fail", f"Unknown assertion type: {atype}"

# ── Reports ────────────────────────────────────────────────────────────────

def write_json(report: dict, path: str):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

def write_md(report: dict, path: str):
    s = report["summary"]; dur = report["duration_seconds"]; m, sc = divmod(int(dur), 60)
    lines = [
        f"# Rapport de tests fonctionnels — {report['project']}",
        f"**Date :** {report['run_at'][:16]}  |  **Duree :** {m}m {sc:02d}s", "",
        "## Resume", "| Total | Pass | Fail | Skip |", "|-------|------|------|------|",
        f"| {s['total']} | {s['pass']} | {s['fail']} | {s['skip']} |", "",
    ]
    failures: list[dict] = []
    for ph in report["phases"]:
        lines.append(f"## {ph['name']}")
        for sr in ph["series"]:
            pc = sum(1 for c in sr["checks"] if c["status"] == "pass")
            tc = len(sr["checks"])
            lines.append(f"### {sr['id']} — {sr['name']} : {sr['status'].upper()} ({pc}/{tc})")
            lines.append("| # | Check | Status | Detail |"); lines.append("|---|-------|--------|--------|")
            for c in sr["checks"]:
                detail = c.get("error", c.get("detail", "—")) or "—"
                lines.append(f"| {c['id']} | {c['description']} | {c['status'].upper()} | {detail} |")
                if c["status"] == "fail": failures.append(c)
            lines.append("")
    if failures:
        lines.append("## Echecs detailles")
        for c in failures:
            lines.append(f"### {c['id']} — {c['description']}")
            lines.append(f"- **Commande :** `{c.get('command', '?')}`")
            if "expected" in c: lines.append(f"- **Attendu :** `{c['expected']}`")
            output = c.get("output", "").strip()
            if output: lines.append(f"- **Obtenu :** `{output[:500]}`")
            lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

# ── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Klodo Functional Test Runner")
    parser.add_argument("--interactive", action="store_true", help="Step-by-step mode")
    parser.add_argument("--phase", nargs="+", type=int, default=None, help="Phase numbers")
    parser.add_argument("--series", nargs="+", default=None, help="Series IDs")
    parser.add_argument("--priority", nargs="+", default=None, help="Priorities")
    parser.add_argument("--var", action="append", default=[], help="Override KEY=VALUE")
    parser.add_argument("--dry-run", action="store_true", help="Show plan only")
    parser.add_argument("--rerun-failures", action="store_true",
                        help="Rerun only FAIL and SKIP series from the latest report")
    parser.add_argument("--no-history", action="store_true",
                        help="Do not save results to DuckDB")
    parser.add_argument("--label", default=None,
                        help="Custom label for this run (default: random name)")
    args = parser.parse_args()

    if not os.path.exists(TY):
        print(f"{red('ERROR')}: {TY} not found."); sys.exit(1)
    with open(TY, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    # --rerun-failures: find latest report, extract FAIL + SKIP series
    if args.rerun_failures:
        # Find latest report: check "latest" symlink or scan run_* dirs
        latest_link = os.path.join(RD, "latest", "report.json")
        run_dirs = sorted(G.glob(os.path.join(RD, "run_*", "report.json")), reverse=True)
        # Also check old-style flat reports for backward compat
        old_reports = sorted(G.glob(os.path.join(RD, "report_*.json")), reverse=True)
        reports = run_dirs + old_reports
        if os.path.exists(latest_link):
            reports.insert(0, latest_link)
        if not reports:
            print(f"{red('ERROR')}: No previous report found in {RD}"); sys.exit(1)
        with open(reports[0], encoding="utf-8") as f:
            prev = json.load(f)
        rerun_ids: list[str] = []
        for ph in prev.get("phases", []):
            for sr in ph.get("series", []):
                if sr.get("status") in ("fail", "skip"):
                    rerun_ids.append(sr["id"])
        if not rerun_ids:
            print(f"{grn('Nothing to rerun')} — all series passed in {os.path.basename(reports[0])}")
            sys.exit(0)
        print(f"  {cyn('Rerunning')} {len(rerun_ids)} series (fail+skip) from {os.path.basename(reports[0])}")
        print(f"  Series: {', '.join(rerun_ids)}\n")
        args.series = rerun_ids

    # Variables
    overrides: dict[str, str] = {}
    for v in args.var:
        if "=" not in v: print(f"{red('ERROR')}: Bad --var: {v}"); sys.exit(1)
        k, val = v.split("=", 1); overrides[k] = val
    try: variables = resolve_vars(data.get("variables", {}), overrides)
    except ValueError as e: print(f"{red('ERROR')}: {e}"); sys.exit(1)

    phases = sub_deep(data.get("phases", []), variables)
    session = sub_deep(data.get("session", {}), variables)
    custom = sub_deep(data.get("custom", []) or [], variables)
    try: req_map, prov_map = resolve_deps(phases)
    except ValueError as e: print(f"{red('ERROR')}: {e}"); sys.exit(1)

    # Filters
    pf = set(args.phase) if args.phase else None
    sf = set(args.series) if args.series else None
    prf = set(args.priority) if args.priority else None
    total_s = total_c = 0
    for i, ph in enumerate(phases):
        if pf and i not in pf: continue
        pri = ph.get("priority", "moyenne")
        for s in ph.get("series", []):
            if sf and s["id"] not in sf: continue
            if prf and pri not in prf: continue
            total_s += 1; total_c += len(s.get("checks", []))

    mode = "interactive" if args.interactive else "dry-run" if args.dry_run else "auto"
    print()
    banner(f"Functional Tests — {data.get('project', '?')} ({mode})")
    print(f"  {total_s} séries, {total_c} checks")
    print(f"  Généré le {data.get('generated_at', '?')[:10]}")
    if mode == "auto": print(f"  {dim('Manual checks → skip')}")
    print()

    # ── DRY RUN ────────────────────────────────────────────────────────
    if args.dry_run:
        pre = session.get("pre_run", [])
        if pre:
            print(f"  {dim('Session pre_run:')}")
            for c in pre: print(f"    {dim('$')} {c}")
            print()
        for i, ph in enumerate(phases):
            if pf and i not in pf: continue
            pri = ph.get("priority", "moyenne")
            pname = ph.get("name", f"Phase {i}")
            hline(); print(f"  {cyn(f'Phase {i} — {pname}')} [{pri}]"); hline()
            for c in ph.get("pre_run", []):
                print(f"    {dim('phase pre_run: $')} {c}")
            for s in ph.get("series", []):
                if sf and s["id"] not in sf: continue
                if prf and pri not in prf: continue
                reqs = s.get("requires", []); provs = s.get("provides", [])
                print(f"\n  ┌─ {bld(s['id'])} — {s['name']}")
                if reqs: print(f"  │  {dim('requires: ' + ', '.join(reqs))}")
                if provs: print(f"  │  {dim('provides: ' + ', '.join(provs))}")
                for c in s.get("pre_run", []):
                    print(f"  │  {dim('pre_run:')} $ {c}")
                for c in s.get("setup", []):
                    print(f"  │  {dim('setup: ')} $ {c}")
                print(f"  │")
                for ck in s.get("checks", []):
                    tag = f" {ylw('[manual]')}" if ck.get("mode") == "manual_check" else ""
                    print(f"  │  [{ck['id']}] {ck['description']}{tag}")
                    cmd = ck.get("command", "")
                    if cmd and cmd != "echo check":
                        print(f"  │          {dim('$')} {cmd}")
                    a = ck.get("assert", {})
                    atype = a.get("type", "")
                    if atype and atype != "manual_check":
                        exp = a.get("expected", a.get("pattern", a.get("seconds", "")))
                        print(f"  │          {dim(f'assert: {atype} → {exp}')}")
                print(f"  └─")
            print()
        post = session.get("post_run", [])
        if post:
            print(f"  {dim('Session post_run:')}")
            for c in post: print(f"    {dim('$')} {c}")
        print()
        banner("DRY RUN complete — nothing executed.")
        print()
        return

    # ── EXECUTION ──────────────────────────────────────────────────────
    t0 = time.monotonic()
    rpt_phases: list[dict] = []
    sm: dict[str, int] = {"total": 0, "pass": 0, "fail": 0, "skip": 0}
    caps: set[str] = set()
    failed: set[str] = set()
    auto_close = False

    # Session pre_run
    pre_cmds = session.get("pre_run", [])
    if pre_cmds:
        print(f"  {dim('Session pre_run...')}")
        if not run_cmds(pre_cmds, "session.pre_run"):
            print(f"\n  {red('ABORT')}: session.pre_run failed."); sys.exit(1)
        print(f"  {grn('✓')} Session ready\n")

    for pi, ph in enumerate(phases):
        if pf and pi not in pf: continue
        pname = ph.get("name", f"Phase {pi}"); ppri = ph.get("priority", "moyenne")
        ph_rpt: dict = {"id": ph.get("id", f"phase_{pi}"), "name": pname, "series": []}
        hline(); print(f"  {cyn(f'Phase {pi} — {pname}')} [{ppri}]"); hline()

        if ph.get("pre_run"):
            print(f"\n  {dim('Phase pre_run:')}")
            if not run_cmds(ph["pre_run"], "phase.pre_run"):
                print(f"  {ylw('⊘ SKIP')}: phase pre_run failed")
                for s in ph.get("series", []):
                    for _ in s.get("checks", []): sm["total"] += 1; sm["skip"] += 1
                rpt_phases.append(ph_rpt)
                continue

        for s in ph.get("series", []):
            sid = s["id"]; sname = s.get("name", sid)
            if sf and sid not in sf: continue
            if prf and ppri not in prf: continue
            sr: dict = {"id": sid, "name": sname, "status": "pass", "duration_ms": 0, "checks": []}
            print(f"\n  ┌─ {bld(sid)} — {sname}")

            # Check requires
            reqs = s.get("requires", [])
            unmet = [r for r in reqs if r in prov_map and prov_map[r] in failed]
            missing = [r for r in reqs if r not in caps and r in prov_map]
            if unmet or missing:
                reason = unmet or missing
                print(f"  │  {ylw('⊘ SKIP')}: unmet dependency {reason}")
                sr["status"] = "skip"
                for ck in s.get("checks", []):
                    sm["total"] += 1; sm["skip"] += 1
                    sr["checks"].append({"id": ck["id"], "description": ck.get("description", ""),
                                         "status": "skip", "detail": f"Unmet: {reason}"})
                print(f"  └─ {bld(sid)}: {sfmt('skip')}")
                ph_rpt["series"].append(sr); failed.add(sid); continue

            st = time.monotonic()
            logs_before = set(G.glob(os.path.join(PR, "logs", "*")))

            # Create structured log directory early (signals which test is running)
            phase_id = ph.get("id", f"phase_{pi}")
            series_log_dir = os.path.join(SD, "logs", phase_id, sid)
            os.makedirs(series_log_dir, exist_ok=True)
            # Write a marker file so the dashboard knows this test is running
            with open(os.path.join(series_log_dir, ".running"), "w") as f:
                f.write(sid)

            # Series pre_run
            if s.get("pre_run"):
                if not run_cmds(s["pre_run"], f"{sid}.pre_run"):
                    print(f"  │  {ylw('⊘ SKIP')}: pre_run failed")
                    sr["status"] = "skip"
                    for ck in s.get("checks", []):
                        sm["total"] += 1; sm["skip"] += 1
                        sr["checks"].append({"id": ck["id"], "description": ck.get("description", ""),
                                             "status": "skip", "detail": "pre_run failed"})
                    print(f"  └─ {bld(sid)}: {sfmt('skip')}")
                    ph_rpt["series"].append(sr); failed.add(sid); continue

            # Setup — capture output for checks that reference setup logs
            series_timeout = s.get("timeout", TO)
            setup_output_file = os.path.join(PR, "logs", f".setup_output_{sid}.txt")
            if s.get("setup"):
                all_output: list[str] = []
                for cmd in s["setup"]:
                    print(f"  │  {dim('setup: $')} {dim(cmd[:100])}", flush=True)
                    t_setup = time.monotonic()
                    code, stdout, stderr, ms = run_cmd(cmd, timeout=series_timeout)
                    elapsed = time.monotonic() - t_setup
                    if code != 0:
                        print(f"  │  {red('✗')} setup exit {code} ({elapsed:.0f}s)")
                        if stderr.strip():
                            for line in stderr.strip().split("\n")[:3]:
                                print(f"  │    {dim(line)}")
                    else:
                        print(f"  │  {grn('✓')} setup done ({elapsed:.0f}s)")
                    all_output.append(stdout)
                    all_output.append(stderr)
                os.makedirs(os.path.dirname(setup_output_file), exist_ok=True)
                with open(setup_output_file, "w", encoding="utf-8") as f:
                    f.write("\n".join(all_output))

            # Checks
            has_fail = False
            for ck in s.get("checks", []):
                cid = ck["id"]; desc = ck.get("description", "")
                chk_mode = ck.get("mode", "auto"); cmd = ck.get("command", "")
                assertion = ck.get("assert", {"type": "exit_code", "expected": 0})
                sm["total"] += 1
                cr: dict = {"id": cid, "description": desc, "status": "skip",
                            "command": cmd, "output": "", "duration_ms": 0}

                if chk_mode == "manual_check" and not args.interactive:
                    sm["skip"] += 1; cr["detail"] = "Manual (auto mode)"
                    print(f"  │  {dotfill(f'[{cid}] {desc}', ylw('⊘ SKIP (manual)'))}")
                    sr["checks"].append(cr); continue

                ec, stdout, stderr, ms = run_cmd(cmd)
                cr["output"] = stdout[:2000]; cr["duration_ms"] = ms
                status, detail = evaluate(assertion, ec, stdout, stderr, ms, args.interactive)
                cr["status"] = status
                if status == "fail":
                    cr["error"] = detail
                    cr["expected"] = str(assertion.get("expected",
                                         assertion.get("pattern", assertion.get("seconds", "?"))))
                else:
                    cr["detail"] = detail
                sm[status] += 1

                if status == "pass":
                    print(f"  │  {dotfill(f'[{cid}] {desc}', grn('✓ PASS'))}")
                elif status == "fail":
                    print(f"  │  {dotfill(f'[{cid}] {desc}', red('✗ FAIL'))}")
                    print(f"  │          {dim(detail)}")
                    has_fail = True
                else:
                    print(f"  │  {dotfill(f'[{cid}] {desc}', ylw('⊘ SKIP'))}")
                sr["checks"].append(cr)

            # Series post_run
            if s.get("post_run"): run_cmds(s["post_run"], f"{sid}.post_run")

            sr["duration_ms"] = (time.monotonic() - st) * 1000
            if has_fail:
                sr["status"] = "fail"; failed.add(sid)
            elif any(c["status"] == "skip" for c in sr["checks"]) and not any(c["status"] == "fail" for c in sr["checks"]):
                has_pass = any(c["status"] == "pass" for c in sr["checks"])
                sr["status"] = "pass" if has_pass else "skip"
                if sr["status"] == "pass":
                    for cap in s.get("provides", []): caps.add(cap)
            else:
                sr["status"] = "pass"
                for cap in s.get("provides", []): caps.add(cap)

            # Capture new log files produced during this series
            logs_after = set(G.glob(os.path.join(PR, "logs", "*")))
            new_logs = sorted(logs_after - logs_before)

            # Move logs to structured directory (already created above)
            # Remove running marker
            running_marker = os.path.join(series_log_dir, ".running")
            if os.path.exists(running_marker):
                os.remove(running_marker)

            if new_logs:
                import shutil
                structured_logs = []
                for lf in new_logs:
                    dest = os.path.join(series_log_dir, os.path.basename(lf))
                    shutil.move(lf, dest)
                    structured_logs.append(os.path.relpath(dest, PR))
                sr["logs"] = structured_logs

            # Move setup output to structured dir
            if os.path.exists(setup_output_file):
                import shutil
                shutil.move(setup_output_file, os.path.join(series_log_dir, ".setup_output.txt"))

            ph_rpt["series"].append(sr)

            pc = sum(1 for c in sr["checks"] if c["status"] == "pass")
            tc_s = len(sr["checks"])
            print(f"  └─ {bld(sid)}: {sfmt(sr['status'])} ({pc}/{tc_s})")
            if new_logs:
                for lf in new_logs:
                    print(f"       {dim('📄 ' + os.path.relpath(lf, PR))}")

            # Interactive mode
            if args.interactive:
                gh = s.get("github_issue")
                if gh:
                    if sr["status"] == "pass":
                        if auto_close:
                            subprocess.run(
                                f'gh issue close {gh} --repo wals110/Manage-Biblio '
                                f'--comment "PASS: {sname} ({pc}/{tc_s})"',
                                shell=True, capture_output=True, cwd=PR)
                            print(f"  Issue #{gh} → auto-closed")
                        else:
                            ans = input(f"  Issue #{gh} → Fermer ? [Y/N/A] ").strip().lower()
                            if ans in ("y", ""):
                                subprocess.run(
                                    f'gh issue close {gh} --repo wals110/Manage-Biblio '
                                    f'--comment "PASS: {sname} ({pc}/{tc_s})"',
                                    shell=True, capture_output=True, cwd=PR)
                            elif ans == "a":
                                auto_close = True
                                subprocess.run(
                                    f'gh issue close {gh} --repo wals110/Manage-Biblio '
                                    f'--comment "PASS: {sname} ({pc}/{tc_s})"',
                                    shell=True, capture_output=True, cwd=PR)
                    elif sr["status"] == "fail":
                        ans = input(f"  Issue #{gh} → Poster erreur ? [Y/N] ").strip().lower()
                        if ans in ("y", ""):
                            fd = [c for c in sr["checks"] if c["status"] == "fail"]
                            body = f"FAIL: {sname}\\n" + "\\n".join(
                                f"- {c['id']}: {c.get('error', '?')}" for c in fd)
                            subprocess.run(
                                f'gh issue comment {gh} --repo wals110/Manage-Biblio --body "{body}"',
                                shell=True, capture_output=True, cwd=PR)
                ans = input(f"  Continuer ? [Y]es / [S]kip / [Q]uit ? ").strip().lower()
                if ans in ("q", "quit"):
                    print(f"\n  {ylw('Quit.')}"); break

        if ph.get("post_run"): run_cmds(ph["post_run"], "phase.post_run")
        rpt_phases.append(ph_rpt)

    # Session post_run
    post_cmds = session.get("post_run", [])
    if post_cmds:
        print(f"\n  {dim('Session post_run...')}")
        run_cmds(post_cmds, "session.post_run")

    # Reports — organized in per-run directories
    dur = time.monotonic() - t0; ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report = {"project": data.get("project", "?"), "run_at": datetime.now().isoformat(),
              "duration_seconds": round(dur), "variables": variables, "summary": sm, "phases": rpt_phases}
    run_dir = os.path.join(RD, f"run_{ts}")
    os.makedirs(run_dir, exist_ok=True)
    jp = os.path.join(run_dir, "report.json"); mp = os.path.join(run_dir, "report.md")
    write_json(report, jp); write_md(report, mp)
    # Update "latest" symlink
    latest = os.path.join(RD, "latest")
    if os.path.islink(latest):
        os.remove(latest)
    os.symlink(f"run_{ts}", latest)

    # Save to DuckDB (long-term tracking)
    if args.no_history:
        print(f"  {dim('Historique : désactivé (--no-history)')}")
    else:
        # Determine run type
        if args.series:
            run_type = f"series_{','.join(args.series)}"
        elif args.phase:
            run_type = f"phase_{'_'.join(str(p) for p in args.phase)}"
        elif args.rerun_failures:
            run_type = "rerun_failures"
        else:
            run_type = "full"

        try:
            from db import insert_run, generate_run_name
            label = args.label or generate_run_name()
            insert_run(report, f"run_{ts}", run_type, label=label)
            print(f"  {dim(f'Historique : {label} (run_{ts})')}")
        except Exception as e:
            print(f"  {ylw(f'Historique : erreur DuckDB — {e}')}")

    # Summary
    m, sc = divmod(int(dur), 60)
    print()
    banner("Résultats")
    print(f"  Total : {sm['total']}")
    print(f"  {grn('Pass')}  : {sm['pass']}")
    print(f"  {red('Fail')}  : {sm['fail']}")
    print(f"  {ylw('Skip')}  : {sm['skip']}")
    print(f"  Durée : {m}m {sc:02d}s")
    dline()
    print(f"\n  Rapports:")
    print(f"    {dim(jp)}")
    print(f"    {dim(mp)}")
    print()
    sys.exit(1 if sm["fail"] > 0 else 0)


if __name__ == "__main__": main()
