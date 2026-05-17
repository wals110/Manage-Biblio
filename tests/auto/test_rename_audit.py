#!/usr/bin/env python3
"""Tests for dashboard/rename.py — scan + categorize + suggest."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from dashboard import data, rename  # noqa: E402


class RenameAuditTestBase(unittest.TestCase):
    """Isolated profile + target with vision_cache wired up."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="klodo-renaud-"))
        self.profile_name = "test"
        self.profile_dir = self.tmpdir / "profiles" / self.profile_name
        self.target = self.tmpdir / "library"
        self.target.mkdir(parents=True, exist_ok=True)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        # profile.yaml with target wired
        (self.profile_dir / "profile.yaml").write_text(yaml.safe_dump({
            "name": "test",
            "target": str(self.target),
            "llm": {"model": "Qwen/Qwen3-VL-32B-Instruct"},
            "defaults": {"pages": 2},
        }))
        self._patcher = mock.patch.object(
            data, "get_project_root", return_value=self.tmpdir,
        )
        self._patcher.start()
        rename.reset_cache()

    def tearDown(self):
        self._patcher.stop()
        rename.reset_cache()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _add_file_with_cache(
        self,
        rel: str,
        title: str,
        author: str = "",
        confidence: float = 0.95,
    ) -> Path:
        """Write a unique-content PDF + add the corresponding vision_cache entry."""
        from lib import vision_cache as vc
        abs_path = self.target / rel
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        # Unique content so the cache_key is distinct per file
        abs_path.write_bytes(f"%PDF-1.4 {rel}".encode())
        key = vc.compute_cache_key(
            str(abs_path),
            model="Qwen/Qwen3-VL-32B-Instruct",
            n_pages=2,
        )
        assert key is not None
        cache_path = self.profile_dir / ".cache" / "vision_cache.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache: dict = {}
        if cache_path.exists():
            try:
                cache = json.loads(cache_path.read_text())
            except (OSError, json.JSONDecodeError):
                cache = {}
        cache[key] = {
            "result": {
                "title": title,
                "author": author,
                "confidence": confidence,
            },
            "model": "Qwen/Qwen3-VL-32B-Instruct",
            "prompt_version": "v3",
        }
        cache_path.write_text(json.dumps(cache))
        return abs_path

    def _set_rename_config(self, **overrides):
        """Patch profile.yaml's rename block."""
        cfg = yaml.safe_load((self.profile_dir / "profile.yaml").read_text())
        cfg["rename"] = overrides
        (self.profile_dir / "profile.yaml").write_text(yaml.safe_dump(cfg))


# ─── Config resolution ───────────────────────────────────────────────────


class TestRenameConfig(RenameAuditTestBase):

    def test_defaults_when_block_missing(self):
        cfg = rename.get_rename_config(self.profile_name)
        self.assertEqual(cfg["template"], rename.DEFAULT_TEMPLATE)
        self.assertEqual(cfg["fallback"], rename.DEFAULT_FALLBACK)
        self.assertEqual(cfg["max_length"], rename.DEFAULT_MAX_LENGTH)

    def test_overrides_picked_up(self):
        self._set_rename_config(
            template="{title} - {author}", fallback="{title}",
            max_length=120, min_title_confidence=0.5,
        )
        cfg = rename.get_rename_config(self.profile_name)
        self.assertEqual(cfg["template"], "{title} - {author}")
        self.assertEqual(cfg["max_length"], 120)
        self.assertEqual(cfg["min_title_confidence"], 0.5)


# ─── Audit ───────────────────────────────────────────────────────────────


class TestRenameAuditBasics(RenameAuditTestBase):

    def test_empty_lib_yields_empty(self):
        r = rename.rename_audit(self.profile_name)
        self.assertEqual(r["stats"]["n_total"], 0)
        self.assertEqual(r["candidates"], [])

    def test_file_without_cache_marked_no_metadata(self):
        # File but no cache entry
        (self.target / "alpha.pdf").write_bytes(b"%PDF unique alpha")
        r = rename.rename_audit(self.profile_name)
        self.assertEqual(r["stats"]["n_total"], 1)
        self.assertEqual(r["stats"]["n_no_metadata"], 1)
        self.assertEqual(r["stats"]["n_with_title"], 0)

    def test_low_confidence_skipped(self):
        self._add_file_with_cache("foo.pdf", title="The Real Title",
                                  confidence=0.4)
        # Default min_title_confidence is 0.85 → this file is skipped
        r = rename.rename_audit(self.profile_name)
        self.assertEqual(r["stats"]["n_no_metadata"], 1)
        self.assertEqual(r["stats"]["n_with_title"], 0)

    def test_high_confidence_audited(self):
        self._add_file_with_cache(
            "rubbish.pdf",
            title="Hands-On Machine Learning",
            author="Aurélien Géron",
            confidence=0.95,
        )
        r = rename.rename_audit(self.profile_name)
        self.assertEqual(r["stats"]["n_with_title"], 1)
        c = r["candidates"][0]
        self.assertEqual(c["current_name"], "rubbish.pdf")
        self.assertEqual(
            c["suggested_name"],
            "Hands-On Machine Learning - Aurélien Géron.pdf",
        )
        self.assertEqual(c["title"], "Hands-On Machine Learning")
        self.assertEqual(c["author"], "Aurélien Géron")


# ─── Categorization ──────────────────────────────────────────────────────


