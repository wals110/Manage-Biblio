#!/usr/bin/env python3
"""Tests de l'agent Onboarding (Vision/LLM mockés, zéro SSD réel)."""

import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)


class TestDraftProfile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="klodo-onb-")
        self.root = Path(self.tmp)
        self.target = self.root / "RAW"
        self.target.mkdir(parents=True)
        self.patch = mock.patch("lib.profile.get_project_root", return_value=self.root)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_create_draft_profile_sets_flag(self):
        from lib import profile as prof
        p = prof.create_draft_profile("perso-2026", str(self.target))
        self.assertTrue(p.onboarding_draft)
        cfg = yaml.safe_load((self.root / "profiles" / "perso-2026" / "profile.yaml").read_text())
        self.assertTrue(cfg["onboarding_draft"])
        self.assertEqual(cfg["target"], str(self.target))

    def test_normal_profile_has_flag_false(self):
        from lib import profile as prof
        prof.init_profile("normal", str(self.target))
        p = prof.Profile("normal")
        self.assertFalse(p.onboarding_draft)

    def test_set_onboarding_draft_toggles(self):
        from lib import profile as prof
        prof.create_draft_profile("perso-2026", str(self.target))
        prof.set_onboarding_draft("perso-2026", False)
        cfg = yaml.safe_load((self.root / "profiles" / "perso-2026" / "profile.yaml").read_text())
        self.assertFalse(cfg["onboarding_draft"])

    def test_draft_profile_uses_enabled_vision_model(self):
        # Régression : init_profile écrivait Qwen/Qwen2.5-VL-7B-Instruct, désactivé
        # chez SiliconFlow (HTTP 403 "Model disabled") → Vision en échec à l'onboarding.
        from lib import profile as prof
        from lib.constants import DEFAULT_VISION_MODEL
        prof.create_draft_profile("m", str(self.target))
        cfg = yaml.safe_load((self.root / "profiles" / "m" / "profile.yaml").read_text())
        self.assertEqual(cfg["llm"]["model"], DEFAULT_VISION_MODEL)
        self.assertNotEqual(cfg["llm"]["model"], "Qwen/Qwen2.5-VL-7B-Instruct")

    def test_set_onboarding_options_persists(self):
        from lib import profile as prof
        prof.create_draft_profile("opt", str(self.target))
        prof.set_onboarding_options("opt", {"max_depth": 3, "numbered_sections": False})
        cfg = yaml.safe_load((self.root / "profiles" / "opt" / "profile.yaml").read_text())
        self.assertEqual(cfg["onboarding_options"]["max_depth"], 3)
        self.assertFalse(cfg["onboarding_options"]["numbered_sections"])
        self.assertTrue(cfg["onboarding_draft"])   # préservé (round-trip)


class TestScanEstimate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="klodo-scan-")
        self.d = Path(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _f(self, rel):
        p = self.d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"%PDF-1.4 x")

    def test_scan_flat(self):
        from agents.onboarding import scan
        for n in ("a.pdf", "b.pdf", "c.epub", "notes.txt"):
            self._f(n)
        r = scan.scan_directory(str(self.d))
        self.assertEqual(r["n_files"], 3)            # pdf+epub, pas le .txt
        self.assertEqual(r["by_format"]["pdf"], 2)
        self.assertEqual(r["by_format"]["epub"], 1)
        self.assertFalse(r["has_subfolders"])        # plat

    def test_scan_preorg(self):
        from agents.onboarding import scan
        for n in ("Prog/a.pdf", "Prog/b.pdf", "Sci/c.pdf"):
            self._f(n)
        r = scan.scan_directory(str(self.d))
        self.assertTrue(r["has_subfolders"])
        self.assertEqual(set(r["top_folders"]), {"Prog", "Sci"})
        self.assertEqual(r["n_files"], 3)
        self.assertEqual(r["by_format"]["pdf"], 3)

    def test_estimate_cost(self):
        from agents.onboarding import scan
        e = scan.estimate_cost(n_files=1000, cost_per_call=0.00034, n_pages=2)
        self.assertEqual(e["n_calls"], 1000)
        self.assertAlmostEqual(e["usd"], 0.34, places=2)
        self.assertIn("eta_min", e)
        self.assertEqual(e["n_pages"], 2)


class TestRunVision(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="klodo-vis-")
        self.root = Path(self.tmp)
        self.target = self.root / "RAW"
        for n in ("a.pdf", "b.pdf"):
            p = self.target / n
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"%PDF-1.4 " + n.encode())
        self.prof = self.root / "profiles" / "perso"
        self.prof.mkdir(parents=True)
        (self.prof / "profile.yaml").write_text(yaml.safe_dump({
            "target": str(self.target), "llm": {"model": "M",
            "endpoint": "https://e"}, "defaults": {"workers": 1, "pages": 2}}),
            encoding="utf-8")
        self.patch = mock.patch("lib.profile.get_project_root", return_value=self.root)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_run_vision_populates_cache_and_progress(self):
        from agents.onboarding import proposition
        seen = []

        def fake(path, **kw):
            return {"title": "T", "theme": "Deep Learning",
                    "themes": [{"theme": "Deep Learning", "confidence": 0.9}], "confidence": 0.9}

        def on_prog(d, t):
            seen.append((d, t))

        with mock.patch("agents.onboarding.proposition.analyze_cover", side_effect=fake):
            r = proposition.run_vision("perso", on_progress=on_prog)
        self.assertEqual(r["n_total"], 2)
        self.assertEqual(r["n_analyzed"], 2)
        cache = json.loads((self.prof / ".cache" / "vision_cache.json").read_text())
        self.assertEqual(len(cache), 2)
        self.assertEqual(seen[-1], (2, 2))            # progression finale

    def test_run_vision_resumes_from_cache(self):
        import lib.vision_cache as vc
        from agents.onboarding import proposition
        # pré-remplir le cache pour a.pdf → doit être skippé
        cache = {}
        key = vc.compute_cache_key(str(self.target / "a.pdf"), model="M", n_pages=2)
        vc.store(cache, key, {"theme": "X", "themes": [], "confidence": 0.5}, "M")
        (self.prof / ".cache").mkdir(exist_ok=True)
        vc.save_cache(self.prof / ".cache" / "vision_cache.json", cache)
        calls = []

        def fake(path, **kw):
            calls.append(path)
            return {"theme": "Y", "themes": [], "confidence": 0.8}

        with mock.patch("agents.onboarding.proposition.analyze_cover", side_effect=fake):
            r = proposition.run_vision("perso", on_progress=lambda d, t: None)
        self.assertEqual(r["n_analyzed"], 1)           # seul b.pdf analysé
        self.assertEqual(len(calls), 1)

    def test_run_vision_does_not_cache_errors(self):
        # Un résultat {"error": ...} (ex. 403 modèle désactivé) ne doit PAS être
        # mis en cache (sinon collé : skippé au re-run reprenable).
        from agents.onboarding import proposition
        with mock.patch("agents.onboarding.proposition.analyze_cover",
                        side_effect=lambda path, **kw: {"error": "api"}):
            r = proposition.run_vision("perso", on_progress=lambda d, t: None)
        self.assertEqual(r["n_total"], 2)
        self.assertEqual(r["n_analyzed"], 0)           # aucun succès → rien d'analysé
        cache_path = self.prof / ".cache" / "vision_cache.json"
        cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
        self.assertEqual(len(cache), 0)                # aucune erreur en cache


