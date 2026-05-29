#!/usr/bin/env python3
"""Tests pour lib.theme_resolver — D.1 du chantier C.2.

Couvre :
  - extract_themes_with_titles : collecte 5 titres par thème, dédup, ordre stable
  - count_themes : occurrences brutes
  - themes_with_titles_iter : ordre lexico déterministe
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from dashboard import data  # noqa: E402
from lib.theme_resolver import (  # noqa: E402
    TITLES_PER_THEME,
    count_themes,
    extract_themes_with_titles,
    themes_with_titles_iter,
)


class _ResolverBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="klodo_resolver_"))
        (self.tmp / "profiles" / "p" / ".cache").mkdir(parents=True)
        self._patcher = mock.patch.object(
            data, "get_project_root", return_value=self.tmp,
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_vision_cache(self, entries: list[dict]) -> None:
        path = self.tmp / "profiles" / "p" / ".cache" / "vision_cache.json"
        cache = {f"hash_{i}": e for i, e in enumerate(entries)}
        path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


class TestExtractThemesWithTitles(_ResolverBase):
    def test_no_cache_returns_empty(self):
        self.assertEqual(extract_themes_with_titles("p"), {})

    def test_basic_collection(self):
        self._write_vision_cache([
            {"result": {"title": "Book A", "themes": [{"theme": "Logic"}]}},
            {"result": {"title": "Book B", "themes": [{"theme": "Logic"}]}},
            {"result": {"title": "Book C", "themes": [{"theme": "Statistics"}]}},
        ])
        out = extract_themes_with_titles("p")
        self.assertEqual(set(out.keys()), {"Logic", "Statistics"})
        self.assertEqual(out["Logic"], ["Book A", "Book B"])
        self.assertEqual(out["Statistics"], ["Book C"])

    def test_caps_at_titles_per_theme(self):
        entries = [
            {"result": {"title": f"Book {i}", "themes": [{"theme": "Foo"}]}}
            for i in range(10)
        ]
        self._write_vision_cache(entries)
        out = extract_themes_with_titles("p")
        self.assertEqual(len(out["Foo"]), TITLES_PER_THEME)  # max 5
        # Premier-arrivé : Book 0..4
        self.assertEqual(out["Foo"], [f"Book {i}" for i in range(5)])

    def test_deduplicates_titles_per_theme(self):
        # Même titre apparaît 2x avec le même thème → 1 seule entrée
        self._write_vision_cache([
            {"result": {"title": "Repeated", "themes": [{"theme": "X"}]}},
            {"result": {"title": "Repeated", "themes": [{"theme": "X"}]}},
            {"result": {"title": "Other", "themes": [{"theme": "X"}]}},
        ])
        out = extract_themes_with_titles("p")
        self.assertEqual(out["X"], ["Repeated", "Other"])

    def test_string_themes_supported(self):
        # Format legacy : theme en str au lieu de dict
        self._write_vision_cache([
            {"result": {"title": "Book", "themes": ["StringTheme", {"theme": "DictTheme"}]}},
        ])
        out = extract_themes_with_titles("p")
        self.assertEqual(set(out.keys()), {"StringTheme", "DictTheme"})

    def test_skips_empty_title(self):
        # Si pas de titre, on skip (sinon les titres injectés deviennent vides)
        self._write_vision_cache([
            {"result": {"title": "", "themes": [{"theme": "X"}]}},
            {"result": {"title": "Real", "themes": [{"theme": "X"}]}},
        ])
        out = extract_themes_with_titles("p")
        self.assertEqual(out["X"], ["Real"])

    def test_skips_empty_theme(self):
        self._write_vision_cache([
            {"result": {"title": "T", "themes": [{"theme": ""}, {"theme": "Real"}]}},
        ])
        out = extract_themes_with_titles("p")
        self.assertEqual(set(out.keys()), {"Real"})

    def test_handles_string_result_legacy(self):
        # Le vision_cache historique stocke parfois result en str repr Python
        legacy = "{'title': 'Legacy Book', 'themes': [{'theme': 'X'}]}"
        self._write_vision_cache([{"result": legacy}])
        out = extract_themes_with_titles("p")
        self.assertEqual(out["X"], ["Legacy Book"])

    def test_skips_corrupted_entries(self):
        self._write_vision_cache([
            {"result": {"title": "Good", "themes": [{"theme": "X"}]}},
            {"result": "not json"},
            {"not_an_entry": True},
            {"result": {"themes": []}},  # pas de title ni thèmes
        ])
        out = extract_themes_with_titles("p")
        self.assertEqual(out["X"], ["Good"])

    def test_strips_whitespace(self):
        self._write_vision_cache([
            {"result": {"title": "  Padded  ", "themes": [{"theme": "  X  "}]}},
        ])
        out = extract_themes_with_titles("p")
        self.assertEqual(out, {"X": ["Padded"]})

    def test_multiple_themes_per_entry(self):
        # Un livre peut avoir plusieurs thèmes
        self._write_vision_cache([
            {"result": {"title": "Multi", "themes": [
                {"theme": "Theme A"}, {"theme": "Theme B"}, {"theme": "Theme C"},
            ]}},
        ])
        out = extract_themes_with_titles("p")
        self.assertEqual(out["Theme A"], ["Multi"])
        self.assertEqual(out["Theme B"], ["Multi"])
        self.assertEqual(out["Theme C"], ["Multi"])


class TestCountThemes(_ResolverBase):
    def test_basic_count(self):
        self._write_vision_cache([
            {"result": {"title": "A", "themes": [{"theme": "X"}, {"theme": "Y"}]}},
            {"result": {"title": "B", "themes": [{"theme": "X"}]}},
        ])
        counts = count_themes("p")
        self.assertEqual(counts, {"X": 2, "Y": 1})

    def test_no_cache_returns_empty(self):
        self.assertEqual(count_themes("p"), {})

    def test_skips_empty_themes(self):
        self._write_vision_cache([
            {"result": {"title": "T", "themes": [{"theme": ""}, {"theme": "Real"}]}},
        ])
        counts = count_themes("p")
        self.assertEqual(counts, {"Real": 1})


class TestThemesWithTitlesIter(unittest.TestCase):
    def test_lexico_order(self):
        themes = {
            "Zebra": ["t1"],
            "apple": ["t2"],
            "Banana": ["t3"],
        }
        keys = [t for t, _ in themes_with_titles_iter(themes)]
        # Ordre lexico Python (default uppercase first) : 'B', 'Z', 'a'
        self.assertEqual(keys, sorted(themes.keys()))

    def test_yields_pairs(self):
        themes = {"X": ["t1", "t2"]}
        result = list(themes_with_titles_iter(themes))
        self.assertEqual(result, [("X", ["t1", "t2"])])

    def test_empty(self):
        self.assertEqual(list(themes_with_titles_iter({})), [])


if __name__ == "__main__":
    unittest.main()
