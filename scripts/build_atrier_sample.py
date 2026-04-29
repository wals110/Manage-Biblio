"""Build a random sample from _A-TRIER (files Klodo could not classify).

Companion to build_validation_sample.py. Where the main sample measures
"when Klodo picked a folder, was it the right one?", this script measures
"when Klodo gave up and dumped to _A-TRIER, what should the right answer
have been?". Together they form the baseline analysis.

Output format (JSONL):
    {
      "file_id": "<sha1 of relative path>",
      "rel_path": "_A-TRIER/Foo.pdf",
      "filename": "Foo.pdf",
      "predicted_folder": "_A-TRIER",   // always _A-TRIER for this sample
      "ground_truth": null,              // filled by validation phase
      "verdict": null,                   // "classified" | "skip"
      "validated_at": null,
      "notes": null
    }

Note: unlike the main sample, "verdict" here can only be "classified" (a
correct folder was picked) or "skip" (uncertain/unreadable). There's no
"correct" verdict because there was no prediction to confirm.

Usage:
    uv run python scripts/build_atrier_sample.py
        [--profile default] [--n 50] [--seed 42]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path

import yaml


def file_id(rel_path: str) -> str:
    return hashlib.sha1(rel_path.encode("utf-8")).hexdigest()[:16]


def main(profile: str, n: int, seed: int, output: Path | None) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    profile_dir = repo_root / "profiles" / profile
    profile_path = profile_dir / "profile.yaml"

    profile_cfg = yaml.safe_load(profile_path.read_text())
    target_base = Path(profile_cfg.get("target") or profile_cfg.get("target_path") or "")
    if not target_base.exists():
        print(f"ERROR: target '{target_base}' does not exist", file=sys.stderr)
        return 2

    fallback_name = profile_cfg.get("fallback", "_A-TRIER")
    atrier_dir = target_base / fallback_name
    if not atrier_dir.exists():
        print(f"ERROR: A-TRIER folder '{atrier_dir}' not found", file=sys.stderr)
        return 2

    print(f"Profile     : {profile}")
    print(f"A-TRIER dir : {atrier_dir}")
    print(f"Sample size : {n}")
    print(f"Seed        : {seed}")
    print()

    # Walk A-TRIER (no stratification — single bucket)
    candidates = [
        p for p in atrier_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in (".pdf", ".epub")
    ]

    if not candidates:
        print("ERROR: A-TRIER is empty — nothing to sample", file=sys.stderr)
        return 2

    print(f"Candidates  : {len(candidates)} files in A-TRIER")
    if n > len(candidates):
        print(f"  (capping to {len(candidates)} since requested n={n} exceeds available)")
        n = len(candidates)

    rng = random.Random(seed)
    sample = rng.sample(candidates, n)
    print(f"Sampled     : {len(sample)} files")
    print()

    # Output
    if output is None:
        output = profile_dir / ".cache" / "validation_atrier_sample.jsonl"
    output.parent.mkdir(exist_ok=True)

    with output.open("w") as f:
        for pdf_path in sample:
            rel = str(pdf_path.relative_to(target_base))
            record = {
                "file_id": file_id(rel),
                "rel_path": rel,
                "filename": pdf_path.name,
                "predicted_folder": fallback_name,
                "ground_truth": None,
                "verdict": None,
                "validated_at": None,
                "notes": None,
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"Sample écrit → {output.relative_to(repo_root)}")
    print(f"  {len(sample)} records, {output.stat().st_size / 1024:.1f} KB")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="default")
    parser.add_argument("--n", type=int, default=50,
                        help="Number of files to sample from A-TRIER (default: 50)")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path, default=None,
                        help="Override output path")
    args = parser.parse_args()
    sys.exit(main(args.profile, args.n, args.seed, args.output))
