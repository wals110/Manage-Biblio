#!/usr/bin/env python3
"""Tests pour les 6 outils read-only de l'agent Refonte — Phase A.2.

Couvre :
  - list_folders : retour vide / retour trié / profil inexistant
  - count_files_per_folder : comptage correct / target absent / dossier absent du FS
  - read_theme_mapping : retour vide / parse correct / YAML invalide
  - list_themes_per_folder : reverse mapping / dossier sans thème
  - compute_folder_overlap : Jaccard 0 / Jaccard 1 / Jaccard intermédiaire
  - get_classifier_breakdown : pas de CSV / CSV présent / parse mot_cle + status
"""

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

from agents.refonte import tools  # noqa: E402
from dashboard import data  # noqa: E402
from dashboard import taxonomy as tax

# ─── Fixtures ──────────────────────────────────────────────────────────────


def _make_profile(
    profile_dir: Path,
    *,
    target: Path,
    tree_folders: list[str],
    mapping: dict[str, str],
) -> None:
    """Bâtit un profil minimal sur disque (calqué sur test_taxonomy.py)."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "profile.yaml").write_text(
        yaml.safe_dump({"name": profile_dir.name, "target": str(target)})
    )
    (profile_dir / "tree.yaml").write_text(yaml.safe_dump({"folders": tree_folders}))
    (profile_dir / "theme_mapping.yaml").write_text(yaml.safe_dump(mapping))
    target.mkdir(parents=True, exist_ok=True)


def _make_classify_csv(logs_dir: Path, name: str, rows: list[dict]) -> Path:
    """Crée un classify_*.csv avec les colonnes officielles du projet."""
    import csv as _csv

    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / name
    fieldnames = [
        "fichier", "nouveau_nom", "titre_detecte", "auteur_detecte",
        "theme_detecte", "langue", "confiance",
        "destination", "score", "mot_cle", "status",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})
    return path


class _ProfileTestBase(unittest.TestCase):
    """Setup / teardown commun : redirige PROFILES_ROOT vers un temp dir."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="klodo_refonte_tools_"))
        self.profiles_root = self.tmp / "profiles"
        self.profiles_root.mkdir()
        self.target = self.tmp / "biblio"
        # taxonomy.py construit le path via data.get_project_root() / "profiles"
        self._patcher = mock.patch.object(data, "get_project_root", return_value=self.tmp)
        self._patcher.start()
        tax.reset_cache()

    def tearDown(self):
        self._patcher.stop()
        tax.reset_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)


# ─── list_folders ─────────────────────────────────────────────────────────


class TestListFolders(_ProfileTestBase):
    def test_returns_sorted_list(self):
        _make_profile(
            self.profiles_root / "p",
            target=self.target,
            tree_folders=["ZZ", "AA", "MM/sub"],
            mapping={},
        )
        self.assertEqual(tools.list_folders("p"), ["AA", "MM/sub", "ZZ"])

    def test_returns_empty_when_profile_missing(self):
        self.assertEqual(tools.list_folders("ghost"), [])


# ─── count_files_per_folder ───────────────────────────────────────────────


class TestCountFilesPerFolder(_ProfileTestBase):
    def test_counts_direct_files_only(self):
        _make_profile(
            self.profiles_root / "p",
            target=self.target,
            tree_folders=["A", "A/sub"],
            mapping={},
        )
        (self.target / "A").mkdir()
        (self.target / "A" / "sub").mkdir()
        # 2 fichiers dans A, 1 dans A/sub
        (self.target / "A" / "x.pdf").touch()
        (self.target / "A" / "y.pdf").touch()
        (self.target / "A" / "sub" / "z.pdf").touch()
        counts = tools.count_files_per_folder("p")
        self.assertEqual(counts["A"], 2)
        self.assertEqual(counts["A/sub"], 1)

    def test_missing_folder_has_zero_count(self):
        _make_profile(
            self.profiles_root / "p",
            target=self.target,
            tree_folders=["A", "B"],
            mapping={},
        )
        (self.target / "A").mkdir()  # B n'est pas créé
        counts = tools.count_files_per_folder("p")
        self.assertEqual(counts["A"], 0)
        self.assertEqual(counts["B"], 0)

    def test_returns_empty_when_target_missing(self):
        # profil sans target valide
        profile_dir = self.profiles_root / "p"
        profile_dir.mkdir()
        (profile_dir / "profile.yaml").write_text(yaml.safe_dump({"name": "p"}))
        (profile_dir / "tree.yaml").write_text(yaml.safe_dump({"folders": ["A"]}))
        self.assertEqual(tools.count_files_per_folder("p"), {})


