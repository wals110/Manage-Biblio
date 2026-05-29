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
    BatchResolution,
    ResolvedTheme,
    _stable_batch_key,
    count_themes,
    extract_themes_with_titles,
    resolve_batch,
    resolve_themes_with_context,
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


class TestStableBatchKey(unittest.TestCase):
    def test_batch_order_matters(self):
        # L'ordre du batch est sémantiquement important (mappings[] ordonné)
        k1 = _stable_batch_key([("A", ["t1"]), ("B", ["t2"])], ["v1"])
        k2 = _stable_batch_key([("B", ["t2"]), ("A", ["t1"])], ["v1"])
        self.assertNotEqual(k1, k2)

    def test_vocab_order_irrelevant(self):
        k1 = _stable_batch_key([("A", ["t1"])], ["v1", "v2"])
        k2 = _stable_batch_key([("A", ["t1"])], ["v2", "v1"])
        self.assertEqual(k1, k2)

    def test_titles_order_irrelevant(self):
        # Les titres dans une entrée sont triés dans la clé — l'ordre de
        # collecte des titres ne doit pas invalider le cache.
        k1 = _stable_batch_key([("A", ["t1", "t2"])], ["v"])
        k2 = _stable_batch_key([("A", ["t2", "t1"])], ["v"])
        self.assertEqual(k1, k2)

    def test_length_16(self):
        self.assertEqual(len(_stable_batch_key([("A", [])], ["B"])), 16)


class TestResolveBatch(unittest.TestCase):
    def _mock_llm(self, mappings: list[ResolvedTheme]):
        mock_llm = mock.MagicMock()
        mock_structured = mock.MagicMock()
        mock_llm.with_structured_output.return_value = mock_structured
        mock_structured.invoke.return_value = BatchResolution(mappings=mappings)
        return mock_llm

    def test_happy_path(self):
        mock_llm = self._mock_llm([
            ResolvedTheme(raw="ML", canonical="Machine Learning",
                          matched_existing=True),
            ResolvedTheme(raw="Logic", canonical="Logic",
                          matched_existing=False),
        ])
        result = resolve_batch(
            [("ML", ["Intro to ML"]), ("Logic", ["A First Course in Logic"])],
            ["Machine Learning"],
            mock_llm,
        )
        self.assertEqual(result, {
            "ML": "Machine Learning",
            "Logic": "Logic",
        })

    def test_forces_function_calling(self):
        # GLM-4.7 ne supporte pas json mode → on doit forcer function_calling
        mock_llm = self._mock_llm([
            ResolvedTheme(raw="A", canonical="A", matched_existing=False),
        ])
        resolve_batch([("A", ["t1"])], [], mock_llm)
        _, kwargs = mock_llm.with_structured_output.call_args
        self.assertEqual(kwargs.get("method"), "function_calling")

    def test_empty_batch(self):
        mock_llm = mock.MagicMock()
        self.assertEqual(resolve_batch([], ["v"], mock_llm), {})
        mock_llm.with_structured_output.assert_not_called()

    def test_safety_net_when_llm_omits_themes(self):
        # LLM ne renvoie qu'1 mapping sur 2 demandés → l'autre → identité
        mock_llm = self._mock_llm([
            ResolvedTheme(raw="A", canonical="X", matched_existing=True),
        ])
        result = resolve_batch(
            [("A", ["t1"]), ("B", ["t2"])], ["X"], mock_llm,
        )
        self.assertEqual(result["A"], "X")
        self.assertEqual(result["B"], "B")

    def test_empty_canonical_falls_back_to_raw(self):
        mock_llm = self._mock_llm([
            ResolvedTheme(raw="A", canonical="   ", matched_existing=False),
        ])
        result = resolve_batch([("A", ["t"])], [], mock_llm)
        self.assertEqual(result["A"], "A")

    def test_titles_included_in_prompt(self):
        """Le user prompt doit inclure les titres en contexte."""
        captured: list = []

        def capture(messages):
            captured.append(messages)
            return BatchResolution(mappings=[
                ResolvedTheme(raw="A", canonical="A", matched_existing=False),
            ])

        mock_llm = mock.MagicMock()
        mock_structured = mock.MagicMock()
        mock_llm.with_structured_output.return_value = mock_structured
        mock_structured.invoke.side_effect = capture
        resolve_batch(
            [("ML", ["Pattern Recognition and Machine Learning",
                     "Introduction to ML"])],
            ["Machine Learning"],
            mock_llm,
        )
        user_msg = captured[0][1].content
        self.assertIn("Machine Learning", user_msg)  # vocab
        self.assertIn("ML", user_msg)
        self.assertIn("Pattern Recognition", user_msg)
        self.assertIn("Introduction to ML", user_msg)


