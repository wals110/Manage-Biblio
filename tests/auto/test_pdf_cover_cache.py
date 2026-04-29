"""Tests for lib/pdf_cover.py — Poppler call mutualization via in-memory LRU cache."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from lib import pdf_cover  # noqa: E402


class FakeImage:
    def __init__(self, label):
        self.label = label

    def __repr__(self):
        return f"FakeImage({self.label})"


class PdfCoverCacheTests(unittest.TestCase):
    def setUp(self):
        pdf_cover.clear_cover_cache()

    def tearDown(self):
        pdf_cover.clear_cover_cache()

    def test_second_call_same_key_uses_cache(self):
        with patch("pdf2image.convert_from_path") as mocked:
            mocked.return_value = [FakeImage("p1")]
            a = pdf_cover.get_cover_image("/fake/a.pdf", dpi=150, n_pages=1)
            b = pdf_cover.get_cover_image("/fake/a.pdf", dpi=150, n_pages=1)

        self.assertEqual(mocked.call_count, 1)
        self.assertIs(a, b)

        stats = pdf_cover.get_cover_cache_stats()
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["misses"], 1)

    def test_different_dpi_is_different_key(self):
        with patch("pdf2image.convert_from_path") as mocked:
            mocked.return_value = [FakeImage("p1")]
            pdf_cover.get_cover_image("/fake/a.pdf", dpi=150, n_pages=1)
            pdf_cover.get_cover_image("/fake/a.pdf", dpi=300, n_pages=1)
        self.assertEqual(mocked.call_count, 2)

    def test_different_n_pages_is_different_key(self):
        with patch("pdf2image.convert_from_path") as mocked:
            mocked.return_value = [FakeImage("p1")]
            pdf_cover.get_cover_image("/fake/a.pdf", dpi=150, n_pages=1)
            pdf_cover.get_cover_image("/fake/a.pdf", dpi=150, n_pages=2)
        self.assertEqual(mocked.call_count, 2)

    def test_clear_cover_cache_resets_state(self):
        with patch("pdf2image.convert_from_path") as mocked:
            mocked.return_value = [FakeImage("p1")]
            pdf_cover.get_cover_image("/fake/a.pdf")
            pdf_cover.clear_cover_cache()
            pdf_cover.get_cover_image("/fake/a.pdf")
        self.assertEqual(mocked.call_count, 2)
        stats = pdf_cover.get_cover_cache_stats()
        self.assertEqual(stats["hits"], 0)

    def test_extraction_failure_returns_none_no_cache(self):
        with patch("pdf2image.convert_from_path", side_effect=RuntimeError("boom")):
            result = pdf_cover.get_cover_image("/fake/bad.pdf")
        self.assertIsNone(result)
        self.assertEqual(pdf_cover.get_cover_cache_stats()["size"], 0)

    def test_empty_result_returns_none(self):
        with patch("pdf2image.convert_from_path", return_value=[]):
            result = pdf_cover.get_cover_image("/fake/empty.pdf")
        self.assertIsNone(result)
        self.assertEqual(pdf_cover.get_cover_cache_stats()["size"], 0)

    def test_lru_eviction_respects_max_entries(self):
        with patch.object(pdf_cover, "_MAX_ENTRIES", 3), \
             patch("pdf2image.convert_from_path") as mocked:
            mocked.return_value = [FakeImage("x")]
            for i in range(5):
                pdf_cover.get_cover_image(f"/fake/{i}.pdf")
            self.assertLessEqual(pdf_cover.get_cover_cache_stats()["size"], 3)


if __name__ == "__main__":
    unittest.main()