class TestCategorization(RenameAuditTestBase):

    def test_placeholder_detected(self):
        self._add_file_with_cache(
            "Title Author.pdf",
            title="Real Book", author="Real Author",
        )
        r = rename.rename_audit(self.profile_name)
        cat = r["candidates"][0]["category"]
        self.assertEqual(cat, "placeholder")
        self.assertEqual(r["stats"]["n_placeholder"], 1)

    def test_placeholder_with_suffix_number_detected(self):
        self._add_file_with_cache(
            "Title Author (7).pdf",
            title="Real Book", author="Real Author",
        )
        r = rename.rename_audit(self.profile_name)
        self.assertEqual(r["candidates"][0]["category"], "placeholder")

    def test_divergent_detected(self):
        self._add_file_with_cache(
            "abcd.pdf",
            title="Hands-On Machine Learning",
            author="Aurélien Géron",
        )
        r = rename.rename_audit(self.profile_name)
        c = r["candidates"][0]
        self.assertEqual(c["category"], "divergent")
        self.assertLess(c["similarity"], 0.4)

    def test_minor_case_detected(self):
        # Same title but different casing → similarity moderate-high
        self._add_file_with_cache(
            "hands-on machine learning.pdf",
            title="Hands-On Machine Learning",
            author="",
        )
        self._set_rename_config(template="{title}", fallback="{title}",
                                min_title_confidence=0.5)
        r = rename.rename_audit(self.profile_name)
        c = r["candidates"][0]
        self.assertEqual(c["category"], "ok")
        self.assertGreaterEqual(c["similarity"], 0.7)

    def test_ok_when_filename_matches_template_output(self):
        self._add_file_with_cache(
            "Hands-On Machine Learning - Aurélien Géron.pdf",
            title="Hands-On Machine Learning",
            author="Aurélien Géron",
        )
        r = rename.rename_audit(self.profile_name)
        c = r["candidates"][0]
        self.assertEqual(c["category"], "ok")


# ─── Sort order ──────────────────────────────────────────────────────────


class TestSortOrder(RenameAuditTestBase):

    def test_placeholder_before_divergent_before_ok(self):
        self._add_file_with_cache("Hands-On Machine Learning - Aurélien Géron.pdf",
                                   title="Hands-On Machine Learning",
                                   author="Aurélien Géron")
        self._add_file_with_cache("xyzzy.pdf",
                                   title="A Totally Different Title",
                                   author="Other Author")
        self._add_file_with_cache("Title Author.pdf",
                                   title="Whatever", author="Whoever")
        r = rename.rename_audit(self.profile_name)
        cats = [c["category"] for c in r["candidates"]]
        # placeholder first, then divergent, then ok last
        self.assertEqual(cats[0], "placeholder")
        self.assertEqual(cats[-1], "ok")


# ─── Cache ───────────────────────────────────────────────────────────────


class TestAuditCache(RenameAuditTestBase):

    def test_cached_second_call(self):
        self._add_file_with_cache("a.pdf", title="A", author="B")
        r1 = rename.rename_audit(self.profile_name)
        # Add a 2nd file but don't reset cache → audit should NOT see it
        self._add_file_with_cache("c.pdf", title="C", author="D")
        r2 = rename.rename_audit(self.profile_name)
        self.assertEqual(r1["stats"]["n_total"], r2["stats"]["n_total"])

    def test_force_reload_bypasses_cache(self):
        self._add_file_with_cache("a.pdf", title="A", author="B")
        rename.rename_audit(self.profile_name)
        self._add_file_with_cache("c.pdf", title="C", author="D")
        r = rename.rename_audit(self.profile_name, force_reload=True)
        self.assertEqual(r["stats"]["n_total"], 2)

    def test_reset_cache_clears(self):
        self._add_file_with_cache("a.pdf", title="A", author="B")
        rename.rename_audit(self.profile_name)
        rename.reset_cache(self.profile_name)
        self._add_file_with_cache("c.pdf", title="C", author="D")
        r = rename.rename_audit(self.profile_name)
        self.assertEqual(r["stats"]["n_total"], 2)


# ─── Similarity util ─────────────────────────────────────────────────────


class TestSimilarity(unittest.TestCase):

    def test_identical_strings(self):
        self.assertEqual(rename._jaccard_3grams("abc", "abc"), 1.0)

    def test_empty_strings(self):
        self.assertEqual(rename._jaccard_3grams("", ""), 0.0)
        self.assertEqual(rename._jaccard_3grams("x", ""), 0.0)

    def test_case_insensitive(self):
        s = rename._jaccard_3grams("Hello World", "hello world")
        self.assertEqual(s, 1.0)

    def test_punctuation_ignored(self):
        s = rename._jaccard_3grams("hands-on ml", "hands on ml")
        self.assertGreater(s, 0.9)

    def test_completely_different(self):
        s = rename._jaccard_3grams("abcdefg", "xyzwvut")
        self.assertEqual(s, 0.0)


# ─── Endpoint ────────────────────────────────────────────────────────────


class TestRenameAuditEndpoint(RenameAuditTestBase):

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from dashboard.app import app
        self.client = TestClient(app)

    def test_endpoint_basic(self):
        r = self.client.get(f"/api/rename/audit?profile={self.profile_name}")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("candidates", body)
        self.assertIn("stats", body)
        self.assertIn("config", body)

    def test_endpoint_force_reload(self):
        self._add_file_with_cache("a.pdf", title="A", author="B")
        # First call caches
        self.client.get(f"/api/rename/audit?profile={self.profile_name}")
        # Add file
        self._add_file_with_cache("c.pdf", title="C", author="D")
        # Without force → cache hit (still 1)
        r1 = self.client.get(f"/api/rename/audit?profile={self.profile_name}")
        self.assertEqual(r1.json()["stats"]["n_total"], 1)
        # With force → re-scan (2)
        r2 = self.client.get(
            f"/api/rename/audit?profile={self.profile_name}&force=true")
        self.assertEqual(r2.json()["stats"]["n_total"], 2)


if __name__ == "__main__":
    unittest.main()
