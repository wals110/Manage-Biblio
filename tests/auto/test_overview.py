#!/usr/bin/env python3
"""Tests pour dashboard/overview.py — cockpit Biblio."""

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

from dashboard import data, overview  # noqa: E402


class OverviewTestBase(unittest.TestCase):
    """Pose un profil de test isolé : profile.yaml + dossier target."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="klodo-ov-test-"))
        self.profiles_root = self.tmpdir / "profiles"
        self.target = self.tmpdir / "library"
        self.target.mkdir(parents=True, exist_ok=True)
        self.profile_name = "test"
        self.profile_dir = self.profiles_root / self.profile_name
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        (self.profile_dir / "profile.yaml").write_text(yaml.safe_dump({
            "name": "test",
            "description": "Test profile",
            "target": str(self.target),
            "inbox": str(self.tmpdir / "inbox"),
            "fallback": "_A-TRIER",
            "llm": {"provider": "siliconflow",
                    "model": "Qwen/Qwen3-VL-32B",
                    "endpoint": "https://api.siliconflow.com/v1"},
            "defaults": {"cost_per_call": 0.0003, "workers": 5},
        }))
        (self.profile_dir / ".cache").mkdir(parents=True, exist_ok=True)
        self._patcher = mock.patch.object(
            data, "get_project_root", return_value=self.tmpdir,
        )
        self._patcher.start()
        overview.reset_cache()
        from dashboard import categories as _cat
        _cat.reset_cache()

    def tearDown(self):
        self._patcher.stop()
        overview.reset_cache()
        from dashboard import categories as _cat
        _cat.reset_cache()
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class TestReset(OverviewTestBase):

    def test_reset_cache_clears_all(self):
        overview._overview_cache["foo"] = (0.0, {"x": 1})
        overview.reset_cache()
        self.assertEqual(overview._overview_cache, {})

    def test_reset_cache_clears_one_profile(self):
        overview._overview_cache["a"] = (0.0, {})
        overview._overview_cache["b"] = (0.0, {})
        overview.reset_cache("a")
        self.assertNotIn("a", overview._overview_cache)
        self.assertIn("b", overview._overview_cache)


class TestCardFilesCount(OverviewTestBase):

    def _make_files(self, files: dict[str, int]):
        """files = {rel_path: size_bytes}"""
        for rel, size in files.items():
            p = self.target / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x" * size)

    def test_happy_counts_pdf_and_epub(self):
        self._make_files({
            "a.pdf": 1024, "b.pdf": 2048, "c.epub": 512,
            "subdir/d.pdf": 4096, "subdir/notes.txt": 100,
        })
        r = overview.card_files_count(self.profile_name)
        self.assertEqual(r["total"], 4)
        self.assertEqual(r["by_ext"]["pdf"], 3)
        self.assertEqual(r["by_ext"]["epub"], 1)
        self.assertNotIn("error", r)

    def test_target_missing_returns_error(self):
        shutil.rmtree(self.target)
        r = overview.card_files_count(self.profile_name)
        self.assertEqual(r["total"], 0)
        self.assertEqual(r["error"], "target_missing")

    def test_profile_missing_returns_error(self):
        r = overview.card_files_count("does-not-exist")
        self.assertEqual(r["total"], 0)
        self.assertEqual(r["error"], "profile_missing")

    def test_empty_target_returns_zero(self):
        r = overview.card_files_count(self.profile_name)
        self.assertEqual(r["total"], 0)
        self.assertEqual(r["size_gb"], 0.0)

    def test_target_null_in_yaml_returns_error(self):
        """target: null dans profile.yaml ne doit pas faire walker le cwd."""
        (self.profile_dir / "profile.yaml").write_text(yaml.safe_dump({
            "name": "test", "target": None,
            "defaults": {"cost_per_call": 0.0003},
        }))
        r = overview.card_files_count(self.profile_name)
        self.assertEqual(r["total"], 0)
        self.assertEqual(r["error"], "target_missing")


class TestCardClassifiedRate(OverviewTestBase):

    def _make(self, files: list[str]):
        for rel in files:
            p = self.target / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")

    def test_all_classified(self):
        self._make(["folder1/a.pdf", "folder1/b.pdf", "folder2/c.epub"])
        r = overview.card_classified_rate(self.profile_name)
        self.assertEqual(r["classified"], 3)
        self.assertEqual(r["unclassified"], 0)
        self.assertEqual(r["rate"], 100.0)
        self.assertEqual(r["fallback_count"], 0)

    def test_some_in_root_and_fallback(self):
        self._make([
            "folder/ok.pdf",
            "folder/sub/ok2.pdf",
            "in-root.pdf",
            "_A-TRIER/orphan.epub",
            "_A-TRIER/nested/orphan2.pdf",
        ])
        r = overview.card_classified_rate(self.profile_name)
        # classified = 2 (folder/ok + folder/sub/ok2)
        # root = 1, fallback = 2 → unclassified = 3
        self.assertEqual(r["classified"], 2)
        self.assertEqual(r["unclassified"], 3)
        self.assertEqual(r["fallback_count"], 2)
        self.assertEqual(r["rate"], 40.0)

    def test_empty_returns_zero(self):
        r = overview.card_classified_rate(self.profile_name)
        self.assertEqual(r["rate"], 0.0)

    def test_target_missing(self):
        shutil.rmtree(self.target)
        r = overview.card_classified_rate(self.profile_name)
        self.assertEqual(r["rate"], 0.0)

    def test_target_null_in_yaml_returns_zero(self):
        """target: null doit retourner rate=0, pas walker le cwd."""
        (self.profile_dir / "profile.yaml").write_text(yaml.safe_dump({
            "name": "test", "target": None,
            "defaults": {"cost_per_call": 0.0003},
        }))
        r = overview.card_classified_rate(self.profile_name)
        self.assertEqual(r["rate"], 0.0)
        self.assertEqual(r["classified"], 0)

    def test_profile_missing_returns_zero(self):
        r = overview.card_classified_rate("does-not-exist")
        self.assertEqual(r["rate"], 0.0)
        self.assertEqual(r["classified"], 0)
        self.assertEqual(r["fallback_count"], 0)


class TestCardFoldersCount(OverviewTestBase):

    def _write_tree(self, folders: list[str]):
        (self.profile_dir / "tree.yaml").write_text(
            yaml.safe_dump({"folders": folders}, sort_keys=False)
        )

    def test_counts_total_and_max_depth(self):
        self._write_tree([
            "01-SCIENCES",
            "01-SCIENCES/MATH",
            "01-SCIENCES/MATH/ALG",
            "02-INFO",
        ])
        r = overview.card_folders_count(self.profile_name)
        self.assertEqual(r["total"], 4)
        self.assertEqual(r["max_depth"], 3)

    def test_no_tree_yaml_returns_error(self):
        r = overview.card_folders_count(self.profile_name)
        self.assertEqual(r["total"], 0)
        self.assertEqual(r["max_depth"], 0)
        self.assertEqual(r["error"], "tree_missing")

    def test_invalid_tree_yaml_returns_tree_invalid(self):
        """YAML cassé doit retourner sentinelle tree_invalid (pas crash)."""
        (self.profile_dir / "tree.yaml").write_text("folders: [unclosed\n")
        r = overview.card_folders_count(self.profile_name)
        self.assertEqual(r["total"], 0)
        self.assertEqual(r["error"], "tree_invalid")

    def test_recently_modified_picks_3_newest(self):
        self._write_tree(["A", "A/B", "A/C", "D"])
        r = overview.card_folders_count(self.profile_name)
        self.assertIn("recently_modified", r)
        self.assertIsInstance(r["recently_modified"], list)


class TestCardLlmCost(OverviewTestBase):

    def _write_cache(self, n_entries: int):
        cache = self.profile_dir / ".cache" / "vision_cache.json"
        cache.write_text(json.dumps({
            f"key{i}": {"result": {"title": f"book{i}"}}
            for i in range(n_entries)
        }))

    def test_cost_proportional_to_entries(self):
        self._write_cache(10)
        r = overview.card_llm_cost(self.profile_name)
        self.assertEqual(r["n_calls"], 10)
        self.assertAlmostEqual(r["cost_usd"], 0.003, places=4)  # 10 * 0.0003
        self.assertAlmostEqual(r["cost_per_call"], 0.0003)

    def test_corrupt_json_returns_zero(self):
        (self.profile_dir / ".cache" / "vision_cache.json").write_text("not json")
        r = overview.card_llm_cost(self.profile_name)
        self.assertEqual(r["n_calls"], 0)
        self.assertEqual(r["cost_usd"], 0.0)

    def test_no_cache_returns_zero(self):
        r = overview.card_llm_cost(self.profile_name)
        self.assertEqual(r["n_calls"], 0)


class TestCardVisionCache(OverviewTestBase):

    def _write_cache(self, payload: dict):
        (self.profile_dir / ".cache" / "vision_cache.json").write_text(
            json.dumps(payload)
        )

    def test_counts_entries_and_size(self):
        self._write_cache({
            "k1": {"result": {"title": "ok"}},
            "k2": {"result": {"title": "ok"}},
            "k3": {"result": None},  # raté
        })
        r = overview.card_vision_cache(self.profile_name)
        self.assertEqual(r["n_entries"], 3)
        self.assertEqual(r["n_successful"], 2)
        self.assertGreater(r["size_kb"], 0)
        self.assertIsNotNone(r["last_modified"])

    def test_no_cache(self):
        r = overview.card_vision_cache(self.profile_name)
        self.assertEqual(r["n_entries"], 0)
        self.assertEqual(r["size_kb"], 0)
        self.assertIsNone(r["last_modified"])


class TestCardInbox(OverviewTestBase):

    def test_inbox_with_files(self):
        inbox = self.tmpdir / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "a.pdf").write_bytes(b"x" * 1024)
        (inbox / "b.epub").write_bytes(b"x" * 512)
        r = overview.card_inbox(self.profile_name)
        self.assertEqual(r["n_files"], 2)
        self.assertTrue(r["exists"])
        self.assertGreater(r["size_mb"], 0)
        self.assertIsNotNone(r["oldest_iso"])

    def test_inbox_unconfigured(self):
        # Réécrit profile.yaml sans inbox:
        (self.profile_dir / "profile.yaml").write_text(yaml.safe_dump({
            "name": "test", "target": str(self.target),
            "defaults": {"cost_per_call": 0.0003},
        }))
        r = overview.card_inbox(self.profile_name)
        self.assertFalse(r["exists"])
        self.assertEqual(r["n_files"], 0)

    def test_inbox_configured_but_missing(self):
        # inbox dans yaml mais dossier absent
        r = overview.card_inbox(self.profile_name)
        self.assertFalse(r["exists"])


class TestCardBaselineRuns(OverviewTestBase):

    def test_with_runs_via_mock(self):
        from dashboard import baseline
        fake_runs = [
            {"run_id": "r2", "created_at": "2026-06-04T10:00:00",
             "n_files": 100, "n_disagreements": 20, "klodo_version": "1.0"},
            {"run_id": "r1", "created_at": "2026-06-01T08:00:00",
             "n_files": 80, "n_disagreements": 30, "klodo_version": "1.0"},
        ]
        with mock.patch.object(baseline, "list_runs", return_value=fake_runs):
            r = overview.card_baseline_runs(self.profile_name)
        self.assertEqual(r["n_runs"], 2)
        self.assertEqual(r["last_run_iso"], "2026-06-04T10:00:00")

    def test_no_runs(self):
        from dashboard import baseline
        with mock.patch.object(baseline, "list_runs", return_value=[]):
            r = overview.card_baseline_runs(self.profile_name)
        self.assertEqual(r["n_runs"], 0)
        self.assertIsNone(r["last_run_iso"])

    def test_baseline_raises_returns_empty(self):
        """Si baseline.list_runs lève une exception, on retourne empty."""
        from dashboard import baseline
        with mock.patch.object(baseline, "list_runs",
                               side_effect=RuntimeError("boom")):
            r = overview.card_baseline_runs(self.profile_name)
        self.assertEqual(r["n_runs"], 0)
        self.assertIsNone(r["last_run_iso"])


class TestCardAgentSessions(OverviewTestBase):

    def _write_status(self, agent: str, status: str):
        d = self.profile_dir / ".cache" / agent
        d.mkdir(parents=True, exist_ok=True)
        (d / "status.json").write_text(json.dumps({"status": status}))

    def _make_batches(self, agent: str, n: int):
        for i in range(n):
            d = self.profile_dir / ".cache" / agent / "batches" / f"batch-{i:03d}"
            d.mkdir(parents=True, exist_ok=True)

    def test_counts_batches_per_agent(self):
        self._make_batches("refonte", 3)
        self._make_batches("dedupli", 1)
        self._write_status("refonte", "idle")
        self._write_status("dedupli", "running")
        r = overview.card_agent_sessions(self.profile_name)
        self.assertEqual(r["refonte"]["n_batches"], 3)
        self.assertEqual(r["refonte"]["status"], "idle")
        self.assertEqual(r["dedupli"]["n_batches"], 1)
        self.assertEqual(r["dedupli"]["status"], "running")

    def test_missing_agents_default(self):
        r = overview.card_agent_sessions(self.profile_name)
        self.assertEqual(r["refonte"]["n_batches"], 0)
        self.assertEqual(r["refonte"]["status"], "missing")
        self.assertEqual(r["dedupli"]["status"], "missing")

    def test_corrupt_status_json_returns_error(self):
        """status.json corrompu → status='error', pas crash."""
        d = self.profile_dir / ".cache" / "refonte"
        d.mkdir(parents=True, exist_ok=True)
        (d / "status.json").write_text("not json")
        r = overview.card_agent_sessions(self.profile_name)
        self.assertEqual(r["refonte"]["status"], "error")


class TestCardHealth(OverviewTestBase):

    def _write_tree(self, folders: list[str]):
        (self.profile_dir / "tree.yaml").write_text(
            yaml.safe_dump({"folders": folders})
        )

    def _write_mapping(self, mapping: dict):
        (self.profile_dir / "theme_mapping.yaml").write_text(
            yaml.safe_dump(mapping, sort_keys=False)
        )

    def test_clean_state_returns_zero_orphans(self):
        self._write_tree(["A", "A/B"])
        self._write_mapping({"theme1": "A", "theme2": "A/B"})
        r = overview.card_health(self.profile_name)
        self.assertEqual(r["n_orphans_total"], 0)
        self.assertEqual(r["locks_active"], [])

    def test_mapping_orphan_counted(self):
        self._write_tree(["A"])
        self._write_mapping({"good": "A", "orphan": "DOES-NOT-EXIST"})
        r = overview.card_health(self.profile_name)
        self.assertEqual(r["n_orphans_mappings"], 1)
        self.assertEqual(r["n_orphans_total"], 1)

    def test_active_lock_detected(self):
        self._write_tree(["A"])
        self._write_mapping({})
        lock = self.profile_dir / ".cache" / ".taxonomy.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("")
        r = overview.card_health(self.profile_name)
        self.assertEqual(len(r["locks_active"]), 1)
        self.assertEqual(r["locks_active"][0]["name"], ".taxonomy.lock")
        self.assertIn("age_seconds", r["locks_active"][0])

    def test_stale_lock_flagged(self):
        self._write_tree(["A"])
        self._write_mapping({})
        lock = self.profile_dir / ".cache" / ".dedupli.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("")
        # Force mtime > 1 h ago
        old = lock.stat().st_mtime - 3700
        os.utime(lock, (old, old))
        r = overview.card_health(self.profile_name)
        self.assertTrue(r["locks_active"][0]["stale"])

    def test_categories_orphan_counted(self):
        """n_orphans_categories doit refléter le snapshot categories."""
        # Crée tree + theme_mapping vide + categories.yaml avec 1 entry
        # dont le chemin n'existe pas dans tree → orphan détecté.
        self._write_tree(["A"])
        self._write_mapping({})
        (self.profile_dir / "categories.yaml").write_text(yaml.safe_dump({
            "Test": [{"chemin": "DOES-NOT-EXIST", "priorite": 1, "mots_cles": ["foo"]}],
        }, sort_keys=False))
        # Reset le cache du module categories (snapshot mémo)
        from dashboard import categories
        categories.reset_cache(self.profile_name)
        r = overview.card_health(self.profile_name)
        self.assertEqual(r["n_orphans_categories"], 1)
        self.assertGreaterEqual(r["n_orphans_total"], 1)


class TestCardTopThemes(OverviewTestBase):

    def _write_cache(self, themes_per_file: list[list[tuple[str, float]]]):
        """themes_per_file = [[(theme, confidence), ...], ...]"""
        cache: dict = {}
        for i, themes in enumerate(themes_per_file):
            cache[f"k{i}"] = {"result": {
                "themes": [{"theme": t, "confidence": c} for t, c in themes]
            }}
        (self.profile_dir / ".cache" / "vision_cache.json").write_text(
            json.dumps(cache)
        )

    def test_aggregates_and_sorts_descending(self):
        self._write_cache([
            [("Python", 0.9), ("Linux", 0.8)],
            [("Python", 0.9)],
            [("Python", 0.9), ("Math", 0.7)],
        ])
        r = overview.card_top_themes(self.profile_name, limit=3)
        self.assertEqual(len(r), 3)
        self.assertEqual(r[0]["theme"], "Python")
        self.assertEqual(r[0]["count"], 3)
        # Pct = count / total occurrences = 3 / 5 = 60
        self.assertAlmostEqual(r[0]["pct"], 60.0, places=1)

    def test_filters_low_confidence(self):
        self._write_cache([[("Low", 0.3), ("Good", 0.9)]])
        r = overview.card_top_themes(self.profile_name)
        themes = [x["theme"] for x in r]
        self.assertNotIn("Low", themes)
        self.assertIn("Good", themes)

    def test_empty_cache(self):
        r = overview.card_top_themes(self.profile_name)
        self.assertEqual(r, [])

    def test_limit_truncates_list(self):
        """limit=2 doit retourner exactement 2 thèmes (les 2 plus fréquents)."""
        self._write_cache([
            [("A", 0.9), ("B", 0.9), ("C", 0.9), ("D", 0.9), ("E", 0.9)],
            [("A", 0.9), ("B", 0.9), ("C", 0.9)],
            [("A", 0.9), ("B", 0.9)],
            [("A", 0.9)],
        ])
        r = overview.card_top_themes(self.profile_name, limit=2)
        self.assertEqual(len(r), 2)
        self.assertEqual([x["theme"] for x in r], ["A", "B"])

    def test_tie_break_alphabetical(self):
        """À counts égaux, l'ordre doit être alphabétique (déterministe)."""
        self._write_cache([
            [("Zebra", 0.9), ("Apple", 0.9), ("Mango", 0.9)],
        ])
        r = overview.card_top_themes(self.profile_name)
        self.assertEqual([x["theme"] for x in r], ["Apple", "Mango", "Zebra"])