class TestClusterCorpus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="klodo-clu-")
        self.root = Path(self.tmp)
        self.prof = self.root / "profiles" / "perso" / ".cache"
        self.prof.mkdir(parents=True)
        cache = {}
        for i, theme in enumerate(["Deep Learning", "deep learning", "Astronomy"]):
            cache[f"k{i}"] = {"result": {"theme": theme,
                "themes": [{"theme": theme, "confidence": 0.9}], "confidence": 0.9},
                "model": "M", "prompt_version": "v3", "cached_at": "t"}
        (self.prof / "vision_cache.json").write_text(json.dumps(cache), encoding="utf-8")
        # extract_themes_from_vision_cache resolves the cache path via
        # theme_canon._vision_cache_path → `from dashboard import data;
        # data.get_project_root()`. Patch that single real helper.
        self.patch = mock.patch("dashboard.data.get_project_root", return_value=self.root)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cluster_corpus_groups_and_counts(self):
        from agents.onboarding import proposition
        clusters = proposition.cluster_corpus("perso")
        # "Deep Learning" + "deep learning" fusionnent (count 2), "Astronomy" seul (count 1)
        by_count = {c["count"] for c in clusters}
        self.assertIn(2, by_count)
        self.assertIn(1, by_count)
        for c in clusters:
            self.assertIn("canonical", c)
            self.assertIn("raw_members", c)
            self.assertIn("count", c)


class TestProposeTaxonomy(unittest.TestCase):
    def _llm(self, clusters, sec_map):
        from agents.onboarding import taxonomy_llm as t
        items = [t._Assign(index=i + 1, section=sec_map[c["canonical"]])
                 for i, c in enumerate(clusters) if c["canonical"] in sec_map]
        llm = mock.Mock()
        llm.with_structured_output.return_value.invoke.return_value = t._Assignments(items=items)
        return llm

    def test_section_slash_theme_depth2(self):
        from agents.onboarding import taxonomy_llm as t
        clusters = [
            {"canonical": "deep learning", "raw_members": ["Deep Learning", "deep learning"], "count": 40},
            {"canonical": "astronomy", "raw_members": ["Astronomy"], "count": 12},
        ]
        llm = self._llm(clusters, {"deep learning": "Informatique", "astronomy": "Sciences"})
        tree, mapping = t.propose_taxonomy(llm, clusters, options={"min_depth": 2, "max_depth": 3})
        self.assertEqual(mapping["Deep Learning"], "01-Informatique/Deep-Learning")
        self.assertEqual(mapping["deep learning"], "01-Informatique/Deep-Learning")
        self.assertEqual(mapping["Astronomy"], "02-Sciences/Astronomy")
        self.assertIn("01-Informatique", tree)
        self.assertIn("01-Informatique/Deep-Learning", tree)
        self.assertIn("_A-TRIER", tree)

    def test_all_clusters_covered(self):
        from agents.onboarding import taxonomy_llm as t
        clusters = [{"canonical": f"t{i}", "raw_members": [f"T{i}"], "count": 1} for i in range(5)]
        llm = self._llm(clusters, {f"t{i}": "Informatique" for i in range(5)})
        _, mapping = t.propose_taxonomy(llm, clusters)
        self.assertEqual(len(mapping), 5)
        for i in range(5):
            self.assertEqual(mapping[f"T{i}"], f"01-Informatique/T{i}")

    def test_index_matching_robust_to_reformulated_section(self):
        # le LLM peut reformuler le nom de domaine ; le matching par index tient
        from agents.onboarding import taxonomy_llm as t
        clusters = [{"canonical": "deep learning", "raw_members": ["X"], "count": 1}]
        llm = mock.Mock()
        llm.with_structured_output.return_value.invoke.return_value = \
            t._Assignments(items=[t._Assign(index=1, section="Informatique")])
        _, m = t.propose_taxonomy(llm, clusters)
        self.assertEqual(m["X"], "01-Informatique/Deep-Learning")

    def test_homonym_sections_consolidated_and_numbered(self):
        from agents.onboarding import taxonomy_llm as t
        clusters = [
            {"canonical": "alpha", "raw_members": ["Alpha"], "count": 3},
            {"canonical": "beta", "raw_members": ["Beta"], "count": 2},
            {"canonical": "gamma", "raw_members": ["Gamma"], "count": 1},
        ]
        llm = self._llm(clusters, {"alpha": "Informatique", "beta": "Informatique", "gamma": "Sciences"})
        tree, mapping = t.propose_taxonomy(llm, clusters)
        self.assertEqual(mapping["Alpha"], "01-Informatique/Alpha")
        self.assertEqual(mapping["Beta"], "01-Informatique/Beta")
        self.assertEqual(mapping["Gamma"], "02-Sciences/Gamma")
        self.assertNotIn("02-Informatique", tree)

    def test_max_depth_1_section_only(self):
        from agents.onboarding import taxonomy_llm as t
        clusters = [{"canonical": "deep learning", "raw_members": ["DL"], "count": 1}]
        llm = self._llm(clusters, {"deep learning": "Informatique"})
        tree, mapping = t.propose_taxonomy(llm, clusters, options={"max_depth": 1})
        self.assertEqual(mapping["DL"], "01-Informatique")
        self.assertNotIn("01-Informatique/Deep-Learning", tree)

    def test_casing_upper_and_lower(self):
        from agents.onboarding import taxonomy_llm as t
        clusters = [{"canonical": "machine learning", "raw_members": ["X"], "count": 1}]
        _, m_up = t.propose_taxonomy(self._llm(clusters, {"machine learning": "Informatique"}),
                                     clusters, options={"folder_case": "upper", "word_separator": "-"})
        self.assertEqual(m_up["X"], "01-INFORMATIQUE/MACHINE-LEARNING")
        _, m_lo = t.propose_taxonomy(self._llm(clusters, {"machine learning": "Informatique"}),
                                     clusters, options={"folder_case": "lower", "word_separator": "_"})
        self.assertEqual(m_lo["X"], "01-informatique/machine_learning")

    def test_separator_none(self):
        from agents.onboarding import taxonomy_llm as t
        clusters = [{"canonical": "deep learning", "raw_members": ["X"], "count": 1}]
        _, m = t.propose_taxonomy(self._llm(clusters, {"deep learning": "Informatique"}),
                                  clusters, options={"word_separator": "none"})
        self.assertEqual(m["X"], "01-Informatique/DeepLearning")

    def test_title_preserves_acronyms(self):
        from agents.onboarding import taxonomy_llm as t
        clusters = [{"canonical": "NLP", "raw_members": ["X"], "count": 1}]
        _, m = t.propose_taxonomy(self._llm(clusters, {"NLP": "Informatique"}), clusters)
        self.assertEqual(m["X"], "01-Informatique/NLP")

    def test_unnumbered_sections(self):
        from agents.onboarding import taxonomy_llm as t
        clusters = [{"canonical": "astro", "raw_members": ["X"], "count": 1}]
        _, m = t.propose_taxonomy(self._llm(clusters, {"astro": "Sciences"}),
                                  clusters, options={"numbered_sections": False})
        self.assertEqual(m["X"], "Sciences/Astro")

    def test_theme_equal_section_uses_general(self):
        from agents.onboarding import taxonomy_llm as t
        clusters = [{"canonical": "informatique", "raw_members": ["X"], "count": 1}]
        _, m = t.propose_taxonomy(self._llm(clusters, {"informatique": "Informatique"}), clusters)
        self.assertEqual(m["X"], "01-Informatique/Général")

    def test_inbox_section_rejected(self):
        from agents.onboarding import taxonomy_llm as t
        clusters = [
            {"canonical": "a", "raw_members": ["A"], "count": 1},
            {"canonical": "b", "raw_members": ["B"], "count": 1},
        ]
        _, m = t.propose_taxonomy(self._llm(clusters, {"a": "_INBOX", "b": "Sciences"}), clusters)
        self.assertNotIn("A", m)
        self.assertEqual(m["B"], "01-Sciences/B")

    def test_empty_clusters(self):
        from agents.onboarding import taxonomy_llm as t
        tree, m = t.propose_taxonomy(mock.Mock(), [])
        self.assertEqual(tree, ["_A-TRIER"])
        self.assertEqual(m, {})

    def test_llm_failure_fallback(self):
        from agents.onboarding import taxonomy_llm as t
        llm = mock.Mock()
        llm.with_structured_output.return_value.invoke.side_effect = RuntimeError("403")
        tree, m = t.propose_taxonomy(llm, [{"canonical": "x", "raw_members": ["X"], "count": 1}])
        self.assertEqual(tree, ["_A-TRIER"])
        self.assertEqual(m, {})

    def test_normalize_options_clamps_invalid(self):
        from agents.onboarding import taxonomy_llm
        o = taxonomy_llm._normalize_options(
            {"min_depth": 3, "max_depth": 2, "folder_case": "zz",
             "word_separator": "x", "folder_language": "zz", "granularity": "huge"})
        self.assertEqual(o["max_depth"], 2)
        self.assertEqual(o["min_depth"], 2)
        self.assertEqual(o["folder_case"], "title")
        self.assertEqual(o["word_separator"], "-")
        self.assertEqual(o["folder_language"], "auto")
        self.assertEqual(o["granularity"], "auto")
        self.assertTrue(o["numbered_sections"])

    def test_assign_system_reflects_options(self):
        from agents.onboarding import taxonomy_llm as t
        s = t._assign_system(t._normalize_options(
            {"folder_language": "fr", "granularity": "detailed"}), "Informatique, Sciences")
        self.assertIn("FRANÇAIS", s.upper())
        self.assertIn("FIN", s.upper())
        self.assertIn("Informatique", s)


