"""Baseline classification — predict-all + diff against current SSD state.

Re-runs the current Klodo classifier in dry-run mode over an entire profile
library, then compares predictions to where each file currently lives.
The output is a versioned run directory containing predictions, the
disagreements that need user adjudication, and a metadata file.

Workflow:
    1. Walk profile.target/**/*.pdf (excluding _A-TRIER and _INBOX subfolders
       by default; those are handled separately as "abandoned" files)
    2. For each file, run process_single_file() which:
        - Extracts cover image
        - Calls vision LLM (cached) to get theme/title
        - Runs classify_combined() → predicted folder
    3. Compare predicted_folder vs current rel folder
    4. Write predictions.jsonl + disagreements.jsonl + meta.json
    5. Update runs.index.json

Storage:
    profiles/<profile>/.cache/baseline/
    ├── runs.index.json
    └── run-<YYYYMMDD-HHMMSS>-<klodo_version>/
        ├── meta.json
        ├── predictions.jsonl
        └── disagreements.jsonl

Usage:
    uv run python scripts/baseline_run.py --profile default
        [--workers 1] [--max 0] [--include-atrier]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from commands.helpers import load_classifiers, process_single_file  # noqa: E402
from lib import __version__ as KLODO_VERSION  # noqa: E402
from lib.profile import Profile  # noqa: E402


def file_id(rel_path: str) -> str:
    return hashlib.sha1(rel_path.encode("utf-8")).hexdigest()[:16]


def walk_profile_pdfs(target_base: Path, include_atrier: bool) -> list[Path]:
    """Walk target_base/** for PDF/EPUB files.

    Skip '_'-prefixed top folders unless include_atrier is True. Note that
    even with include_atrier, we keep _INBOX excluded (it's the staging
    area, not classified).
    """
    files: list[Path] = []
    for p in target_base.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in (".pdf", ".epub"):
            continue
        rel = p.relative_to(target_base)
        parts = rel.parts
        if not parts:
            continue
        top = parts[0]
        if top == "_INBOX":
            continue
        if top.startswith("_") and not include_atrier:
            continue
        files.append(p)
    return files


def run_predict(
    profile_name: str,
    workers: int,
    max_files: int,
    include_atrier: bool,
    verbose: bool,
) -> tuple[Path, list[dict], int]:
    """Run predictions and return (run_dir, predictions, disagreement_count)."""
    profile = Profile(profile_name)
    target_base = Path(profile.target)
    if not target_base.exists():
        raise SystemExit(f"ERROR: profile target {target_base} does not exist")

    api_key = os.environ.get("SILICONFLOW_API_KEY", "")

    # Load classifier + LLM mapper (matches commands/classify.py logic)
    classifier, llm_mapper = load_classifiers(
        profile, api_key, verbose=verbose, vision=False,
    )
    theme_mapping = profile.theme_mapping
    min_confidence = profile.defaults.get("min_confidence", 0.5)
    n_pages = profile.defaults.get("pages", 1)

    cache_dir = Path(profile.cache_dir)
    vision_cache_path = str(cache_dir / "vision_cache.json")
    print(f"Vision cache : {vision_cache_path}")

    files = walk_profile_pdfs(target_base, include_atrier)
    if max_files and len(files) > max_files:
        files = files[:max_files]
    print(f"Walked       : {len(files)} files in {target_base}")

    # Run-dir layout
    run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{KLODO_VERSION}"
    run_dir = cache_dir / "baseline" / f"run-{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)

    predictions: list[dict] = []
    started_at = time.time()
    fallback = profile.fallback or "_A-TRIER"

    def _process_one(pdf_path: Path) -> dict:
        rel = pdf_path.relative_to(target_base)
        current_folder = str(rel.parent) if str(rel.parent) != "." else ""
        try:
            res = process_single_file(
                str(pdf_path),
                api_key,
                profile.llm_endpoint,
                profile.llm_model,
                theme_mapping,
                classifier=classifier,
                llm_mapper=llm_mapper,
                verbose=False,
                min_confidence=min_confidence,
                n_pages=n_pages,
                vision_cache_path=vision_cache_path,
            )
        except Exception as e:
            # On exception, defer to current location (no false disagreement).
            return {
                "file_id": file_id(str(rel)),
                "rel_path": str(rel),
                "filename": pdf_path.name,
                "current_folder": current_folder,
                "predicted_folder": current_folder or fallback,
                "status": f"error: {e}",
                "confidence": 0.0,
                "title": "", "theme": "", "language": "",
            }

        # Klodo "gave up" (no destination) → defer to current_folder so we
        # don't create a false disagreement against the user's existing
        # decision. Predicting fallback (_A-TRIER) here would mean "Klodo
        # wants to demote this file" which is rarely the intent.
        predicted = res.get("destination") or current_folder or fallback
        return {
            "file_id": file_id(str(rel)),
            "rel_path": str(rel),
            "filename": pdf_path.name,
            "current_folder": current_folder,
            "predicted_folder": predicted,
            "status": res.get("status", ""),
            "confidence": res.get("confiance", 0.0),
            "score": res.get("score", 0.0),
            "title": res.get("titre_detecte", ""),
            "theme": res.get("theme_detecte", ""),
            "language": res.get("langue", ""),
            "source": res.get("mot_cle", ""),
        }

    print(f"Processing   : {workers} worker(s)")
    print()

    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for i, rec in enumerate(pool.map(_process_one, files), 1):
                predictions.append(rec)
                if i % 50 == 0:
                    elapsed = time.time() - started_at
                    rate = i / elapsed if elapsed > 0 else 0
                    eta = (len(files) - i) / rate if rate > 0 else 0
                    print(f"  [{i:4d}/{len(files)}] rate={rate:.1f}/s eta={eta/60:.1f}min")
    else:
        for i, pdf in enumerate(files, 1):
            rec = _process_one(pdf)
            predictions.append(rec)
            if i % 50 == 0:
                elapsed = time.time() - started_at
                rate = i / elapsed if elapsed > 0 else 0
                eta = (len(files) - i) / rate if rate > 0 else 0
                print(f"  [{i:4d}/{len(files)}] rate={rate:.1f}/s eta={eta/60:.1f}min")

    elapsed = time.time() - started_at
    print(f"\nPredict done : {len(predictions)} files in {elapsed/60:.1f}min")

    # Compute disagreements
    disagreements = [
        p for p in predictions
        if p["current_folder"] != p["predicted_folder"]
    ]
    print(f"Disagreements: {len(disagreements)} ({len(disagreements)/max(1,len(predictions)):.1%})")

    # Persist outputs
    with (run_dir / "predictions.jsonl").open("w") as f:
        for p in predictions:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    with (run_dir / "disagreements.jsonl").open("w") as f:
        for d in disagreements:
            d_record = {**d, "verdict": None, "ground_truth": None,
                        "validated_at": None, "notes": None}
            f.write(json.dumps(d_record, ensure_ascii=False) + "\n")

    meta = {
        "run_id": run_id,
        "profile": profile_name,
        "klodo_version": KLODO_VERSION,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "target": str(target_base),
        "include_atrier": include_atrier,
        "n_files": len(predictions),
        "n_disagreements": len(disagreements),
        "duration_s": round(elapsed, 1),
        "workers": workers,
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2))

    # Append to runs.index.json
    index_path = cache_dir / "baseline" / "runs.index.json"
    if index_path.exists():
        index = json.loads(index_path.read_text())
    else:
        index = []
    index.insert(0, meta)
    index_path.write_text(json.dumps(index, indent=2))

    return run_dir, predictions, len(disagreements)


def carry_over_verdicts(profile_name: str, new_run_dir: Path) -> int:
    """Replay verdicts from the previous run for identical (file_id, current_folder, predicted_folder) triples.

    Returns the number of verdicts re-applied automatically.
    """
    profile = Profile(profile_name)
    cache_dir = Path(profile.cache_dir)
    base = cache_dir / "baseline"
    if not base.exists():
        return 0

    # Find the previous run dir (other than new_run_dir)
    runs = sorted(
        (d for d in base.iterdir() if d.is_dir() and d.name.startswith("run-")),
        reverse=True,
    )
    prev = None
    for r in runs:
        if r != new_run_dir:
            prev = r
            break
    if prev is None:
        return 0

    prev_disag_path = prev / "disagreements.jsonl"
    if not prev_disag_path.exists():
        return 0

    # Build lookup of previous verdicts
    prev_verdicts: dict[tuple, dict] = {}
    for line in prev_disag_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if rec.get("verdict"):
            key = (rec["file_id"], rec["current_folder"], rec["predicted_folder"])
            prev_verdicts[key] = {
                "verdict": rec["verdict"],
                "ground_truth": rec.get("ground_truth"),
                "validated_at": rec.get("validated_at"),
                "notes": rec.get("notes"),
            }

    if not prev_verdicts:
        return 0

    # Apply to new run
    new_disag_path = new_run_dir / "disagreements.jsonl"
    if not new_disag_path.exists():
        return 0

    records = [json.loads(line) for line in new_disag_path.read_text().splitlines() if line.strip()]
    carried = 0
    for r in records:
        key = (r["file_id"], r["current_folder"], r["predicted_folder"])
        if key in prev_verdicts:
            for k, v in prev_verdicts[key].items():
                r[k] = v
            r["notes"] = (r.get("notes") or "") + f" [carried from {prev.name}]"
            carried += 1

    if carried > 0:
        with new_disag_path.open("w") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return carried


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="default")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--max", type=int, default=0,
                        help="Max files to process (0 = all)")
    parser.add_argument("--include-atrier", action="store_true",
                        help="Also walk _A-TRIER folder (default: skip)")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--no-carry", action="store_true",
                        help="Skip carry-over of previous verdicts")
    args = parser.parse_args()

    print(f"=== Baseline run — profile {args.profile!r} ===")
    print(f"Klodo version : {KLODO_VERSION}")
    print()

    run_dir, _preds, n_disag = run_predict(
        args.profile, args.workers, args.max, args.include_atrier, args.verbose,
    )

    if not args.no_carry:
        carried = carry_over_verdicts(args.profile, run_dir)
        if carried > 0:
            print(f"Verdicts carried over from previous run: {carried}")

    print()
    print(f"Run dir       : {run_dir}")
    print(f"Disagreements : {n_disag} (à arbitrer dans le dashboard /baseline)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
