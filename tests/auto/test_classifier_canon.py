#!/usr/bin/env python3
"""Canonicalisation des thèmes avant le lookup theme_mapping (P1)."""

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from lib.classifier import classify_by_theme, classify_combined  # noqa: E402


class TestCanonInClassifyByTheme(unittest.TestCase):
    def setUp(self):
        self.mapping = {"Deep Learning": "02-INFO/IA/Deep-Learning"}

    def test_variant_routes_via_canon_table(self):
        # "dl variant" n'est PAS dans le mapping, mais la table de canon le
        # ramène à "Deep Learning" → doit router.
        canon = {"dl variant": "Deep Learning"}
        self.assertEqual(
            classify_by_theme("dl variant", self.mapping, canon_table=canon),
            "02-INFO/IA/Deep-Learning")

    def test_no_canon_table_is_unchanged(self):
        self.assertIsNone(
            classify_by_theme("dl variant", self.mapping, canon_table=None))
        self.assertEqual(
            classify_by_theme("deep learning", self.mapping),
            "02-INFO/IA/Deep-Learning")  # exact match insensible casse, inchangé


class TestCanonInClassifyCombined(unittest.TestCase):
    def test_combined_threads_canon_to_p1(self):
        mapping = {"Deep Learning": "02-INFO/IA/Deep-Learning"}
        canon = {"dl variant": "Deep Learning"}
        result = {"themes": [{"theme": "dl variant", "confidence": 0.95}],
                  "confidence": 0.95}
        dest, _score, source = classify_combined(
            result, "x.pdf", mapping, canon_table=canon)
        self.assertEqual(dest, "02-INFO/IA/Deep-Learning")
        self.assertTrue(source.startswith("LLM"))

    def test_combined_without_canon_unchanged(self):
        mapping = {"Deep Learning": "02-INFO/IA/Deep-Learning"}
        result = {"themes": [{"theme": "dl variant", "confidence": 0.95}],
                  "confidence": 0.95}
        dest, _score, _source = classify_combined(result, "x.pdf", mapping)
        self.assertIsNone(dest)  # pas de canon → pas de match


if __name__ == "__main__":
    unittest.main()
