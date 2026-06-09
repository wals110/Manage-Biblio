#!/usr/bin/env python3
"""Tests des agrégations pures de la restitution Phase B (refonte_results)."""

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from dashboard.refonte_results import bucket_source, confidence_band  # noqa: E402


class TestBucketSource(unittest.TestCase):

    def test_p1_refined_before_p1_theme(self):
        # "LLM (theme→refined)" doit matcher refined AVANT theme (ordre important)
        self.assertEqual(bucket_source("LLM (theme→refined)"), "p1_refined")
        self.assertEqual(bucket_source("LLM (theme→N3-refined)"), "p1_refined")

    def test_p1_theme(self):
        self.assertEqual(bucket_source("LLM (theme)"), "p1_theme")
        self.assertEqual(bucket_source("LLM (theme-generic)"), "p1_theme")

    def test_keyword(self):
        self.assertEqual(bucket_source("Keyword (logic)"), "keyword")
        self.assertEqual(bucket_source("Keyword ()"), "keyword")

    def test_fallback(self):
        self.assertEqual(bucket_source("LLM (fallback)"), "fallback")

    def test_failed_and_empty(self):
        self.assertEqual(bucket_source("FAILED"), "failed")
        self.assertEqual(bucket_source(""), "failed")
        self.assertEqual(bucket_source(None), "failed")


class TestConfidenceBand(unittest.TestCase):

    def test_bands(self):
        self.assertEqual(confidence_band(0.0), "0-0.5")
        self.assertEqual(confidence_band(0.49), "0-0.5")
        self.assertEqual(confidence_band(0.5), "0.5-0.7")
        self.assertEqual(confidence_band(0.69), "0.5-0.7")
        self.assertEqual(confidence_band(0.7), "0.7-0.9")
        self.assertEqual(confidence_band(0.89), "0.7-0.9")
        self.assertEqual(confidence_band(0.9), "0.9-1.0")
        self.assertEqual(confidence_band(1.0), "0.9-1.0")

    def test_none_is_lowest(self):
        self.assertEqual(confidence_band(None), "0-0.5")


if __name__ == "__main__":
    unittest.main()
