"""Moteur de déplacement de fichiers source-agnostique.

Partagé par l'apply refonte (agent_refonte_apply) et l'apply global
(reclassify_apply). Ne connaît NI run_id NI refonte : il reçoit
(target, liste de moves, profile_dir, callback de progression) et
exécute la boucle dangereuse une seule fois, ici.
"""

from __future__ import annotations

import csv
import os
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from dashboard import data, taxonomy
from lib import move_journal

_PROGRESS_EVERY = 50


class ApplyError(Exception):
    """Erreur transport (status HTTP porté par l'exception)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def spawn(target: Callable, args: tuple, name: str) -> None:
    """Lance ``target`` dans un thread daemon. Indirection volontaire :
    les tests patchent ``spawn`` pour exécuter en synchrone."""
    thread = threading.Thread(target=target, args=args, daemon=True, name=name)
    thread.start()


def logs_dir() -> Path:
    d = data.get_project_root() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def target_path(profile: str) -> Path:
    target = taxonomy._profile_target_path(profile)
    if target is None or not target.exists():
        raise ApplyError("target du profil introuvable (SSD non monté ?)", 500)
    return target


def safe_target_subdir(target: Path, rel: str) -> Path:
    """Résout ``rel`` sous le target et refuse toute évasion (../…)."""
    d = (target / rel).resolve()
    try:
        d.relative_to(target.resolve())
    except ValueError as exc:
        raise ApplyError(f"chemin hors du target : {rel!r}", 400) from exc
    return d


def prune_empty_dirs(target: Path, rel_folders: set[str]) -> None:
    """Supprime les dossiers sources devenus vides en remontant —
    jamais le target lui-même."""
    target = target.resolve()
    for rel in sorted(rel_folders, key=lambda p: p.count("/"), reverse=True):
        d = (target / rel).resolve()
        while d != target and d.is_relative_to(target):
            try:
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
                else:
                    break
            except OSError:
                break
            d = d.parent


def execute_move_batch(
    target: Path,
    moves: list[dict],
    profile_dir: Path,
    on_progress: Callable[[dict], None],
) -> dict[str, Any]:
    """Boucle de déplacement (synchrone). Pré-vol anti-évasion → par
    fichier : garde fraîcheur (stale) → garde collision → os.rename →
    journal. Échec individuel = skip + rapport, jamais d'abort. Écrit le
    rapport CSV, prune les dossiers vides. ``on_progress`` reçoit les
    payloads de progression (sans clé 'op' — l'appelant l'ajoute).

    Crash mid-batch : trou d'1 record max (ordre move→journal).
    """
    for m in moves:
        safe_target_subdir(target, m["rel_path"])
        safe_target_subdir(target, m["proposed_folder"])

    batch_id = move_journal.generate_batch_id()
    n_moved = n_failed = n_skipped = 0
    report_rows: list[dict[str, str]] = []
    source_folders: set[str] = set()
    n_total = len(moves)
    on_progress({"status": "running", "n_done": 0, "n_total": n_total,
                 "n_failed": 0, "n_skipped": 0, "error": None})

    for i, m in enumerate(moves, start=1):
        rel = m["rel_path"]
        old = target / rel
        new = target / m["proposed_folder"] / os.path.basename(rel)
        status = ""
        detail = ""
        if not old.exists():
            status, n_skipped = "stale", n_skipped + 1
            detail = "source absente (déplacée depuis la simulation)"
        elif new.exists():
            status, n_skipped = "collision", n_skipped + 1
            detail = "destination occupée — jamais d'écrasement"
        else:
            try:
                new.parent.mkdir(parents=True, exist_ok=True)
                os.rename(old, new)
                move_journal.append_move(
                    profile_dir, str(old), str(new), batch_id=batch_id)
                status, n_moved = "moved", n_moved + 1
                source_folders.add(os.path.dirname(rel))
            except OSError as exc:
                status, n_failed = "error", n_failed + 1
                detail = str(exc)
        report_rows.append({"rel_path": rel, "old": str(old), "new": str(new),
                            "status": status, "detail": detail})
        if i % _PROGRESS_EVERY == 0:
            on_progress({"status": "running", "n_done": i, "n_total": n_total,
                         "n_failed": n_failed, "n_skipped": n_skipped,
                         "error": None})

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = logs_dir() / f"rapport_apply_{ts}.csv"
    with report_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["rel_path", "old", "new", "status", "detail"])
        writer.writeheader()
        writer.writerows(report_rows)

    prune_empty_dirs(target, source_folders)
    return {"n_moved": n_moved, "n_failed": n_failed, "n_skipped": n_skipped,
            "n_total": n_total, "report": report_path.name, "batch_id": batch_id}