class TestProposeCategories(unittest.TestCase):
    def test_build_categories_from_tree(self):
        from agents.onboarding import proposition
        tree = ["02-INFORMATIQUE", "02-INFORMATIQUE/Deep-Learning", "_A-TRIER"]
        fake_llm = mock.Mock()
        with mock.patch("agents.onboarding.proposition.get_agent_llm", return_value=fake_llm), \
             mock.patch("agents.onboarding.proposition.propose_keywords_for_new_folders",
                        return_value=[{"chemin": "02-INFORMATIQUE/Deep-Learning",
                                       "groupe": "informatique", "priorite": 5,
                                       "mots_cles": ["neural", "deep learning"]}]) as pk:
            cats = proposition.propose_categories(tree)
        # categories.yaml groupé par 'groupe'
        self.assertIn("informatique", cats)
        self.assertEqual(cats["informatique"][0]["chemin"], "02-INFORMATIQUE/Deep-Learning")
        # creations passées = dossiers feuilles (pas _A-TRIER, pas la racine de section)
        creations = pk.call_args.kwargs["creations"]
        paths = {c["path"] for c in creations}
        self.assertIn("02-INFORMATIQUE/Deep-Learning", paths)
        self.assertNotIn("_A-TRIER", paths)

    def test_folder_hints_enrich_rationale(self):
        from agents.onboarding import proposition
        tree = ["01-INFO", "01-INFO/Machine-Learning", "_A-TRIER"]
        hints = {"01-INFO/Machine-Learning": ["deep learning", "transformers", "cnn"]}
        fake_llm = mock.Mock()
        with mock.patch("agents.onboarding.proposition.get_agent_llm", return_value=fake_llm), \
             mock.patch("agents.onboarding.proposition.propose_keywords_for_new_folders",
                        return_value=[]) as pk:
            proposition.propose_categories(tree, folder_hints=hints)
        creations = pk.call_args.kwargs["creations"]
        ml = next(c for c in creations if c["path"] == "01-INFO/Machine-Learning")
        self.assertIn("deep learning", ml["rationale"])
        self.assertIn("regroupe", ml["rationale"])


