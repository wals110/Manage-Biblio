"""Tests for lib/vision_cache.py — persistent JSON cache for LLM Vision results."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from lib import vision, vision_cache  # noqa: E402


class CacheKeyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pdf = Path(self.tmp.name) / "book.pdf"
        self.pdf.write_bytes(b"%PDF-1.7\n" + b"A" * 2000)

    def tearDown(self):
        self.tmp.cleanup()

    def test_key_is_deterministic(self):
        k1 = vision_cache.compute_cache_key(self.pdf, model="m1", n_pages=1)
        k2 = vision_cache.compute_cache_key(self.pdf, model="m1", n_pages=1)
        self.assertEqual(k1, k2)

    def test_key_depends_on_model(self):
        k1 = vision_cache.compute_cache_key(self.pdf, model="m1", n_pages=1)
        k2 = vision_cache.compute_cache_key(self.pdf, model="m2", n_pages=1)
        self.assertNotEqual(k1, k2)

    def test_key_depends_on_n_pages(self):
        k1 = vision_cache.compute_cache_key(self.pdf, model="m1", n_pages=1)
        k2 = vision_cache.compute_cache_key(self.pdf, model="m1", n_pages=2)
        self.assertNotEqual(k1, k2)

    def test_key_depends_on_prompt_version(self):
        k1 = vision_cache.compute_cache_key(self.pdf, "m1", 1, prompt_version="v1")
        k2 = vision_cache.compute_cache_key(self.pdf, "m1", 1, prompt_version="v2")
        self.assertNotEqual(k1, k2)

    def test_key_depends_on_content(self):
        other = Path(self.tmp.name) / "other.pdf"
        other.write_bytes(b"%PDF-1.7\n" + b"B" * 2000)
        k1 = vision_cache.compute_cache_key(self.pdf, "m1", 1)
        k2 = vision_cache.compute_cache_key(other, "m1", 1)
        self.assertNotEqual(k1, k2)

    def test_missing_file_returns_none(self):
        self.assertIsNone(
            vision_cache.compute_cache_key(Path("/no/such/file.pdf"), "m1", 1))


class LoadSaveTests(unittest.TestCase):
    def test_load_missing_file_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            self.assertEqual(vision_cache.load_cache(path), {})

    def test_load_corrupt_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            path.write_text("{ not json")
            self.assertEqual(vision_cache.load_cache(path), {})

    def test_save_and_reload_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "cache.json"
            cache = {}
            vision_cache.store(cache, "abc", {"title": "T"}, model="m1")
            vision_cache.save_cache(path, cache)
            reloaded = vision_cache.load_cache(path)
            self.assertIn("abc", reloaded)
            self.assertEqual(reloaded["abc"]["result"]["title"], "T")


class AnalyzeCoverCachedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pdf = Path(self.tmp.name) / "book.pdf"
        self.pdf.write_bytes(b"%PDF-1.7\n" + b"Z" * 4096)
        self.cache_path = Path(self.tmp.name) / "vision_cache.json"
        vision_cache.reset_stats()

    def tearDown(self):
        self.tmp.cleanup()
        vision_cache.reset_stats()

    def _fake_vision_result(self):
        return {
            "title": "Clean Code",
            "author": "Robert Martin",
            "theme": "Programming",
            "language": "en",
            "confidence": 0.95,
        }

    def test_miss_then_hit(self):
        with patch.object(vision, "analyze_cover", return_value=self._fake_vision_result()) as mocked:
            r1 = vision.analyze_cover_cached(
                str(self.pdf), self.cache_path, model="m1")
            r2 = vision.analyze_cover_cached(
                str(self.pdf), self.cache_path, model="m1")

        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(r1["title"], "Clean Code")
        self.assertEqual(r2["title"], "Clean Code")
        self.assertTrue(self.cache_path.exists())

    def test_different_model_causes_new_call(self):
        with patch.object(vision, "analyze_cover", return_value=self._fake_vision_result()) as mocked:
            vision.analyze_cover_cached(str(self.pdf), self.cache_path, model="m1")
            vision.analyze_cover_cached(str(self.pdf), self.cache_path, model="m2")
        self.assertEqual(mocked.call_count, 2)

    def test_error_result_is_not_cached(self):
        with patch.object(vision, "analyze_cover", return_value={"error": "api"}) as mocked:
            vision.analyze_cover_cached(str(self.pdf), self.cache_path, model="m1")
            vision.analyze_cover_cached(str(self.pdf), self.cache_path, model="m1")
        self.assertEqual(mocked.call_count, 2)
        self.assertFalse(self.cache_path.exists())

    def test_empty_title_not_cached(self):
        empty = {
            "title": "", "author": "", "theme": "",
            "language": "", "confidence": 0.1,
        }
        with patch.object(vision, "analyze_cover", return_value=empty) as mocked:
            vision.analyze_cover_cached(str(self.pdf), self.cache_path, model="m1")
            vision.analyze_cover_cached(str(self.pdf), self.cache_path, model="m1")
        self.assertEqual(mocked.call_count, 2)

    def test_persists_across_fresh_loads(self):
        with patch.object(vision, "analyze_cover", return_value=self._fake_vision_result()):
            vision.analyze_cover_cached(str(self.pdf), self.cache_path, model="m1")

        with open(self.cache_path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(len(data), 1)
        entry = next(iter(data.values()))
        self.assertEqual(entry["model"], "m1")
        self.assertIn("cached_at", entry)
        self.assertEqual(entry["result"]["title"], "Clean Code")

        with patch.object(vision, "analyze_cover") as mocked:
            result = vision.analyze_cover_cached(
                str(self.pdf), self.cache_path, model="m1")
        self.assertEqual(mocked.call_count, 0)
        self.assertEqual(result["title"], "Clean Code")


if __name__ == "__main__":
    unittest.main()