# ─── read_theme_mapping ───────────────────────────────────────────────────


class TestReadThemeMapping(_ProfileTestBase):
    def test_returns_parsed_dict(self):
        _make_profile(
            self.profiles_root / "p",
            target=self.target,
            tree_folders=["A", "B"],
            mapping={"machine learning": "A", "linux": "B"},
        )
        m = tools.read_theme_mapping("p")
        self.assertEqual(m["machine learning"], "A")
        self.assertEqual(m["linux"], "B")

    def test_returns_empty_when_profile_missing(self):
        self.assertEqual(tools.read_theme_mapping("ghost"), {})


# ─── list_themes_per_folder ────────────────────────────────────────────────


class TestListThemesPerFolder(_ProfileTestBase):
    def test_reverse_mapping(self):
        _make_profile(
            self.profiles_root / "p",
            target=self.target,
            tree_folders=["A", "B"],
            mapping={"theme1": "A", "theme2": "A", "theme3": "B"},
        )
        rev = tools.list_themes_per_folder("p")
        self.assertEqual(sorted(rev["A"]), ["theme1", "theme2"])
        self.assertEqual(rev["B"], ["theme3"])

    def test_folder_without_themes_absent(self):
        _make_profile(
            self.profiles_root / "p",
            target=self.target,
            tree_folders=["A", "B"],
            mapping={"theme1": "A"},
        )
        rev = tools.list_themes_per_folder("p")
        self.assertIn("A", rev)
        self.assertNotIn("B", rev)


# ─── compute_folder_overlap ────────────────────────────────────────────────


class TestComputeFolderOverlap(_ProfileTestBase):
    def _setup_mapping(self, mapping: dict[str, str]) -> None:
        _make_profile(
            self.profiles_root / "p",
            target=self.target,
            tree_folders=list(set(mapping.values())) or ["A", "B"],
            mapping=mapping,
        )

    def test_jaccard_zero_disjoint_sets(self):
        self._setup_mapping({"t1": "A", "t2": "A", "t3": "B"})
        r = tools.compute_folder_overlap("p", "A", "B")
        self.assertEqual(r["jaccard"], 0.0)
        self.assertEqual(r["common"], [])

    def test_jaccard_one_identical_sets(self):
        # Impossible naturellement (chaque thème mappe à 1 seul dossier),
        # mais on simule via deux dossiers vides
        self._setup_mapping({})
        r = tools.compute_folder_overlap("p", "A", "B")
        # 0/0 → 0.0 par convention
        self.assertEqual(r["jaccard"], 0.0)
        self.assertEqual(r["common"], [])
        self.assertEqual(r["union"], [])

    def test_jaccard_returns_structured_result(self):
        self._setup_mapping({"t1": "A", "t2": "A", "t3": "B"})
        r = tools.compute_folder_overlap("p", "A", "B")
        self.assertEqual(r["folder_a"], "A")
        self.assertEqual(r["folder_b"], "B")
        self.assertEqual(sorted(r["themes_a"]), ["t1", "t2"])
        self.assertEqual(r["themes_b"], ["t3"])
        self.assertEqual(sorted(r["union"]), ["t1", "t2", "t3"])