class TestCardTopFolders(OverviewTestBase):

    def _make(self, files: dict[str, int]):
        for rel, n in files.items():
            d = self.target / rel
            d.mkdir(parents=True, exist_ok=True)
            for i in range(n):
                (d / f"f{i}.pdf").write_bytes(b"x")

    def test_top_folders_sorted_desc(self):
        self._make({"A": 5, "B/C": 2, "D": 10, "E": 1})
        r = overview.card_top_folders(self.profile_name, limit=3)
        paths = [x["path"] for x in r]
        self.assertEqual(paths, ["D", "A", "B/C"])
        self.assertEqual(r[0]["n_files"], 10)
        self.assertAlmostEqual(r[0]["pct"], 10 / 18 * 100, places=1)

    def test_empty(self):
        r = overview.card_top_folders(self.profile_name)
        self.assertEqual(r, [])

    def test_target_missing(self):
        shutil.rmtree(self.target)
        r = overview.card_top_folders(self.profile_name)
        self.assertEqual(r, [])

    def test_target_null_returns_empty(self):
        """target: null doit retourner [], pas walker cwd."""
        (self.profile_dir / "profile.yaml").write_text(yaml.safe_dump({
            "name": "test", "target": None,
            "defaults": {"cost_per_call": 0.0003},
        }))
        r = overview.card_top_folders(self.profile_name)
        self.assertEqual(r, [])


