#!/usr/bin/env python3
"""Tests pour lib/page_selector.py — smart page selection pour le LLM Vision.

Couvre la règle critique : la page 1 (couverture) est TOUJOURS incluse
dans la sélection, même si elle a peu de texte. Sans cette règle, le
heuristique de densité de texte abandonne la cover (typiquement
sparse, juste un grand titre) au profit de TOC/intro, et le LLM se
retrouve à analyser des pages sans le titre principal du livre.
"""

import os
import sys
import unittest
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from lib.page_selector import select_top_pages  # noqa: E402


class TestSelectTopPages(unittest.TestCase):
    """Vérifie le contrat de select_top_pages : page 1 toujours incluse."""

    def _patch_scores(self, scores):
        """Patch score_pages to return a fixed list of (page_idx, alpha_count)."""
        return mock.patch("lib.page_selector.score_pages", return_value=scores)

    def test_page_1_always_included_when_sparse(self):
        """Le cas classique d'un livre Springer : page 1 a peu de texte (titre seul),
        pages 4 et 5 sont les plus denses (TOC + intro). Sans la garantie page 1,
        l'ancien algorithme renvoyait [4, 5] et perdait le titre."""
        scores = [(1, 10), (2, 5), (3, 0), (4, 200), (5, 150)]
        with self._patch_scores(scores):
            result = select_top_pages("dummy.pdf", n_candidates=5, n_keep=2)
        self.assertIn(1, result, "Page 1 doit toujours être incluse")
        self.assertEqual(result, [1, 4])  # page 1 + plus dense parmi 2-5

    def test_page_1_already_densest(self):
        """Quand page 1 est la plus dense, on garde page 1 + 2e plus dense."""
        scores = [(1, 500), (2, 100), (3, 50), (4, 30), (5, 10)]
        with self._patch_scores(scores):
            result = select_top_pages("dummy.pdf", n_candidates=5, n_keep=2)
        self.assertEqual(result, [1, 2])

    def test_n_keep_3_with_sparse_cover(self):
        """Avec n_keep=3, page 1 + top 2 des restantes."""
        scores = [(1, 10), (2, 200), (3, 150), (4, 100), (5, 50)]
        with self._patch_scores(scores):
            result = select_top_pages("dummy.pdf", n_candidates=5, n_keep=3)
        # Page 1 obligatoire + pages 2 et 3 (les plus denses parmi 2-5)
        self.assertEqual(result, [1, 2, 3])

    def test_all_pages_empty_falls_back_to_first_n(self):
        """Si tout est sous min_score (PDF scanné/blanche), fallback sur les
        n_keep premières pages."""
        scores = [(1, 0), (2, 0), (3, 0), (4, 0), (5, 0)]
        with self._patch_scores(scores):
            result = select_top_pages("dummy.pdf", n_candidates=5, n_keep=2)
        self.assertEqual(result, [1, 2])

    def test_empty_scores_falls_back(self):
        """Pas de scores (pypdf échoue) → fallback sur n_keep premières."""
        with self._patch_scores([]):
            result = select_top_pages("dummy.pdf", n_candidates=5, n_keep=2)
        self.assertEqual(result, [1, 2])

    def test_returns_sorted_indices(self):
        """Le résultat doit toujours être trié (ordre de lecture naturel)."""
        scores = [(1, 50), (2, 10), (3, 200), (4, 5), (5, 100)]
        with self._patch_scores(scores):
            result = select_top_pages("dummy.pdf", n_candidates=5, n_keep=3)
        self.assertEqual(result, sorted(result))

    def test_n_keep_1_just_returns_page_1(self):
        """Avec n_keep=1, on ne renvoie que la cover (page 1)."""
        scores = [(1, 10), (2, 500), (3, 400)]
        with self._patch_scores(scores):
            result = select_top_pages("dummy.pdf", n_candidates=3, n_keep=1)
        self.assertEqual(result, [1])

    def test_short_pdf_fewer_pages_than_n_candidates(self):
        """PDF de 3 pages avec n_candidates=5 : on ne dépasse pas."""
        scores = [(1, 10), (2, 200), (3, 100)]
        with self._patch_scores(scores):
            result = select_top_pages("dummy.pdf", n_candidates=5, n_keep=2)
        self.assertIn(1, result)
        self.assertEqual(result, [1, 2])  # page 1 + plus dense (page 2)

    def test_n_keep_zero(self):
        """Cas pathologique : n_keep=0 → liste vide, pas d'appel à score_pages."""
        result = select_top_pages("dummy.pdf", n_candidates=5, n_keep=0)
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
