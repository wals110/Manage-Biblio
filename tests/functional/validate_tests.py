#!/usr/bin/env python3
"""Validate and auto-fix tests/functional/tests.yaml."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: pip install pyyaml"); sys.exit(2)

# ── Paths ──────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
TESTS_YAML = SCRIPT_DIR / "tests.yaml"
PROJECT_ROOT = SCRIPT_DIR.parent.parent

# ── Colors ─────────────────────────────────────────────────
CO = sys.stdout.isatty()
def _c(code, text): return f"\033[{code}m{text}\033[0m" if CO else str(text)
def grn(t): return _c("32", t)
def red(t): return _c("31", t)
def ylw(t): return _c("33", t)
def bld(t): return _c("1", t)
def dim(t): return _c("2", t)


# ── Data classes ───────────────────────────────────────────
class Finding:
    def __init__(self, rule: str, location: str, message: str,
                 severity: str = "error", before: str = "", after: str = ""):
        self.rule = rule
        self.location = location
        self.message = message
        self.severity = severity  # "fix", "error", "warning", "info"
        self.before = before
        self.after = after


# ── YAML loading ───────────────────────────────────────────
def load_yaml(path: Path) -> dict:
    if not path.exists():
        print(f"{red('ERROR')}: {path} not found")
        sys.exit(2)
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def save_yaml(data: dict, path: Path):
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


# ── Helpers ────────────────────────────────────────────────
def iter_commands(data: dict):
    """Yield (location_str, command_str, container_obj, key) for all commands."""
    # Session pre_run / post_run
    for section in ("pre_run", "post_run"):
        for i, cmd in enumerate(data.get("session", {}).get(section, [])):
            yield f"session.{section}[{i}]", cmd, data["session"][section], i

    # Phases
    for ph in data.get("phases", []):
        pid = ph.get("id", "?")
        for section in ("pre_run", "post_run"):
            for i, cmd in enumerate(ph.get(section, [])):
                yield f"{pid}.{section}[{i}]", cmd, ph[section], i

        for sr in ph.get("series", []):
            sid = sr.get("id", "?")
            for section in ("pre_run", "post_run"):
                for i, cmd in enumerate(sr.get(section, [])):
                    yield f"{sid}.{section}[{i}]", cmd, sr[section], i
            for i, cmd in enumerate(sr.get("setup", [])):
                yield f"{sid}.setup[{i}]", cmd, sr["setup"], i
            for ck in sr.get("checks", []):
                cid = ck.get("id", "?")
                cmd = ck.get("command", "")
                if cmd:
                    yield f"{cid}.command", cmd, ck, "command"

    # Custom
    for sr in data.get("custom", []) or []:
        sid = sr.get("id", "?")
        for ck in sr.get("checks", []):
            cid = ck.get("id", "?")
            cmd = ck.get("command", "")
            if cmd:
                yield f"{cid}.command", cmd, ck, "command"


# ── Runtime file patterns (don't flag these) ───────────────
RUNTIME_PATTERNS = {".setup_output_", ".bench_", "report_"}

def is_runtime_path(path: str) -> bool:
    return any(p in path for p in RUNTIME_PATTERNS)


# ════════════════════════════════════════════════════════════
#  RULES — Auto-fixable
# ════════════════════════════════════════════════════════════

def rule_grep_c_echo(data: dict, fix: bool = False) -> list[Finding]:
    """Rule 1: grep -c ... || echo 0 → ; true"""
    findings = []
    pattern = re.compile(r'\|\|\s*echo\s+0')
    for loc, cmd, container, key in iter_commands(data):
        if 'grep' in cmd and pattern.search(cmd):
            new_cmd = pattern.sub('; true', cmd)
            if fix:
                container[key] = new_cmd
            findings.append(Finding(
                rule="grep_c_echo", location=loc, severity="fix" if fix else "error",
                message="grep -c || echo 0 produces duplicate output",
                before=cmd[:80], after=new_cmd[:80] if fix else ""
            ))
    return findings


def rule_grep_c_chain(data: dict, fix: bool = False) -> list[Finding]:
    """Rule 2: )&&grep -c → ); grep -c"""
    findings = []
    pattern = re.compile(r'\)&&\s*grep\s+-c')
    for loc, cmd, container, key in iter_commands(data):
        if pattern.search(cmd):
            new_cmd = re.sub(r'\)&&\s*grep', '); grep', cmd)
            if fix:
                container[key] = new_cmd
            findings.append(Finding(
                rule="grep_c_chain", location=loc, severity="fix" if fix else "error",
                message="&&grep -c can skip grep when prior command fails",
                before=cmd[:80], after=new_cmd[:80] if fix else ""
            ))
    return findings


def rule_missing_yes(data: dict, fix: bool = False) -> list[Finding]:
    """Rule 3: classify/process commands without -y"""
    findings = []
    cli_pattern = re.compile(r'(?:classify|process)\b')
    skip_pattern = re.compile(r'--help|grep.*classify|grep.*process')
    has_yes = re.compile(r'-y\b|--yes\b')
    profile_pattern = re.compile(r'--profile\s+\S+')

    for loc, cmd, container, key in iter_commands(data):
        if cli_pattern.search(cmd) and not skip_pattern.search(cmd) and not has_yes.search(cmd):
            if fix:
                m = profile_pattern.search(cmd)
                if m:
                    insert_pos = m.end()
                    new_cmd = cmd[:insert_pos] + " -y" + cmd[insert_pos:]
                else:
                    new_cmd = cmd.replace("classify ", "classify -y ", 1).replace("process ", "process -y ", 1)
                container[key] = new_cmd
                findings.append(Finding(
                    rule="missing_yes", location=loc, severity="fix",
                    message="classify/process without -y will block",
                    before=cmd[:80], after=new_cmd[:80]
                ))
            else:
                findings.append(Finding(
                    rule="missing_yes", location=loc, severity="error",
                    message="classify/process without -y will block",
                    before=cmd[:80]
                ))
    return findings


def rule_clean_all(data: dict, fix: bool = False) -> list[Finding]:
    """Rule 4: clean all in pre_run → clean progress"""
    findings = []

    def check_list(cmds: list, loc_prefix: str):
        for i, cmd in enumerate(cmds):
            if "clean all" in cmd:
                if fix:
                    cmds[i] = cmd.replace("clean all", "clean progress")
                    findings.append(Finding(
                        rule="clean_all", location=f"{loc_prefix}[{i}]", severity="fix",
                        message="clean all deletes reports needed for validation",
                        before="clean all", after="clean progress"
                    ))
                else:
                    findings.append(Finding(
                        rule="clean_all", location=f"{loc_prefix}[{i}]", severity="error",
                        message="clean all deletes reports needed for validation",
                        before="clean all"
                    ))

    check_list(data.get("session", {}).get("pre_run", []), "session.pre_run")
    check_list(data.get("session", {}).get("post_run", []), "session.post_run")
    for ph in data.get("phases", []):
        pid = ph.get("id", "?")
        check_list(ph.get("pre_run", []), f"{pid}.pre_run")
        for sr in ph.get("series", []):
            sid = sr.get("id", "?")
            check_list(sr.get("pre_run", []), f"{sid}.pre_run")

    return findings


# ════════════════════════════════════════════════════════════
#  RULES — Warning / validation only
# ════════════════════════════════════════════════════════════

def rule_absolute_paths(data: dict) -> list[Finding]:
    """Rule 5: absolute paths outside ${VAR}"""
    findings = []
    pattern = re.compile(r'(?<!\$\{)(?<!\w)/(?:Users|home|Volumes|tmp|etc|var)\b')
    for loc, cmd, _, _ in iter_commands(data):
        if pattern.search(cmd):
            findings.append(Finding(
                rule="absolute_paths", location=loc, severity="warning",
                message=f"Absolute path detected: {cmd[:60]}..."
            ))
    return findings


def rule_blocking_commands(data: dict) -> list[Finding]:
    """Rule 10: commands that might block on user input"""
    findings = []
    block_pattern = re.compile(r'\bread\s+-p\b|\binput\s*\(|\bconfirm\b')
    safe_pattern = re.compile(r'-y\b|--yes\b|--force\b|echo.*\|')

    for loc, cmd, _, _ in iter_commands(data):
        if block_pattern.search(cmd) and not safe_pattern.search(cmd):
            findings.append(Finding(
                rule="blocking_commands", location=loc, severity="warning",
                message=f"Command may block on user input: {cmd[:60]}..."
            ))
    return findings


def rule_dependencies(data: dict) -> list[Finding]:
    """Rule 9: check requires/provides consistency"""
    findings = []
    all_provides: dict[str, str] = {}
    all_requires: dict[str, list[str]] = {}

    for ph in data.get("phases", []):
        for sr in ph.get("series", []):
            sid = sr.get("id", "?")
            for cap in sr.get("provides", []):
                all_provides[cap] = sid
            all_requires[sid] = sr.get("requires", [])

    # Unmet requires
    for sid, reqs in all_requires.items():
        for req in reqs:
            if req not in all_provides:
                findings.append(Finding(
                    rule="dependencies", location=sid, severity="warning",
                    message=f"requires '{req}' but no series provides it"
                ))

    # Cycle detection (DFS)
    graph: dict[str, set[str]] = {}
    for sid, reqs in all_requires.items():
        graph[sid] = set()
        for req in reqs:
            if req in all_provides:
                graph[sid].add(all_provides[req])

    visited: set[str] = set()
    in_stack: set[str] = set()

    def dfs(node: str) -> str | None:
        if node in in_stack:
            return node
        if node in visited:
            return None
        visited.add(node)
        in_stack.add(node)
        for nb in graph.get(node, set()):
            cycle = dfs(nb)
            if cycle:
                return cycle
        in_stack.discard(node)
        return None

    for sid in all_requires:
        cycle = dfs(sid)
        if cycle:
            findings.append(Finding(
                rule="dependencies", location=cycle, severity="error",
                message=f"Circular dependency involving {cycle}"
            ))
            break

    return findings


# ════════════════════════════════════════════════════════════
#  RULES — Structure validation
# ════════════════════════════════════════════════════════════

VALID_ASSERT_TYPES = {
    "output_equals", "output_contains", "output_not_contains",
    "exit_code", "file_exists", "file_not_exists",
    "line_count", "manual_check", "duration_under"
}
VALID_MODES = {"auto", "manual_check"}


def rule_yaml_structure(data: dict) -> list[Finding]:
    """Rule 8: validate YAML structure (required fields, valid types)"""
    findings = []

    for field in ("version", "project", "phases"):
        if field not in data:
            findings.append(Finding(
                rule="yaml_structure", location="root", severity="error",
                message=f"Missing required top-level field: {field}"
            ))

    for ph in data.get("phases", []):
        pid = ph.get("id", "?")
        for field in ("id", "name", "series"):
            if field not in ph:
                findings.append(Finding(
                    rule="yaml_structure", location=pid, severity="error",
                    message=f"Phase missing required field: {field}"
                ))

        for sr in ph.get("series", []):
            sid = sr.get("id", "?")
            for field in ("id", "name", "checks"):
                if field not in sr:
                    findings.append(Finding(
                        rule="yaml_structure", location=sid, severity="error",
                        message=f"Series missing required field: {field}"
                    ))

            for ck in sr.get("checks", []):
                cid = ck.get("id", "?")
                for field in ("id", "description", "command", "assert", "mode"):
                    if field not in ck:
                        findings.append(Finding(
                            rule="yaml_structure", location=cid, severity="error",
                            message=f"Check missing required field: {field}"
                        ))

                assert_type = ck.get("assert", {}).get("type", "")
                if assert_type and assert_type not in VALID_ASSERT_TYPES:
                    findings.append(Finding(
                        rule="yaml_structure", location=cid, severity="error",
                        message=f"Unknown assertion type: {assert_type}"
                    ))

                mode = ck.get("mode", "")
                if mode and mode not in VALID_MODES:
                    findings.append(Finding(
                        rule="yaml_structure", location=cid, severity="error",
                        message=f"Unknown mode: {mode}"
                    ))

    return findings


# ════════════════════════════════════════════════════════════
#  RULES — Context-dependent (optional)
# ════════════════════════════════════════════════════════════

def load_tree_yaml(profile: str = "test") -> list[str] | None:
    """Load tree.yaml and return flat list of folder paths."""
    tree_path = PROJECT_ROOT / "profiles" / profile / "tree.yaml"
    if not tree_path.exists():
        return None
    with open(tree_path, encoding="utf-8") as f:
        tree = yaml.safe_load(f)
    if not tree:
        return None
    paths: list[str] = []
    def walk(node, prefix=""):
        if isinstance(node, dict):
            for k, v in node.items():
                p = f"{prefix}/{k}" if prefix else k
                paths.append(p)
                walk(v, p)
        elif isinstance(node, list):
            for item in node:
                walk(item, prefix)
    walk(tree)
    return paths


def resolve_variables(cmd: str, variables: dict) -> str:
    result = cmd
    for _ in range(10):
        new = re.sub(r'\$\{(\w+)\}', lambda m: variables.get(m.group(1), m.group(0)), result)
        if new == result:
            break
        result = new
    return result


def rule_paths_exist(data: dict) -> list[Finding]:
    """Rule 6: check that referenced paths exist in tree.yaml or on disk"""
    findings = []
    variables = data.get("variables", {})
    profile = variables.get("PROF", "test")
    tree_paths = load_tree_yaml(profile)
    if tree_paths is None:
        return findings  # SKIP

    ls_pattern = re.compile(r'ls\s+(\S+)')
    find_pattern = re.compile(r'find\s+(\S+)')

    for loc, cmd, _, _ in iter_commands(data):
        resolved = resolve_variables(cmd, variables)
        if is_runtime_path(resolved):
            continue
        for pat in (ls_pattern, find_pattern):
            for match in pat.finditer(resolved):
                path = match.group(1).rstrip("/")
                # Skip paths with wildcards (test-created files)
                if "*" in path or "?" in path:
                    continue
                biblio_test = variables.get("BIBLIO_TEST", "")
                if biblio_test and path.startswith(biblio_test):
                    relative = path[len(biblio_test):].strip("/")
                    if relative and "/" in relative:
                        parts = relative.split("/")
                        if len(parts) >= 2:
                            child = parts[1]
                            matched = any(child in tp for tp in tree_paths)
                            if not matched and not os.path.exists(path):
                                findings.append(Finding(
                                    rule="paths_exist", location=loc, severity="error",
                                    message=f"Path '{relative}' not found in tree.yaml or on disk",
                                ))
    return findings


def rule_report_names(data: dict) -> list[Finding]:
    """Rule 7: check that report name patterns match code source"""
    findings = []
    valid_prefixes: set[str] = set()
    for search_dir in (PROJECT_ROOT / "commands", PROJECT_ROOT / "lib"):
        if not search_dir.exists():
            continue
        for py_file in search_dir.glob("*.py"):
            try:
                content = py_file.read_text(encoding="utf-8")
            except Exception:
                continue
            # Pattern: save_report(results, logs_dir, 'rapport_classify')
            for m in re.finditer(r"save_report\([^,]+,\s*[^,]+,\s*['\"]([^'\"]+)['\"]", content):
                valid_prefixes.add(m.group(1))
            # Pattern: 'refine_{}.csv'.format(...)
            for m in re.finditer(r"['\"](\w+)_\{\}\.csv['\"]", content):
                valid_prefixes.add(m.group(1))
            # Pattern: f"rapport_rename_{ts}.csv" or f"log_renommage_{ts}.csv"
            for m in re.finditer(r'f["\'](\w+)_\{', content):
                valid_prefixes.add(m.group(1))
            # Pattern: .glob('rapport_rename_*.csv')
            for m in re.finditer(r"glob\(['\"](\w+)_\*", content):
                valid_prefixes.add(m.group(1))

    if not valid_prefixes:
        return findings  # SKIP

    report_pattern = re.compile(r'(?:rapport_|refine_|log_)\w*\*')
    for loc, cmd, _, _ in iter_commands(data):
        if is_runtime_path(cmd):
            continue
        for m in report_pattern.finditer(cmd):
            prefix = m.group(0).rstrip("*").rstrip("_")
            if prefix not in valid_prefixes and prefix.rstrip("_") not in valid_prefixes:
                findings.append(Finding(
                    rule="report_names", location=loc, severity="error",
                    message=f"Report pattern '{m.group(0)}' — prefix '{prefix}' not found in source. Valid: {valid_prefixes}"
                ))
    return findings


# ════════════════════════════════════════════════════════════
#  Output
# ════════════════════════════════════════════════════════════

def print_report(findings: list[Finding], yaml_path: Path):
    print(f"\n{bld('validate_tests.py')} — {yaml_path}\n")

    fixes = [f for f in findings if f.severity == "fix"]
    errors = [f for f in findings if f.severity == "error"]
    warnings = [f for f in findings if f.severity == "warning"]

    if fixes:
        print(f"{grn(f'AUTO-FIXED ({len(fixes)}):')}")
        for f in fixes:
            print(f"  {grn('✓')} {f.location}: {f.message}")
            if f.before and f.after:
                print(f"    {dim(f.before[:60])} → {f.after[:60]}")
        print()

    if errors:
        print(f"{red(f'ERRORS ({len(errors)}):')}")
        for f in errors:
            print(f"  {red('✗')} {f.location}: {f.message}")
            if f.before:
                print(f"    {dim(f.before[:80])}")
        print()

    if warnings:
        print(f"{ylw(f'WARNINGS ({len(warnings)}):')}")
        for f in warnings:
            print(f"  {ylw('⚠')} {f.location}: {f.message}")
        print()

    if not fixes and not errors and not warnings:
        print(f"  {grn('✓ No issues found')}\n")

    print(f"{'─' * 40}")
    print(f"Summary: {len(fixes)} fixed, {len(errors)} errors, {len(warnings)} warnings\n")


def print_json(findings: list[Finding]):
    result = {
        "fixed": [{"rule": f.rule, "location": f.location, "message": f.message,
                    "before": f.before, "after": f.after}
                   for f in findings if f.severity == "fix"],
        "errors": [{"rule": f.rule, "location": f.location, "message": f.message}
                    for f in findings if f.severity == "error"],
        "warnings": [{"rule": f.rule, "location": f.location, "message": f.message}
                      for f in findings if f.severity == "warning"],
        "summary": {
            "fixed": sum(1 for f in findings if f.severity == "fix"),
            "errors": sum(1 for f in findings if f.severity == "error"),
            "warnings": sum(1 for f in findings if f.severity == "warning"),
        }
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))


# ════════════════════════════════════════════════════════════
#  Main
# ════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Validate tests/functional/tests.yaml")
    parser.add_argument("--fix", action="store_true", help="Auto-fix fixable issues")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Output as JSON")
    parser.add_argument("--yaml", default=str(TESTS_YAML), help="Path to tests.yaml")
    args = parser.parse_args()

    yaml_path = Path(args.yaml)
    data = load_yaml(yaml_path)

    findings: list[Finding] = []

    # Auto-fixable rules
    findings.extend(rule_grep_c_echo(data, fix=args.fix))
    findings.extend(rule_grep_c_chain(data, fix=args.fix))
    findings.extend(rule_missing_yes(data, fix=args.fix))
    findings.extend(rule_clean_all(data, fix=args.fix))

    # Warning rules
    findings.extend(rule_absolute_paths(data))
    findings.extend(rule_blocking_commands(data))
    findings.extend(rule_dependencies(data))

    # Structure validation
    findings.extend(rule_yaml_structure(data))

    # Context-dependent (optional, skip if context unavailable)
    findings.extend(rule_paths_exist(data))
    findings.extend(rule_report_names(data))

    # Save if fixed
    if args.fix and any(f.severity == "fix" for f in findings):
        save_yaml(data, yaml_path)

    # Output
    if args.json_output:
        print_json(findings)
    else:
        print_report(findings, yaml_path)

    # Exit code
    errors = [f for f in findings if f.severity == "error"]
    if errors:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
