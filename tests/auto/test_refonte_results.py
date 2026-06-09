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


from dashboard.refonte_results import build_risk_matrix  # noqa: E402


class TestRiskMatrix(unittest.TestCase):

    def _rows(self):
        return [
            {"source": "LLM (theme)", "confidence": "0.95"},
            {"source": "LLM (theme)", "confidence": "0.95"},
            {"source": "Keyword (x)", "confidence": "0.40"},
            {"source": "LLM (fallback)", "confidence": "0.95"},
            {"source": "FAILED", "confidence": "0.0"},
        ]

    def test_matrix_counts(self):
        out = build_risk_matrix(self._rows())
        cell = next(c for c in out["matrix"]
                    if c["source_class"] == "p1_theme" and c["band"] == "0.9-1.0")
        self.assertEqual(cell["count"], 2)

    def test_budget_totals(self):
        out = build_risk_matrix(self._rows())
        budget = {b["source_class"]: b["count"] for b in out["budget"]}
        self.assertEqual(budget["p1_theme"], 2)
        self.assertEqual(budget["keyword"], 1)
        self.assertEqual(budget["fallback"], 1)
        self.assertEqual(budget["failed"], 1)

    def test_n_doubt_excludes_safe_p1(self):
        # doute = tout sauf P1(theme/refined) à conf>=0.7. Ici keyword+fallback+failed=3
        out = build_risk_matrix(self._rows())
        self.assertEqual(out["n_doubt"], 3)


from dashboard.refonte_results import enrich_row, select_doubt_files  # noqa: E402


class TestDoubtFiles(unittest.TestCase):

    def _rows(self):
        return [
            {"rel_path": "a.pdf", "current_folder": "_INBOX",
             "proposed_folder": "02-INFORMATIQUE/14-Web", "source": "LLM (theme)",
             "top_theme": "Web", "confidence": "0.95"},
            {"rel_path": "b.pdf", "current_folder": "04-SHS/03-HISTOIRE",
             "proposed_folder": "01-SCIENCES/02-PHYSIQUE/Astro", "source": "LLM (fallback)",
             "top_theme": "Astro", "confidence": "0.93"},
            {"rel_path": "c.pdf", "current_folder": "_INBOX",
             "proposed_folder": "", "source": "FAILED", "top_theme": "",
             "confidence": "0.0"},
        ]

    def test_enrich_flags(self):
        row = enrich_row(self._rows()[1], creations={"01-SCIENCES/02-PHYSIQUE/Astro"})
        self.assertEqual(row["source_class"], "fallback")
        self.assertTrue(row["is_jump"])
        self.assertTrue(row["is_new_dest"])
        self.assertIn("risk", row)

    def test_select_doubt_excludes_safe_p1(self):
        out = select_doubt_files(self._rows(), creations=set(), page=1, page_size=50)
        paths = [r["rel_path"] for r in out["rows"]]
        self.assertNotIn("a.pdf", paths)
        self.assertIn("b.pdf", paths)
        self.assertIn("c.pdf", paths)
        self.assertEqual(out["total"], 2)

    def test_sorted_by_risk_desc(self):
        out = select_doubt_files(self._rows(), creations=set(), page=1, page_size=50)
        risks = [r["risk"] for r in out["rows"]]
        self.assertEqual(risks, sorted(risks, reverse=True))

    def test_filter_by_source_class_and_band(self):
        out = select_doubt_files(self._rows(), creations=set(), page=1, page_size=50,
                                 source_class="failed")
        self.assertEqual([r["rel_path"] for r in out["rows"]], ["c.pdf"])

    def test_pagination(self):
        out = select_doubt_files(self._rows(), creations=set(), page=1, page_size=1)
        self.assertEqual(len(out["rows"]), 1)
        self.assertEqual(out["total"], 2)
        self.assertEqual(out["page"], 1)


if __name__ == "__main__":
    unittest.main()
