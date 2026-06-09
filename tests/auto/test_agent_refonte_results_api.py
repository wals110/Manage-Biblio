#!/usr/bin/env python3
"""Tests d'intégration HTTP des endpoints de restitution Phase B."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml
from fastapi.testclient import TestClient

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from dashboard.app import app  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.profiles = Path(self.tmp) / "profiles"
        run = self.profiles / "default" / ".cache" / "refonte" / "run1"
        (run / "proposed").mkdir(parents=True)
        (run / "simulation").mkdir(parents=True)
        (run / "simulation" / "reclassify-projection.csv").write_text(
            "rel_path,current_folder,proposed_folder,changed,source,top_theme,confidence,score\n"
            "a.pdf,_INBOX,02-INFO/Web,true,LLM (theme),Web,0.95,0.9\n"
            "b.pdf,04-SHS/HIST,01-SCI/Astro,true,LLM (fallback),Astro,0.93,0.5\n"
            "c.pdf,_INBOX,,false,FAILED,,0.0,0.0\n", encoding="utf-8")
        (run / "proposed" / "changes.json").write_text(
            json.dumps({"creations": [{"path": "01-SCI/Astro", "rationale": "x"}]}),
            encoding="utf-8")
        (run / "proposed" / "tree-proposed.yaml").write_text(
            yaml.safe_dump({"folders": ["02-INFO/Web", "01-SCI/Astro"]}),
            encoding="utf-8")
        self.patch = mock.patch("dashboard.data.get_project_root",
                                return_value=Path(self.tmp))
        self.patch.start()
        self.client = TestClient(app)

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestRiskMatrixAPI(_Base):
    def test_risk_matrix_endpoint(self):
        r = self.client.get(
            "/api/agent/refonte/proposition/run1/risk-matrix?profile=default")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertIn("matrix", d)
        self.assertIn("budget", d)
        self.assertEqual(d["n_doubt"], 2)  # fallback + failed


class TestDoubtFilesAPI(_Base):
    def test_doubt_files_excludes_safe_and_sorts(self):
        r = self.client.get(
            "/api/agent/refonte/proposition/run1/doubt-files?profile=default")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        paths = [x["rel_path"] for x in d["rows"]]
        self.assertNotIn("a.pdf", paths)        # P1 0.95 sûr
        self.assertEqual(d["total"], 2)
        b = next(x for x in d["rows"] if x["rel_path"] == "b.pdf")
        self.assertTrue(b["is_jump"])
        self.assertTrue(b["is_new_dest"])

    def test_doubt_files_filter_source_class(self):
        r = self.client.get(
            "/api/agent/refonte/proposition/run1/doubt-files"
            "?profile=default&source_class=failed")
        d = r.json()
        self.assertEqual([x["rel_path"] for x in d["rows"]], ["c.pdf"])


class TestProposedTreeAPI(_Base):
    def test_tree_endpoint(self):
        r = self.client.get(
            "/api/agent/refonte/proposition/run1/proposed-tree?profile=default")
        self.assertEqual(r.status_code, 200)
        tree = r.json()["tree"]
        names = [c["name"] for c in tree["children"]]
        self.assertIn("02-INFO", names)
        self.assertIn("01-SCI", names)


class TestProvenanceAPI(_Base):
    def test_provenance_endpoint(self):
        r = self.client.get(
            "/api/agent/refonte/proposition/run1/folder-provenance"
            "?profile=default&folder=01-SCI/Astro")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["origins"], [{"folder": "04-SHS/HIST", "count": 1}])


if __name__ == "__main__":
    unittest.main()
