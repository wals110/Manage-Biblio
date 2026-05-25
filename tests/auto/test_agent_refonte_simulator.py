#!/usr/bin/env python3
"""Tests pour le simulateur reclassify de Phase B.

Module Python pur (no LLM). Couvre :
  - Pas de target FS → summary vide gracieusement
  - Fichier qui changerait de dossier avec le mapping proposé
  - Fichier qui resterait stable (déjà au bon endroit)
  - Fichier sans theme observé → no_prediction
  - CSV + summary.json bien produits avec les bonnes colonnes
"""

import csv
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

from agents.refonte.simulator import simulate_reclassify  # noqa: E402
from dashboard import data  # noqa: E402
from dashboard import taxonomy as tax

# ─── Fixtures ──────────────────────────────────────────────────────────────


def _make_profile(profiles_root: Path, name: str, *, target: Path,
                  folders: list[str], mapping: dict[str, str]) -> None:
    pdir = profiles_root / name
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "profile.yaml").write_text(yaml.safe_dump({"name": name, "target": str(target)}))
    (pdir / "tree.yaml").write_text(yaml.safe_dump({"folders": folders}))
    (pdir / "theme_mapping.yaml").write_text(yaml.safe_dump(mapping))
    target.mkdir(parents=True, exist_ok=True)


def _seed_vision_cache(profiles_root: Path, profile: str, entries: dict) -> None:
    """Écrit un vision_cache.json avec les entrées fournies."""
    cache_dir = profiles_root / profile / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "vision_cache.json").write_text(json.dumps(entries))


def _write_proposed_mapping(proposal_dir: Path, mapping: dict[str, str]) -> None:
    """Écrit le mapping-proposed.yaml dans un dossier proposed/."""
    proposal_dir.mkdir(parents=True, exist_ok=True)
    (proposal_dir / "theme_mapping-proposed.yaml").write_text(
        yaml.safe_dump(mapping, allow_unicode=True)
    )


