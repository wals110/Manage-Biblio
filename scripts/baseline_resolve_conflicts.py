"""Resolve conflicts left over from baseline_apply.py.

When baseline_apply tries to move a file but a homonym already exists at
the destination, it skips. This script handles those skipped cases:

- If SHA256(source) == SHA256(destination) → it's a real duplicate.
  Delete the source (the destination is the authoritative copy per
  Klodo's prediction). Record the deletion in the audit log.

- If SHA256 differs → these are different files sharing a name. Print
  them for manual resolution (script does NOT touch them).

Dry-run by default. --execute to actually delete duplicates.

Output:
- profiles/<profile>/.cache/baseline/run-<id>/conflicts_resolved_<ts>.csv
  with rows {file_id, filename, action, src_path, src_hash, dst_hash}

Usage:
    uv run python scripts/baseline_resolve_conflicts.py [--profile default]
        [--run-id <id>] [--execute]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import yaml


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


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


def compute_trusted_classes(records: list[dict]) -> set[str]:
    """Same Tier 1 logic as baseline_apply.py."""
    by_class: dict[str, dict[str, int]] = defaultdict(lambda: {"kr": 0, "kw": 0})
    for r in records:
        v = r.get("verdict")
        if v == "klodo_right":
            by_class[r["predicted_folder"]]["kr"] += 1
        elif v in ("actual_right", "neither_right"):
            by_class[r["predicted_folder"]]["kw"] += 1
    return {
        c for c, st in by_class.items()
        if (st["kr"] + st["kw"]) >= 3 and st["kr"] / (st["kr"] + st["kw"]) >= 1.0
    }


def main(profile: str, run_id: str | None, execute: bool) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    profile_dir = repo_root / "profiles" / profile
    cfg = yaml.safe_load((profile_dir / "profile.yaml").read_text())
    target_base = Path(cfg.get("target") or cfg.get("target_path") or "")
    if not target_base.exists():
        print(f"ERROR: target {target_base} missing", file=sys.stderr)
        return 2

    if run_id:
        run_dir = profile_dir / ".cache" / "baseline" / f"run-{run_id}"
    else:
        run_dir = latest_run_dir(profile_dir)
    if not run_dir or not run_dir.exists():
        print("ERROR: no run dir", file=sys.stderr)
        return 2

    disag_path = run_dir / "disagreements.jsonl"
    records = load_jsonl(disag_path)
    if not records:
        print("ERROR: no disagreements", file=sys.stderr)
        return 2

    trusted = compute_trusted_classes(records)

    # Find unresolved conflicts (manual KR or auto-trust class, not applied,
    # source exists, destination exists)
    conflicts: list[dict] = []
    for r in records:
        if r.get("applied_at"):
            continue
        v = r.get("verdict")
        is_manual_kr = v == "klodo_right"
        is_auto = (v is None) and (r["predicted_folder"] in trusted)
        if not (is_manual_kr or is_auto):
            continue
        gt = r.get("ground_truth") if is_manual_kr else r["predicted_folder"]
        if not gt or gt == r.get("current_folder", ""):
            continue
        if gt.startswith("_") or "/_" in gt:
            continue
        src = target_base / r["rel_path"]
        if not src.exists():
            continue
        dst = target_base / gt / src.name
        if not dst.exists():
            continue
        conflicts.append({"record": r, "src": src, "dst": dst, "gt": gt,
                          "kind": "manual" if is_manual_kr else "auto"})

    print(f"Profile  : {profile}")
    print(f"Run dir  : {run_dir.name}")
    print(f"Mode     : {'EXECUTE' if execute else 'DRY-RUN'}")
    print(f"Conflits : {len(conflicts)}")
    print()

    if not conflicts:
        print("Aucun conflit à résoudre.")
        return 0

    # Hash compare
    duplicates: list[dict] = []   # same content
    different: list[dict] = []     # different content
    print("Calcul des hash SHA256 (peut prendre quelques secondes)...")
    for c in conflicts:
        src_h = sha256_file(c["src"])
        dst_h = sha256_file(c["dst"])
        c["src_hash"] = src_h
        c["dst_hash"] = dst_h
        if src_h == dst_h:
            duplicates.append(c)
        else:
            different.append(c)
    print(f"  Doublons confirmés (hash identique) : {len(duplicates)}")
    print(f"  Homonymes au contenu différent      : {len(different)}")
    print()

    if duplicates:
        print("DOUBLONS — la source sera supprimée (destination conservée)")
        print("─" * 70)
        for c in duplicates[:30]:
            r = c["record"]
            print(f"  [{c['kind']}] {r['filename'][:55]:55s}")
            print(f"        rm {c['src']}")
            print(f"        keep {c['dst']}")
        if len(duplicates) > 30:
            print(f"  … et {len(duplicates) - 30} autres.")
        print()

    if different:
        print("HOMONYMES DIFFÉRENTS — pas touchés (résoudre manuellement)")
        print("─" * 70)
        for c in different:
            r = c["record"]
            ssz = c["src"].stat().st_size
            dsz = c["dst"].stat().st_size
            print(f"  [{c['kind']}] {r['filename'][:55]:55s}")
            print(f"        src ({ssz:,} bytes): {c['src']}")
            print(f"        dst ({dsz:,} bytes): {c['dst']}")
            print("        → décider manuellement quel garder, ou renommer")
        print()

    if not execute:
        print("DRY-RUN — aucun fichier touché. --execute pour supprimer les doublons.")
        return 0

    # Execute deletions for true duplicates
    log_path = run_dir / f"conflicts_resolved_{datetime.now():%Y%m%d-%H%M%S}.csv"
    timestamp = datetime.now().isoformat(timespec="seconds")
    deleted = 0
    failed: list[tuple[dict, str]] = []

    with log_path.open("w", newline="") as fcsv:
        writer = csv.DictWriter(fcsv, fieldnames=[
            "applied_at", "kind", "action", "file_id", "filename",
            "src_path", "dst_path", "src_hash", "dst_hash", "error",
        ])
        writer.writeheader()
        for c in duplicates:
            r = c["record"]
            try:
                c["src"].unlink()
                # Mark in disagreements.jsonl
                r["applied_at"] = timestamp
                r["applied_via"] = "conflict_resolved_dup_deleted"
                r["applied_to_rel_path"] = str(c["dst"].relative_to(target_base))
                if not r.get("verdict"):
                    r["verdict"] = "klodo_right"
                    r["validated_at"] = f"auto:{timestamp}"
                    r["ground_truth"] = c["gt"]
                deleted += 1
                writer.writerow({
                    "applied_at": timestamp,
                    "kind": c["kind"],
                    "action": "deleted_source_dup",
                    "file_id": r["file_id"],
                    "filename": r["filename"],
                    "src_path": str(c["src"]),
                    "dst_path": str(c["dst"]),
                    "src_hash": c["src_hash"],
                    "dst_hash": c["dst_hash"],
                    "error": "",
                })
            except Exception as e:
                failed.append((r, str(e)))
                writer.writerow({
                    "applied_at": timestamp,
                    "kind": c["kind"],
                    "action": "delete_failed",
                    "file_id": r["file_id"],
                    "filename": r["filename"],
                    "src_path": str(c["src"]),
                    "dst_path": str(c["dst"]),
                    "src_hash": c["src_hash"],
                    "dst_hash": c["dst_hash"],
                    "error": str(e)[:200],
                })
        # Log non-handled different files
        for c in different:
            r = c["record"]
            writer.writerow({
                "applied_at": timestamp,
                "kind": c["kind"],
                "action": "skipped_different",
                "file_id": r["file_id"],
                "filename": r["filename"],
                "src_path": str(c["src"]),
                "dst_path": str(c["dst"]),
                "src_hash": c["src_hash"],
                "dst_hash": c["dst_hash"],
                "error": "",
            })

    save_jsonl(disag_path, records)

    print()
    print("RÉSULTATS")
    print("─" * 70)
    print(f"  ✅ Doublons supprimés : {deleted}")
    print(f"  ❌ Suppressions échouées : {len(failed)}")
    print(f"  ⏸  Homonymes ≠ laissés intacts : {len(different)}")
    print()
    print(f"Log audit : {log_path.relative_to(repo_root)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="default")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    sys.exit(main(args.profile, args.run_id, args.execute))
