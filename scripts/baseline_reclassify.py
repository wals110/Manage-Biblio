"""Re-run the classifier on an existing baseline run, reusing the
vision_cache (no LLM calls). Cheap iteration after a classifier fix.

Reads:
  - profiles/<profile>/.cache/baseline/run-<id>/predictions.jsonl
  - profiles/<profile>/.cache/vision_cache.json (current state)

Writes (in-place):
  - <run-dir>/predictions.jsonl   (updated predicted_folder)
  - <run-dir>/disagreements.jsonl (recomputed from new predictions,
                                   verdicts/applied_at PRESERVED if the
                                   triple (file_id, current, predicted)
                                   matches a previous arbitration)
  - <run-dir>/meta.json           ('reclassified_at' field added)

Usage:
    uv run python scripts/baseline_reclassify.py [--profile default]
        [--run-id <id>] [--dry-run]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from lib import vision_cache  # noqa: E402
from lib.classifier import classify_combined  # noqa: E402
from lib.profile import Profile  # noqa: E402

CONFIDENCE_THRESHOLD = 0.5


def file_id(rel_path: str) -> str:
    return hashlib.sha1(rel_path.encode("utf-8")).hexdigest()[:16]


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def save_jsonl(path: Path, records: list[dict]) -> None:
    tmp = path.with_suffix(".jsonl.tmp")
    with tmp.open("w") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(path)


def latest_run_dir(profile_dir: Path) -> Path | None:
    base = profile_dir / ".cache" / "baseline"
    if not base.exists():
        return None
    runs = sorted(
        (d for d in base.iterdir() if d.is_dir() and d.name.startswith("run-")),
        reverse=True,
    )
    return runs[0] if runs else None


def main(profile: str, run_id: str | None, dry_run: bool) -> int:
    profile_obj = Profile(profile)
    target_base = Path(profile_obj.target)
    if not target_base.exists():
        print(f"ERROR: target {target_base} missing", file=sys.stderr)
        return 2

    profile_dir = REPO_ROOT / "profiles" / profile
    if run_id:
        run_dir = profile_dir / ".cache" / "baseline" / f"run-{run_id}"
    else:
        run_dir = latest_run_dir(profile_dir)
    if not run_dir or not run_dir.exists():
        print(f"ERROR: no baseline run", file=sys.stderr)
        return 2

    pred_path = run_dir / "predictions.jsonl"
    disag_path = run_dir / "disagreements.jsonl"
    if not pred_path.exists():
        print(f"ERROR: {pred_path} missing", file=sys.stderr)
        return 2

    print(f"Profile      : {profile}")
    print(f"Run dir      : {run_dir.name}")
    print(f"Mode         : {'DRY-RUN' if dry_run else 'EXECUTE (in-place)'}")
    print()

    # Load existing predictions (we'll update predicted_folder fields)
    predictions = load_jsonl(pred_path)
    print(f"Predictions  : {len(predictions)}")

    # Load existing disagreement verdicts to preserve them
    old_disag = load_jsonl(disag_path)
    # Index by (file_id, predicted_folder) — verdict is meaningful for the
    # specific (file, klodo prediction, current location) triple
    old_verdicts: dict[tuple, dict] = {}
    for d in old_disag:
        key = (d["file_id"], d["current_folder"], d["predicted_folder"])
        if d.get("verdict") or d.get("applied_at"):
            old_verdicts[key] = d
    print(f"Existing arbitrations carried over: {len(old_verdicts)}")
    print()

    # Load vision cache
    cache_path = Path(profile_obj.cache_dir) / "vision_cache.json"
    cache = vision_cache.load_cache(cache_path)
    print(f"Vision cache : {len(cache)} entrées")
    print()

    # Re-classify each prediction using the cached vision result
    theme_mapping = profile_obj.theme_mapping
    n_pages = profile_obj.defaults.get("pages", 1)
    model = profile_obj.llm_model

    n_changed = 0
    n_unchanged = 0
    n_no_cache = 0
    fallback = profile_obj.fallback or "_A-TRIER"

    for p in predictions:
        rel = p["rel_path"]
        pdf_path = target_base / rel
        # Build cache key (same as analyze_cover_cached)
        key = vision_cache.compute_cache_key(
            str(pdf_path), model=model, n_pages=n_pages)
        if key is None:
            n_no_cache += 1
            continue
        hit = vision_cache.lookup(cache, key)
        if hit is None:
            n_no_cache += 1
            continue
        # Re-run classify_combined on the cached vision result
        try:
            dest, score, source = classify_combined(
                hit, Path(rel).name, theme_mapping,
                classifier=None, llm_mapper=None, pdf_path=str(pdf_path),
            )
        except Exception:
            continue
        new_predicted = dest or p["current_folder"] or fallback
        if new_predicted != p["predicted_folder"]:
            n_changed += 1
            p["predicted_folder"] = new_predicted
            p["score"] = float(score) if score else 0.0
            p["source"] = source or ""
            # Refresh theme/title/confidence from cache (in case schema changed)
            p["theme"] = hit.get("theme", p.get("theme", ""))
            p["title"] = hit.get("title", p.get("title", ""))
            p["confidence"] = float(hit.get("confidence", p.get("confidence", 0)))
            p["language"] = hit.get("language", p.get("language", ""))
            p["status"] = "classifié" if dest else "non_classifié"
        else:
            n_unchanged += 1

    print(f"Predictions stats")
    print(f"  changées             : {n_changed}")
    print(f"  inchangées           : {n_unchanged}")
    print(f"  sans cache (skip)    : {n_no_cache}")
    print()

    # Recompute disagreements list
    new_disag: list[dict] = []
    for p in predictions:
        if p["current_folder"] == p["predicted_folder"]:
            continue
        key = (p["file_id"], p["current_folder"], p["predicted_folder"])
        existing = old_verdicts.get(key)
        record = {
            **p,
            "verdict": existing["verdict"] if existing else None,
            "ground_truth": existing.get("ground_truth") if existing else None,
            "validated_at": existing.get("validated_at") if existing else None,
            "notes": existing.get("notes") if existing else None,
            "applied_at": existing.get("applied_at") if existing else None,
            "in_sample": existing.get("in_sample") if existing else None,
        }
        new_disag.append(record)

    print(f"Disagreements: {len(new_disag)} (anciennement {len(old_disag)})")
    n_carried = sum(1 for d in new_disag if d.get("verdict") or d.get("applied_at"))
    print(f"  Verdicts/applied préservés : {n_carried}")
    print()

    if dry_run:
        print("DRY-RUN — aucun fichier modifié.")
        return 0

    # Persist
    save_jsonl(pred_path, predictions)
    save_jsonl(disag_path, new_disag)

    # Update meta.json
    meta_path = run_dir / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    meta["reclassified_at"] = datetime.now().isoformat(timespec="seconds")
    meta["n_disagreements"] = len(new_disag)
    meta_path.write_text(json.dumps(meta, indent=2))

    # Update runs.index.json (read by dashboard in priority over meta.json)
    index_path = run_dir.parent / "runs.index.json"
    if index_path.exists():
        try:
            index = json.loads(index_path.read_text())
            for entry in index:
                if entry.get("run_id") == meta.get("run_id"):
                    entry["n_disagreements"] = meta["n_disagreements"]
                    entry["reclassified_at"] = meta["reclassified_at"]
                    break
            index_path.write_text(json.dumps(index, indent=2))
        except Exception as e:
            print(f"WARN: runs.index.json non mis à jour ({e})", file=sys.stderr)

    print(f"✅ Run mis à jour in-place: {run_dir.name}")
    print(f"   predictions.jsonl + disagreements.jsonl + meta.json")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="default")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    sys.exit(main(args.profile, args.run_id, args.dry_run))