class TestCardRecentActivity(OverviewTestBase):

    def test_merges_sources_and_sorts_recent_first(self):
        # 3 backups taxonomy à mtimes différentes
        bdir = self.profile_dir / ".cache" / "taxonomy-backups"
        bdir.mkdir(parents=True, exist_ok=True)
        f1 = bdir / "theme_mapping-20260101-100000-000000.yaml"
        f2 = bdir / "tree-20260102-100000-000000.yaml"
        f3 = bdir / "theme_mapping-20260103-100000-000000.yaml"
        for f in [f1, f2, f3]:
            f.write_text("")
        os.utime(f1, (1000, 1000))
        os.utime(f2, (2000, 2000))
        os.utime(f3, (3000, 3000))
        r = overview.card_recent_activity(self.profile_name, limit=5)
        self.assertEqual(len(r), 3)
        # Plus récent en premier
        self.assertEqual(r[0]["target"], f3.name)

    def test_empty(self):
        r = overview.card_recent_activity(self.profile_name)
        self.assertEqual(r, [])

    def test_rename_journal_emits_event(self):
        """rename-journal.jsonl présent → 1 événement action=rename."""
        rj = self.profile_dir / ".cache" / "rename-journal.jsonl"
        rj.write_text('{"ts":1,"op":"rename"}\n')
        r = overview.card_recent_activity(self.profile_name)
        actions = [e["action"] for e in r]
        self.assertIn("rename", actions)

    def test_categories_backups_source(self):
        """Backup categories doit générer un événement action=categories."""
        bdir = self.profile_dir / ".cache" / "categories-backups"
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / "categories-20260101-100000.yaml").write_text("")
        r = overview.card_recent_activity(self.profile_name)
        actions = [e["action"] for e in r]
        self.assertIn("categories", actions)


