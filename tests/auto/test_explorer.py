#!/usr/bin/env python3
"""Tests Explorateur (Vision/LLM mockés, zéro SSD réel)."""
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


def _make_profile(root: Path, profile: str, files: dict, mapping: dict, categories: str = "{}"):
    """files: {nom_fichier: theme|None}. None → pas d'entrée vision_cache (non analysé)."""
    import lib.vision_cache as vc
    prof = root / "profiles" / profile
    (prof / ".cache").mkdir(parents=True)
    target = root / "LIB"
    target.mkdir(exist_ok=True)
    (prof / "profile.yaml").write_text(yaml.safe_dump(
        {"target": str(target), "llm": {"model": "M"}, "defaults": {"pages": 2}}), encoding="utf-8")
    (prof / "theme_mapping.yaml").write_text(yaml.safe_dump(mapping), encoding="utf-8")
    (prof / "categories.yaml").write_text(categories, encoding="utf-8")
    (prof / "tree.yaml").write_text(yaml.safe_dump({"folders": list(set(mapping.values()))}), encoding="utf-8")
    cache: dict = {}
    for fname, theme in files.items():
        fp = target / fname
        fp.write_bytes(b"%PDF-1.4 " + fname.encode())
        if theme is not None:
            key = vc.compute_cache_key(str(fp), model="M", n_pages=2)
            vc.store(cache, key, {"theme": theme,
                                  "themes": [{"theme": theme, "confidence": 0.9}],
                                  "confidence": 0.9}, "M")
    vc.save_cache(prof / ".cache" / "vision_cache.json", cache)
    return target


class TestScanAnalyzedAndProgress(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="klodo-expl-")
        self.root = Path(self.tmp)
        _make_profile(self.root, "p",
                      {"a.pdf": "Deep Learning", "b.pdf": None},
                      mapping={"deep learning": "01-Info/ML"})
        # Mirror patching from test_reclassify_apply.py and test_taxonomy.py:
        # taxonomy calls data.get_project_root() via _profile_dir().
        self.patch = mock.patch("dashboard.data.get_project_root", return_value=self.root)
        self.patch.start()
        # Reset taxonomy in-memory caches to pick up our fixture
        from dashboard import taxonomy
        taxonomy.reset_cache()

    def tearDown(self):
        mock.patch.stopall()
        from dashboard import taxonomy
        taxonomy.reset_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reclassify_include_keyword_constant(self):
        from dashboard import taxonomy
        self.assertEqual(taxonomy.RECLASSIFY_INCLUDE_KEYWORD, True)

    def test_scan_marks_analyzed_per_file(self):
        from dashboard import taxonomy
        rows = {r["rel_path"]: r for r in taxonomy._scan_and_classify("p", include_step2=True)}
        self.assertTrue(rows["a.pdf"]["analyzed"])
        self.assertFalse(rows["b.pdf"]["analyzed"])

    def test_scan_reports_progress(self):
        from dashboard import taxonomy
        seen = []
        taxonomy._scan_and_classify("p", include_step2=True,
                                    on_progress=lambda d, t: seen.append((d, t)))
        self.assertTrue(seen)
        self.assertEqual(seen[-1], (2, 2))


if __name__ == "__main__":
    unittest.main()
