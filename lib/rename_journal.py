"""Append-only JSONL journal of filename rename operations.

Each rename (single or part of a bulk batch) appends one JSON line to
``profile/.cache/rename-journal.jsonl``. The journal is the single
source of truth for undo: there is no other record of the previous
filenames once the FS rename completes.

Record schema
-------------

::

    {"ts": "2026-05-17T10:30:00", "old": "/abs/old.pdf",
     "new": "/abs/new.pdf", "batch": "20260517-103000-a1b2c3"}

- ``ts``     local ISO timestamp at rename time (second precision).
- ``old``    absolute path BEFORE the rename.
- ``new``    absolute path AFTER the rename.
- ``batch``  optional id grouping renames that ran as one bulk; empty
             string for single (one-shot) renames. Undoing a batch
             replays each line in reverse and tags the inverse ops with
             ``"undo-<original_batch>"`` so the journal stays append-only.

Append-only by design — never edit nor rewrite existing lines. Recovery
is by replay.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path


def _journal_path(profile_dir: Path) -> Path:
    return profile_dir / ".cache" / "rename-journal.jsonl"


def generate_batch_id() -> str:
    """Sortable batch id (chronological prefix + short random suffix).

    Example: ``"20260517-103000-a1b2c3"``. Used to tag every record
    written during one bulk operation so the user can undo the lot.
    """
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{ts}-{uuid.uuid4().hex[:6]}"


def append_rename(
    profile_dir: Path,
    old_abs: str,
    new_abs: str,
    batch_id: str = "",
) -> dict:
    """Append one rename record to the journal. Returns the dict written.

    The caller is responsible for performing the actual FS rename — this
    function only records it. Recommended call order:

      1. journal.append_rename(...)   ← record FIRST so a crash leaves a
                                        trace even if the rename later fails
      2. os.rename(old, new)

    Wait — but if the rename then fails, the journal lies. So most callers
    prefer the opposite order (rename then journal). Both are valid; the
    safer one depends on what you fear more: losing the trace, or having
    a stale entry. We do "rename then journal" in the production caller
    (`commit_rename`) and recommend that pattern.
    """
    journal = _journal_path(profile_dir)
    journal.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "old": old_abs,
        "new": new_abs,
        "batch": batch_id,
    }
    with open(journal, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def read_journal(profile_dir: Path) -> list[dict]:
    """Return every record in the journal, oldest first.

    Tolerates malformed lines (skips them) but reports nothing — readers
    that care about integrity should validate themselves.
    """
    journal = _journal_path(profile_dir)
    if not journal.exists():
        return []
    out: list[dict] = []
    with open(journal, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def list_renames(profile_dir: Path, limit: int = 100) -> list[dict]:
    """Last ``limit`` records, newest first. Convenient for an audit UI."""
    all_recs = read_journal(profile_dir)
    return list(reversed(all_recs))[:limit]


def list_batches(profile_dir: Path) -> list[dict]:
    """Group records by batch id, newest first.

    Single (un-tagged, ``batch == ""``) renames are skipped from this
    view — they're individually visible via ``list_renames``. Each batch
    summary: ``{batch, ts, n_renames, records[]}``.
    """
    by_batch: dict[str, dict] = {}
    for r in read_journal(profile_dir):
        bid = r.get("batch") or ""
        if not bid or bid.startswith("undo-"):
            continue
        slot = by_batch.setdefault(bid, {
            "batch": bid,
            "ts": r.get("ts", ""),
            "n_renames": 0,
            "records": [],
        })
        slot["n_renames"] += 1
        slot["records"].append(r)
        # Keep the earliest ts as the batch start
        if r.get("ts", "") < slot["ts"]:
            slot["ts"] = r["ts"]
    return sorted(by_batch.values(), key=lambda g: g["ts"], reverse=True)


def undo_record(profile_dir: Path, record: dict) -> dict:
    """Reverse one rename: move ``new`` back to ``old``. The inverse op
    is appended to the journal as well (tagged with ``"undo-<batch>"``)
    so the audit trail remains complete.

    Raises:
      FileNotFoundError — if the ``new`` path doesn't exist (someone moved
                          the file outside the dashboard).
      FileExistsError   — if a file already sits at ``old`` (collision —
                          the user re-created a file with the original
                          name in between).
    """
    old = record["old"]
    new = record["new"]
    if not os.path.exists(new):
        raise FileNotFoundError(
            f"new path missing, cannot undo: {new}")
    if os.path.exists(old):
        # APFS / HFS+ case-insensitive guard: ``old`` may "exist" because
        # it refers to the same inode as ``new`` (case-only rename). In
        # that case it's not a real collision — os.rename will simply
        # change the visible case.
        try:
            same = os.path.samefile(old, new)
        except OSError:
            same = False
        if not same:
            raise FileExistsError(
                f"old path is occupied, cannot undo: {old}")
    os.rename(new, old)
    original_batch = record.get("batch") or ""
    inverse_tag = "undo-" + original_batch if original_batch else "undo"
    return append_rename(
        profile_dir, old_abs=new, new_abs=old, batch_id=inverse_tag)


def undo_batch(profile_dir: Path, batch_id: str) -> dict:
    """Reverse every record of a batch in REVERSE order.

    Returns a summary ``{undone: [paths_now_back_to], errors: [{old, new,
    err}]}`` — errors are surfaced per-record, the batch otherwise
    completes (best-effort). Records already undone (their inverse is in
    the journal) are skipped.
    """
    records = [r for r in read_journal(profile_dir)
               if (r.get("batch") or "") == batch_id]
    if not records:
        raise FileNotFoundError(f"no records for batch {batch_id!r}")

    # Detect already-undone records: an inverse exists whose 'old' equals
    # this record's 'new' AND batch is "undo-<batch_id>".
    inverse_tag = "undo-" + batch_id
    already_undone_new_paths = {
        r["old"] for r in read_journal(profile_dir)
        if r.get("batch") == inverse_tag
    }

    undone: list[str] = []
    errors: list[dict] = []
    # Undo in REVERSE order — useful if a chain of renames depended on
    # earlier ones (shouldn't happen within a batch, but defensive).
    for r in reversed(records):
        if r["new"] in already_undone_new_paths:
            continue
        try:
            undo_record(profile_dir, r)
            undone.append(r["old"])
        except (FileNotFoundError, FileExistsError) as e:
            errors.append({"old": r["old"], "new": r["new"], "err": str(e)})
    return {"batch": batch_id, "undone": undone, "errors": errors,
            "n_undone": len(undone), "n_errors": len(errors)}