class _SimBase(unittest.TestCase):
    """Setup commun : un profil sur disque + vision_cache + target avec quelques files."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="klodo_sim_"))
        self.profiles_root = self.tmp / "profiles"
        self.profiles_root.mkdir()
        self.target = self.tmp / "biblio"
        self._patcher = mock.patch.object(data, "get_project_root", return_value=self.tmp)
        self._patcher.start()
        tax.reset_cache()

    def tearDown(self):
        self._patcher.stop()
        tax.reset_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)


# ─── Tests ─────────────────────────────────────────────────────────────────


class TestSimulateReclassify(_SimBase):
    def test_target_missing_returns_empty(self):
        """Profil sans target → summary vide gracieusement."""
        # Profile with target set but pointing to nonexistent directory
        nonexistent = self.tmp / "doesnotexist"
        profile_dir = self.profiles_root / "p"
        profile_dir.mkdir()
        (profile_dir / "profile.yaml").write_text(yaml.safe_dump({
            "name": "p", "target": str(nonexistent),
        }))
        proposal_dir = self.tmp / "proposed"
        _write_proposed_mapping(proposal_dir, {})
        result = simulate_reclassify("p", proposal_dir)
        self.assertEqual(result["n_files"], 0)
        self.assertIn("summary_path", result)

    def test_file_would_move_with_proposed_mapping(self):
        """Un fichier classé via le current mapping irait ailleurs avec le proposed."""
        _make_profile(
            self.profiles_root, "p",
            target=self.target,
            folders=["OLD", "NEW"],
            mapping={"machine learning": "OLD"},
        )
        # Place un fichier dans OLD/ avec le bon nom de base
        old_dir = self.target / "OLD"
        old_dir.mkdir()
        pdf = old_dir / "ml_book.pdf"
        pdf.write_bytes(b"%PDF-1.4\nfake\n" + b"\x00" * 100)
        # vision_cache : clé = MD5 truqué (on patche compute_cache_key pour le test)
        with mock.patch("lib.vision_cache.compute_cache_key", return_value="HASH-ML"):
            _seed_vision_cache(self.profiles_root, "p", {
                "HASH-ML": {
                    "result": {
                        "title": "Machine Learning Book",
                        "theme": "machine learning",
                        "confidence": 0.95,
                    },
                    "model": "test",
                    "prompt_version": "v1",
                },
            })
            # Mapping proposed : machine learning → NEW (au lieu de OLD)
            proposal_dir = self.tmp / "proposed"
            _write_proposed_mapping(proposal_dir, {"machine learning": "NEW"})
            result = simulate_reclassify("p", proposal_dir)
        self.assertEqual(result["n_files"], 1)
        self.assertEqual(result["n_moving"], 1)
        self.assertEqual(result["n_stable"], 0)
        # Top destination = NEW (1 fichier entrant)
        self.assertEqual(result["top_destinations"][0]["folder"], "NEW")
        self.assertEqual(result["top_destinations"][0]["n_incoming"], 1)

    def test_file_stable_when_proposed_matches_current(self):
        """Un fichier qui irait au même endroit que son dossier actuel = stable."""
        _make_profile(
            self.profiles_root, "p",
            target=self.target,
            folders=["A"],
            mapping={"data": "A"},
        )
        a_dir = self.target / "A"
        a_dir.mkdir()
        pdf = a_dir / "data.pdf"
        pdf.write_bytes(b"%PDF-1.4\n" + b"\x00" * 100)
        with mock.patch("lib.vision_cache.compute_cache_key", return_value="HASH-D"):
            _seed_vision_cache(self.profiles_root, "p", {
                "HASH-D": {
                    "result": {"title": "x", "theme": "data", "confidence": 0.9},
                    "model": "test", "prompt_version": "v1",
                },
            })
            proposal_dir = self.tmp / "proposed"
            _write_proposed_mapping(proposal_dir, {"data": "A"})  # même que current
            result = simulate_reclassify("p", proposal_dir)
        self.assertEqual(result["n_moving"], 0)
        self.assertEqual(result["n_stable"], 1)

    def test_file_no_prediction_when_no_theme(self):
        """Un fichier sans theme dans vision_cache et nom de fichier non-significatif → no_prediction."""
        _make_profile(
            self.profiles_root, "p",
            target=self.target,
            folders=["A"],
            mapping={"theme1": "A"},
        )
        pdf = self.target / "xyz123.pdf"  # nom non-significatif (pas d'isbn, pas de keywords)
        pdf.write_bytes(b"%PDF-1.4\n" + b"\x00" * 100)
        # Pas d'entrée dans vision_cache (compute_cache_key → None ou pas en cache)
        with mock.patch("lib.vision_cache.compute_cache_key", return_value=None):
            _seed_vision_cache(self.profiles_root, "p", {})
            proposal_dir = self.tmp / "proposed"
            _write_proposed_mapping(proposal_dir, {"theme1": "A"})
            result = simulate_reclassify("p", proposal_dir)
        # Le fichier ne peut pas être classé → no_prediction
        self.assertEqual(result["n_files"], 1)
        self.assertEqual(result["n_no_prediction"], 1)

    def test_csv_has_correct_structure(self):
        """Le CSV produit a les bonnes colonnes et 1 ligne par fichier."""
        _make_profile(
            self.profiles_root, "p",
            target=self.target,
            folders=["A", "B"],
            mapping={"t1": "A"},
        )
        for name in ("f1.pdf", "f2.pdf", "f3.pdf"):
            (self.target / name).write_bytes(b"%PDF-1.4\n" + b"\x00" * 100)
        with mock.patch("lib.vision_cache.compute_cache_key", return_value=None):
            _seed_vision_cache(self.profiles_root, "p", {})
            proposal_dir = self.tmp / "proposed"
            _write_proposed_mapping(proposal_dir, {"t1": "B"})
            result = simulate_reclassify("p", proposal_dir)
        csv_path = Path(result["csv_path"])
        self.assertTrue(csv_path.exists())
        with csv_path.open() as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 3)
        # Colonnes attendues
        expected_cols = {
            "rel_path", "current_folder", "proposed_folder", "changed",
            "source", "top_theme", "confidence", "score",
        }
        self.assertEqual(set(rows[0].keys()), expected_cols)

    def test_summary_json_written_alongside_csv(self):
        """Le simulation-summary.json est écrit dans le même dossier sim/."""
        _make_profile(
            self.profiles_root, "p",
            target=self.target,
            folders=["A"],
            mapping={},
        )
        proposal_dir = self.tmp / "proposed"
        _write_proposed_mapping(proposal_dir, {})
        result = simulate_reclassify("p", proposal_dir)
        summary_path = Path(result["summary_path"])
        self.assertTrue(summary_path.exists())
        data_ = json.loads(summary_path.read_text())
        # Le summary.json contient les mêmes clés que le retour
        self.assertEqual(data_["n_files"], result["n_files"])
        self.assertEqual(data_["n_moving"], result["n_moving"])

    def test_missing_proposed_mapping_raises(self):
        """proposal_dir sans theme_mapping-proposed.yaml → FileNotFoundError."""
        _make_profile(
            self.profiles_root, "p",
            target=self.target,
            folders=["A"],
            mapping={},
        )
        proposal_dir = self.tmp / "empty-proposed"
        proposal_dir.mkdir()  # dossier existe mais pas de yaml dedans
        with self.assertRaises(FileNotFoundError) as ctx:
            simulate_reclassify("p", proposal_dir)
        self.assertIn("theme_mapping-proposed.yaml manquant", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