class TestBuildProposal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="klodo-prop-")
        self.root = Path(self.tmp)
        self.target = self.root / "RAW"
        (self.target / "a.pdf").parent.mkdir(parents=True, exist_ok=True)
        (self.target / "a.pdf").write_bytes(b"%PDF-1.4 a")
        self.prof = self.root / "profiles" / "perso"
        (self.prof / ".cache").mkdir(parents=True)
        (self.prof / "profile.yaml").write_text(yaml.safe_dump({
            "target": str(self.target), "fallback": "_A-TRIER",
            "llm": {"model": "M"}, "defaults": {"pages": 2}}), encoding="utf-8")
        (self.prof / "tree.yaml").write_text(yaml.safe_dump({"folders": []}), encoding="utf-8")
        (self.prof / "theme_mapping.yaml").write_text("{}", encoding="utf-8")
        (self.prof / "categories.yaml").write_text("{}", encoding="utf-8")
        for m in ("lib.profile", "agents.onboarding.proposition",
                  "dashboard.data", "lib.theme_canon", "dashboard.taxonomy"):
            try:
                mock.patch(f"{m}.get_project_root", return_value=self.root).start()
            except Exception:
                pass

    def tearDown(self):
        mock.patch.stopall()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_proposal_writes_3_yamls_and_coverage(self):
        from agents.onboarding import proposition
        tree = ["02-INFO", "02-INFO/DL", "_A-TRIER"]
        mapping = {"Deep Learning": "02-INFO/DL"}
        cats = {"autres": [{"chemin": "02-INFO/DL", "priorite": 5, "mots_cles": ["dl"]}]}
        cov = proposition.write_proposal("perso", tree, mapping, cats)
        wrote = yaml.safe_load((self.prof / "tree.yaml").read_text())
        self.assertIn("02-INFO/DL", wrote["folders"])
        self.assertEqual(yaml.safe_load((self.prof / "theme_mapping.yaml").read_text()),
                         {"Deep Learning": "02-INFO/DL"})
        self.assertIn("coverage", cov)
        self.assertIn("stats", cov)

    def test_build_proposal_end_to_end(self):
        # Chaîne complète vision → cluster → propose → categories → write → dry-run
        # avec les VRAIES fonctions (seuls Vision + LLM sont mockés) : verrouille
        # la cohérence des shapes à chaque frontière contre toute dérive future.
        from agents.onboarding import proposition, taxonomy_llm
        vis = {"title": "T", "theme": "Deep Learning",
               "themes": [{"theme": "Deep Learning", "confidence": 0.9}], "confidence": 0.9}

        def fake_invoke(messages):
            # le LLM assigne chaque thème (par son numéro) à un domaine
            import re as _re
            idxs = _re.findall(r"^(\d+)\. ", messages[-1]["content"], _re.M)
            return taxonomy_llm._Assignments(items=[
                taxonomy_llm._Assign(index=int(n), section="Informatique") for n in idxs])

        fake_llm = mock.Mock()
        fake_llm.with_structured_output.return_value.invoke.side_effect = fake_invoke
        seen = []
        with mock.patch("agents.onboarding.proposition.analyze_cover",
                        side_effect=lambda path, **kw: vis), \
             mock.patch("agents.onboarding.proposition.get_agent_llm", return_value=fake_llm), \
             mock.patch("agents.onboarding.proposition.propose_keywords_for_new_folders",
                        return_value=[{"chemin": "01-Informatique/Deep-Learning",
                                       "groupe": "informatique", "priorite": 5,
                                       "mots_cles": ["neural"]}]):
            cov = proposition.build_proposal("perso", lambda d, t: seen.append((d, t)))
        # Vision a peuplé le cache
        cache = json.loads((self.prof / ".cache" / "vision_cache.json").read_text())
        self.assertEqual(len(cache), 1)
        # structure SECTION/Thème (profondeur 2) écrite dans tree.yaml
        tree = yaml.safe_load((self.prof / "tree.yaml").read_text())
        self.assertIn("01-Informatique", tree["folders"])      # section
        self.assertTrue(any(f.startswith("01-Informatique/") for f in tree["folders"]))
        self.assertIn("_A-TRIER", tree["folders"])
        mapping = yaml.safe_load((self.prof / "theme_mapping.yaml").read_text())
        self.assertIn("Deep Learning", mapping)
        self.assertTrue(mapping["Deep Learning"].startswith("01-Informatique/"))  # sous-dossier
        self.assertIn("coverage", cov)
        self.assertIn("stats", cov)
        self.assertTrue(seen)                                  # on_progress appelé

    def test_build_proposal_warns_on_total_vision_failure(self):
        # Vision échoue sur tous les fichiers → build_proposal renvoie un warning
        # explicite (au lieu d'une taxonomie vide présentée comme un succès).
        from agents.onboarding import proposition
        fake_llm = mock.Mock()   # non appelé : 0 cluster après échec Vision total
        with mock.patch("agents.onboarding.proposition.analyze_cover",
                        side_effect=lambda p, **k: {"error": "api"}), \
             mock.patch("agents.onboarding.proposition.get_agent_llm", return_value=fake_llm):
            rep = proposition.build_proposal("perso", lambda d, t: None)
        self.assertEqual(rep["vision"]["n_total"], 1)
        self.assertEqual(rep["vision"]["n_analyzed"], 0)
        self.assertIn("warning", rep)
        self.assertIn("Vision", rep["warning"])

    def test_build_proposal_warns_when_proposition_fails(self):
        # Vision OK (thèmes détectés) mais l'appel LLM de proposition échoue
        # (modèle agent désactivé/indisponible) → arbre vide. On veut un warning
        # explicite « proposition », pas une taxonomie vide présentée en succès.
        from agents.onboarding import proposition
        vis = {"title": "T", "theme": "Machine Learning",
               "themes": [{"theme": "Machine Learning", "confidence": 0.9}],
               "confidence": 0.9}
        fake_llm = mock.Mock()
        fake_llm.with_structured_output.return_value.invoke.side_effect = \
            RuntimeError("403 Model disabled")
        with mock.patch("agents.onboarding.proposition.analyze_cover",
                        side_effect=lambda p, **k: vis), \
             mock.patch("agents.onboarding.proposition.get_agent_llm", return_value=fake_llm):
            rep = proposition.build_proposal("perso", lambda d, t: None)
        self.assertEqual(rep["vision"]["n_analyzed"], 1)        # la Vision a réussi
        self.assertIn("warning", rep)
        self.assertIn("proposition", rep["warning"].lower())   # warning ciblé proposition

    def test_build_proposal_derives_folder_hints(self):
        from agents.onboarding import proposition, taxonomy_llm
        vis = {"title": "T", "theme": "Deep Learning",
               "themes": [{"theme": "Deep Learning", "confidence": 0.9}], "confidence": 0.9}

        def fake_invoke(messages):
            import re as _re
            idxs = _re.findall(r"^(\d+)\. ", messages[-1]["content"], _re.M)
            return taxonomy_llm._Assignments(items=[
                taxonomy_llm._Assign(index=int(n), section="Informatique") for n in idxs])

        fake_llm = mock.Mock()
        fake_llm.with_structured_output.return_value.invoke.side_effect = fake_invoke
        with mock.patch("agents.onboarding.proposition.analyze_cover",
                        side_effect=lambda path, **kw: vis), \
             mock.patch("agents.onboarding.proposition.get_agent_llm", return_value=fake_llm), \
             mock.patch("agents.onboarding.proposition.propose_keywords_for_new_folders",
                        return_value=[]) as pk:
            proposition.build_proposal("perso", lambda d, t: None)
        creations = pk.call_args.kwargs["creations"]
        self.assertTrue(creations)                              # au moins 1 dossier feuille
        self.assertTrue(any("deep learning" in c["rationale"].lower() for c in creations))

    def test_build_proposal_passes_onboarding_options(self):
        # Les options stockées dans profile.yaml descendent jusqu'à propose_taxonomy.
        from agents.onboarding import proposition
        pj = self.prof / "profile.yaml"
        cfg = yaml.safe_load(pj.read_text())
        cfg["onboarding_options"] = {"max_depth": 3, "folder_language": "en"}
        pj.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        captured = {}

        def fake_propose(llm, clusters, options=None, **kwargs):
            captured["options"] = options
            return ["_A-TRIER"], {}

        with mock.patch("agents.onboarding.proposition.analyze_cover",
                        side_effect=lambda p, **k: {"theme": "X",
                            "themes": [{"theme": "X", "confidence": 0.9}], "confidence": 0.9}), \
             mock.patch("agents.onboarding.proposition.get_agent_llm", return_value=mock.Mock()), \
             mock.patch("agents.onboarding.proposition.propose_taxonomy", side_effect=fake_propose):
            proposition.build_proposal("perso", lambda d, t: None)
        self.assertEqual(captured["options"], {"max_depth": 3, "folder_language": "en"})

    def test_build_proposal_reports_phases(self):
        from agents.onboarding import proposition, taxonomy_llm
        vis = {"title": "T", "theme": "Deep Learning",
               "themes": [{"theme": "Deep Learning", "confidence": 0.9}], "confidence": 0.9}

        def fake_invoke(messages):
            import re as _re
            idxs = _re.findall(r"^(\d+)\. ", messages[-1]["content"], _re.M)
            return taxonomy_llm._Assignments(items=[
                taxonomy_llm._Assign(index=int(n), section="Informatique") for n in idxs])

        fake_llm = mock.Mock()
        fake_llm.with_structured_output.return_value.invoke.side_effect = fake_invoke
        phases = []
        with mock.patch("agents.onboarding.proposition.analyze_cover",
                        side_effect=lambda path, **kw: vis), \
             mock.patch("agents.onboarding.proposition.get_agent_llm", return_value=fake_llm), \
             mock.patch("agents.onboarding.proposition.propose_keywords_for_new_folders",
                        return_value=[]):
            proposition.build_proposal("perso", lambda d, t: None,
                                       on_phase=lambda ph, d, t: phases.append(ph))
        # phases post-Vision émises (au moins clustering + taxonomie + catégories + écriture)
        self.assertIn("clustering", phases)
        self.assertTrue(any("taxonomie" in p for p in phases))   # via on_step de propose_taxonomy
        self.assertIn("catégories", phases)
        self.assertIn("écriture", phases)

    def test_build_proposal_on_phase_optional(self):
        # rétro-compat : sans on_phase, build_proposal fonctionne comme avant
        from agents.onboarding import proposition, taxonomy_llm
        vis = {"title": "T", "theme": "X",
               "themes": [{"theme": "X", "confidence": 0.9}], "confidence": 0.9}
        fake_llm = mock.Mock()
        fake_llm.with_structured_output.return_value.invoke.side_effect = \
            lambda m: taxonomy_llm._Assignments(items=[taxonomy_llm._Assign(index=1, section="Sciences")])
        with mock.patch("agents.onboarding.proposition.analyze_cover",
                        side_effect=lambda path, **kw: vis), \
             mock.patch("agents.onboarding.proposition.get_agent_llm", return_value=fake_llm), \
             mock.patch("agents.onboarding.proposition.propose_keywords_for_new_folders",
                        return_value=[]):
            rep = proposition.build_proposal("perso", lambda d, t: None)   # pas de on_phase
        self.assertIn("coverage", rep)


