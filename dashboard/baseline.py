"""Baseline classification — run-based store for cycle-of-improvement evaluation.

Each "baseline run" lives under:
    profiles/<profile>/.cache/baseline/run-<timestamp>-<klodo_version>/
        ├── meta.json
        ├── predictions.jsonl
        └── disagreements.jsonl  (with verdicts written incrementally)

A run is created by `scripts/baseline_run.py`. The dashboard reads the latest
(or selected) run and lets the user adjudicate each disagreement.

Verdict values for disagreements:
    'klodo_right'   → predicted_folder is correct, current_folder is wrong
    'actual_right'  → current_folder is correct, predicted_folder is wrong
    'neither_right' → both wrong; ground_truth provided separately
    'skip'          → cannot tell (illegible / out of scope)
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

from dashboard import data

VERDICT_VALUES = {"klodo_right", "actual_right", "neither_right", "skip"}

# In-memory cache: (profile, run_id) → list[dict] of disagreement records
_records_cache: dict[tuple[str, str], list[dict]] = {}
_lock = threading.Lock()


# ─── Path helpers ─────────────────────────────────────────────────────────


def _baseline_dir(profile: str) -> Path:
    return data.get_project_root() / "profiles" / profile / ".cache" / "baseline"


def _run_dir(profile: str, run_id: str) -> Path:
    return _baseline_dir(profile) / f"run-{run_id}"


def _disagreements_path(profile: str, run_id: str) -> Path:
    return _run_dir(profile, run_id) / "disagreements.jsonl"


def _runs_index_path(profile: str) -> Path:
    return _baseline_dir(profile) / "runs.index.json"


# ─── Run discovery ────────────────────────────────────────────────────────


def list_runs(profile: str) -> list[dict]:
    """Return all runs for a profile, latest first.

    Reads from runs.index.json if present; otherwise scans run-* dirs and
    rebuilds the index in-memory (without persisting).
    """
    index_path = _runs_index_path(profile)
    if index_path.exists():
        try:
            return json.loads(index_path.read_text())
        except Exception:
            pass

    base = _baseline_dir(profile)
    if not base.exists():
        return []
    out: list[dict] = []
    for d in sorted(base.iterdir(), reverse=True):
        if not d.is_dir() or not d.name.startswith("run-"):
            continue
        meta_path = d / "meta.json"
        if not meta_path.exists():
            continue
        try:
            out.append(json.loads(meta_path.read_text()))
        except Exception:
            continue
    return out


def latest_run(profile: str) -> dict | None:
    runs = list_runs(profile)
    return runs[0] if runs else None


def get_run(profile: str, run_id: str | None = None) -> dict | None:
    """Return run meta for run_id, or the latest if run_id is None."""
    if run_id is None:
        return latest_run(profile)
    for r in list_runs(profile):
        if r.get("run_id") == run_id:
            return r
    return None


# ─── Disagreement records ─────────────────────────────────────────────────


def _load(profile: str, run_id: str, force_reload: bool = False) -> list[dict]:
    key = (profile, run_id)
    with _lock:
        if not force_reload and key in _records_cache:
            return _records_cache[key]

        path = _disagreements_path(profile, run_id)
        if not path.exists():
            _records_cache[key] = []
            return []

        records: list[dict] = []
        for line in path.read_text().splitlines():
            line = line.strip()
            if line:
                records.append(json.loads(line))
        _records_cache[key] = records
        return records


def _save(profile: str, run_id: str, records: list[dict]) -> None:
    """Atomically rewrite the disagreements JSONL."""
    path = _disagreements_path(profile, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".jsonl.tmp")
    with tmp.open("w") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(path)


def stats(profile: str, run_id: str) -> dict:
    """Return progression stats for a run's disagreements."""
    records = _load(profile, run_id)
    total = len(records)
    counts: dict[str, int] = {v: 0 for v in VERDICT_VALUES}
    for r in records:
        v = r.get("verdict")
        if v in counts:
            counts[v] += 1
    validated = sum(counts.values())

    # Total predictions (incl. agreements) for context
    meta = get_run(profile, run_id)
    n_files = meta.get("n_files", 0) if meta else 0
    n_agreements = max(0, n_files - total)

    return {
        "exists": total > 0 or n_files > 0,
        "n_files": n_files,
        "n_agreements": n_agreements,
        "n_disagreements": total,
        "validated": validated,
        "remaining": total - validated,
        "klodo_right": counts["klodo_right"],
        "actual_right": counts["actual_right"],
        "neither_right": counts["neither_right"],
        "skip": counts["skip"],
    }


