#!/usr/bin/env python3
"""Tests de l'agent Onboarding (Vision/LLM mockés, zéro SSD réel)."""

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
    def test_propose_tree_and_mapping(self):
        from agents.onboarding import taxonomy_llm
        clusters = [
            {"canonical": "deep learning", "raw_members": ["Deep Learning", "deep learning"], "count": 40},
            {"canonical": "astronomy", "raw_members": ["Astronomy"], "count": 12},
        ]
        # LLM mocké : retourne le schéma Pydantic attendu
        fake_out = taxonomy_llm._ProposedTaxonomy(
            sections=[
                taxonomy_llm._Section(folder="02-INFORMATIQUE/Deep-Learning",
                                      cluster_canonicals=["deep learning"]),
                taxonomy_llm._Section(folder="01-SCIENCES/Astronomie",
                                      cluster_canonicals=["astronomy"]),
            ])
        fake_llm = mock.Mock()
        fake_llm.with_structured_output.return_value.invoke.return_value = fake_out
        tree, mapping = taxonomy_llm.propose_taxonomy(fake_llm, clusters)
        # tree contient les dossiers + parents implicites + _A-TRIER
        self.assertIn("02-INFORMATIQUE", tree)
        self.assertIn("02-INFORMATIQUE/Deep-Learning", tree)
        self.assertIn("_A-TRIER", tree)
        # mapping : chaque raw_member du cluster → son dossier
        self.assertEqual(mapping["Deep Learning"], "02-INFORMATIQUE/Deep-Learning")
        self.assertEqual(mapping["deep learning"], "02-INFORMATIQUE/Deep-Learning")
        self.assertEqual(mapping["Astronomy"], "01-SCIENCES/Astronomie")

    def test_assignment_to_unknown_cluster_skipped(self):
        from agents.onboarding import taxonomy_llm
        clusters = [{"canonical": "a", "raw_members": ["A"], "count": 1}]
        fake_out = taxonomy_llm._ProposedTaxonomy(sections=[
            taxonomy_llm._Section(folder="X/Y", cluster_canonicals=["zzz-inexistant"])])
        fake_llm = mock.Mock()
        fake_llm.with_structured_output.return_value.invoke.return_value = fake_out
        tree, mapping = taxonomy_llm.propose_taxonomy(fake_llm, clusters)
        self.assertEqual(mapping, {})                 # cluster inconnu → ignoré
        self.assertIn("_A-TRIER", tree)

    def test_default_casing_title_and_inbox_rejected(self):
        from agents.onboarding import taxonomy_llm
        clusters = [
            {"canonical": "a", "raw_members": ["A"], "count": 5},
            {"canonical": "b", "raw_members": ["B"], "count": 3},
        ]
        fake_out = taxonomy_llm._ProposedTaxonomy(sections=[
            taxonomy_llm._Section(folder="03-sciences/Astronomie", cluster_canonicals=["a"]),
            taxonomy_llm._Section(folder="_INBOX/Foo", cluster_canonicals=["b"]),
        ])
        fake_llm = mock.Mock()
        fake_llm.with_structured_output.return_value.invoke.return_value = fake_out
        tree, mapping = taxonomy_llm.propose_taxonomy(fake_llm, clusters)
        # défaut = Casse Titre (préfixe numérique préservé) : "sciences" → "Sciences"
        self.assertEqual(mapping["A"], "03-Sciences/Astronomie")
        self.assertIn("03-Sciences", tree)
        self.assertIn("03-Sciences/Astronomie", tree)
        # _INBOX réservé → B non mappé, _INBOX absent de l'arbre
        self.assertNotIn("B", mapping)
        self.assertNotIn("_INBOX", tree)
        self.assertNotIn("_INBOX/Foo", tree)

    def test_duplicate_section_names_consolidated(self):
        # Régression écran Beta-tests : le LLM a produit 01-/02-/03-INFORMATIQUE
        # (3 sections homonymes) → on les fusionne sous UNE section + sous-dossiers.
        from agents.onboarding import taxonomy_llm
        clusters = [
            {"canonical": "ai", "raw_members": ["AI"], "count": 3},
            {"canonical": "net", "raw_members": ["Net"], "count": 2},
            {"canonical": "meth", "raw_members": ["Meth"], "count": 1},
        ]
        fake_out = taxonomy_llm._ProposedTaxonomy(sections=[
            taxonomy_llm._Section(folder="01-Informatique/IA", cluster_canonicals=["ai"]),
            taxonomy_llm._Section(folder="02-Informatique/Reseaux", cluster_canonicals=["net"]),
            taxonomy_llm._Section(folder="03-Informatique/Methodes", cluster_canonicals=["meth"]),
        ])
        fake_llm = mock.Mock()
        fake_llm.with_structured_output.return_value.invoke.return_value = fake_out
        tree, m = taxonomy_llm.propose_taxonomy(fake_llm, clusters)   # défaut numéroté
        self.assertEqual(m["AI"], "01-Informatique/IA")
        self.assertEqual(m["Net"], "01-Informatique/Reseaux")        # consolidé sous 01-
        self.assertEqual(m["Meth"], "01-Informatique/Methodes")
        self.assertIn("01-Informatique", tree)
        self.assertNotIn("02-Informatique", tree)
        self.assertNotIn("03-Informatique", tree)

    def test_distinct_sections_keep_their_numbers(self):
        from agents.onboarding import taxonomy_llm
        clusters = [
            {"canonical": "a", "raw_members": ["A"], "count": 2},
            {"canonical": "b", "raw_members": ["B"], "count": 1},
        ]
        fake_out = taxonomy_llm._ProposedTaxonomy(sections=[
            taxonomy_llm._Section(folder="02-Informatique/IA", cluster_canonicals=["a"]),
            taxonomy_llm._Section(folder="01-Sciences/Physique", cluster_canonicals=["b"]),
        ])
        fake_llm = mock.Mock()
        fake_llm.with_structured_output.return_value.invoke.return_value = fake_out
        _, m = taxonomy_llm.propose_taxonomy(fake_llm, clusters)
        self.assertEqual(m["A"], "02-Informatique/IA")               # numéros LLM préservés
        self.assertEqual(m["B"], "01-Sciences/Physique")

    def test_options_casing_upper_and_lower(self):
        from agents.onboarding import taxonomy_llm
        clusters = [{"canonical": "x", "raw_members": ["X"], "count": 1}]
        fake_out = taxonomy_llm._ProposedTaxonomy(sections=[
            taxonomy_llm._Section(folder="01-Machine Learning/Deep Learning",
                                  cluster_canonicals=["x"])])
        fake_llm = mock.Mock()
        fake_llm.with_structured_output.return_value.invoke.return_value = fake_out
        _, m_up = taxonomy_llm.propose_taxonomy(fake_llm, clusters,
            options={"folder_case": "upper", "word_separator": "-"})
        self.assertEqual(m_up["X"], "01-MACHINE-LEARNING/DEEP-LEARNING")
        _, m_lo = taxonomy_llm.propose_taxonomy(fake_llm, clusters,
            options={"folder_case": "lower", "word_separator": "_"})
        self.assertEqual(m_lo["X"], "01-machine_learning/deep_learning")

    def test_options_separator_none_collapses_words(self):
        from agents.onboarding import taxonomy_llm
        clusters = [{"canonical": "x", "raw_members": ["X"], "count": 1}]
        fake_out = taxonomy_llm._ProposedTaxonomy(sections=[
            taxonomy_llm._Section(folder="01-Deep Learning", cluster_canonicals=["x"])])
        fake_llm = mock.Mock()
        fake_llm.with_structured_output.return_value.invoke.return_value = fake_out
        _, m = taxonomy_llm.propose_taxonomy(fake_llm, clusters,
            options={"folder_case": "title", "word_separator": "none"})
        self.assertEqual(m["X"], "01-DeepLearning")

    def test_title_casing_preserves_acronyms(self):
        from agents.onboarding import taxonomy_llm
        clusters = [{"canonical": "x", "raw_members": ["X"], "count": 1}]
        fake_out = taxonomy_llm._ProposedTaxonomy(sections=[
            taxonomy_llm._Section(folder="02-Informatique/NLP", cluster_canonicals=["x"])])
        fake_llm = mock.Mock()
        fake_llm.with_structured_output.return_value.invoke.return_value = fake_out
        _, m = taxonomy_llm.propose_taxonomy(fake_llm, clusters)   # défaut title
        self.assertEqual(m["X"], "02-Informatique/NLP")            # NLP non altéré

    def test_options_max_depth_3_keeps_three_levels(self):
        from agents.onboarding import taxonomy_llm
        clusters = [{"canonical": "x", "raw_members": ["X"], "count": 3}]
        fake_out = taxonomy_llm._ProposedTaxonomy(sections=[
            taxonomy_llm._Section(folder="01-SCIENCES/Informatique/Reseaux",
                                  cluster_canonicals=["x"])])
        fake_llm = mock.Mock()
        fake_llm.with_structured_output.return_value.invoke.return_value = fake_out
        tree, mapping = taxonomy_llm.propose_taxonomy(fake_llm, clusters, options={"max_depth": 3})
        self.assertIn("01-SCIENCES/Informatique/Reseaux", tree)   # 3 niveaux conservés
        self.assertIn("01-SCIENCES/Informatique", tree)           # parent implicite
        self.assertEqual(mapping["X"], "01-SCIENCES/Informatique/Reseaux")

    def test_options_default_depth_2_truncates(self):
        from agents.onboarding import taxonomy_llm
        clusters = [{"canonical": "x", "raw_members": ["X"], "count": 3}]
        fake_out = taxonomy_llm._ProposedTaxonomy(sections=[
            taxonomy_llm._Section(folder="01-SCIENCES/Informatique/Reseaux",
                                  cluster_canonicals=["x"])])
        fake_llm = mock.Mock()
        fake_llm.with_structured_output.return_value.invoke.return_value = fake_out
        tree, mapping = taxonomy_llm.propose_taxonomy(fake_llm, clusters)   # défaut max_depth=2
        self.assertIn("01-SCIENCES/Informatique", tree)
        self.assertNotIn("01-SCIENCES/Informatique/Reseaux", tree)         # tronqué
        self.assertEqual(mapping["X"], "01-SCIENCES/Informatique")

    def test_build_system_reflects_options(self):
        from agents.onboarding import taxonomy_llm
        s = taxonomy_llm._build_system(taxonomy_llm._normalize_options(
            {"folder_case": "upper", "folder_language": "fr", "min_depth": 1,
             "max_depth": 2, "granularity": "compact", "word_separator": "_"}))
        self.assertIn("MAJUSCULES", s)
        self.assertIn("FRANÇAIS", s.upper())
        self.assertIn("PEU", s)                    # granularité compacte
        self.assertIn("underscore", s.lower())     # séparateur _
        self.assertIn("entre 1 et 2", s)           # fourchette de profondeur
        s2 = taxonomy_llm._build_system(taxonomy_llm._normalize_options(
            {"folder_case": "lower", "min_depth": 3, "max_depth": 3,
             "granularity": "detailed", "numbered_sections": False}))
        self.assertIn("minuscules", s2)
        self.assertIn("EXACTEMENT 3", s2)          # min == max
        self.assertIn("FINES", s2)
        self.assertIn("pas de préfixe", s2.lower())

    def test_normalize_options_clamps_invalid(self):
        from agents.onboarding import taxonomy_llm
        o = taxonomy_llm._normalize_options(
            {"min_depth": 3, "max_depth": 2, "folder_case": "zz",
             "word_separator": "x", "folder_language": "zz", "granularity": "huge"})
        self.assertEqual(o["max_depth"], 2)
        self.assertEqual(o["min_depth"], 2)            # min > max → ramené à max
        self.assertEqual(o["folder_case"], "title")
        self.assertEqual(o["word_separator"], "-")
        self.assertEqual(o["folder_language"], "auto")
        self.assertEqual(o["granularity"], "auto")
        self.assertTrue(o["numbered_sections"])


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
            import re as _re
            canons = _re.findall(r"canonical='([^']*)'", messages[-1]["content"])
            return taxonomy_llm._ProposedTaxonomy(sections=[
                taxonomy_llm._Section(folder="02-INFORMATIQUE/Deep-Learning",
                                      cluster_canonicals=canons)])

        fake_llm = mock.Mock()
        fake_llm.with_structured_output.return_value.invoke.side_effect = fake_invoke
        seen = []
        with mock.patch("agents.onboarding.proposition.analyze_cover",
                        side_effect=lambda path, **kw: vis), \
             mock.patch("agents.onboarding.proposition.get_agent_llm", return_value=fake_llm), \
             mock.patch("agents.onboarding.proposition.propose_keywords_for_new_folders",
                        return_value=[{"chemin": "02-INFORMATIQUE/Deep-Learning",
                                       "groupe": "informatique", "priorite": 5,
                                       "mots_cles": ["neural"]}]):
            cov = proposition.build_proposal("perso", lambda d, t: seen.append((d, t)))
        # Vision a peuplé le cache
        cache = json.loads((self.prof / ".cache" / "vision_cache.json").read_text())
        self.assertEqual(len(cache), 1)
        # les 3 YAMLs reflètent la proposition de bout en bout
        tree = yaml.safe_load((self.prof / "tree.yaml").read_text())
        self.assertIn("02-INFORMATIQUE/Deep-Learning", tree["folders"])
        self.assertIn("02-INFORMATIQUE", tree["folders"])      # parent implicite
        self.assertIn("_A-TRIER", tree["folders"])
        mapping = yaml.safe_load((self.prof / "theme_mapping.yaml").read_text())
        self.assertEqual(mapping.get("Deep Learning"), "02-INFORMATIQUE/Deep-Learning")
        cats = yaml.safe_load((self.prof / "categories.yaml").read_text())
        self.assertIn("informatique", cats)
        self.assertIn("coverage", cov)
        self.assertIn("stats", cov)
        self.assertTrue(seen)                                  # on_progress appelé

    def test_build_proposal_warns_on_total_vision_failure(self):
        # Vision échoue sur tous les fichiers → build_proposal renvoie un warning
        # explicite (au lieu d'une taxonomie vide présentée comme un succès).
        from agents.onboarding import proposition, taxonomy_llm
        fake_llm = mock.Mock()
        fake_llm.with_structured_output.return_value.invoke.return_value = \
            taxonomy_llm._ProposedTaxonomy(sections=[])
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

    def test_build_proposal_passes_onboarding_options(self):
        # Les options stockées dans profile.yaml descendent jusqu'à propose_taxonomy.
        from agents.onboarding import proposition
        pj = self.prof / "profile.yaml"
        cfg = yaml.safe_load(pj.read_text())
        cfg["onboarding_options"] = {"max_depth": 3, "folder_language": "en"}
        pj.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        captured = {}

        def fake_propose(llm, clusters, options=None):
            captured["options"] = options
            return ["_A-TRIER"], {}

        with mock.patch("agents.onboarding.proposition.analyze_cover",
                        side_effect=lambda p, **k: {"theme": "X",
                            "themes": [{"theme": "X", "confidence": 0.9}], "confidence": 0.9}), \
             mock.patch("agents.onboarding.proposition.get_agent_llm", return_value=mock.Mock()), \
             mock.patch("agents.onboarding.proposition.propose_taxonomy", side_effect=fake_propose):
            proposition.build_proposal("perso", lambda d, t: None)
        self.assertEqual(captured["options"], {"max_depth": 3, "folder_language": "en"})


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


if __name__ == "__main__":
    unittest.main()