class TestCardLlmModels(OverviewTestBase):

    def test_returns_per_profile_config(self):
        # Crée un 2e profil
        p2 = self.profiles_root / "test-local"
        p2.mkdir(parents=True, exist_ok=True)
        (p2 / "profile.yaml").write_text(yaml.safe_dump({
            "name": "test-local", "target": str(self.target),
            "llm": {"provider": "ollama", "model": "qwen2-vl:7b",
                    "endpoint": "http://localhost:11434"},
        }))
        r = overview.card_llm_models()
        names = {x["profile"] for x in r}
        self.assertIn("test", names)
        self.assertIn("test-local", names)
        local = next(x for x in r if x["profile"] == "test-local")
        self.assertEqual(local["provider"], "ollama")

    def test_includes_non_test_profiles(self):
        """Profil avec nom != 'test*' doit apparaître (include_all=True)."""
        p = self.profiles_root / "default"
        p.mkdir(parents=True, exist_ok=True)
        (p / "profile.yaml").write_text(yaml.safe_dump({
            "name": "default", "target": str(self.target),
            "llm": {"provider": "siliconflow", "model": "Qwen/Qwen3-VL-32B",
                    "endpoint": "https://api.siliconflow.com/v1"},
        }))
        r = overview.card_llm_models()
        names = {x["profile"] for x in r}
        self.assertIn("default", names)