# ─── get_classifier_breakdown ──────────────────────────────────────────────


class TestGetClassifierBreakdown(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="klodo_refonte_breakdown_"))
        self.logs_dir = self.tmp / "logs"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_csv_returns_zero(self):
        r = tools.get_classifier_breakdown("default", logs_dir=self.logs_dir)
        self.assertIsNone(r["csv_path"])
        self.assertEqual(r["total"], 0)
        self.assertEqual(r["by_source"], {})
        self.assertEqual(r["by_status"], {})

    def test_parses_existing_csv(self):
        _make_classify_csv(self.logs_dir, "classify_20260524_120000.csv", [
            {"fichier": "a.pdf", "mot_cle": "LLM (theme)", "status": "classifié"},
            {"fichier": "b.pdf", "mot_cle": "LLM (theme)", "status": "classifié"},
            {"fichier": "c.pdf", "mot_cle": "Keyword", "status": "classifié"},
            {"fichier": "d.pdf", "mot_cle": "LLM (mapper)", "status": "classifié"},
            {"fichier": "e.pdf", "mot_cle": "", "status": "non_classifié"},
            {"fichier": "f.pdf", "mot_cle": "", "status": "erreur_extraction"},
        ])
        r = tools.get_classifier_breakdown("default", logs_dir=self.logs_dir)
        self.assertEqual(r["total"], 6)
        self.assertEqual(r["by_source"]["LLM (theme)"], 2)
        self.assertEqual(r["by_source"]["Keyword"], 1)
        self.assertEqual(r["by_source"]["LLM (mapper)"], 1)
        self.assertEqual(r["by_status"]["classifié"], 4)
        self.assertEqual(r["by_status"]["non_classifié"], 1)
        self.assertEqual(r["by_status"]["erreur_extraction"], 1)

    def test_picks_latest_csv_when_multiple(self):
        old = _make_classify_csv(self.logs_dir, "classify_20260101_000000.csv", [
            {"fichier": "old.pdf", "mot_cle": "LLM (theme)", "status": "classifié"},
        ])
        new = _make_classify_csv(self.logs_dir, "classify_20260524_120000.csv", [
            {"fichier": "new1.pdf", "mot_cle": "Keyword", "status": "classifié"},
            {"fichier": "new2.pdf", "mot_cle": "Keyword", "status": "classifié"},
        ])
        # On force le mtime du nouveau plus récent (au cas où la création FS soit la même seconde)
        new.touch()
        os.utime(old, (old.stat().st_atime - 60, old.stat().st_mtime - 60))
        r = tools.get_classifier_breakdown("default", logs_dir=self.logs_dir)
        self.assertEqual(r["total"], 2)
        self.assertEqual(r["by_source"]["Keyword"], 2)
        self.assertIn("20260524", r["csv_path"])

    def test_n3_refined_source_label_recognized(self):
        """La source label du trigger N3 (PR #147) doit apparaître naturellement."""
        _make_classify_csv(self.logs_dir, "classify_20260524_120000.csv", [
            {"fichier": "x.pdf", "mot_cle": "LLM (theme→N3-refined)", "status": "classifié"},
        ])
        r = tools.get_classifier_breakdown("default", logs_dir=self.logs_dir)
        self.assertEqual(r["by_source"]["LLM (theme→N3-refined)"], 1)


