"""Sample a subset of disagreements for manual arbitration.

The full disagreements.jsonl can be very large (e.g., 6209 cases for the
default profile baseline). Arbitrating all of them is impractical; this
script marks a stratified subset with `in_sample: true` so the dashboard
shows only those for arbitration. Statistical signal per class is
preserved by stratifying on predicted_folder.

Strategy:
- For each predicted_folder bucket:
    if size <= floor → take all
    else → random.sample(min(per_class, cap, size))
- Write back disagreements.jsonl with `in_sample` field set on selected
  records (others get in_sample=False).

Re-sampling: re-running this script overwrites the in_sample field with
a fresh selection (same seed → reproducible).

Usage:
    uv run python scripts/baseline_sample.py
        --profile default --run-id <id>
        [--per-class 5] [--floor 2] [--cap 10] [--seed 42]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from pathlib import Path


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def save_jsonl(path: Path, records: list[dict]) -> None:
    tmp = path.with_suffix(".jsonl.tmp")
    with tmp.open("w") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(path)


def stratified_select(
    records: list[dict],
    per_class: int,
    floor: int,
    cap: int,
    seed: int,
) -> set[str]:
    """Return the set of file_ids to mark in_sample=True."""
    by_class: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_class[r["predicted_folder"]].append(r)

    rng = random.Random(seed)
    selected: set[str] = set()
    for cls, items in by_class.items():
        n = len(items)
        if n <= floor:
            chosen = items
        else:
            target = min(max(per_class, floor), cap, n)
            chosen = rng.sample(items, target)
        for r in chosen:
            selected.add(r["file_id"])
    return selected


def main(profile: str, run_id: str | None, per_class: int, floor: int, cap: int, seed: int) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    base = repo_root / "profiles" / profile / ".cache" / "baseline"
    if not base.exists():
        print(f"ERROR: no baseline runs found for profile {profile!r}", file=sys.stderr)
        return 2

    if run_id:
        run_dir = base / f"run-{run_id}"
    else:
        runs = sorted((d for d in base.iterdir() if d.is_dir() and d.name.startswith("run-")), reverse=True)
        if not runs:
            print("ERROR: no run directories", file=sys.stderr)
            return 2
        run_dir = runs[0]

    if not run_dir.exists():
        print(f"ERROR: run dir {run_dir} not found", file=sys.stderr)
        return 2

    path = run_dir / "disagreements.jsonl"
    records = load_jsonl(path)
    if not records:
        print(f"ERROR: no disagreements in {path}", file=sys.stderr)
        return 2

    print(f"Profile     : {profile}")
    print(f"Run         : {run_dir.name}")
    print(f"Total disagreements : {len(records)}")
    print(f"per-class   : {per_class}  (floor {floor}, cap {cap})")
    print(f"Seed        : {seed}")
    print()

    selected = stratified_select(records, per_class, floor, cap, seed)
    print(f"Selected    : {len(selected)} ({len(selected)/len(records):.1%} of total)")

    # Distribution stats
    by_class: dict[str, int] = defaultdict(int)
    for r in records:
        if r["file_id"] in selected:
            by_class[r["predicted_folder"]] += 1
    sizes = sorted(by_class.values())
    print(f"Per-class   : min={sizes[0]}, max={sizes[-1]}, "
          f"median={sizes[len(sizes)//2]}, mean={sum(sizes)/len(sizes):.1f}")
    print(f"Classes covered : {len(by_class)} (sur "
          f"{len({r['predicted_folder'] for r in records})} prédites)")
    print()

    # Apply in_sample flag (preserves verdicts already set)
    for r in records:
        r["in_sample"] = r["file_id"] in selected

    save_jsonl(path, records)
    print(f"Updated     : {path.relative_to(repo_root)}")
    print("  Lance le dashboard /baseline pour arbitrer le sample.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="default")
    parser.add_argument("--run-id", default=None,
                        help="Run ID without 'run-' prefix. Default: latest.")
    parser.add_argument("--per-class", type=int, default=5,
                        help="Target sample size per predicted_folder (default: 5)")
    parser.add_argument("--floor", type=int, default=2,
                        help="Below this class size, take all (default: 2)")
    parser.add_argument("--cap", type=int, default=10,
                        help="Cap per-class sample (default: 10)")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    sys.exit(main(args.profile, args.run_id, args.per_class, args.floor, args.cap, args.seed))