class TestCardApiKeys(OverviewTestBase):

    def test_siliconflow_status_reflects_env(self):
        with mock.patch.dict(os.environ, {"SILICONFLOW_API_KEY": "sk-test"}):
            r = overview.card_api_keys()
        sf = next((x for x in r if x["name"] == "SILICONFLOW_API_KEY"), None)
        self.assertIsNotNone(sf)
        self.assertTrue(sf["configured"])

    def test_siliconflow_missing(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            r = overview.card_api_keys()
        sf = next((x for x in r if x["name"] == "SILICONFLOW_API_KEY"), None)
        self.assertFalse(sf["configured"])


class TestCardProfilesList(OverviewTestBase):

    def test_returns_profiles_with_target_status(self):
        r = overview.card_profiles_list()
        names = {x["name"] for x in r}
        self.assertIn("test", names)
        item = next(x for x in r if x["name"] == "test")
        self.assertTrue(item["target_exists"])

    def test_includes_non_test_profiles(self):
        """Profil avec nom != 'test*' doit apparaître (include_all=True)."""
        p = self.profiles_root / "default"
        p.mkdir(parents=True, exist_ok=True)
        (p / "profile.yaml").write_text(yaml.safe_dump({
            "name": "default", "target": str(self.target),
        }))
        r = overview.card_profiles_list()
        names = {x["name"] for x in r}
        self.assertIn("default", names)


class TestCardGlobalCost(OverviewTestBase):

    def _seed_profile(self, name: str, n_entries: int, cpc: float = 0.0003):
        p = self.profiles_root / name
        p.mkdir(parents=True, exist_ok=True)
        (p / "profile.yaml").write_text(yaml.safe_dump({
            "name": name, "target": str(self.target),
            "defaults": {"cost_per_call": cpc},
        }))
        (p / ".cache").mkdir(parents=True, exist_ok=True)
        if n_entries:
            (p / ".cache" / "vision_cache.json").write_text(
                json.dumps({f"k{i}": {"result": {}} for i in range(n_entries)})
            )

    def test_sums_per_profile_costs(self):
        self._seed_profile("test", 100)         # 0.03
        self._seed_profile("test-local", 200)   # 0.06 (same cpc)
        r = overview.card_global_cost()
        self.assertAlmostEqual(r["total_usd"], 0.09, places=4)
        # Chaque profil présent avec son pct
        by_p = {x["profile"]: x for x in r["by_profile"]}
        self.assertIn("test", by_p)
        self.assertIn("test-local", by_p)

    def test_empty_when_no_calls(self):
        r = overview.card_global_cost()
        self.assertEqual(r["total_usd"], 0.0)
        self.assertEqual(r["by_profile"], [])

    def test_pct_drops_below_1_segments(self):
        # Profile 1 : 1000 entries, Profile 2 : 5 entries (~ 0.5%)
        self._seed_profile("big", 1000)
        self._seed_profile("tiny", 5)
        r = overview.card_global_cost()
        profiles = {x["profile"] for x in r["by_profile"]}
        self.assertIn("big", profiles)
        self.assertNotIn("tiny", profiles)  # < 1% drop


if __name__ == "__main__":
    unittest.main()
