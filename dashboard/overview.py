"""Cockpit Overview — agrège des métriques par profil + métriques globales.

Cache mémoire 30 s par profil (snapshot) — invalidé par `?refresh=1` côté
endpoint ou `reset_cache()` côté tests.
"""
from __future__ import annotations

import json  # noqa: F401 — used by upcoming card functions (Task 2+)
import os  # noqa: F401 — used by upcoming card functions (Task 2+)
import threading
import time  # noqa: F401 — used by upcoming card functions (Task 2+)
from pathlib import Path  # noqa: F401 — used by upcoming card functions (Task 2+)

import yaml  # noqa: F401 — used by upcoming card functions (Task 2+)

from dashboard import data  # noqa: F401 — used by upcoming card functions (Task 2+)

# ─── Cache ────────────────────────────────────────────────────────
_CACHE_TTL_SECONDS = 30
_cache_lock = threading.Lock()
_overview_cache: dict[str, tuple[float, dict]] = {}


def reset_cache(profile: str | None = None) -> None:
    """Vide le cache (un profil ou tout). Utilisé par les tests."""
    with _cache_lock:
        if profile is None:
            _overview_cache.clear()
        else:
            _overview_cache.pop(profile, None)
