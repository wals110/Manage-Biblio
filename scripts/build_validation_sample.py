"""Build a stratified validation sample from the classified library.

Walks the profile target (e.g. /Volumes/ExtSSD/BIBLIO/) and selects N files
per existing folder, with a floor and a cap to balance under/over-represented
classes. The output is a JSONL ready for manual validation.

Why stratified, not uniform random:
- Random sampling would be dominated by the largest folders (e.g.
  02-INFORMATIQUE/03-Langages-Programmation/Autres has 4 586 files).
- Stratified ensures every existing class is represented, so the
  resulting confusion matrix has signal on every diagonal element.
- A floor (default 3) keeps tiny classes visible.
- A cap (default 15) prevents huge classes from dominating.

Output format (JSONL, one record per line):
    {
      "file_id": "<sha1 of relative path>",
      "rel_path": "02-INFORMATIQUE/05-IA-ML/NLP/Foo.pdf",
      "filename": "Foo.pdf",
      "predicted_folder": "02-INFORMATIQUE/05-IA-ML/NLP",
      "ground_truth": null,           // filled by validation phase
      "verdict": null,                 // "correct" | "wrong" | "skip"
      "validated_at": null,
      "notes": null
    }

Usage:
    uv run python scripts/build_validation_sample.py
        [--profile default] [--per-class 6] [--floor 3] [--cap 15]
        [--seed 42] [--output profiles/<p>/.cache/validation_sample.jsonl]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

import yaml


def stratified_sample(
    files_by_folder: dict[str, list[Path]],
    per_class: int,
    floor: int,
    cap: int,
    seed: int,
) -> list[tuple[Path, str]]:
    """Return a list of (file_path, folder) sampled stratified.

    For each folder:
        - if folder size <= floor → take all
        - else → take min(per_class, cap, folder_size)
    """
    rng = random.Random(seed)
    sample: list[tuple[Path, str]] = []
    for folder, files in sorted(files_by_folder.items()):
        n = len(files)
        if n <= floor:
            chosen = list(files)
        else:
            target = min(max(per_class, floor), cap, n)
            chosen = rng.sample(files, target)
        for f in chosen:
            sample.append((f, folder))
    return sample


def file_id(rel_path: str) -> str:
    return hashlib.sha1(rel_path.encode("utf-8")).hexdigest()[:16]


def main(
    profile: str,
    per_class: int,
    floor: int,
    cap: int,
    seed: int,
    output: Path | None,
) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    profile_dir = repo_root / "profiles" / profile
    profile_path = profile_dir / "profile.yaml"

    profile_cfg = yaml.safe_load(profile_path.read_text())
    target_base = Path(profile_cfg.get("target") or profile_cfg.get("target_path") or "")
    if not target_base.exists():
        print(f"ERROR: target '{target_base}' does not exist", file=sys.stderr)
        return 2

    print(f"Profile     : {profile}")
    print(f"Target      : {target_base}")
    print(f"per-class   : {per_class}  (floor {floor}, cap {cap})")
    print(f"Seed        : {seed}")
    print()

    # Walk and bucket by folder
    files_by_folder: dict[str, list[Path]] = defaultdict(list)
    for pdf in target_base.rglob("*.pdf"):
        rel = pdf.relative_to(target_base)
        folder = str(rel.parent)
        if folder.startswith("_") or "/_" in folder:
            continue  # skip _A-TRIER, _INBOX
        files_by_folder[folder].append(pdf)

    folder_count = len(files_by_folder)
    file_total = sum(len(v) for v in files_by_folder.values())
    print(f"Walked      : {file_total} files across {folder_count} folders")

    # Sample
    sample = stratified_sample(files_by_folder, per_class, floor, cap, seed)
    print(f"Sampled     : {len(sample)} records ({len(sample)/file_total:.1%} of population)")
    print()

    # Distribution stats
    by_folder_count: dict[str, int] = defaultdict(int)
    for _, folder in sample:
        by_folder_count[folder] += 1
    sizes = sorted(by_folder_count.values())
    print(f"Per-class : min={sizes[0]}, max={sizes[-1]}, median={sizes[len(sizes)//2]}")
    print(f"Folders fully sampled (≤ floor): {sum(1 for f, files in files_by_folder.items() if len(files) <= floor)}")
    print()

    # Show 3 smallest + 3 largest folders sampled
    print("Smallest sampled folders:")
    for folder in sorted(by_folder_count, key=lambda f: by_folder_count[f])[:3]:
        print(f"  {by_folder_count[folder]:3d} / {len(files_by_folder[folder]):4d}  {folder}")
    print("Largest sampled folders:")
    for folder in sorted(by_folder_count, key=lambda f: -by_folder_count[f])[:3]:
        print(f"  {by_folder_count[folder]:3d} / {len(files_by_folder[folder]):4d}  {folder}")
    print()

    # Output
    if output is None:
        output = profile_dir / ".cache" / "validation_sample.jsonl"
    output.parent.mkdir(exist_ok=True)

    with output.open("w") as f:
        for pdf_path, folder in sample:
            rel = str(pdf_path.relative_to(target_base))
            record = {
                "file_id": file_id(rel),
                "rel_path": rel,
                "filename": pdf_path.name,
                "predicted_folder": folder,
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
    parser.add_argument("--per-class", type=int, default=3,
                        help="Target sample size per class (default: 3 → ~280 files)")
    parser.add_argument("--floor", type=int, default=2,
                        help="Below this folder size, take all files (default: 2)")
    parser.add_argument("--cap", type=int, default=10,
                        help="Cap per-class sample size (default: 10)")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path, default=None,
                        help="Override output path")
    args = parser.parse_args()
    sys.exit(main(args.profile, args.per_class, args.floor,
                  args.cap, args.seed, args.output))