class TestAgentOnboardingWrapper(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="klodo-wrap-")
        self.root = Path(self.tmp)
        self.target = self.root / "RAW"
        (self.target / "a.pdf").parent.mkdir(parents=True, exist_ok=True)
        (self.target / "a.pdf").write_bytes(b"%PDF-1.4 a")
        self.patch = mock.patch("dashboard.data.get_project_root", return_value=self.root)
        self.patch.start()
        self.patch2 = mock.patch("lib.profile.get_project_root", return_value=self.root)
        self.patch2.start()

    def tearDown(self):
        mock.patch.stopall()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scan_endpoint_logic(self):
        from dashboard import agent_onboarding as ao
        r = ao.scan(str(self.target))
        self.assertEqual(r["n_files"], 1)
        self.assertIn("estimate", r)

    def test_start_then_status_done(self):
        from dashboard import agent_onboarding as ao
        # exécution synchrone : on patche _spawn pour appeler la cible direct
        with mock.patch.object(ao, "_spawn", lambda fn, args, name: fn(*args)), \
             mock.patch("agents.onboarding.proposition.build_proposal",
                        return_value={"coverage": 80.0, "stats": {"n_in_lib": 10}}):
            res = ao.start_onboarding("perso-2026", str(self.target))
            run_id = res["run_id"]
            st = ao.get_status("perso-2026", run_id)
        self.assertEqual(st["status"], "done")
        self.assertEqual(st["coverage"], 80.0)
        # profil brouillon créé
        cfg = yaml.safe_load((self.root / "profiles" / "perso-2026" / "profile.yaml").read_text())
        self.assertTrue(cfg["onboarding_draft"])

    def test_finalize_clears_flag(self):
        from dashboard import agent_onboarding as ao
        from lib import profile as prof
        prof.create_draft_profile("perso-2026", str(self.target))
        ao.finalize("perso-2026")
        cfg = yaml.safe_load((self.root / "profiles" / "perso-2026" / "profile.yaml").read_text())
        self.assertFalse(cfg["onboarding_draft"])

    def test_run_writes_error_status_on_failure(self):
        from dashboard import agent_onboarding as ao
        with mock.patch.object(ao, "_spawn", lambda fn, args, name: fn(*args)), \
             mock.patch("agents.onboarding.proposition.build_proposal",
                        side_effect=RuntimeError("boom")):
            res = ao.start_onboarding("perso-err", str(self.target))
            st = ao.get_status("perso-err", res["run_id"])
        self.assertEqual(st["status"], "error")
        self.assertIn("boom", st["error"])

    def test_status_carries_vision_warning(self):
        from dashboard import agent_onboarding as ao
        with mock.patch.object(ao, "_spawn", lambda fn, args, name: fn(*args)), \
             mock.patch("agents.onboarding.proposition.build_proposal",
                        return_value={"coverage": 0.0, "stats": {"n_in_lib": 3},
                                      "warning": "Vision en échec sur les 3 fichiers"}):
            res = ao.start_onboarding("perso-warn", str(self.target))
            st = ao.get_status("perso-warn", res["run_id"])
        self.assertEqual(st["status"], "done")
        self.assertIn("warning", st)
        self.assertIn("Vision", st["warning"])

    def test_start_onboarding_stores_options(self):
        from dashboard import agent_onboarding as ao
        with mock.patch.object(ao, "_spawn", lambda fn, args, name: fn(*args)), \
             mock.patch("agents.onboarding.proposition.build_proposal",
                        return_value={"coverage": 0.0, "stats": {}}):
            ao.start_onboarding("perso-opt", str(self.target),
                                options={"max_depth": 3, "numbered_sections": False})
        cfg = yaml.safe_load((self.root / "profiles" / "perso-opt" / "profile.yaml").read_text())
        self.assertEqual(cfg["onboarding_options"]["max_depth"], 3)
        self.assertFalse(cfg["onboarding_options"]["numbered_sections"])

    def test_wrapper_writes_post_vision_phases(self):
        from dashboard import agent_onboarding as ao
        writes = []

        def fake_build(profile, on_progress, on_phase=None):
            on_progress(2, 2)
            if on_phase:
                on_phase("taxonomie · regroupement", 1, 3)
            return {"coverage": 50.0, "stats": {}, "by_destination": [],
                    "vision": {"n_total": 2, "n_analyzed": 2}}

        with mock.patch.object(ao, "_write_status",
                               side_effect=lambda p, r, payload: writes.append(payload)), \
             mock.patch("agents.onboarding.proposition.build_proposal",
                        side_effect=fake_build):
            ao._run("perso", "run-xyz")

        phases = [w.get("phase") for w in writes]
        self.assertIn("vision", phases)
        self.assertIn("taxonomie · regroupement", phases)
        self.assertTrue(any(w.get("status") == "done" for w in writes))