def next_record(profile: str, run_id: str) -> dict | None:
    records = _load(profile, run_id)
    for r in records:
        if not r.get("verdict"):
            return r
    return None


def get_record(profile: str, run_id: str, file_id: str) -> dict | None:
    records = _load(profile, run_id)
    for r in records:
        if r.get("file_id") == file_id:
            return r
    return None


def save_verdict(
    profile: str,
    run_id: str,
    file_id: str,
    verdict: str,
    ground_truth: str | None = None,
    notes: str | None = None,
) -> bool:
    """Update a record's verdict. Returns False if file_id not found."""
    if verdict not in VERDICT_VALUES:
        raise ValueError(f"Invalid verdict {verdict!r}")
    if verdict == "neither_right" and not ground_truth:
        raise ValueError("ground_truth required when verdict='neither_right'")

    records = _load(profile, run_id)
    for r in records:
        if r.get("file_id") == file_id:
            r["verdict"] = verdict
            r["validated_at"] = datetime.now().isoformat(timespec="seconds")
            if verdict == "klodo_right":
                r["ground_truth"] = r.get("predicted_folder")
            elif verdict == "actual_right":
                r["ground_truth"] = r.get("current_folder")
            elif verdict == "neither_right":
                r["ground_truth"] = ground_truth
            else:
                r["ground_truth"] = None
            if notes is not None:
                r["notes"] = notes
            _save(profile, run_id, records)
            with _lock:
                _records_cache.pop((profile, run_id), None)
            return True
    return False


def reset_cache() -> None:
    with _lock:
        _records_cache.clear()


# ─── Misc helpers ─────────────────────────────────────────────────────────


def list_target_folders(profile: str) -> list[str]:
    """Return valid folders for ground-truth selection (autocomplete)."""
    tree_path = data.get_project_root() / "profiles" / profile / "tree.yaml"
    if not tree_path.exists():
        return []
    import yaml
    tree = yaml.safe_load(tree_path.read_text())
    return sorted(
        f for f in tree.get("folders", [])
        if not f.startswith("_")
    )


def resolve_source_path(profile: str, rel_path: str) -> Path | None:
    """Resolve a relative path to its absolute location on disk."""
    import yaml
    profile_path = data.get_project_root() / "profiles" / profile / "profile.yaml"
    if not profile_path.exists():
        return None
    cfg = yaml.safe_load(profile_path.read_text())
    target = Path(cfg.get("target") or cfg.get("target_path") or "")
    candidate = target / rel_path
    return candidate if candidate.exists() else None


def baseline_thumbnail_dir(profile: str, file_id: str) -> Path:
    return _baseline_dir(profile) / ".thumbs" / file_id


def smart_default_profile() -> str:
    """Pick the profile to display when /baseline is opened without ?profile=.

    Strategy (per user's option C):
    1. Most recent run across all profiles → take its profile
    2. Otherwise fall back to 'default'
    """
    profiles = data.get_available_profiles()
    best: tuple[str, str] | None = None  # (created_at, profile)
    for p in profiles:
        name = p.get("name") if isinstance(p, dict) else p
        if not name:
            continue
        runs = list_runs(name)
        if not runs:
            continue
        latest = runs[0]
        created = latest.get("created_at", "")
        if best is None or created > best[0]:
            best = (created, name)
    return best[1] if best else "default"
