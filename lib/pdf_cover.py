"""
Cache mémoire intra-run pour l'extraction Poppler des premières pages d'un PDF.

Mutualise l'appel `pdf2image.convert_from_path()` entre les différents call sites
(rename LLM, classify LLM, viewer Curation si même process). Un seul Poppler par
(pdf_path, dpi, n_pages) — les appels suivants retournent l'image en RAM.

Cache LRU borné en mémoire (PIL.Image ~11 MB chacune à 1700×2200 RGB).
"""

from __future__ import annotations

from collections import OrderedDict
from threading import Lock
from typing import TYPE_CHECKING

from lib.constants import PDF_DPI, PDF_EXTRACT_THREADS
from lib.logger import get_logger

if TYPE_CHECKING:
    from PIL import Image

log = get_logger()

_MAX_ENTRIES = 32
_cache: OrderedDict[tuple[str, int, int], list] = OrderedDict()
_lock = Lock()
_stats = {"hits": 0, "misses": 0}


def get_cover_image(
    pdf_path: str,
    dpi: int = PDF_DPI,
    n_pages: int = 1,
    page_indices: tuple[int, ...] | None = None,
) -> list["Image.Image"] | None:
    """Extraction mutualisée des pages d'un PDF.

    Args:
        n_pages: si page_indices=None, extrait les n_pages premières
                 (comportement legacy, contigu).
        page_indices: si fourni (1-based), extrait spécifiquement ces pages.
                      Permet de récupérer des pages non-contiguës après
                      smart page selection (lib/page_selector).

    Cache mémoire LRU intra-process. Retourne une liste d'images PIL
    ou None en cas d'échec.
    """
    if page_indices is not None:
        page_indices = tuple(sorted(set(page_indices)))
        key = (str(pdf_path), dpi, "idx", page_indices)
    else:
        key = (str(pdf_path), dpi, "n", n_pages)

    with _lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            _stats["hits"] += 1
            return hit

    try:
        from pdf2image import convert_from_path
    except ImportError:
        log.warning("  ⚠ pdf2image non installé")
        return None

    try:
        if page_indices is not None and len(page_indices) > 0:
            # Extract the contiguous span covering all selected pages,
            # then slice. Cheaper than N separate Poppler invocations.
            first = page_indices[0]
            last = page_indices[-1]
            span = convert_from_path(
                pdf_path, first_page=first, last_page=last,
                dpi=dpi, fmt="png", thread_count=PDF_EXTRACT_THREADS,
            )
            # span[i] corresponds to page (first + i)
            images = [span[i - first] for i in page_indices
                      if 0 <= i - first < len(span)]
        else:
            images = convert_from_path(
                pdf_path, first_page=1, last_page=n_pages,
                dpi=dpi, fmt="png", thread_count=PDF_EXTRACT_THREADS,
            )
    except Exception as e:
        log.error(f"  ⚠ Erreur extraction couverture: {e}")
        return None

    if not images:
        return None

    with _lock:
        _cache[key] = images
        _cache.move_to_end(key)
        while len(_cache) > _MAX_ENTRIES:
            _cache.popitem(last=False)
        _stats["misses"] += 1

    return images


def clear_cover_cache() -> None:
    """Vide complètement le cache (tests, fin de run explicite)."""
    with _lock:
        _cache.clear()
        _stats["hits"] = 0
        _stats["misses"] = 0


def get_cover_cache_stats() -> dict:
    """Retourne (hits, misses, size) — utile pour logs de fin de run."""
    with _lock:
        return {
            "hits": _stats["hits"],
            "misses": _stats["misses"],
            "size": len(_cache),
        }
