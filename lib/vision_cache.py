"""
Cache persistant des résultats LLM Vision.

Stocke les réponses `analyze_cover()` dans un fichier JSON par profil afin
d'éliminer les appels LLM redondants entre runs (cas d'usage typique :
tests fonctionnels répétés sur le même dataset).

Clé de cache : hash(model + prompt_version + sha256(premiers 64 KiB du PDF)).
Les 64 premiers KiB suffisent à identifier un PDF (header + xref).

Invalidation automatique : changer le modèle ou `PROMPT_VERSION` produit
de nouvelles clés, les anciennes entrées deviennent inertes (purgeables
via `./klodo.sh clean vision-cache`).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from threading import Lock

from lib.logger import get_logger

log = get_logger()

PROMPT_VERSION = "v3"  # v3: smart page selection (top-K par densité texte au lieu des N premières)
_PDF_HEAD_BYTES = 64 * 1024

_cache_lock = Lock()
_stats = {"hits": 0, "misses": 0, "writes": 0}


def compute_cache_key(
    pdf_path: str | Path,
    model: str,
    n_pages: int,
    prompt_version: str = PROMPT_VERSION,
) -> str | None:
    """Calcule la clé de cache pour un PDF donné.

    Retourne None si le PDF est illisible (cache miss forcé, pas de write).
    """
    try:
        with open(pdf_path, "rb") as f:
            head = f.read(_PDF_HEAD_BYTES)
    except OSError:
        return None

    if not head:
        return None

    composite = f"{model}|{prompt_version}|{n_pages}|{hashlib.sha256(head).hexdigest()}"
    return hashlib.md5(composite.encode()).hexdigest()


def load_cache(cache_path: Path) -> dict:
    """Charge le cache JSON depuis disque. Retourne {} si absent ou corrompu."""
    if not cache_path.exists():
        return {}
    try:
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except (OSError, json.JSONDecodeError) as e:
        log.warning(f"  ⚠ Cache vision illisible ({cache_path.name}): {e}")
        return {}


def save_cache(cache_path: Path, cache: dict) -> None:
    """Écrit le cache JSON sur disque, concurrency-safe.

    Plusieurs workers (ThreadPoolExecutor) peuvent appeler cette fonction
    en parallèle ; on sérialise les écritures avec `_cache_lock` et on
    relit la version sur disque sous lock pour ne pas écraser les ajouts
    d'autres workers (lost-update + tmp-rename race).
    """
    with _cache_lock:
        # Re-read disk state to avoid lost updates from other workers
        on_disk: dict = {}
        if cache_path.exists():
            try:
                with open(cache_path, encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    on_disk = loaded
            except (OSError, json.JSONDecodeError):
                # Corrupted on-disk version — discard, our in-memory copy wins
                pass
        # Merge: in-memory entries take precedence (latest writes win)
        merged = {**on_disk, **cache}
        # Atomic write
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        tmp.replace(cache_path)


def lookup(cache: dict, key: str) -> dict | None:
    """Retourne l'entrée en cache pour cette clé, ou None."""
    entry = cache.get(key)
    if not isinstance(entry, dict):
        return None
    with _cache_lock:
        _stats["hits"] += 1
    return entry.get("result")


def store(
    cache: dict,
    key: str,
    result: dict,
    model: str,
    prompt_version: str = PROMPT_VERSION,
) -> None:
    """Ajoute ou met à jour une entrée dans le dict cache en mémoire."""
    cache[key] = {
        "result": result,
        "model": model,
        "prompt_version": prompt_version,
        "cached_at": datetime.now().isoformat(timespec="seconds"),
    }
    with _cache_lock:
        _stats["writes"] += 1


def note_miss() -> None:
    """Incrémente le compteur de miss (appelé avant l'appel LLM réel)."""
    with _cache_lock:
        _stats["misses"] += 1


def get_stats() -> dict:
    """Retourne les stats d'utilisation du cache pour le run courant."""
    with _cache_lock:
        return dict(_stats)


def reset_stats() -> None:
    """Remet les compteurs à zéro (tests + début de run)."""
    with _cache_lock:
        _stats["hits"] = 0
        _stats["misses"] = 0
        _stats["writes"] = 0