class TestOnboardingEndpoints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="klodo-ep-")
        self.root = Path(self.tmp)
        self.target = self.root / "RAW"
        (self.target / "a.pdf").parent.mkdir(parents=True, exist_ok=True)
        (self.target / "a.pdf").write_bytes(b"%PDF-1.4 a")
        mock.patch("dashboard.data.get_project_root", return_value=self.root).start()
        mock.patch("lib.profile.get_project_root", return_value=self.root).start()
        from fastapi.testclient import TestClient

        from dashboard.app import app
        self.client = TestClient(app)

    def tearDown(self):
        mock.patch.stopall()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scan_endpoint(self):
        r = self.client.post("/api/agent/onboarding/scan", json={"inbox_path": str(self.target)})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["n_files"], 1)

    def test_start_status_finalize_flow(self):
        from dashboard import agent_onboarding as ao

        def sync(fn, args, name):
            return fn(*args)

        with mock.patch.object(ao, "_spawn", sync), \
             mock.patch("agents.onboarding.proposition.build_proposal",
                        return_value={"coverage": 75.0, "stats": {"n_in_lib": 4}}):
            s = self.client.post("/api/agent/onboarding/start",
                json={"profile_name": "perso", "inbox_path": str(self.target)})
        self.assertEqual(s.status_code, 200)
        run_id = s.json()["run_id"]
        st = self.client.get(f"/api/agent/onboarding/status?profile=perso&run_id={run_id}")
        self.assertEqual(st.json()["status"], "done")
        f = self.client.post("/api/agent/onboarding/finalize", json={"profile": "perso"})
        self.assertEqual(f.status_code, 200)

    def test_start_with_options_persists(self):
        from dashboard import agent_onboarding as ao

        def sync(fn, args, name):
            return fn(*args)

        with mock.patch.object(ao, "_spawn", sync), \
             mock.patch("agents.onboarding.proposition.build_proposal",
                        return_value={"coverage": 0.0, "stats": {}}):
            r = self.client.post("/api/agent/onboarding/start",
                json={"profile_name": "perso", "inbox_path": str(self.target),
                      "options": {"max_depth": 3, "folder_language": "en"}})
        self.assertEqual(r.status_code, 200)
        cfg = yaml.safe_load((self.root / "profiles" / "perso" / "profile.yaml").read_text())
        self.assertEqual(cfg["onboarding_options"]["max_depth"], 3)
        self.assertEqual(cfg["onboarding_options"]["folder_language"], "en")

    def test_start_ignores_malformed_options(self):
        from dashboard import agent_onboarding as ao

        def sync(fn, args, name):
            return fn(*args)

        with mock.patch.object(ao, "_spawn", sync), \
             mock.patch("agents.onboarding.proposition.build_proposal",
                        return_value={"coverage": 0.0, "stats": {}}):
            r = self.client.post("/api/agent/onboarding/start",
                json={"profile_name": "perso", "inbox_path": str(self.target),
                      "options": ["junk", "not", "a", "dict"]})
        self.assertEqual(r.status_code, 200)   # liste ignorée (isinstance dict) → défauts
        cfg = yaml.safe_load((self.root / "profiles" / "perso" / "profile.yaml").read_text())
        self.assertNotIn("onboarding_options", cfg)   # rien stocké (options=None)

    def test_start_missing_fields_400(self):
        r = self.client.post("/api/agent/onboarding/start", json={"profile_name": "x"})
        self.assertEqual(r.status_code, 400)

    def test_start_rejects_traversal_name(self):
        r = self.client.post("/api/agent/onboarding/start",
            json={"profile_name": "../evil", "inbox_path": str(self.target)})
        self.assertEqual(r.status_code, 400)
        # aucun dossier créé hors du sandbox profiles/
        self.assertFalse((self.root / "evil").exists())
        self.assertFalse((self.root.parent / "evil").exists())

    def test_status_rejects_traversal_run_id(self):
        r = self.client.get(
            "/api/agent/onboarding/status?profile=perso&run_id=../../x")
        self.assertEqual(r.status_code, 404)

    def test_finalize_rejects_traversal_name(self):
        r = self.client.post("/api/agent/onboarding/finalize",
            json={"profile": "../../etc"})
        self.assertEqual(r.status_code, 400)

    def test_onboarding_page_has_cancel_button(self):
        # garde-fou : le bouton Annuler/Recommencer est présent dans la page
        r = self.client.get("/onboarding")
        self.assertEqual(r.status_code, 200)
        body = r.text
        self.assertIn("onb-cancel-btn", body)
        self.assertIn("Annuler / Recommencer", body)


class TestFsBrowse(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="klodo-fs-")
        self.root = Path(self.tmp)
        (self.root / "Sub A").mkdir()
        (self.root / "Sub B").mkdir()
        (self.root / ".hidden").mkdir()
        (self.root / "a.pdf").write_bytes(b"%PDF")
        (self.root / "b.epub").write_bytes(b"x")
        (self.root / "notes.txt").write_bytes(b"x")
        from fastapi.testclient import TestClient

        from dashboard.app import app
        self.client = TestClient(app)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_browse_lists_subdirs_and_counts(self):
        r = self.client.get("/api/fs/browse", params={"path": str(self.root)})
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertEqual([d["name"] for d in j["dirs"]], ["Sub A", "Sub B"])  # triés, .hidden exclu
        self.assertEqual(j["n_files"], 2)             # pdf + epub, pas .txt
        self.assertIsNotNone(j["parent"])
        self.assertTrue(j["path"].endswith(self.root.name))

    def test_browse_invalid_path_400(self):
        r = self.client.get("/api/fs/browse", params={"path": "/private/tmp/nope-xyz-123"})
        self.assertEqual(r.status_code, 400)


