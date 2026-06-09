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


from dashboard.refonte_results import is_inter_discipline_jump  # noqa: E402


class TestJump(unittest.TestCase):

    def test_jump_between_disciplines(self):
        self.assertTrue(is_inter_discipline_jump(
            "04-SHS/03-HISTOIRE", "01-SCIENCES/02-PHYSIQUE"))

    def test_same_discipline_no_jump(self):
        self.assertFalse(is_inter_discipline_jump(
            "01-SCIENCES/PHYSIQUE", "01-SCIENCES/MATHEMATIQUES"))

    def test_inbox_origin_is_not_a_jump(self):
        self.assertFalse(is_inter_discipline_jump("_INBOX", "02-INFORMATIQUE/14-Web"))
        self.assertFalse(is_inter_discipline_jump("", "02-INFORMATIQUE/14-Web"))

    def test_empty_destination_no_jump(self):
        self.assertFalse(is_inter_discipline_jump("04-SHS", ""))


from dashboard.refonte_results import risk_score  # noqa: E402


class TestRiskScore(unittest.TestCase):

    def test_failed_is_max(self):
        r = risk_score("failed", confidence=0.0, is_jump=False, is_new_dest=False)
        self.assertGreater(r, 0.5)

    def test_p1_theme_high_conf_is_low(self):
        r = risk_score("p1_theme", confidence=1.0, is_jump=False, is_new_dest=False)
        self.assertEqual(r, 0.0)

    def test_jump_and_new_dest_add_risk(self):
        base = risk_score("keyword", confidence=0.9, is_jump=False, is_new_dest=False)
        more = risk_score("keyword", confidence=0.9, is_jump=True, is_new_dest=True)
        self.assertGreater(more, base)

    def test_ordering_fallback_gt_keyword_gt_p1(self):
        a = risk_score("fallback", 0.9, False, False)
        b = risk_score("keyword", 0.9, False, False)
        c = risk_score("p1_refined", 0.9, False, False)
        self.assertGreater(a, b)
        self.assertGreater(b, c)


if __name__ == "__main__":
    unittest.main()
