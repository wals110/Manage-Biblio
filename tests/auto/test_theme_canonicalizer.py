#!/usr/bin/env python3
"""Tests pour lib.theme_canonicalizer — C-light du chantier dédupli.

Mocke le LLM (pas de call SiliconFlow), valide :
  - build_vocabulary : top-N par fréquence, ties par lowercase asc
  - canonicalize_batch : appel LLM + parsing + fallbacks
  - canonicalize_themes : batching + parallélisation + cache + ordre
"""

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
from lib.theme_canonicalizer import (  # noqa: E402
    BatchCanonicalization,
    RawCanonicalPair,
    _stable_batch_key,
    build_vocabulary,
    canonicalize_batch,
    canonicalize_themes,
)


class _CanoBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="klodo_cano_"))
        (self.tmp / "profiles" / "p").mkdir(parents=True)
        self._patcher = mock.patch.object(
            data, "get_project_root", return_value=self.tmp,
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cache_path(self):
        return self.tmp / "profiles" / "p" / ".cache" / "theme-canonicalizer.json"


class TestBuildVocabulary(unittest.TestCase):
    def test_basic_top_n(self):
        counts = {"A": 100, "B": 50, "C": 200, "D": 10}
        vocab = build_vocabulary(counts, top_n=2)
        self.assertEqual(vocab, ["C", "A"])  # 200, 100

    def test_top_n_larger_than_input(self):
        counts = {"A": 5, "B": 3}
        vocab = build_vocabulary(counts, top_n=100)
        # Tout est inclus, sans crash
        self.assertEqual(set(vocab), {"A", "B"})

    def test_ties_sorted_by_lowercase_asc(self):
        # Egalité de count → tri lexicographique sur lowercase
        counts = {"Zebra": 10, "apple": 10, "Banana": 10}
        vocab = build_vocabulary(counts, top_n=3)
        self.assertEqual(vocab, ["apple", "Banana", "Zebra"])

    def test_empty_input(self):
        self.assertEqual(build_vocabulary({}), [])


class TestStableBatchKey(unittest.TestCase):
    def test_batch_order_matters(self):
        # Le batch est ordonné (le LLM produit mappings[] dans l'ordre)
        # donc la clé doit changer si l'ordre change.
        k1 = _stable_batch_key(["A", "B", "C"], ["v1", "v2"])
        k2 = _stable_batch_key(["C", "B", "A"], ["v1", "v2"])
        self.assertNotEqual(k1, k2)

    def test_vocab_order_irrelevant(self):
        # Le vocab est trié dans la clé pour ne pas dépendre de son ordre.
        k1 = _stable_batch_key(["A", "B"], ["v1", "v2"])
        k2 = _stable_batch_key(["A", "B"], ["v2", "v1"])
        self.assertEqual(k1, k2)

    def test_length_16(self):
        self.assertEqual(len(_stable_batch_key(["A"], ["B"])), 16)


class TestCanonicalizeBatch(unittest.TestCase):
    def _mock_llm(self, mappings: list[RawCanonicalPair]):
        mock_llm = mock.MagicMock()
        mock_structured = mock.MagicMock()
        mock_llm.with_structured_output.return_value = mock_structured
        mock_structured.invoke.return_value = BatchCanonicalization(mappings=mappings)
        return mock_llm

    def test_happy_path(self):
        mock_llm = self._mock_llm([
            RawCanonicalPair(raw="ML", canonical="Machine Learning", matched_existing=True),
            RawCanonicalPair(raw="machine learning", canonical="Machine Learning",
                            matched_existing=True),
        ])
        result = canonicalize_batch(
            ["ML", "machine learning"],
            ["Machine Learning"],
            mock_llm,
        )
        self.assertEqual(result, {
            "ML": "Machine Learning",
            "machine learning": "Machine Learning",
        })
        # Vérifie qu'on a forcé method=function_calling (GLM-4.7 compat)
        mock_llm.with_structured_output.assert_called_once_with(
            BatchCanonicalization, method="function_calling",
        )

    def test_uses_canonical_function_calling_method(self):
        # GLM-4.7 ne supporte pas json mode — on doit forcer function_calling
        mock_llm = self._mock_llm([
            RawCanonicalPair(raw="A", canonical="A", matched_existing=False),
        ])
        canonicalize_batch(["A"], [], mock_llm)
        _, kwargs = mock_llm.with_structured_output.call_args
        self.assertEqual(kwargs.get("method"), "function_calling")

    def test_empty_batch(self):
        mock_llm = mock.MagicMock()
        result = canonicalize_batch([], ["voc"], mock_llm)
        self.assertEqual(result, {})
        # Pas d'appel LLM sur batch vide
        mock_llm.with_structured_output.assert_not_called()

    def test_safety_net_when_llm_omits_themes(self):
        # Le LLM renvoie 1 mapping sur 2 demandés → l'autre doit avoir
        # identité par défaut.
        mock_llm = self._mock_llm([
            RawCanonicalPair(raw="A", canonical="X", matched_existing=True),
        ])
        result = canonicalize_batch(["A", "B"], ["X"], mock_llm)
        self.assertEqual(result["A"], "X")
        self.assertEqual(result["B"], "B")  # filet identité

    def test_empty_canonical_falls_back_to_raw(self):
        # Le LLM renvoie un canonical vide → identité
        mock_llm = self._mock_llm([
            RawCanonicalPair(raw="A", canonical="   ", matched_existing=False),
        ])
        result = canonicalize_batch(["A"], [], mock_llm)
        self.assertEqual(result["A"], "A")

    def test_deduplicates_repeated_raw(self):
        # Si le LLM répète un raw dans mappings, on garde la 1ère décision
        mock_llm = self._mock_llm([
            RawCanonicalPair(raw="A", canonical="X", matched_existing=True),
            RawCanonicalPair(raw="A", canonical="Y", matched_existing=False),
        ])
        result = canonicalize_batch(["A"], ["X"], mock_llm)
        self.assertEqual(result["A"], "X")


class TestCanonicalizeThemes(_CanoBase):
    def _scripted_llm(self, mapping: dict[str, str]):
        """LLM qui retourne un canonical par raw selon un dict scripté."""
        mock_llm = mock.MagicMock()
        mock_structured = mock.MagicMock()
        mock_llm.with_structured_output.return_value = mock_structured

        def respond(messages):
            # Parse le batch depuis le HumanMessage
            content = messages[1].content
            # Trouve les lignes "  - 'theme'"
            import re
            raws = re.findall(r"^\s*-\s*'([^']+)'", content, re.MULTILINE)
            pairs = []
            for raw in raws:
                canonical = mapping.get(raw, raw)
                pairs.append(RawCanonicalPair(
                    raw=raw, canonical=canonical,
                    matched_existing=canonical != raw,
                ))
            return BatchCanonicalization(mappings=pairs)

        mock_structured.invoke.side_effect = respond
        return mock_llm

    def test_vocabulary_themes_skip_llm(self):
        mock_llm = mock.MagicMock()
        result = canonicalize_themes(
            ["Machine Learning", "Data Science"],
            vocabulary=["Machine Learning", "Data Science"],
            llm=mock_llm,
            profile="p",
            use_cache=False,
        )
        self.assertEqual(result, {
            "Machine Learning": "Machine Learning",
            "Data Science": "Data Science",
        })
        # Aucun appel LLM (tous dans vocab)
        mock_llm.with_structured_output.assert_not_called()

    def test_basic_canonicalization(self):
        llm = self._scripted_llm({
            "ML": "Machine Learning",
            "machine learning": "Machine Learning",
        })
        result = canonicalize_themes(
            ["Machine Learning", "ML", "machine learning"],
            vocabulary=["Machine Learning"],
            llm=llm,
            profile="p",
            batch_size=10,
            max_workers=1,
            use_cache=False,
        )
        # "Machine Learning" → identity (skip LLM)
        # "ML" + "machine learning" → "Machine Learning" (LLM)
        self.assertEqual(result["Machine Learning"], "Machine Learning")
        self.assertEqual(result["ML"], "Machine Learning")
        self.assertEqual(result["machine learning"], "Machine Learning")

    def test_batching(self):
        # 25 thèmes avec batch_size=10 → 3 batches
        themes = [f"theme_{i}" for i in range(25)]
        llm = self._scripted_llm({})  # tout → identity
        result = canonicalize_themes(
            themes, vocabulary=[], llm=llm, profile="p",
            batch_size=10, max_workers=1, use_cache=False,
        )
        self.assertEqual(len(result), 25)

    def test_cache_persists_across_calls(self):
        llm = self._scripted_llm({"A": "X"})
        # 1er appel
        canonicalize_themes(
            ["A"], vocabulary=[], llm=llm, profile="p",
            batch_size=10, max_workers=1, use_cache=True,
        )
        self.assertTrue(self._cache_path().exists())
        # 2e appel : cache hit, LLM jamais ré-appelé
        llm.with_structured_output.return_value.invoke.reset_mock()
        canonicalize_themes(
            ["A"], vocabulary=[], llm=llm, profile="p",
            batch_size=10, max_workers=1, use_cache=True,
        )
        llm.with_structured_output.return_value.invoke.assert_not_called()

    def test_progress_callback(self):
        themes = [f"theme_{i}" for i in range(15)]
        llm = self._scripted_llm({})
        calls: list[tuple[int, int]] = []
        canonicalize_themes(
            themes, vocabulary=[], llm=llm, profile="p",
            batch_size=5,  # → 3 batches
            max_workers=1, use_cache=False,
            on_progress=lambda done, total: calls.append((done, total)),
        )
        # 3 batches → 3 callbacks min, dont le dernier (3, 3)
        self.assertGreaterEqual(len(calls), 3)
        self.assertEqual(calls[-1], (3, 3))

    def test_empty_input(self):
        mock_llm = mock.MagicMock()
        self.assertEqual(
            canonicalize_themes([], ["v"], mock_llm, "p", use_cache=False),
            {},
        )
        mock_llm.with_structured_output.assert_not_called()

    def test_no_themes_outside_vocab(self):
        # Tous les thèmes sont dans le vocab → identity, pas d'appel LLM
        mock_llm = mock.MagicMock()
        result = canonicalize_themes(
            ["A", "B", "C"], vocabulary=["A", "B", "C"], llm=mock_llm,
            profile="p", use_cache=False,
        )
        self.assertEqual(result, {"A": "A", "B": "B", "C": "C"})
        mock_llm.with_structured_output.assert_not_called()

    def test_llm_failure_falls_back_to_identity(self):
        """Si le LLM raise sur un batch, le pipeline retombe sur identité."""
        mock_llm = mock.MagicMock()
        mock_structured = mock.MagicMock()
        mock_llm.with_structured_output.return_value = mock_structured
        mock_structured.invoke.side_effect = RuntimeError("simulated LLM down")

        themes = ["A", "B", "C"]
        result = canonicalize_themes(
            themes, vocabulary=[], llm=mock_llm, profile="p",
            batch_size=10, max_workers=1, use_cache=False,
        )
        # Pas d'exception remontée — tout est en identité
        self.assertEqual(result, {"A": "A", "B": "B", "C": "C"})

    def test_parallel_workers_preserve_completeness(self):
        # 50 thèmes en 5 batches de 10 sur 4 workers → tous présents
        themes = [f"th_{i}" for i in range(50)]
        llm = self._scripted_llm({})
        result = canonicalize_themes(
            themes, vocabulary=[], llm=llm, profile="p",
            batch_size=10, max_workers=4, use_cache=False,
        )
        self.assertEqual(set(result.keys()), set(themes))


if __name__ == "__main__":
    unittest.main()
