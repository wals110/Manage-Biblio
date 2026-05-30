#!/usr/bin/env python3
"""Tests pour lib.theme_canon — Phase 4 du chantier dédupli.

Couvre :
  - extract_themes_from_vision_cache (parsing dict/str JSON/str repr)
  - assemble_canon_mapping (members/splits/filet de sécurité)
  - build_canon_table (pipeline end-to-end avec mock LLM)
  - load/save canon_table (atomique, robuste)
  - canonicalize (hot path)
  - Branchement dans dashboard.taxonomy._aggregate_themes_llm
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
from lib.theme_canon import (  # noqa: E402
    CANON_VERSION,
    _parse_cached_result,
    assemble_canon_mapping,
    build_canon_table,
    canonicalize,
    extract_themes_from_vision_cache,
    load_canon_table,
    save_canon_table,
)
from lib.theme_judge import JudgeResult, JudgeSplit  # noqa: E402


class _CanonBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="klodo_canon_"))
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
        # vision_cache.json = {hash: {result: str|dict, ...}}
        cache = {f"hash_{i}": e for i, e in enumerate(entries)}
        path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


class TestParseCachedResult(unittest.TestCase):
    def test_dict_input(self):
        d = {"title": "Foo", "themes": [{"theme": "Bar"}]}
        self.assertEqual(_parse_cached_result(d), d)

    def test_json_string(self):
        s = '{"title": "Foo", "themes": [{"theme": "Bar"}]}'
        self.assertEqual(_parse_cached_result(s)["title"], "Foo")

    def test_python_repr_string(self):
        # Legacy format observé dans le vision_cache.json réel
        s = "{'title': 'Foo', 'themes': [{'theme': 'Bar'}]}"
        self.assertEqual(_parse_cached_result(s)["title"], "Foo")

    def test_corrupted_input(self):
        self.assertIsNone(_parse_cached_result("not json"))
        self.assertIsNone(_parse_cached_result(""))
        self.assertIsNone(_parse_cached_result(None))
        self.assertIsNone(_parse_cached_result(42))


class TestExtractThemesFromVisionCache(_CanonBase):
    def test_no_cache_returns_empty(self):
        self.assertEqual(extract_themes_from_vision_cache("p"), {})

    def test_basic_extraction(self):
        self._write_vision_cache([
            {"result": {"title": "A", "themes": [{"theme": "Machine Learning"}]}},
            {"result": {"title": "B", "themes": [{"theme": "Machine Learning"}]}},
            {"result": {"title": "C", "themes": [{"theme": "Deep Learning"}]}},
        ])
        themes = extract_themes_from_vision_cache("p")
        self.assertEqual(themes, {"Machine Learning": 2, "Deep Learning": 1})

    def test_handles_string_result(self):
        # Mix dict + str JSON + str repr (cas réel)
        self._write_vision_cache([
            {"result": {"themes": [{"theme": "Foo"}]}},
            {"result": '{"themes": [{"theme": "Foo"}]}'},
            {"result": "{'themes': [{'theme': 'Foo'}]}"},
        ])
        themes = extract_themes_from_vision_cache("p")
        self.assertEqual(themes, {"Foo": 3})

    def test_string_themes_supported(self):
        # Quelques entrées historiques ont des thèmes en str au lieu de dict
        self._write_vision_cache([
            {"result": {"themes": ["BareTheme", {"theme": "DictTheme"}]}},
        ])
        themes = extract_themes_from_vision_cache("p")
        self.assertEqual(themes, {"BareTheme": 1, "DictTheme": 1})

    def test_skips_corrupted_entries(self):
        self._write_vision_cache([
            {"result": {"themes": [{"theme": "Good"}]}},
            {"result": "not json"},
            {"not_an_entry": True},
            {"result": {"themes": []}},
        ])
        themes = extract_themes_from_vision_cache("p")
        self.assertEqual(themes, {"Good": 1})

    def test_strips_whitespace(self):
        self._write_vision_cache([
            {"result": {"themes": [{"theme": "  Foo  "}]}},
        ])
        themes = extract_themes_from_vision_cache("p")
        self.assertEqual(themes, {"Foo": 1})


class TestAssembleCanonMapping(unittest.TestCase):
    def test_members_to_canonical(self):
        clusters = [{"raw_members": ["Machine Learning", "Machine learning"]}]
        judgments = [JudgeResult(
            canonical="Machine Learning",
            members=["Machine Learning", "Machine learning"],
        )]
        mapping = assemble_canon_mapping(clusters, judgments)
        self.assertEqual(mapping, {
            "Machine Learning": "Machine Learning",
            "Machine learning": "Machine Learning",
        })

    def test_splits_keep_identity(self):
        clusters = [{"raw_members": ["Machine Learning", "Unsupervised Machine Learning"]}]
        judgments = [JudgeResult(
            canonical="Machine Learning",
            members=["Machine Learning"],
            splits=[JudgeSplit(theme="Unsupervised Machine Learning",
                              reason="sous-domaine")],
        )]
        mapping = assemble_canon_mapping(clusters, judgments)
        self.assertEqual(mapping, {
            "Machine Learning": "Machine Learning",
            "Unsupervised Machine Learning": "Unsupervised Machine Learning",
        })

    def test_safety_net_for_forgotten_variants(self):
        # Le LLM oublie une variante (ni dans members ni dans splits)
        clusters = [{"raw_members": ["A", "B", "C"]}]
        judgments = [JudgeResult(
            canonical="A",
            members=["A", "B"],
            # C oublié
        )]
        mapping = assemble_canon_mapping(clusters, judgments)
        self.assertEqual(mapping["A"], "A")
        self.assertEqual(mapping["B"], "A")
        self.assertEqual(mapping["C"], "C")  # identité par défaut

    def test_singleton_identity(self):
        clusters = [{"raw_members": ["Solo Theme"]}]
        judgments = [JudgeResult(canonical="Solo Theme", members=["Solo Theme"])]
        mapping = assemble_canon_mapping(clusters, judgments)
        self.assertEqual(mapping, {"Solo Theme": "Solo Theme"})

    def test_length_mismatch_raises(self):
        with self.assertRaises(ValueError):
            assemble_canon_mapping([{"raw_members": ["A"]}], [])


class TestLoadSaveCanonTable(_CanonBase):
    def test_save_then_load(self):
        table = {
            "version": CANON_VERSION,
            "built_at": "2026-05-27T00:00:00",
            "mapping": {"Foo": "Foo Canonical", "Bar": "Bar"},
        }
        save_canon_table("p", table)
        mapping = load_canon_table("p")
        self.assertEqual(mapping, {"Foo": "Foo Canonical", "Bar": "Bar"})

    def test_load_missing_returns_none(self):
        self.assertIsNone(load_canon_table("p"))

    def test_load_corrupted_returns_none(self):
        path = self.tmp / "profiles" / "p" / ".cache" / "theme-canon.json"
        path.write_text("not json", encoding="utf-8")
        self.assertIsNone(load_canon_table("p"))

    def test_load_missing_mapping_field_returns_none(self):
        path = self.tmp / "profiles" / "p" / ".cache" / "theme-canon.json"
        path.write_text('{"version": 1}', encoding="utf-8")
        self.assertIsNone(load_canon_table("p"))

    def test_save_is_atomic(self):
        # save_canon_table écrit dans .tmp puis renomme — pas de fichier
        # partiel laissé en cas d'interruption
        table = {"version": 1, "mapping": {"Foo": "Foo"}}
        save_canon_table("p", table)
        path = self.tmp / "profiles" / "p" / ".cache" / "theme-canon.json"
        self.assertTrue(path.exists())
        # Pas de fichier .tmp résiduel
        self.assertFalse(path.with_suffix(".tmp").exists())


class TestCanonicalize(unittest.TestCase):
    def test_resolves_via_table(self):
        table = {"Machine learning": "Machine Learning"}
        self.assertEqual(canonicalize("Machine learning", table), "Machine Learning")

    def test_unknown_theme_returns_identity(self):
        table = {"Foo": "Foo Canonical"}
        self.assertEqual(canonicalize("Unknown", table), "Unknown")

    def test_none_table_returns_identity(self):
        self.assertEqual(canonicalize("Whatever", None), "Whatever")

    def test_empty_table_returns_identity(self):
        self.assertEqual(canonicalize("Whatever", {}), "Whatever")


class TestBuildCanonTable(_CanonBase):
    def test_empty_profile_yields_empty_table(self):
        mock_llm = mock.MagicMock()
        table = build_canon_table("p", mock_llm)
        self.assertEqual(table["mapping"], {})
        self.assertEqual(table["raw_count"], 0)
        self.assertEqual(table["canonical_count"], 0)
        # Pas d'appel LLM si pas de thèmes
        mock_llm.with_structured_output.assert_not_called()

    def test_with_themes_auto_merge_only(self):
        # 2 variantes très proches → auto-merge sans LLM
        self._write_vision_cache([
            {"result": {"themes": [{"theme": "Machine Learning"}]}},
            {"result": {"themes": [{"theme": "Machine learning"}]}},
            {"result": {"themes": [{"theme": "Deep Learning"}]}},
        ])
        mock_llm = mock.MagicMock()
        table = build_canon_table("p", mock_llm, use_judge_cache=False)

        mapping = table["mapping"]
        # Les 2 ML variantes pointent sur le même canonical
        self.assertEqual(mapping["Machine Learning"], mapping["Machine learning"])
        # Deep Learning isolé
        self.assertEqual(mapping["Deep Learning"], "Deep Learning")
        # LLM pas appelé (auto-merge)
        mock_llm.with_structured_output.assert_not_called()

    def test_table_persisted_on_disk(self):
        self._write_vision_cache([
            {"result": {"themes": [{"theme": "Foo"}]}},
        ])
        mock_llm = mock.MagicMock()
        build_canon_table("p", mock_llm)
        path = self.tmp / "profiles" / "p" / ".cache" / "theme-canon.json"
        self.assertTrue(path.exists())
        loaded = json.loads(path.read_text())
        self.assertEqual(loaded["version"], CANON_VERSION)
        self.assertIn("built_at", loaded)
        self.assertIn("mapping", loaded)


class TestTaxonomyIntegration(_CanonBase):
    """Vérifie que _aggregate_themes_llm consomme bien theme-canon.json."""

    def setUp(self):
        super().setUp()
        # Mock data.get_project_root pour le module dashboard.taxonomy
        # (utilise déjà self._patcher du _CanonBase)
        # Profile minimal
        (self.tmp / "profiles" / "p" / "profile.yaml").write_text("name: p\n")

    def _vision_with_themes(self, themes: list[str]) -> None:
        entries = [
            {"result": {"title": f"Doc{i}",
                       "themes": [{"theme": t, "confidence": 0.9}]}}
            for i, t in enumerate(themes)
        ]
        self._write_vision_cache(entries)

    def test_without_canon_table_baseline_behavior(self):
        # Sans theme-canon.json → comportement historique
        # (key = lowercase, on dédoublonne juste la casse)
        from dashboard.taxonomy import _aggregate_themes_llm

        self._vision_with_themes([
            "Machine Learning", "Machine learning",  # même casse-lowercase
            "Deep Learning",
        ])
        themes_out, stats = _aggregate_themes_llm("p", {})
        # 2 thèmes : machine learning (count 2 via lowercasing) + deep learning
        keys = {t["theme"].lower() for t in themes_out}
        self.assertEqual(keys, {"machine learning", "deep learning"})

    def test_with_canon_table_consolidates_counts(self):
        from dashboard.taxonomy import _aggregate_themes_llm

        # 3 doc avec 3 variantes "Optimization" (USA / UK / lowercase)
        self._vision_with_themes([
            "Optimization", "Optimisation", "optimization",
        ])
        # Écrit une canon_table qui mappe les 3 sur "Optimization"
        save_canon_table("p", {
            "version": 1,
            "mapping": {
                "Optimization": "Optimization",
                "Optimisation": "Optimization",
                "optimization": "Optimization",
            },
        })
        themes_out, stats = _aggregate_themes_llm("p", {})
        # Un seul thème "Optimization" avec count = 3
        self.assertEqual(len(themes_out), 1)
        self.assertEqual(themes_out[0]["theme"], "Optimization")
        self.assertEqual(themes_out[0]["count"], 3)

    def test_with_canon_table_keeps_splits_separate(self):
        from dashboard.taxonomy import _aggregate_themes_llm

        self._vision_with_themes([
            "Machine Learning", "Machine Learning",
            "Unsupervised Machine Learning",
        ])
        save_canon_table("p", {
            "version": 1,
            "mapping": {
                "Machine Learning": "Machine Learning",
                "Unsupervised Machine Learning": "Unsupervised Machine Learning",
            },
        })
        themes_out, _ = _aggregate_themes_llm("p", {})
        by_theme = {t["theme"]: t["count"] for t in themes_out}
        self.assertEqual(by_theme["Machine Learning"], 2)
        self.assertEqual(by_theme["Unsupervised Machine Learning"], 1)


class TestBuildCanonTableSemanticMode(_CanonBase):
    """Mode semantic (C-light) : vocabulaire + canonisation LLM globale."""

    def _write_vision(self, themes_counts: dict[str, int]) -> None:
        """Crée un vision_cache avec ces occurrences."""
        entries = []
        for theme, count in themes_counts.items():
            for _ in range(count):
                entries.append(
                    {"result": {"title": f"Doc-{theme}",
                                "themes": [{"theme": theme, "confidence": 0.9}]}}
                )
        self._write_vision_cache(entries)

    def test_unknown_mode_raises(self):
        from lib.theme_canon import build_canon_table
        mock_llm = mock.MagicMock()
        with self.assertRaises(ValueError):
            build_canon_table("p", mock_llm, mode="weird")

    def test_semantic_uses_canonicalizer(self):
        # 3 raws : "ML" (1), "Machine Learning" (5), "AI" (1)
        # → vocabulary = ["Machine Learning"] (top 1)
        # → "ML" et "AI" passent au LLM, on script la réponse
        from lib.theme_canon import build_canon_table

        self._write_vision({
            "Machine Learning": 5,
            "ML": 1,
            "AI": 1,
        })

        from lib.theme_canonicalizer import (
            BatchCanonicalization,
            RawCanonicalPair,
        )
        mock_llm = mock.MagicMock()
        mock_structured = mock.MagicMock()
        mock_llm.with_structured_output.return_value = mock_structured
        mock_structured.invoke.return_value = BatchCanonicalization(mappings=[
            RawCanonicalPair(raw="AI", canonical="Machine Learning",
                            matched_existing=True),
            RawCanonicalPair(raw="ML", canonical="Machine Learning",
                            matched_existing=True),
        ])

        table = build_canon_table(
            "p", mock_llm,
            mode="semantic",
            vocabulary_top_n=1,  # Top 1 = ["Machine Learning"]
            use_judge_cache=False,
        )

        self.assertEqual(table["mode"], "semantic")
        self.assertEqual(table["vocabulary_size"], 1)
        # Tous mappent vers "Machine Learning"
        mapping = table["mapping"]
        self.assertEqual(mapping["Machine Learning"], "Machine Learning")
        self.assertEqual(mapping["ML"], "Machine Learning")
        self.assertEqual(mapping["AI"], "Machine Learning")
        self.assertEqual(table["canonical_count"], 1)

        # 1 cluster final avec count cumulé = 5 + 1 + 1 = 7
        self.assertEqual(len(table["clusters"]), 1)
        self.assertEqual(table["clusters"][0]["canonical"], "Machine Learning")
        self.assertEqual(table["clusters"][0]["count_cumulative"], 7)

    def test_semantic_empty_profile(self):
        from lib.theme_canon import build_canon_table
        mock_llm = mock.MagicMock()
        table = build_canon_table(
            "p", mock_llm, mode="semantic", vocabulary_top_n=10,
        )
        self.assertEqual(table["mapping"], {})
        self.assertEqual(table["clusters"], [])
        self.assertEqual(table["mode"], "semantic")

    def test_semantic_progress_phases(self):
        from lib.theme_canon import build_canon_table

        self._write_vision({"A": 3, "B": 1})  # vocab=[A], "B" → LLM

        from lib.theme_canonicalizer import (
            BatchCanonicalization,
            RawCanonicalPair,
        )
        mock_llm = mock.MagicMock()
        mock_structured = mock.MagicMock()
        mock_llm.with_structured_output.return_value = mock_structured
        mock_structured.invoke.return_value = BatchCanonicalization(mappings=[
            RawCanonicalPair(raw="B", canonical="A", matched_existing=True),
        ])

        phases: list[str] = []
        build_canon_table(
            "p", mock_llm, mode="semantic", vocabulary_top_n=1,
            on_progress=lambda d, t, phase: phases.append(phase),
            use_judge_cache=False,
        )
        self.assertIn("extracting", phases)
        self.assertIn("canonicalizing", phases)
        self.assertIn("reclustering", phases)
        self.assertEqual(phases[-1], "done")


class TestBuildCanonTableSourceMode(_CanonBase):
    """Mode source (C.2) : contexte titres via LLM."""

    def test_source_uses_resolver_with_titles(self):
        from lib.theme_canon import build_canon_table
        from lib.theme_resolver import BatchResolution, ResolvedTheme

        # 3 raws : ML (1 occ + 1 titre), Machine Learning (5 + 1), Logic (1 + 1)
        self._write_vision_cache([
            {"result": {"title": "Pattern Recognition and ML",
                       "themes": [{"theme": "Machine Learning", "confidence": 0.9}]}},
            {"result": {"title": "ML for Dummies",
                       "themes": [{"theme": "Machine Learning", "confidence": 0.9}]}},
            {"result": {"title": "Introduction to ML",
                       "themes": [{"theme": "ML", "confidence": 0.9}]}},
            {"result": {"title": "A First Course in Logic",
                       "themes": [{"theme": "Logic", "confidence": 0.9}]}},
        ])

        mock_llm = mock.MagicMock()
        mock_structured = mock.MagicMock()
        mock_llm.with_structured_output.return_value = mock_structured

        # Vocabulary top_n=1 → "Machine Learning"
        # to_process = ["ML", "Logic"] (lexico) → LLM voit ML + son titre,
        # Logic + son titre. ML fusionne, Logic reste identité.
        mock_structured.invoke.return_value = BatchResolution(mappings=[
            ResolvedTheme(raw="Logic", canonical="Logic",
                          matched_existing=False),
            ResolvedTheme(raw="ML", canonical="Machine Learning",
                          matched_existing=True),
        ])

        table = build_canon_table(
            "p", mock_llm,
            mode="source",
            vocabulary_top_n=1,
            use_judge_cache=False,
        )

        self.assertEqual(table["mode"], "source")
        mapping = table["mapping"]
        self.assertEqual(mapping["Machine Learning"], "Machine Learning")
        self.assertEqual(mapping["ML"], "Machine Learning")
        self.assertEqual(mapping["Logic"], "Logic")

        # 2 clusters : Machine Learning (count 2+1=3) + Logic (count 1)
        clusters = table["clusters"]
        self.assertEqual(len(clusters), 2)
        # Vérifie présence des sample_titles dans les clusters
        ml_cluster = next(c for c in clusters if c["canonical"] == "Machine Learning")
        self.assertGreater(len(ml_cluster["sample_titles"]), 0)

    def test_source_progress_phases(self):
        from lib.theme_canon import build_canon_table
        from lib.theme_resolver import BatchResolution, ResolvedTheme

        self._write_vision_cache([
            {"result": {"title": "T1", "themes": [{"theme": "A", "confidence": 0.9}]}},
            {"result": {"title": "T2", "themes": [{"theme": "B", "confidence": 0.9}]}},
        ])

        mock_llm = mock.MagicMock()
        mock_structured = mock.MagicMock()
        mock_llm.with_structured_output.return_value = mock_structured
        mock_structured.invoke.return_value = BatchResolution(mappings=[
            ResolvedTheme(raw="B", canonical="A", matched_existing=True),
        ])

        phases: list[str] = []
        build_canon_table(
            "p", mock_llm, mode="source", vocabulary_top_n=1,
            use_judge_cache=False,
            on_progress=lambda d, t, phase: phases.append(phase),
        )
        self.assertIn("extracting", phases)
        self.assertIn("resolving", phases)
        # Mode 'source' n'a pas de re-clustering fuzzy (skip cluster_themes
        # par-dessus la sortie LLM) — la phase finale est "finalizing".
        self.assertIn("finalizing", phases)
        self.assertEqual(phases[-1], "done")

    def test_source_empty_profile(self):
        from lib.theme_canon import build_canon_table
        mock_llm = mock.MagicMock()
        table = build_canon_table(
            "p", mock_llm, mode="source", vocabulary_top_n=10,
        )
        self.assertEqual(table["mapping"], {})
        self.assertEqual(table["clusters"], [])
        # Le mode demandé est tagué même quand le profil est vide
        self.assertEqual(table.get("mode"), "source")


class TestSyntacticModeTagged(_CanonBase):
    """Le mode syntactique (défaut) tague aussi mode dans la sortie."""

    def test_syntactic_mode_in_table(self):
        from lib.theme_canon import build_canon_table
        self._write_vision_cache([
            {"result": {"themes": [{"theme": "Foo"}]}},
        ])
        mock_llm = mock.MagicMock()
        table = build_canon_table("p", mock_llm)  # défaut syntactic
        self.assertEqual(table["mode"], "syntactic")


if __name__ == "__main__":
    unittest.main()