class TestMacroThemeFactorization(unittest.TestCase):
    def test_macro_system_reflects_target_and_language(self):
        from agents.onboarding import taxonomy_llm as t
        s = t._macro_system(t._normalize_options({"folder_language": "fr"}),
                            "Machine Learning, Bases de Données", low=3, high=6)
        self.assertIn("3", s)
        self.assertIn("6", s)
        self.assertIn("GRANDS THÈMES", s)               # distingue la passe 2 dans les mocks
        self.assertIn("FRANÇAIS", s.upper())
        self.assertIn("Machine Learning", s)            # grands thèmes déjà créés réinjectés
        self.assertIn("réservés", s.lower())            # règle anti-collision (section/Général/Divers)

    def test_collides_detects_section_and_reserved(self):
        from agents.onboarding import taxonomy_llm as t
        self.assertTrue(t._collides("", "Sciences"))
        self.assertTrue(t._collides("Sciences", "Sciences"))
        self.assertTrue(t._collides("sciences", "SCIENCES"))   # insensible à la casse
        self.assertTrue(t._collides("INBOX", "Sciences"))
        self.assertTrue(t._collides("_INBOX", "Sciences"))
        self.assertFalse(t._collides("Astrophysique", "Sciences"))
        self.assertFalse(t._collides("Divers", "Sciences"))   # bucket légitime, pas une collision

    def test_granularity_table_values(self):
        from agents.onboarding import taxonomy_llm as t
        self.assertIsNone(t._GRANULARITY["detailed"])
        self.assertEqual(t._GRANULARITY["compact"], {"skip": 6, "low": 3, "high": 6, "cap": 8})
        self.assertEqual(t._GRANULARITY["auto"], {"skip": 12, "low": 6, "high": 12, "cap": 16})
        self.assertEqual(t._DIVERS["fr"], "Divers")

    def _llm(self, sec_map, macro_map=None, fail_pass2=False):
        """Mock LLM conscient des passes : inspecte le payload (jamais positionnel).
        sec_map  : {canonical: section}  (passe 1)
        macro_map: {canonical: grand_thème} (passe 2)
        """
        from agents.onboarding import taxonomy_llm as t

        def _invoke(messages):
            system = messages[0]["content"]
            user = messages[-1]["content"]
            is_macro = "GRANDS THÈMES" in system
            if is_macro and fail_pass2:
                raise RuntimeError("403 passe 2")
            items = []
            for line in user.splitlines():
                m = re.match(r"\s*(\d+)\.\s+(.*?)\s+\(volume", line)
                if not m:
                    continue
                idx, name = int(m.group(1)), m.group(2)
                label = (macro_map or {}).get(name) if is_macro else sec_map.get(name)
                if label:
                    items.append(t._Assign(index=idx, section=label))
            return t._Assignments(items=items)

        llm = mock.Mock()
        llm.with_structured_output.return_value.invoke.side_effect = _invoke
        return llm

    def test_assign_macro_themes_index_matching(self):
        from agents.onboarding import taxonomy_llm as t
        sec = [{"canonical": "deep learning", "raw_members": ["DL"], "count": 5},
               {"canonical": "sql", "raw_members": ["SQL"], "count": 3}]
        llm = self._llm({}, macro_map={"deep learning": "Machine Learning", "sql": "Bases de Données"})
        out = t._assign_macro_themes(llm, sec, t._normalize_options(None), low=3, high=6)
        self.assertEqual(out["deep learning"], "Machine Learning")
        self.assertEqual(out["sql"], "Bases de Données")

    def test_assign_models_tolerate_malformed_item(self):
        # Un item LLM malformé ({}) ne doit PAS faire échouer tout le lot : grâce aux
        # défauts, il devient (index=-1, section="") et est ignoré, les items valides
        # étant conservés. Régression : sans défauts, un seul {} nuke le regroupement
        # d'une grosse section → retour au grain fin (INFORMATIQUE 128 sous-dossiers).
        from agents.onboarding import taxonomy_llm as t
        self.assertEqual(t._Assign().index, -1)
        self.assertEqual(t._Assign().section, "")
        sec = [{"canonical": "deep learning", "raw_members": ["DL"], "count": 5}]
        llm = mock.Mock()
        llm.with_structured_output.return_value.invoke.return_value = t._Assignments(items=[
            t._Assign(index=1, section="Machine Learning"),
            t._Assign(),   # malformé → ignoré, ne casse pas le lot
        ])
        out = t._assign_macro_themes(llm, sec, t._normalize_options(None), low=3, high=6)
        self.assertEqual(out, {"deep learning": "Machine Learning"})

    def test_assign_macro_themes_ignores_out_of_range_and_empty(self):
        from agents.onboarding import taxonomy_llm as t
        sec = [{"canonical": "a", "raw_members": ["A"], "count": 1}]
        llm = mock.Mock()
        llm.with_structured_output.return_value.invoke.return_value = t._Assignments(items=[
            t._Assign(index=99, section="X"),      # hors-borne → ignoré
            t._Assign(index=1, section="   "),     # vide → ignoré
        ])
        out = t._assign_macro_themes(llm, sec, t._normalize_options(None), low=3, high=6)
        self.assertEqual(out, {})

    def test_assign_macro_themes_reuses_macros_across_batches(self):
        # > _MACRO_CHUNK clusters → 2 lots ; le 2e lot doit voir les grands thèmes du 1er
        from agents.onboarding import taxonomy_llm as t
        sec = [{"canonical": f"t{i}", "raw_members": [f"T{i}"], "count": 1} for i in range(t._MACRO_CHUNK + 5)]
        seen_existing = []

        def _invoke(messages):
            seen_existing.append(messages[0]["content"])
            items = [t._Assign(index=int(n), section="Macro")
                     for n in re.findall(r"^\s*(\d+)\. ", messages[-1]["content"], re.M)]
            return t._Assignments(items=items)

        llm = mock.Mock()
        llm.with_structured_output.return_value.invoke.side_effect = _invoke
        out = t._assign_macro_themes(llm, sec, t._normalize_options(None), low=3, high=6)
        self.assertEqual(len(out), t._MACRO_CHUNK + 5)               # tous assignés
        self.assertIn("Macro", seen_existing[1])               # 2e lot voit le grand thème du 1er

    def test_assign_macro_themes_single_call_for_whole_section(self):
        # une section de 50 thèmes (≤ _MACRO_CHUNK) → UN SEUL appel LLM (pas de batch)
        from agents.onboarding import taxonomy_llm as t
        sec = [{"canonical": f"t{i}", "raw_members": [f"T{i}"], "count": 1} for i in range(50)]
        llm = self._llm({}, macro_map={f"t{i}": "Macro" for i in range(50)})
        t._assign_macro_themes(llm, sec, t._normalize_options(None), low=3, high=6)
        self.assertEqual(llm.with_structured_output.return_value.invoke.call_count, 1)
        self.assertGreaterEqual(t._MACRO_CHUNK, 200)   # un seul appel pour les grosses sections

    @staticmethod
    def _cl(name, count):
        return {"canonical": name, "raw_members": [name.upper()], "count": count}

    def test_enforce_cap_under_cap_unchanged(self):
        from agents.onboarding import taxonomy_llm as t
        cl = [self._cl("a", 1), self._cl("b", 1), self._cl("c", 1)]
        macro = {"a": "A", "b": "B", "c": "C"}
        self.assertEqual(t._enforce_cap(macro, cl, cap=8, lang="fr"), macro)

    def test_enforce_cap_zero_clamped_to_one(self):
        # cap=0 borné à 1 (max(1, cap)) : pas d'IndexError, résultat déterministe
        from agents.onboarding import taxonomy_llm as t
        cl = [self._cl("a", 5), self._cl("b", 3)]
        macro = {"a": "A", "b": "B"}
        self.assertEqual(t._enforce_cap(macro, cl, cap=0, lang="fr"),
                         t._enforce_cap(macro, cl, cap=1, lang="fr"))

    def test_enforce_cap_collapses_keeping_biggest(self):
        from agents.onboarding import taxonomy_llm as t
        cl = [self._cl("big1", 100), self._cl("big2", 90),
              self._cl("small1", 5), self._cl("small2", 4)]
        macro = {"big1": "Alpha", "big2": "Beta", "small1": "Gamma", "small2": "Delta"}
        out = t._enforce_cap(macro, cl, cap=3, lang="fr")   # garde cap-1=2 plus gros
        self.assertEqual(out["big1"], "Alpha")
        self.assertEqual(out["big2"], "Beta")
        self.assertEqual(out["small1"], "Divers")
        self.assertEqual(out["small2"], "Divers")
        self.assertEqual(set(out.values()), {"Alpha", "Beta", "Divers"})

    def test_enforce_cap_idempotent_when_llm_named_divers(self):
        from agents.onboarding import taxonomy_llm as t
        # le LLM a lui-même nommé un grand thème "Divers" : le collapse fusionne dedans
        cl = [self._cl("big1", 100), self._cl("big2", 90),
              self._cl("d1", 5), self._cl("d2", 4), self._cl("s1", 3)]
        macro = {"big1": "Alpha", "big2": "Beta",
                 "d1": "Divers", "d2": "Divers", "s1": "Gamma"}
        out = t._enforce_cap(macro, cl, cap=3, lang="fr")   # 4 macros > cap → garde 2
        self.assertEqual(out["big1"], "Alpha")
        self.assertEqual(out["big2"], "Beta")
        self.assertEqual(out["d1"], "Divers")               # déjà Divers → reste (idempotent)
        self.assertEqual(out["d2"], "Divers")
        self.assertEqual(out["s1"], "Divers")               # Gamma fusionné dans le Divers existant
        self.assertEqual(set(out.values()), {"Alpha", "Beta", "Divers"})

    def test_enforce_cap_deterministic_tiebreak(self):
        from agents.onboarding import taxonomy_llm as t
        # 1 gros + 3 ex-aequo ; cap=3 → garde gros + 1 ex-aequo (tie-break par nom)
        cl = [self._cl("huge", 100), self._cl("z", 10), self._cl("a", 10), self._cl("b", 10)]
        macro = {"huge": "Huge", "z": "Zeta", "a": "Alpha", "b": "Beta"}
        out1 = t._enforce_cap(macro, cl, cap=3, lang="fr")
        out2 = t._enforce_cap(macro, cl, cap=3, lang="fr")
        self.assertEqual(out1, out2)                         # reproductible
        self.assertEqual(out1["huge"], "Huge")
        self.assertEqual(out1["a"], "Alpha")                # (-10, "Alpha") gagne le dernier slot
        self.assertEqual(out1["b"], "Divers")
        self.assertEqual(out1["z"], "Divers")

    def test_enforce_cap_divers_dominant_degrades_to_fine(self):
        from agents.onboarding import taxonomy_llm as t
        # queue collapsée (8+8+8=24) > plus gros conservé (10) → grain fin
        cl = [self._cl("k1", 10), self._cl("k2", 9),
              self._cl("t1", 8), self._cl("t2", 8), self._cl("t3", 8)]
        macro = {"k1": "A", "k2": "B", "t1": "C", "t2": "D", "t3": "E"}
        out = t._enforce_cap(macro, cl, cap=3, lang="fr")
        self.assertEqual(out, {"k1": "k1", "k2": "k2", "t1": "t1", "t2": "t2", "t3": "t3"})
        self.assertNotIn("Divers", out.values())

    def test_compact_engorged_section_collapses_to_macros(self):
        from agents.onboarding import taxonomy_llm as t
        # 8 clusters (> skip=6) tous en "Informatique" → 3 grands thèmes
        cl = [self._cl(f"t{i}", 10 - i) for i in range(8)]
        sec_map = {f"t{i}": "Informatique" for i in range(8)}
        macro_map = {**{f"t{i}": "Machine Learning" for i in range(4)},
                     **{f"t{i}": "Bases de Données" for i in range(4, 7)},
                     "t7": "Réseaux"}
        llm = self._llm(sec_map, macro_map=macro_map)
        tree, mapping = t.propose_taxonomy(llm, cl, options={"granularity": "compact"})
        self.assertEqual(mapping["T0"], "01-Informatique/Machine-Learning")
        self.assertEqual(mapping["T7"], "01-Informatique/Réseaux")
        subs = {f for f in tree if f.startswith("01-Informatique/")}
        self.assertEqual(len(subs), 3)                        # factorisé : 3 dossiers, pas 8

    def test_compact_small_section_skips_pass2(self):
        from agents.onboarding import taxonomy_llm as t
        cl = [self._cl(f"t{i}", 1) for i in range(4)]         # 4 ≤ skip=6
        llm = self._llm({f"t{i}": "Sciences" for i in range(4)},
                        macro_map={f"t{i}": "NE_DOIT_PAS_ETRE_UTILISE" for i in range(4)})
        tree, mapping = t.propose_taxonomy(llm, cl, options={"granularity": "compact"})
        self.assertEqual(mapping["T0"], "01-Sciences/T0")     # grain fin conservé
        # passe 2 jamais appelée → un seul invoke (la passe 1)
        self.assertEqual(llm.with_structured_output.return_value.invoke.call_count, 1)

    def test_detailed_disables_pass2(self):
        from agents.onboarding import taxonomy_llm as t
        cl = [self._cl(f"t{i}", 1) for i in range(10)]        # > 6 mais detailed → pas de passe 2
        llm = self._llm({f"t{i}": "Informatique" for i in range(10)},
                        macro_map={f"t{i}": "Macro" for i in range(10)})
        _, mapping = t.propose_taxonomy(llm, cl, options={"granularity": "detailed"})
        self.assertEqual(mapping["T0"], "01-Informatique/T0")     # 1 dossier = 1 thème
        self.assertEqual(llm.with_structured_output.return_value.invoke.call_count, 1)

    def test_anti_refusion_general_keeps_fine_grain(self):
        from agents.onboarding import taxonomy_llm as t
        # 8 clusters en "Sciences" ; le LLM nomme 2 grands thèmes "Sciences" (collision)
        cl = [self._cl(f"t{i}", 10 - i) for i in range(8)]
        sec_map = {f"t{i}": "Sciences" for i in range(8)}
        macro_map = {**{f"t{i}": "Astrophysique" for i in range(6)},
                     "t6": "Sciences", "t7": "Sciences"}      # collisions
        llm = self._llm(sec_map, macro_map=macro_map)
        _, mapping = t.propose_taxonomy(llm, cl, options={"granularity": "compact"})
        # les deux collisions retombent sur leurs thèmes FINS distincts, pas un "Général" partagé
        self.assertEqual(mapping["T6"], "01-Sciences/T6")
        self.assertEqual(mapping["T7"], "01-Sciences/T7")
        self.assertNotEqual(mapping["T6"], mapping["T7"])

    def test_pass2_failure_degrades_to_fine(self):
        # invoke() lève en passe 2 → capté par le garde-fou PAR LOT de
        # _assign_macro_themes (retourne {}) → propose_taxonomy retombe au grain fin.
        from agents.onboarding import taxonomy_llm as t
        cl = [self._cl(f"t{i}", 1) for i in range(8)]         # > skip → passe 2 tentée
        llm = self._llm({f"t{i}": "Informatique" for i in range(8)},
                        macro_map={f"t{i}": "Macro" for i in range(8)}, fail_pass2=True)
        _, mapping = t.propose_taxonomy(llm, cl, options={"granularity": "compact"})
        for i in range(8):                                    # tous mappés au grain fin
            self.assertEqual(mapping[f"T{i}"], f"01-Informatique/T{i}")

    def test_enforce_cap_degrade_propagates_to_fine_grain(self):
        # bout-en-bout : 10 grands thèmes distincts, volumes égaux → cap (8) dépassé
        # ET « Divers » dominant → _enforce_cap dégrade → tout retombe au grain fin.
        from agents.onboarding import taxonomy_llm as t
        cl = [self._cl(f"t{i}", 10) for i in range(10)]
        sec_map = {f"t{i}": "Informatique" for i in range(10)}
        macro_map = {f"t{i}": f"Macro{i}" for i in range(10)}      # 10 macros distincts
        llm = self._llm(sec_map, macro_map=macro_map)
        _, mapping = t.propose_taxonomy(llm, cl, options={"granularity": "compact"})
        for i in range(10):
            self.assertEqual(mapping[f"T{i}"], f"01-Informatique/T{i}")

    def test_on_step_reports_pass1_and_pass2(self):
        from agents.onboarding import taxonomy_llm as t
        # 50 clusters → 2 lots passe 1 ; tous en "Informatique" (>skip=6) → 1 section engorgée
        cl = [self._cl(f"t{i}", 50 - i) for i in range(50)]
        sec_map = {f"t{i}": "Informatique" for i in range(50)}
        macro_map = {f"t{i}": ("Machine Learning" if i % 2 else "Réseaux") for i in range(50)}
        llm = self._llm(sec_map, macro_map=macro_map)
        steps = []
        t.propose_taxonomy(llm, cl, options={"granularity": "compact"},
                           on_step=lambda label, done, total: steps.append((label, done, total)))
        labels = [s[0] for s in steps]
        self.assertTrue(any("domaines" in lbl for lbl in labels))        # passe 1
        self.assertTrue(any("regroupement" in lbl for lbl in labels))    # passe 2
        # passe 1 : 2 lots (ceil(50/40)) ; le dernier rapport domaines doit être (2, 2)
        dom = [s for s in steps if "domaines" in s[0]]
        self.assertEqual(dom[-1][1:], (2, 2))
        # passe 2 : 1 section engorgée → dernier rapport (1, 1)
        grp = [s for s in steps if "regroupement" in s[0]]
        self.assertEqual(grp[-1][1:], (1, 1))

    def test_pass2_parallel_groups_all_sections(self):
        from agents.onboarding import taxonomy_llm as t
        # 2 sections engorgées (8 thèmes chacune) → toutes deux regroupées (parallèle)
        cl = ([self._cl(f"i{i}", 10 - i) for i in range(8)]
              + [self._cl(f"s{i}", 10 - i) for i in range(8)])
        sec_map = {**{f"i{i}": "Informatique" for i in range(8)},
                   **{f"s{i}": "Sciences" for i in range(8)}}
        macro_map = {**{f"i{i}": "Machine Learning" for i in range(8)},
                     **{f"s{i}": "Physique" for i in range(8)}}
        llm = self._llm(sec_map, macro_map=macro_map)
        steps = []
        tree, mapping = t.propose_taxonomy(llm, cl, options={"granularity": "compact"},
                                           on_step=lambda lbl, d, tot: steps.append((lbl, d, tot)))
        self.assertEqual(mapping["I0"], "01-Informatique/Machine-Learning")
        self.assertEqual(mapping["S0"], "02-Sciences/Physique")
        grp = [s for s in steps if "regroupement" in s[0]]
        self.assertEqual(grp[-1][1:], (2, 2))   # 2 sections engorgées rapportées


if __name__ == "__main__":
    unittest.main()
