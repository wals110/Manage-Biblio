"""Build SetFit gold dataset from SSD classified files.

Walks /Volumes/ExtSSD/BIBLIO/ (configurable via profile.yaml target path)
and extracts (cleaned_filename, folder) pairs for every classified PDF.

Skip conditions:
- Folders starting with '_' (A-TRIER, INBOX — unclassified)
- Files with very short filenames (< 3 significant tokens)
- Folders with < 2 files (can't be stratified for eval)

Output: profiles/<profile>/.cache/setfit_dataset.jsonl (gitignored)
Each line: {"text": "...", "label": "<folder>"}

Usage: uv run --group ml python scripts/build_setfit_dataset.py [--profile default]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

import yaml


# Tokens to strip from filenames (noise that doesn't help classification)
NOISE_PATTERNS = [
    r"\b(?:19|20)\d{2}\b",              # years
    r"\b\d{9,13}\b",                    # ISBNs (9-13 digits)
    r"\b(?:v|vol|volume|ed|edition)\s*\d+\b",  # volume/edition numbers
    r"\b\d+(?:st|nd|rd|th)\s+edition\b",
    r"\(.*?\)",                         # anything in parentheses
    r"\[.*?\]",                         # anything in brackets
    r"\bpdf\b",
    r"[_\-–—]+",                        # separators → space
    r"[^\w\s]",                         # non-word/non-space
]


def clean_filename(filename: str) -> str:
    """Normalize a filename into searchable text."""
    # Drop extension
    name = Path(filename).stem
    # Replace common separators with space
    name = name.replace("_", " ").replace("-", " ").replace(".", " ")
    # Remove noise patterns
    text = name.lower()
    for pat in NOISE_PATTERNS:
        text = re.sub(pat, " ", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def walk_biblio(target_base: Path, tree_folders: set[str]) -> list[tuple[str, str]]:
    """Walk the SSD and extract (text, folder) pairs.

    Only keeps files whose parent folder (relative to target_base) is in tree_folders.
    """
    pairs: list[tuple[str, str]] = []
    rejected_folder_not_in_tree = 0
    rejected_short = 0

    for pdf in target_base.rglob("*.pdf"):
        # Build relative folder path from target_base
        rel = pdf.relative_to(target_base)
        folder = str(rel.parent)

        # Skip top-level _A-TRIER, _INBOX, etc.
        if folder.startswith("_") or "/_" in folder:
            continue

        # Only keep folders that are in the declared tree
        if folder not in tree_folders:
            rejected_folder_not_in_tree += 1
            continue

        text = clean_filename(pdf.name)
        tokens = text.split()
        if len(tokens) < 3:
            rejected_short += 1
            continue

        pairs.append((text, folder))

    print(f"Files walked    : {len(pairs) + rejected_folder_not_in_tree + rejected_short}")
    print(f"  kept          : {len(pairs)}")
    print(f"  rejected (folder not in tree.yaml): {rejected_folder_not_in_tree}")
    print(f"  rejected (filename < 3 tokens)    : {rejected_short}")
    return pairs


def main(profile: str) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    profile_dir = repo_root / "profiles" / profile
    tree_path = profile_dir / "tree.yaml"
    profile_path = profile_dir / "profile.yaml"

    profile_cfg = yaml.safe_load(profile_path.read_text())
    target_base = Path(profile_cfg.get("target_path") or profile_cfg.get("target") or "")
    if not target_base.exists():
        print(f"ERROR: target_path '{target_base}' does not exist", file=sys.stderr)
        return 2

    tree = yaml.safe_load(tree_path.read_text())
    tree_folders = {f for f in tree.get("folders", []) if not f.startswith("_")}

    print(f"Profile      : {profile}")
    print(f"Target base  : {target_base}")
    print(f"Tree folders : {len(tree_folders)}")
    print()

    pairs = walk_biblio(target_base, tree_folders)
    print()

    # Distribution stats
    counts = Counter(f for _, f in pairs)
    print(f"Classes (folders used): {len(counts)}")
    print(f"  folders in tree but empty: {len(tree_folders - set(counts))}")
    print(f"  min files/folder : {min(counts.values())}")
    print(f"  max files/folder : {max(counts.values())}")
    print(f"  avg files/folder : {sum(counts.values())/len(counts):.1f}")
    print(f"  folders with 1 file : {sum(1 for c in counts.values() if c == 1)}")
    print(f"  folders with ≥ 10  : {sum(1 for c in counts.values() if c >= 10)}")
    print(f"  folders with ≥ 50  : {sum(1 for c in counts.values() if c >= 50)}")
    print()

    # Text length stats
    lens = [len(t.split()) for t, _ in pairs]
    print(f"Tokens per text : min={min(lens)}, max={max(lens)}, avg={sum(lens)/len(lens):.1f}")
    print()

    # Write JSONL
    out_path = profile_dir / ".cache" / "setfit_dataset.jsonl"
    out_path.parent.mkdir(exist_ok=True)
    with out_path.open("w") as f:
        for text, folder in pairs:
            f.write(json.dumps({"text": text, "label": folder}) + "\n")

    print(f"Dataset écrit → {out_path.relative_to(repo_root)}")
    print(f"  {len(pairs)} examples, {out_path.stat().st_size / 1024:.1f} KB")

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="default")
    args = parser.parse_args()
    sys.exit(main(args.profile))
