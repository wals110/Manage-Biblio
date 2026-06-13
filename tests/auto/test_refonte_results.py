#!/usr/bin/env python3
"""Tests des agrégations pures de la restitution Phase B (refonte_results)."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

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


from dashboard.refonte_results import build_proposed_tree  # noqa: E402


class TestProposedTree(unittest.TestCase):

    def test_incoming_counts_and_creation_flag(self):
        folders = ["02-INFORMATIQUE/14-Web", "02-INFORMATIQUE/05-IA-ML/RAG"]
        rows = [
            {"proposed_folder": "02-INFORMATIQUE/14-Web"},
            {"proposed_folder": "02-INFORMATIQUE/14-Web"},
            {"proposed_folder": "02-INFORMATIQUE/05-IA-ML/RAG"},
        ]
        tree = build_proposed_tree(folders, rows,
                                   creations={"02-INFORMATIQUE/05-IA-ML/RAG"})
        info = next(c for c in tree["children"] if c["name"] == "02-INFORMATIQUE")
        self.assertEqual(info["n_incoming"], 3)
        rag = _find(info, "RAG")
        self.assertTrue(rag["is_creation"])
        self.assertEqual(rag["n_incoming"], 1)


from dashboard.refonte_results import folder_provenance  # noqa: E402


class TestProvenance(unittest.TestCase):

    def test_top_origins_for_folder(self):
        rows = [
            {"proposed_folder": "X/Y", "current_folder": "_INBOX"},
            {"proposed_folder": "X/Y", "current_folder": "_INBOX"},
            {"proposed_folder": "X/Y", "current_folder": "04-SHS"},
            {"proposed_folder": "Z", "current_folder": "_INBOX"},
        ]
        out = folder_provenance(rows, "X/Y")
        self.assertEqual(out["origins"][0], {"folder": "_INBOX", "count": 2})
        self.assertEqual(out["origins"][1], {"folder": "04-SHS", "count": 1})

    def test_limit(self):
        rows = [{"proposed_folder": "X", "current_folder": f"o{i}"} for i in range(20)]
        out = folder_provenance(rows, "X", limit=8)
        self.assertEqual(len(out["origins"]), 8)


def _find(node, name):
    if node["name"] == name:
        return node
    for c in node.get("children", []):
        r = _find(c, name)
        if r:
            return r
    return None


import dashboard.refonte_results as refonte_results  # noqa: E402
from dashboard.refonte_results import (  # noqa: E402
    load_creations,
    load_proposed_folders,
    read_projection_rows,
)


class TestReaders(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.run = Path(self.tmp) / "run1"
        (self.run / "proposed").mkdir(parents=True)
        (self.run / "simulation").mkdir(parents=True)
        (self.run / "simulation" / "reclassify-projection.csv").write_text(
            "rel_path,current_folder,proposed_folder,changed,source,top_theme,confidence,score\n"
            "a.pdf,_INBOX,02-INFO/Web,true,LLM (theme),Web,0.95,0.9\n",
            encoding="utf-8")
        (self.run / "proposed" / "changes.json").write_text(
            json.dumps({"creations": [{"path": "02-INFO/Web", "rationale": "x"}]}),
            encoding="utf-8")
        import yaml
        (self.run / "proposed" / "tree-proposed.yaml").write_text(
            yaml.safe_dump({"folders": ["02-INFO/Web", "02-INFO/IA"]}),
            encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_read_rows(self):
        rows = read_projection_rows(self.run)
        self.assertEqual(rows[0]["rel_path"], "a.pdf")
        self.assertEqual(rows[0]["proposed_folder"], "02-INFO/Web")

    def test_load_creations(self):
        self.assertEqual(load_creations(self.run), {"02-INFO/Web"})

    def test_load_proposed_folders(self):
        self.assertEqual(load_proposed_folders(self.run),
                         ["02-INFO/Web", "02-INFO/IA"])

    def test_missing_files_return_empty(self):
        empty = Path(self.tmp) / "nope"
        self.assertEqual(read_projection_rows(empty), [])
        self.assertEqual(load_creations(empty), set())
        self.assertEqual(load_proposed_folders(empty), [])


class TestSelectMoveRows(unittest.TestCase):
    """select_move_rows : changed + non-doute uniquement."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="klodo-selmoves-"))
        (self.tmp / "simulation").mkdir(parents=True)
        (self.tmp / "proposed").mkdir(parents=True)
        (self.tmp / "simulation" / "reclassify-projection.csv").write_text(
            "rel_path,current_folder,proposed_folder,changed,source,top_theme,confidence,score\n"
            # P1 sûr + changed → INCLUS
            "A/sure.pdf,A,B/Dest,True,LLM (theme),X,0.95,0.9\n"
            # P1 raffiné sûr + changed → INCLUS
            "A/ref.pdf,A,B/Ref,True,LLM (theme→refined),X,0.8,0.8\n"
            # P1 mais confiance < 0.7 → DOUTE (exclu)
            "A/lowconf.pdf,A,B/Low,True,LLM (theme),X,0.5,0.5\n"
            # Keyword → DOUTE (exclu)
            "A/kw.pdf,A,B/Kw,True,Keyword,X,0.9,0.9\n"
            # changed=False → STABLE
            "A/stay.pdf,A,A,False,LLM (theme),X,0.95,0.9\n"
            # FAILED sans destination → STABLE (pas de move possible)
            "A/fail.pdf,A,,False,FAILED,,0.0,0.0\n",
            encoding="utf-8")
        (self.tmp / "proposed" / "changes.json").write_text(
            json.dumps({"creations": [{"path": "B/Dest", "rationale": "x"}]}),
            encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_partitions_moves_doubt_stable(self):
        r = refonte_results.select_move_rows(self.tmp)
        self.assertEqual(r["n_moves"], 2)
        self.assertEqual(r["n_doubt_excluded"], 2)
        self.assertEqual(r["n_stable"], 2)
        rels = {m["rel_path"] for m in r["moves"]}
        self.assertEqual(rels, {"A/sure.pdf", "A/ref.pdf"})

    def test_moves_are_enriched_rows(self):
        r = refonte_results.select_move_rows(self.tmp)
        m = next(x for x in r["moves"] if x["rel_path"] == "A/sure.pdf")
        self.assertEqual(m["proposed_folder"], "B/Dest")
        self.assertEqual(m["source_class"], "p1_theme")
        self.assertIsInstance(m["confidence"], float)

    def test_empty_run_dir(self):
        empty = Path(tempfile.mkdtemp(prefix="klodo-selmoves-empty-"))
        try:
            r = refonte_results.select_move_rows(empty)
            self.assertEqual(r, {"moves": [], "n_moves": 0,
                                 "n_doubt_excluded": 0, "n_stable": 0})
        finally:
            shutil.rmtree(empty, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
