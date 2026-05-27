#!/usr/bin/env python3
"""Tests pour lib.theme_judge — Phase 3 du chantier dédupli.

Mocke le LLM (pas de call SiliconFlow), valide :
  - should_auto_merge sur cas réels
  - judge_cluster avec mock structured_output
  - judge_clusters : skip auto, cache hit/miss, persistance
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
from lib.theme_judge import (  # noqa: E402
    JudgeResult,
    JudgeSplit,
    _stable_cluster_key,
    judge_cluster,
    judge_clusters,
    should_auto_merge,
)


class TestShouldAutoMerge(unittest.TestCase):
    def test_singleton_is_trivially_mergeable(self):
        self.assertTrue(should_auto_merge(["machine learning"]))

    def test_empty(self):
        self.assertTrue(should_auto_merge([]))

    def test_case_variant_high_similarity(self):
        # token_sort_ratio('machine learning', 'machine learning') = 100
        self.assertTrue(should_auto_merge(["machine learning", "machine learning"]))

    def test_plural_variant(self):
        # 'neural network' vs 'neural networks' : ratio ~96-97
        # On vérifie selon threshold
        self.assertTrue(should_auto_merge(["neural network", "neural networks"], threshold=90))

    def test_distinct_subdomain_not_mergeable(self):
        # 'machine learning' vs 'deep learning' : token_sort_ratio = 55.2
        self.assertFalse(should_auto_merge(["machine learning", "deep learning"]))

    def test_one_outlier_breaks_auto_merge(self):
        # 4 variantes très proches + 1 outlier → pas d'auto-merge
        # (on exige la similarité min sur toutes les paires)
        variants = [
            "machine learning",
            "machine learning",
            "machine learnings",
            "deep learning",  # outlier
        ]
        self.assertFalse(should_auto_merge(variants))

    def test_threshold_argument(self):
        # 'neural network' vs 'artificial neural network' : ~72
        # → auto_merge False à threshold 97, True à threshold 70
        variants = ["neural network", "artificial neural network"]
        self.assertFalse(should_auto_merge(variants, threshold=97))
        self.assertTrue(should_auto_merge(variants, threshold=70))


class TestStableClusterKey(unittest.TestCase):
    def test_order_independent(self):
        k1 = _stable_cluster_key(["foo", "bar", "baz"])
        k2 = _stable_cluster_key(["baz", "foo", "bar"])
        self.assertEqual(k1, k2)

    def test_different_clusters_different_keys(self):
        k1 = _stable_cluster_key(["foo", "bar"])
        k2 = _stable_cluster_key(["foo", "baz"])
        self.assertNotEqual(k1, k2)

    def test_length_16(self):
        self.assertEqual(len(_stable_cluster_key(["foo"])), 16)


class TestJudgeCluster(unittest.TestCase):
    """Vérifie l'orchestration de l'appel LLM (mocked)."""

    def test_calls_with_structured_output(self):
        mock_llm = mock.MagicMock()
        mock_structured = mock.MagicMock()
        mock_llm.with_structured_output.return_value = mock_structured
        mock_structured.invoke.return_value = JudgeResult(
            canonical="Machine Learning",
            members=["Machine Learning", "Machine learning"],
        )

        result = judge_cluster(["Machine Learning", "Machine learning"], mock_llm)
        self.assertEqual(result.canonical, "Machine Learning")
        self.assertEqual(len(result.members), 2)
        self.assertEqual(result.splits, [])
        # Vérifie que le bon schema a été passé
        mock_llm.with_structured_output.assert_called_once_with(JudgeResult)
        # Vérifie qu'on a bien passé deux messages (system + human)
        args = mock_structured.invoke.call_args[0][0]
        self.assertEqual(len(args), 2)

    def test_handles_splits(self):
        mock_llm = mock.MagicMock()
        mock_structured = mock.MagicMock()
        mock_llm.with_structured_output.return_value = mock_structured
        mock_structured.invoke.return_value = JudgeResult(
            canonical="Machine Learning",
            members=["Machine Learning"],
            splits=[
                JudgeSplit(theme="Unsupervised Machine Learning",
                          reason="sous-domaine"),
            ],
        )

        result = judge_cluster(
            ["Machine Learning", "Unsupervised Machine Learning"],
            mock_llm,
        )
        self.assertEqual(len(result.members), 1)
        self.assertEqual(len(result.splits), 1)
        self.assertEqual(result.splits[0].theme, "Unsupervised Machine Learning")


class _JudgeBase(unittest.TestCase):
    """Setup commun : project root temp + profil test."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="klodo_judge_"))
        (self.tmp / "profiles" / "p").mkdir(parents=True)
        self._patcher = mock.patch.object(
            data, "get_project_root", return_value=self.tmp,
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cache_path(self):
        return self.tmp / "profiles" / "p" / ".cache" / "theme-judge.json"


class TestJudgeClustersAutoMerge(_JudgeBase):
    def test_singleton_yields_trivial_result(self):
        mock_llm = mock.MagicMock()
        results = judge_clusters([["Machine Learning"]], "p", mock_llm)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].canonical, "Machine Learning")
        self.assertEqual(results[0].members, ["Machine Learning"])
        # LLM jamais appelé
        mock_llm.with_structured_output.assert_not_called()

    def test_high_similarity_auto_merged_without_llm(self):
        mock_llm = mock.MagicMock()
        results = judge_clusters(
            [["Machine Learning", "Machine learning"]],
            "p", mock_llm,
        )
        self.assertEqual(len(results), 1)
        # Canonical = la variante la plus longue (heuristique fallback)
        self.assertIn(results[0].canonical, {"Machine Learning", "Machine learning"})
        self.assertEqual(len(results[0].members), 2)
        # LLM jamais appelé sur ce cluster homogène
        mock_llm.with_structured_output.assert_not_called()

    def test_ambiguous_cluster_triggers_llm(self):
        mock_llm = mock.MagicMock()
        mock_structured = mock.MagicMock()
        mock_llm.with_structured_output.return_value = mock_structured
        mock_structured.invoke.return_value = JudgeResult(
            canonical="Machine Learning",
            members=["Machine Learning"],
            splits=[JudgeSplit(theme="Unsupervised Machine Learning",
                              reason="sous-domaine")],
        )

        results = judge_clusters(
            [["Machine Learning", "Unsupervised Machine Learning"]],
            "p", mock_llm,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(len(results[0].splits), 1)
        # LLM appelé une fois
        mock_llm.with_structured_output.assert_called_once()


class TestJudgeClustersCache(_JudgeBase):
    def test_cache_miss_then_hit(self):
        mock_llm = mock.MagicMock()
        mock_structured = mock.MagicMock()
        mock_llm.with_structured_output.return_value = mock_structured
        mock_structured.invoke.return_value = JudgeResult(
            canonical="Foo",
            members=["Foo Variant A"],
            splits=[JudgeSplit(theme="Foo Variant B", reason="distinct")],
        )

        cluster = ["Foo Variant A", "Foo Variant B"]

        # Premier appel : LLM appelé
        r1 = judge_clusters([cluster], "p", mock_llm)
        self.assertEqual(mock_structured.invoke.call_count, 1)
        # Cache écrit sur disque
        self.assertTrue(self._cache_path().exists())

        # Deuxième appel : cache hit, LLM pas réappelé
        r2 = judge_clusters([cluster], "p", mock_llm)
        self.assertEqual(mock_structured.invoke.call_count, 1)  # toujours 1
        # Même résultat
        self.assertEqual(r1[0].canonical, r2[0].canonical)
        self.assertEqual(
            [s.theme for s in r1[0].splits],
            [s.theme for s in r2[0].splits],
        )

    def test_cache_order_independent(self):
        """Le cache key est calculé sur les variantes triées : deux
        appels avec le même cluster en ordre différent doivent matcher."""
        mock_llm = mock.MagicMock()
        mock_structured = mock.MagicMock()
        mock_llm.with_structured_output.return_value = mock_structured
        mock_structured.invoke.return_value = JudgeResult(
            canonical="X",
            members=["A", "B"],
        )

        judge_clusters([["A", "B", "C"]], "p", mock_llm)
        # Re-juger avec ordre inversé
        judge_clusters([["C", "B", "A"]], "p", mock_llm)
        # LLM appelé une seule fois grâce au tri dans la clé
        self.assertEqual(mock_structured.invoke.call_count, 1)

    def test_use_cache_false_skips_io(self):
        mock_llm = mock.MagicMock()
        mock_structured = mock.MagicMock()
        mock_llm.with_structured_output.return_value = mock_structured
        mock_structured.invoke.return_value = JudgeResult(
            canonical="X", members=["A"],
        )

        judge_clusters([["A", "B", "C"]], "p", mock_llm, use_cache=False)
        # Pas d'écriture cache
        self.assertFalse(self._cache_path().exists())


class TestJudgeClustersBatch(_JudgeBase):
    def test_mixed_singletons_homogeneous_ambiguous(self):
        """Trois clusters de natures différentes en un appel."""
        mock_llm = mock.MagicMock()
        mock_structured = mock.MagicMock()
        mock_llm.with_structured_output.return_value = mock_structured
        mock_structured.invoke.return_value = JudgeResult(
            canonical="Java Development",
            members=["Java Development"],
            splits=[JudgeSplit(theme="JavaFX Development",
                              reason="techno UI différente")],
        )

        clusters = [
            ["Standalone Theme"],  # singleton
            ["Machine Learning", "Machine learning"],  # auto-merge
            ["Java Development", "JavaFX Development"],  # LLM
        ]
        results = judge_clusters(clusters, "p", mock_llm)

        self.assertEqual(len(results), 3)
        # 1) singleton
        self.assertEqual(results[0].canonical, "Standalone Theme")
        # 2) auto-merge sans LLM
        self.assertEqual(len(results[1].members), 2)
        self.assertEqual(results[1].splits, [])
        # 3) ambigu : LLM a séparé
        self.assertEqual(len(results[2].splits), 1)
        # LLM appelé une SEULE fois (sur le cluster ambigu)
        self.assertEqual(mock_structured.invoke.call_count, 1)


class TestJudgeResultSchema(unittest.TestCase):
    """Vérifie que le schéma Pydantic est utilisable côté tests."""

    def test_minimal_result(self):
        r = JudgeResult(canonical="Foo", members=["foo"])
        self.assertEqual(r.canonical, "Foo")
        self.assertEqual(r.members, ["foo"])
        self.assertEqual(r.splits, [])

    def test_full_result_serializable(self):
        r = JudgeResult(
            canonical="Foo",
            members=["foo"],
            splits=[JudgeSplit(theme="Foo Bar", reason="différent")],
        )
        # Round-trip JSON
        payload = json.loads(r.model_dump_json())
        self.assertEqual(payload["canonical"], "Foo")
        self.assertEqual(payload["splits"][0]["theme"], "Foo Bar")


if __name__ == "__main__":
    unittest.main()
