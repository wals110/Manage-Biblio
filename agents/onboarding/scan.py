"""Scan & estimation — léger, read-only, aucun appel LLM."""

from __future__ import annotations

import os

_EXTS = (".pdf", ".epub")


def scan_directory(path: str) -> dict:
    """Compte les fichiers classables + formats + détecte une pré-organisation."""
    n_files = 0
    by_format: dict[str, int] = {}
    top_folders: set[str] = set()
    has_sub = False
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        rel = os.path.relpath(root, path)
        if rel != ".":
            has_sub = True
            top_folders.add(rel.split(os.sep)[0])
        for f in files:
            if f.startswith(".") or not f.lower().endswith(_EXTS):
                continue
            n_files += 1
            ext = f.lower().rsplit(".", 1)[-1]
            by_format[ext] = by_format.get(ext, 0) + 1
    return {
        "path": path,
        "n_files": n_files,
        "by_format": by_format,
        "has_subfolders": has_sub,
        "top_folders": sorted(top_folders),
    }


def estimate_cost(n_files: int, cost_per_call: float, n_pages: int = 2,
                  sec_per_call: float = 1.1) -> dict:
    """Estimation Vision : 1 appel/fichier. Coût + durée approximatifs."""
    n_calls = n_files
    return {
        "n_calls": n_calls,
        "usd": round(n_calls * cost_per_call, 2),
        "eta_min": round(n_calls * sec_per_call / 60, 1),
    }