class TestResolveThemesWithContext(_ResolverBase):
    def _scripted_llm(self, mapping: dict[str, str]):
        """LLM qui retourne {canonical} d'après un dict scripté pour raw."""
        mock_llm = mock.MagicMock()
        mock_structured = mock.MagicMock()
        mock_llm.with_structured_output.return_value = mock_structured

        def respond(messages):
            # Le user prompt liste "Thème i : 'X'" → on extrait les X
            import re
            content = messages[1].content
            raws = re.findall(r"^Thème \d+ : '([^']+)'", content, re.MULTILINE)
            pairs = [
                ResolvedTheme(
                    raw=raw,
                    canonical=mapping.get(raw, raw),
                    matched_existing=mapping.get(raw, raw) != raw,
                )
                for raw in raws
            ]
            return BatchResolution(mappings=pairs)

        mock_structured.invoke.side_effect = respond
        return mock_llm

    def test_empty_input(self):
        mock_llm = mock.MagicMock()
        self.assertEqual(
            resolve_themes_with_context({}, ["v"], mock_llm, "p",
                                         use_cache=False),
            {},
        )
        mock_llm.with_structured_output.assert_not_called()

    def test_vocab_themes_skip_llm(self):
        mock_llm = mock.MagicMock()
        out = resolve_themes_with_context(
            {"Machine Learning": ["t1"], "Data Science": ["t2"]},
            ["Machine Learning", "Data Science"],
            mock_llm, "p", use_cache=False,
        )
        self.assertEqual(out, {
            "Machine Learning": "Machine Learning",
            "Data Science": "Data Science",
        })
        mock_llm.with_structured_output.assert_not_called()

    def test_basic_resolution(self):
        llm = self._scripted_llm({
            "ML": "Machine Learning",
            "machine learning": "Machine Learning",
        })
        out = resolve_themes_with_context(
            {
                "Machine Learning": ["t1"],
                "ML": ["Intro to ML"],
                "machine learning": ["ML for Dummies"],
            },
            ["Machine Learning"],
            llm, "p", batch_size=10, max_workers=1, use_cache=False,
        )
        self.assertEqual(out["Machine Learning"], "Machine Learning")
        self.assertEqual(out["ML"], "Machine Learning")
        self.assertEqual(out["machine learning"], "Machine Learning")

    def test_cache_persists(self):
        llm = self._scripted_llm({"A": "X"})
        themes_titles = {"A": ["title_A"]}
        # 1er appel : LLM appelé, cache écrit
        resolve_themes_with_context(
            themes_titles, [], llm, "p",
            batch_size=10, max_workers=1, use_cache=True,
        )
        cache_path = (
            self.tmp / "profiles" / "p" / ".cache" / "theme-resolver.json"
        )
        self.assertTrue(cache_path.exists())
        # 2e appel : LLM jamais ré-appelé (cache hit)
        llm.with_structured_output.return_value.invoke.reset_mock()
        resolve_themes_with_context(
            themes_titles, [], llm, "p",
            batch_size=10, max_workers=1, use_cache=True,
        )
        llm.with_structured_output.return_value.invoke.assert_not_called()

    def test_llm_failure_falls_back_to_identity(self):
        mock_llm = mock.MagicMock()
        mock_structured = mock.MagicMock()
        mock_llm.with_structured_output.return_value = mock_structured
        mock_structured.invoke.side_effect = RuntimeError("LLM down")

        out = resolve_themes_with_context(
            {"A": ["t1"], "B": ["t2"]}, [], mock_llm, "p",
            batch_size=10, max_workers=1, use_cache=False,
        )
        self.assertEqual(out, {"A": "A", "B": "B"})

    def test_progress_callback(self):
        themes_titles = {f"T{i}": [f"title_{i}"] for i in range(15)}
        llm = self._scripted_llm({})
        calls: list[tuple[int, int]] = []
        resolve_themes_with_context(
            themes_titles, [], llm, "p",
            batch_size=5, max_workers=1, use_cache=False,
            on_progress=lambda d, t: calls.append((d, t)),
        )
        # 15 / 5 = 3 batches → 3 callbacks min, dernier = (3, 3)
        self.assertGreaterEqual(len(calls), 3)
        self.assertEqual(calls[-1], (3, 3))

    def test_parallel_workers_preserve_completeness(self):
        themes_titles = {f"T{i}": [f"t_{i}"] for i in range(50)}
        llm = self._scripted_llm({})
        out = resolve_themes_with_context(
            themes_titles, [], llm, "p",
            batch_size=10, max_workers=4, use_cache=False,
        )
        self.assertEqual(set(out.keys()), set(themes_titles.keys()))


if __name__ == "__main__":
    unittest.main()