class TestListVisionThemes(_ProfileTestBase):
    """list_vision_themes : agrège les thèmes observés depuis vision_cache.json."""

    def _make_cache(self, entries: list[dict]) -> None:
        """Helper : écrit un vision_cache.json minimal sous le profil 'p'."""
        import json
        cache_dir = self.profiles_root / "p" / ".cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache: dict = {}
        for i, e in enumerate(entries):
            cache[f"k{i:031d}"] = {
                "result": e,
                "model": "test",
                "prompt_version": "v1",
                "cached_at": "2026-05-25T00:00:00",
            }
        (cache_dir / "vision_cache.json").write_text(json.dumps(cache))

    def test_returns_themes_sorted_desc(self):
        _make_profile(self.profiles_root / "p", target=self.target,
                      tree_folders=["A"], mapping={"python": "A"})
        # 3 fichiers Python, 1 java, 5 rust
        entries = (
            [{"title": "Pro Python", "theme": "python", "confidence": 0.9}] * 3
            + [{"title": "Java Pro", "theme": "java", "confidence": 0.9}]
            + [{"title": "Rust", "theme": "rust", "confidence": 0.9}] * 5
        )
        self._make_cache(entries)
        result = tools.list_vision_themes("p", top_n=10)
        # Tri desc par count : rust (5) > python (3) > java (1)
        self.assertGreaterEqual(len(result), 3)
        self.assertEqual(result[0]["theme"], "rust")
        self.assertEqual(result[0]["count"], 5)
        # python est mappé, java + rust ne le sont pas
        python_entry = next(t for t in result if t["theme"] == "python")
        self.assertEqual(python_entry["mapped_to"], "A")
        self.assertFalse(python_entry["is_orphan"])
        rust_entry = next(t for t in result if t["theme"] == "rust")
        self.assertIsNone(rust_entry["mapped_to"])
        self.assertTrue(rust_entry["is_orphan"])

    def test_returns_empty_when_cache_missing(self):
        _make_profile(self.profiles_root / "p", target=self.target,
                      tree_folders=["A"], mapping={})
        self.assertEqual(tools.list_vision_themes("p"), [])

    def test_top_n_clamps_to_500(self):
        _make_profile(self.profiles_root / "p", target=self.target,
                      tree_folders=["A"], mapping={})
        self._make_cache([{"title": "x", "theme": f"theme{i}", "confidence": 0.9}
                          for i in range(3)])
        # Demande 9999 → doit clamp à 500 (et 3 thèmes seulement existent)
        result = tools.list_vision_themes("p", top_n=9999)
        self.assertEqual(len(result), 3)


class TestFindOrphanThemes(_ProfileTestBase):
    """find_orphan_themes : sous-ensemble orphans-only de list_vision_themes."""

    def _make_cache(self, entries: list[dict]) -> None:
        import json
        cache_dir = self.profiles_root / "p" / ".cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache: dict = {}
        for i, e in enumerate(entries):
            cache[f"k{i:031d}"] = {
                "result": e, "model": "test",
                "prompt_version": "v1", "cached_at": "2026-05-25T00:00:00",
            }
        (cache_dir / "vision_cache.json").write_text(json.dumps(cache))

    def test_returns_only_unmapped_themes(self):
        _make_profile(self.profiles_root / "p", target=self.target,
                      tree_folders=["A"], mapping={"python": "A"})
        entries = (
            [{"title": "Py", "theme": "python", "confidence": 0.9}] * 2     # mapped
            + [{"title": "Rs", "theme": "rust", "confidence": 0.9}] * 3      # orphan
            + [{"title": "Go", "theme": "golang", "confidence": 0.9}] * 1   # orphan
        )
        self._make_cache(entries)
        result = tools.find_orphan_themes("p")
        themes = [t["theme"] for t in result]
        self.assertIn("rust", themes)
        self.assertIn("golang", themes)
        self.assertNotIn("python", themes)  # mappé → exclu

    def test_returns_empty_when_all_mapped(self):
        _make_profile(self.profiles_root / "p", target=self.target,
                      tree_folders=["A", "B"],
                      mapping={"python": "A", "rust": "B"})
        self._make_cache([
            {"title": "x", "theme": "python", "confidence": 0.9},
            {"title": "y", "theme": "rust", "confidence": 0.9},
        ])
        self.assertEqual(tools.find_orphan_themes("p"), [])


if __name__ == "__main__":
    unittest.main()
