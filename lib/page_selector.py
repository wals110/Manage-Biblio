"""Smart page selection for vision LLM input.

Many PDFs have largely-empty front matter:
- page 1: title page (sparse, just the title)
- page 2: blank or copyright/legal
- page 3: dedication or short foreword
- page 4: table of contents (highest signal)
- page 5: introduction (high signal)

Sending pages 1-3 in raw order misses the TOC and wastes tokens on blank
pages. This module picks the K most informative pages out of N candidates,
based on embedded text density (no OCR — uses pypdf's direct extraction).

For scanned PDFs (no embedded text), falls back to default page ordering.
"""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path

from lib.logger import get_logger

log = get_logger()

# Silence pypdf's noisy warnings about malformed objects in third-party PDFs.
# pypdf logs at WARNING level for parser inconsistencies AND prints raw to
# stderr for "Object N not defined" — both pollute the run log without
# affecting our scoring (we tolerate per-page failures via try/except).
for _name in ("pypdf", "pypdf.generic", "pypdf._reader", "PyPDF2"):
    logging.getLogger(_name).setLevel(logging.ERROR)


@contextlib.contextmanager
def _silence_stderr():
    """Redirect raw stderr writes (incl. pypdf's print() statements about
    malformed PDF objects) into a discarded buffer."""
    devnull = open(os.devnull, "w")
    try:
        with contextlib.redirect_stderr(devnull):
            yield
    finally:
        devnull.close()


def _alpha_count(text: str) -> int:
    """Count alpha chars (letters in any script). Whitespace, digits, and
    punctuation are excluded — they're not signal for topic detection."""
    if not text:
        return 0
    return sum(1 for c in text if c.isalpha())


def score_pages(pdf_path: str | Path, n_candidates: int = 5) -> list[tuple[int, int]]:
    """Return a list of (page_index_1based, alpha_char_count) for the
    first n_candidates pages of the PDF.

    Pages where extraction fails get a score of 0. If the entire PDF
    yields zero text (scanned), all scores are 0 — caller should fall back.
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        log.warning("  ⚠ pypdf non installé — pas de smart page selection")
        return []

    with _silence_stderr():
        try:
            reader = PdfReader(str(pdf_path))
        except Exception as e:
            log.warning(f"  ⚠ pypdf échoue sur {Path(pdf_path).name}: {e}")
            return []

        n = min(len(reader.pages), n_candidates)
        out: list[tuple[int, int]] = []
        for i in range(n):
            try:
                text = reader.pages[i].extract_text() or ""
                out.append((i + 1, _alpha_count(text)))
            except Exception:
                out.append((i + 1, 0))
    return out


def select_top_pages(
    pdf_path: str | Path,
    n_candidates: int = 5,
    n_keep: int = 2,
    min_score: int = 30,
) -> list[int]:
    """Return the indices (1-based) of the top n_keep most informative
    pages out of the first n_candidates. Returns sorted indices for
    natural reading order.

    Page 1 is ALWAYS included — it's the cover/title page, and most title
    pages have low text density (a single large title), which means the
    text-density heuristic would otherwise skip it. We then fill the
    remaining n_keep-1 slots with the densest pages from pages 2..N.
    Without this, files like Springer Lecture Notes, slides, and many
    monographs return empty titles from the LLM (it received TOC+intro
    but not the cover).

    Behavior:
    - If pypdf yields no scores (scanned PDF, broken PDF, missing dep)
      → fall back to first n_keep pages.
    - If all scores are below min_score → likely a sparse document; fall
      back to first n_keep pages (the heuristic is unreliable here).
    - Otherwise → page 1 + top (n_keep-1) by score among pages 2..N.

    Args:
        n_candidates: how many initial pages to consider (typical 5)
        n_keep: how many pages to keep (typical 2)
        min_score: below this alpha count, a page is treated as "blank-ish"
                   and the heuristic is bypassed if ALL pages are this empty.
    """
    if n_keep <= 0:
        return []
    n_keep = max(1, n_keep)
    n_candidates = max(n_keep, n_candidates)

    scores = score_pages(pdf_path, n_candidates=n_candidates)
    if not scores:
        return list(range(1, n_keep + 1))

    max_score = max(s for _, s in scores)
    if max_score < min_score:
        # Whole document looks sparse / scanned — heuristic is noisy
        return list(range(1, n_keep + 1))

    # Page 1 is the title page → always include. Then fill remaining slots
    # with the densest pages from 2..N. Ties broken by lower page index.
    chosen = {1}
    rest = sorted((p for p in scores if p[0] != 1),
                  key=lambda x: (-x[1], x[0]))
    for idx, _ in rest:
        if len(chosen) >= n_keep:
            break
        chosen.add(idx)
    return sorted(chosen)
