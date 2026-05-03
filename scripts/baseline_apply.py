"""Apply baseline arbitration verdicts as actual file moves on the SSD.

Reads the latest baseline run's disagreements.jsonl and moves files
to where you said they should go. Dry-run by default — passes
--execute to actually move.

Records considered:
  - verdict == 'klodo_right'   → move to predicted_folder (default)
  - verdict == 'neither_right' → move to user-saisi ground_truth (opt-in)
  - verdict == 'actual_right'  → no move needed (filtered out automatically)
  - verdict == 'skip'          → no move (filtered out)

Pass --verdicts to control which buckets to apply, e.g.:
  --verdicts klodo_right,neither_right
Default is just klodo_right (the safest, highest-confidence subset).

Auto-trust by class
~~~~~~~~~~~~~~~~~~~
With --auto-trust-tier1, also apply Klodo's predictions for non-arbitrated
records whose predicted_folder reached 100% precision (with ≥3 arbitrated
samples) on the manual subset. This extends the safe corrections to the
unsampled disagreements pointing to "trusted" classes.

Auto-applied records get verdict='klodo_right' with validated_at='auto:<ts>'
to preserve traceability vs manually-arbitrated ones.

Adjust thresholds with --min-precision and --min-sample.

Safety
~~~~~~
- Dry-run by default — print plan, no changes
- Never overwrites: if the destination file already exists with the same
  name, the move is skipped (logged) so you can resolve manually
- Refuses to move INTO any '_*' folder (e.g. _A-TRIER) — that's a regression
- Source-existence check: if the source file is no longer where the run
  said (you moved it manually since), the move is skipped
- After successful moves, updates disagreements.jsonl with `applied_at`
  to ensure idempotency on re-runs
- Writes an audit log CSV in the run dir with every action

Usage
~~~~~
  # Dry-run — see what would happen
  uv run python scripts/baseline_apply.py --profile default

  # Apply (Klodo-right only)
  uv run python scripts/baseline_apply.py --profile default --execute

  # Also apply 'neither_right' corrections
  uv run python scripts/baseline_apply.py --profile default \\
      --verdicts klodo_right,neither_right --execute
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import yaml


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def compute_trusted_classes(
    records: list[dict], min_precision: float, min_sample: int,
) -> dict[str, dict]:
    """Identify predicted_folders where Klodo proved reliable on the sample.

    Returns dict[predicted_folder] -> {n_kr, n_kw, precision} for each class
    that satisfies min_precision and min_sample.
    """
    by_class: dict[str, dict[str, int]] = defaultdict(lambda: {"kr": 0, "kw": 0})
    for r in records:
        v = r.get("verdict")
        if v == "klodo_right":
            by_class[r["predicted_folder"]]["kr"] += 1
        elif v in ("actual_right", "neither_right"):
            by_class[r["predicted_folder"]]["kw"] += 1

    trusted: dict[str, dict] = {}
    for cls, st in by_class.items():
        n = st["kr"] + st["kw"]
        if n < min_sample:
            continue
        precision = st["kr"] / n
        if precision < min_precision:
            continue
        trusted[cls] = {"n_kr": st["kr"], "n_kw": st["kw"],
                        "n_total": n, "precision": precision}
    return trusted


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


def main(profile: str, run_id: str | None, verdicts_str: str,
         auto_trust: bool, min_precision: float, min_sample: int,
         execute: bool) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    profile_dir = repo_root / "profiles" / profile

    # Load profile.yaml for target base
    profile_yaml = profile_dir / "profile.yaml"
    cfg = yaml.safe_load(profile_yaml.read_text())
    target_base = Path(cfg.get("target") or cfg.get("target_path") or "")
    if not target_base.exists():
        print(f"ERROR: target {target_base} does not exist", file=sys.stderr)
        return 2

    # Locate run dir
    if run_id:
        run_dir = profile_dir / ".cache" / "baseline" / f"run-{run_id}"
    else:
        run_dir = latest_run_dir(profile_dir)
    if not run_dir or not run_dir.exists():
        print(f"ERROR: no baseline run found in {profile_dir}", file=sys.stderr)
        return 2

    disag_path = run_dir / "disagreements.jsonl"
    records = load_jsonl(disag_path)
    if not records:
        print(f"ERROR: no disagreements in {disag_path}", file=sys.stderr)
        return 2

    verdicts_to_apply = {v.strip() for v in verdicts_str.split(",") if v.strip()}
    valid_verdicts = {"klodo_right", "neither_right", "actual_right", "skip"}
    invalid = verdicts_to_apply - valid_verdicts
    if invalid:
        print(f"ERROR: invalid verdict(s) in --verdicts: {invalid}", file=sys.stderr)
        return 2
    if "actual_right" in verdicts_to_apply or "skip" in verdicts_to_apply:
        print("WARNING: actual_right/skip don't trigger moves; ignored.")
        verdicts_to_apply -= {"actual_right", "skip"}
    if not verdicts_to_apply:
        print("ERROR: nothing to apply", file=sys.stderr)
        return 2

    # Compute trusted classes if auto-trust is enabled
    trusted_classes: dict[str, dict] = {}
    if auto_trust:
        trusted_classes = compute_trusted_classes(records, min_precision, min_sample)

    print(f"Profile      : {profile}")
    print(f"Run dir      : {run_dir.name}")
    print(f"Target base  : {target_base}")
    print(f"Verdicts     : {sorted(verdicts_to_apply)}")
    print(f"Auto-trust   : {'on' if auto_trust else 'off'}"
          + (f"  (≥{min_precision:.0%} precision, n≥{min_sample}, "
             f"{len(trusted_classes)} classes trusted)" if auto_trust else ""))
    print(f"Mode         : {'EXECUTE' if execute else 'DRY-RUN (no changes)'}")
    print()

    if auto_trust and trusted_classes:
        print("Trusted classes (auto-apply non-arbitrated predictions):")
        for cls in sorted(trusted_classes.keys()):
            tc = trusted_classes[cls]
            print(f"  {tc['precision']:.0%}  K={tc['n_kr']}/{tc['n_total']}  {cls}")
        print()

    # Plan moves
    planned: list[dict] = []
    skipped_already: list[dict] = []   # already applied
    skipped_no_source: list[dict] = []  # source moved/missing
    skipped_conflict: list[dict] = []   # destination exists
    skipped_protected: list[dict] = []  # ground_truth starts with _

    def plan_move(r: dict, gt: str, applied_via: str) -> None:
        """Add a move to the plan if it's safe."""
        cur = r.get("current_folder", "")
        if not gt or gt == cur:
            return
        if gt.startswith("_") or "/_" in gt:
            skipped_protected.append(r)
            return
        source = target_base / r["rel_path"]
        if not source.exists():
            skipped_no_source.append(r)
            return
        dest_dir = target_base / gt
        dest = dest_dir / source.name
        if dest.exists():
            skipped_conflict.append(r)
            return
        planned.append({
            "record": r,
            "source": source,
            "dest": dest,
            "ground_truth": gt,
            "applied_via": applied_via,
        })

    for r in records:
        if r.get("applied_at"):
            skipped_already.append(r)
            continue
        v = r.get("verdict")
        if v in verdicts_to_apply:
            # Manual verdict path
            plan_move(r, r.get("ground_truth", ""), "manual")
        elif (auto_trust and not v
              and r["predicted_folder"] in trusted_classes):
            # Auto-trusted path: treat as klodo_right
            plan_move(r, r["predicted_folder"], "auto:tier1")

    n_manual = sum(1 for e in planned if e["applied_via"] == "manual")
    n_auto = len(planned) - n_manual

    print("PLAN")
    print("─" * 60)
    print(f"  À déplacer (total): {len(planned)}")
    print(f"    — manuel        : {n_manual}")
    print(f"    — auto-trusted  : {n_auto}")
    print(f"  Déjà appliqué     : {len(skipped_already)}")
    print(f"  Source disparue   : {len(skipped_no_source)}")
    print(f"  Conflit (existe)  : {len(skipped_conflict)}")
    print(f"  Dest. protégée    : {len(skipped_protected)}")
    print()

    if not planned:
        print("Rien à faire.")
        return 0

    # Show first 10 moves
    print("Aperçu (10 premières entrées) :")
    for entry in planned[:10]:
        r = entry["record"]
        tag = "[auto]" if entry["applied_via"] != "manual" else "[manu]"
        print(f"  {tag} {r['filename'][:55]:55s}")
        print(f"         {r['current_folder']}  →  {entry['ground_truth']}")
    if len(planned) > 10:
        print(f"  … et {len(planned) - 10} autres.")
    print()

    if not execute:
        print("DRY-RUN — aucun fichier déplacé. Relance avec --execute pour appliquer.")
        return 0

    # Execute
    log_path = run_dir / f"applied_{datetime.now():%Y%m%d-%H%M%S}.csv"
    moved_count = 0
    failed: list[tuple[dict, str]] = []
    timestamp = datetime.now().isoformat(timespec="seconds")

    with log_path.open("w", newline="") as fcsv:
        writer = csv.DictWriter(fcsv, fieldnames=[
            "applied_at", "applied_via", "file_id", "filename",
            "from_folder", "to_folder", "verdict", "status", "error",
        ])
        writer.writeheader()
        for entry in planned:
            r = entry["record"]
            source: Path = entry["source"]
            dest: Path = entry["dest"]
            gt = entry["ground_truth"]
            via = entry["applied_via"]
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(dest))
                # If auto-applied, mark the verdict so it's traceable
                if via != "manual" and not r.get("verdict"):
                    r["verdict"] = "klodo_right"
                    r["validated_at"] = f"auto:{timestamp}"
                    r["ground_truth"] = gt
                r["applied_at"] = timestamp
                r["applied_via"] = via
                # Update rel_path so future runs see new location
                new_rel = str(dest.relative_to(target_base))
                r["applied_to_rel_path"] = new_rel
                moved_count += 1
                writer.writerow({
                    "applied_at": timestamp,
                    "applied_via": via,
                    "file_id": r["file_id"],
                    "filename": r["filename"],
                    "from_folder": r["current_folder"],
                    "to_folder": gt,
                    "verdict": r.get("verdict", ""),
                    "status": "moved",
                    "error": "",
                })
            except Exception as e:
                failed.append((r, str(e)))
                writer.writerow({
                    "applied_at": timestamp,
                    "applied_via": via,
                    "file_id": r["file_id"],
                    "filename": r["filename"],
                    "from_folder": r["current_folder"],
                    "to_folder": gt,
                    "verdict": r.get("verdict", ""),
                    "status": "failed",
                    "error": str(e)[:200],
                })

    # Persist applied_at into disagreements.jsonl
    save_jsonl(disag_path, records)

    print()
    print("RÉSULTATS")
    print("─" * 60)
    print(f"  ✅ Déplacés       : {moved_count}")
    print(f"  ❌ Échecs         : {len(failed)}")
    if failed:
        print()
        print("Échecs :")
        for r, err in failed[:5]:
            print(f"  {r['filename'][:50]} → {err[:80]}")
        if len(failed) > 5:
            print(f"  … et {len(failed) - 5} autres (voir CSV).")
    print()
    print(f"Log audit : {log_path.relative_to(repo_root)}")
    print(f"Mapping mis à jour : {disag_path.relative_to(repo_root)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="default")
    parser.add_argument("--run-id", default=None,
                        help="Run ID without 'run-' prefix. Default: latest.")
    parser.add_argument("--verdicts", default="klodo_right",
                        help="Comma-list of verdicts to apply (default: klodo_right)")
    parser.add_argument("--auto-trust-tier1", action="store_true",
                        help="Also apply Klodo predictions on classes proven 100%% "
                             "reliable on the manual sample (≥3 arbitrated, all KR)")
    parser.add_argument("--auto-trust", action="store_true",
                        help="Custom auto-trust with --min-precision/--min-sample")
    parser.add_argument("--min-precision", type=float, default=1.0,
                        help="Minimum precision for auto-trust (default: 1.0 = 100%%)")
    parser.add_argument("--min-sample", type=int, default=3,
                        help="Minimum arbitrated samples for auto-trust (default: 3)")
    parser.add_argument("--execute", action="store_true",
                        help="Actually move files. Without it, dry-run only.")
    args = parser.parse_args()

    auto_trust = args.auto_trust or args.auto_trust_tier1
    min_precision = 1.0 if args.auto_trust_tier1 else args.min_precision
    min_sample = 3 if args.auto_trust_tier1 else args.min_sample

    sys.exit(main(args.profile, args.run_id, args.verdicts,
                  auto_trust, min_precision, min_sample, args.execute))
