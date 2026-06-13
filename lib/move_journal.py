"""Journal append-only des déplacements de fichiers (refonte Apply/Execute).

Miroir de lib/rename_journal.py pour les MOVES inter-dossiers :
JSONL ``profiles/<p>/.cache/move-journal.jsonl``, records
``{ts, old, new, batch}``. Les undos sont eux-mêmes journalisés sous
``batch: "undo-<batch_id>"`` — audit trail complet, append-only.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path

JOURNAL_NAME = "move-journal.jsonl"


def _journal_path(profile_dir: Path) -> Path:
    return Path(profile_dir) / ".cache" / JOURNAL_NAME


def generate_batch_id() -> str:
    """Identifiant de batch unique, triable chronologiquement
    (même format que rename_journal : timestamp + suffixe aléatoire)."""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{ts}-{uuid.uuid4().hex[:6]}"


def append_move(
    profile_dir: Path,
    old_abs: str,
    new_abs: str,
    batch_id: str = "",
) -> dict:
    """Journalise un move déjà effectué (ordre : move PUIS journal,
    comme le ``commit_rename`` de rename_journal)."""
    journal = _journal_path(profile_dir)
    journal.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "old": str(old_abs),
        "new": str(new_abs),
        "batch": batch_id,
    }
    with open(journal, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def read_journal(profile_dir: Path) -> list[dict]:
    """Tous les records, du plus ancien au plus récent. Lignes
    malformées skippées silencieusement."""
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


def undo_record(profile_dir: Path, record: dict) -> dict:
    """Reverse un move : ``new`` → ``old``. L'inverse est journalisé
    (``batch: "undo-<batch>"``).

    Raises:
      FileNotFoundError — ``new`` n'existe plus (déplacé hors dashboard).
      FileExistsError   — un fichier occupe déjà ``old`` (collision).
    """
    old = record["old"]
    new = record["new"]
    if not os.path.exists(new):
        raise FileNotFoundError(f"new path missing, cannot undo: {new}")
    if os.path.exists(old):
        raise FileExistsError(f"collision at original path: {old}")
    os.makedirs(os.path.dirname(old), exist_ok=True)
    os.rename(new, old)
    original_batch = record.get("batch") or ""
    inverse_batch = ("undo-" + original_batch) if original_batch else "undo"
    return append_move(profile_dir, old_abs=new, new_abs=old, batch_id=inverse_batch)


def undo_batch(profile_dir: Path, batch_id: str) -> dict:
    """Reverse tous les moves d'un batch, du plus récent au plus ancien.
    Échecs individuels skippés + rapportés (jamais d'abort global).

    Returns:
        {"batch": str, "n_undone": int, "n_failed": int,
         "failures": [{"old", "new", "error"}]}
    """
    records = [r for r in read_journal(profile_dir)
               if r.get("batch") == batch_id]
    n_undone = 0
    failures: list[dict] = []
    for rec in reversed(records):
        try:
            undo_record(profile_dir, rec)
            n_undone += 1
        except OSError as exc:
            failures.append({
                "old": rec.get("old", ""),
                "new": rec.get("new", ""),
                "error": str(exc),
            })
    return {
        "batch": batch_id,
        "n_undone": n_undone,
        "n_failed": len(failures),
        "failures": failures,
    }
