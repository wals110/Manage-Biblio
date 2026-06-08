#!/usr/bin/env python3
"""Tests pour les helpers de Phase B liés à categories.yaml.

Couvre :
  1. _groupe_from_path_prefix : inférence du groupe depuis le préfixe
  2. _cascade_categories_changes : application déterministe des renames/
     fusions/deletions sur les entries existantes
  3. categories_llm : Pydantic schemas + appel LLM + fallback
"""

import os
import sys
import unittest
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from agents.refonte.proposition_tools import _groupe_from_path_prefix  # noqa: E402


def _new_category_entry_lax(**kwargs):
    """Construit un _NewCategoryEntry sans la validation 'groupe' /
    'chemin' (utile pour simuler le retour d'un LLM qui hallucinerait)."""
    from agents.refonte.categories_llm import _NewCategoryEntry
    return _NewCategoryEntry.model_construct(**kwargs)


class TestGroupeInference(unittest.TestCase):

    def setUp(self):
        self.existing_categories = {
            "informatique": [
                {"chemin": "02-INFORMATIQUE/14-Web", "priorite": 3, "mots_cles": ["html"]},
                {"chemin": "02-INFORMATIQUE/05-IA-ML", "priorite": 2, "mots_cles": ["ml"]},
            ],
            "sciences": [
                {"chemin": "01-SCIENCES/PHYSIQUE", "priorite": 4, "mots_cles": ["physics"]},
            ],
            "bureautique": [
                {"chemin": "09-BUREAU/Excel", "priorite": 5, "mots_cles": ["excel"]},
            ],
        }

    def test_groupe_inference_from_path_prefix(self):
        # New path under 02-INFORMATIQUE → groupe "informatique"
        self.assertEqual(
            _groupe_from_path_prefix("02-INFORMATIQUE/05-IA-ML/RAG", self.existing_categories),
            "informatique",
        )
        # New path under 01-SCIENCES → groupe "sciences"
        self.assertEqual(
            _groupe_from_path_prefix("01-SCIENCES/CHIMIE/04-Materiaux", self.existing_categories),
            "sciences",
        )

    def test_groupe_inference_no_match_falls_back_to_autres(self):
        # No existing entry under 99-UNKNOWN → fallback "autres"
        self.assertEqual(
            _groupe_from_path_prefix("99-UNKNOWN/Whatever", self.existing_categories),
            "autres",
        )


from agents.refonte.proposition_tools import _cascade_categories_changes  # noqa: E402


class TestCascadeCategories(unittest.TestCase):

    def test_cascade_renames_simple_path(self):
        current = {
            "informatique": [
                {"chemin": "02-INFORMATIQUE/14-Web", "priorite": 3,
                 "mots_cles": ["html", "css"]},
            ],
        }
        renamings = [{"old_path": "02-INFORMATIQUE/14-Web",
                      "new_path": "02-INFORMATIQUE/14-Web-Frontend"}]
        new_cats, log = _cascade_categories_changes(
            current, renamings=renamings, fusions=[], deletions=[])
        self.assertEqual(new_cats["informatique"][0]["chemin"],
                         "02-INFORMATIQUE/14-Web-Frontend")
        # Mots_cles + priorite inchangés
        self.assertEqual(new_cats["informatique"][0]["mots_cles"], ["html", "css"])
        self.assertEqual(new_cats["informatique"][0]["priorite"], 3)
        # Log contient l'entry rename
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["type"], "rename")
        self.assertEqual(log[0]["old"], "02-INFORMATIQUE/14-Web")
        self.assertEqual(log[0]["new"], "02-INFORMATIQUE/14-Web-Frontend")
        self.assertEqual(log[0]["n_entries"], 1)

    def test_cascade_renames_prefix_propagation(self):
        current = {
            "informatique": [
                {"chemin": "02-INFORMATIQUE/14-Web/React", "priorite": 4,
                 "mots_cles": ["react", "jsx"]},
                {"chemin": "02-INFORMATIQUE/14-Web/Vue", "priorite": 4,
                 "mots_cles": ["vue", "vuex"]},
            ],
        }
        renamings = [{"old_path": "02-INFORMATIQUE/14-Web",
                      "new_path": "02-INFORMATIQUE/14-Web-Frontend"}]
        new_cats, log = _cascade_categories_changes(
            current, renamings=renamings, fusions=[], deletions=[])
        chemins = [e["chemin"] for e in new_cats["informatique"]]
        self.assertIn("02-INFORMATIQUE/14-Web-Frontend/React", chemins)
        self.assertIn("02-INFORMATIQUE/14-Web-Frontend/Vue", chemins)
        # Log : type rename_prefix pour les 2 entries
        prefix_logs = [le for le in log if le["type"] == "rename_prefix"]
        self.assertEqual(len(prefix_logs), 2)

    def test_cascade_renames_collision_merges_mots_cles(self):
        # L'user avait déjà créé une entry pour le new_path → collision
        current = {
            "informatique": [
                {"chemin": "02-INFORMATIQUE/14-Web", "priorite": 3,
                 "mots_cles": ["html", "css", "JavaScript"]},
                {"chemin": "02-INFORMATIQUE/14-Web-Frontend", "priorite": 5,
                 "mots_cles": ["frontend", "javascript"]},  # collision target
            ],
        }
        renamings = [{"old_path": "02-INFORMATIQUE/14-Web",
                      "new_path": "02-INFORMATIQUE/14-Web-Frontend"}]
        new_cats, log = _cascade_categories_changes(
            current, renamings=renamings, fusions=[], deletions=[])

        # Une seule entry restante (les deux ont fusionné)
        chemins = [e["chemin"] for e in new_cats["informatique"]]
        self.assertEqual(chemins.count("02-INFORMATIQUE/14-Web-Frontend"), 1)
        self.assertNotIn("02-INFORMATIQUE/14-Web", chemins)

        merged = next(e for e in new_cats["informatique"]
                      if e["chemin"] == "02-INFORMATIQUE/14-Web-Frontend")
        # mots_cles : dedup case-insensitive (javascript == JavaScript)
        self.assertEqual(
            sorted([k.lower() for k in merged["mots_cles"]]),
            sorted(["html", "css", "javascript", "frontend"]),
        )
        # priorite = min(3, 5) = 3
        self.assertEqual(merged["priorite"], 3)
        # Log mentionne la collision
        rename_log = next(le for le in log if le["type"] == "rename")
        self.assertEqual(rename_log.get("n_collisions", 0), 1)

    def test_cascade_fusion_sources_merge_into_target(self):
        current = {
            "bureautique": [
                {"chemin": "09-BUREAU/Excel", "priorite": 5,
                 "mots_cles": ["excel", "xlsx"]},
                {"chemin": "09-BUREAU/Microsoft-Excel", "priorite": 3,
                 "mots_cles": ["microsoft excel"]},
            ],
        }
        fusions = [{"sources": ["09-BUREAU/Excel"],
                    "target": "09-BUREAU/Microsoft-Excel"}]
        new_cats, log = _cascade_categories_changes(
            current, renamings=[], fusions=fusions, deletions=[])

        chemins = [e["chemin"] for e in new_cats["bureautique"]]
        self.assertEqual(chemins, ["09-BUREAU/Microsoft-Excel"])
        merged = new_cats["bureautique"][0]
        self.assertEqual(
            sorted([k.lower() for k in merged["mots_cles"]]),
            sorted(["microsoft excel", "excel", "xlsx"]),
        )
        self.assertEqual(merged["priorite"], 3)
        fusion_log = next(le for le in log if le["type"] == "fusion")
        self.assertEqual(fusion_log["new"], "09-BUREAU/Microsoft-Excel")

    def test_cascade_fusion_target_does_not_preexist(self):
        # Target absent → fusion crée l'entry à partir des sources
        current = {
            "bureautique": [
                {"chemin": "09-BUREAU/Excel", "priorite": 5,
                 "mots_cles": ["excel"]},
                {"chemin": "09-BUREAU/Calc", "priorite": 6,
                 "mots_cles": ["libreoffice calc"]},
            ],
        }
        fusions = [{"sources": ["09-BUREAU/Excel", "09-BUREAU/Calc"],
                    "target": "09-BUREAU/Tableurs"}]
        new_cats, log = _cascade_categories_changes(
            current, renamings=[], fusions=fusions, deletions=[])
        chemins = [e["chemin"] for e in new_cats["bureautique"]]
        self.assertEqual(chemins, ["09-BUREAU/Tableurs"])
        merged = new_cats["bureautique"][0]
        self.assertEqual(merged["priorite"], 5)  # min(5, 6)
        self.assertIn("excel", [k.lower() for k in merged["mots_cles"]])
        self.assertIn("libreoffice calc", [k.lower() for k in merged["mots_cles"]])

    def test_cascade_deletion_drops_entries(self):
        current = {
            "informatique": [
                {"chemin": "02-INFORMATIQUE/14-Web", "priorite": 3,
                 "mots_cles": ["html"]},
                {"chemin": "02-INFORMATIQUE/05-IA-ML", "priorite": 2,
                 "mots_cles": ["ml"]},
            ],
        }
        deletions = [{"path": "02-INFORMATIQUE/14-Web"}]
        new_cats, log = _cascade_categories_changes(
            current, renamings=[], fusions=[], deletions=deletions)

        chemins = [e["chemin"] for e in new_cats["informatique"]]
        self.assertEqual(chemins, ["02-INFORMATIQUE/05-IA-ML"])
        del_log = next(le for le in log if le["type"] == "deletion")
        self.assertEqual(del_log["old"], "02-INFORMATIQUE/14-Web")
        self.assertEqual(del_log["n_entries"], 1)

    def test_cascade_no_changes_no_modifications(self):
        # Aucun rename/fusion/deletion → categories inchangée + log vide
        current = {
            "informatique": [
                {"chemin": "02-INFORMATIQUE/14-Web", "priorite": 3,
                 "mots_cles": ["html"]},
            ],
        }
        new_cats, log = _cascade_categories_changes(
            current, renamings=[], fusions=[], deletions=[])
        self.assertEqual(new_cats, current)
        self.assertEqual(log, [])


class TestCategoriesLLM(unittest.TestCase):

    def test_propose_keywords_zero_creations_no_call(self):
        from agents.refonte.categories_llm import propose_keywords_for_new_folders
        mock_llm = mock.MagicMock()
        result = propose_keywords_for_new_folders(
            llm=mock_llm,
            creations=[],
            existing_groupes=["informatique"],
            groupe_inference={},
            sample_entries={},
        )
        self.assertEqual(result, [])
        mock_llm.with_structured_output.assert_not_called()

    def test_propose_keywords_pydantic_min_max_mots_cles(self):
        from agents.refonte.categories_llm import _NewCategoryEntry
        # min 3 mots_cles
        with self.assertRaises(Exception):
            _NewCategoryEntry(chemin="X/Y", groupe="informatique",
                              priorite=5, mots_cles=["a", "b"])
        # max 15 mots_cles
        with self.assertRaises(Exception):
            _NewCategoryEntry(chemin="X/Y", groupe="informatique",
                              priorite=5, mots_cles=[f"k{i}" for i in range(16)])
        # OK in range
        entry = _NewCategoryEntry(chemin="X/Y", groupe="informatique",
                                  priorite=5, mots_cles=["a", "b", "c"])
        self.assertEqual(len(entry.mots_cles), 3)

    def test_propose_keywords_pydantic_groupe_validation(self):
        """Le LLM retourne un groupe inconnu → drop entry + fallback."""
        from agents.refonte.categories_llm import (
            _NewCategoriesProposal,
            propose_keywords_for_new_folders,
        )
        # Mock LLM retourne un groupe non listé dans existing_groupes
        fake_proposal = _NewCategoriesProposal(entries=[
            _new_category_entry_lax(chemin="02-INFO/RAG", groupe="INVALID",
                                    priorite=5, mots_cles=["a", "b", "c"]),
        ])
        mock_llm = mock.MagicMock()
        mock_llm.with_structured_output.return_value.invoke.return_value = fake_proposal

        result = propose_keywords_for_new_folders(
            llm=mock_llm,
            creations=[{"path": "02-INFO/RAG", "rationale": "test"}],
            existing_groupes=["informatique", "sciences"],
            groupe_inference={"02-INFO/RAG": "informatique"},
            sample_entries={"informatique": []},
        )
        # Groupe invalide → fallback entry vide
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["mots_cles"], [])
        self.assertEqual(result[0]["chemin"], "02-INFO/RAG")
        # Fallback utilise le groupe inféré, pas celui du LLM
        self.assertEqual(result[0]["groupe"], "informatique")

    def test_propose_keywords_pydantic_chemin_mismatch_rejected(self):
        """Le LLM retourne un chemin différent des créations → refus."""
        from agents.refonte.categories_llm import (
            _NewCategoriesProposal,
            propose_keywords_for_new_folders,
        )
        fake_proposal = _NewCategoriesProposal(entries=[
            _new_category_entry_lax(chemin="02-INFO/HALLUCINATED",
                                    groupe="informatique", priorite=5,
                                    mots_cles=["a", "b", "c"]),
        ])
        mock_llm = mock.MagicMock()
        mock_llm.with_structured_output.return_value.invoke.return_value = fake_proposal

        result = propose_keywords_for_new_folders(
            llm=mock_llm,
            creations=[{"path": "02-INFO/RAG", "rationale": "test"}],
            existing_groupes=["informatique"],
            groupe_inference={"02-INFO/RAG": "informatique"},
            sample_entries={"informatique": []},
        )
        # Le LLM a renvoyé HALLUCINATED, la création était RAG → fallback
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["chemin"], "02-INFO/RAG")
        self.assertEqual(result[0]["mots_cles"], [])

    def test_propose_keywords_llm_error_fallback(self):
        """LLM raise une exception → entries vides retournées."""
        from agents.refonte.categories_llm import propose_keywords_for_new_folders
        mock_llm = mock.MagicMock()
        mock_llm.with_structured_output.return_value.invoke.side_effect = \
            RuntimeError("rate limit")
        result = propose_keywords_for_new_folders(
            llm=mock_llm,
            creations=[{"path": "X/Y", "rationale": "test"}],
            existing_groupes=["informatique"],
            groupe_inference={"X/Y": "informatique"},
            sample_entries={"informatique": []},
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["mots_cles"], [])
        self.assertEqual(result[0]["priorite"], 99)
        self.assertEqual(result[0]["groupe"], "informatique")

    def test_propose_keywords_retry_on_validation_fail(self):
        """Premier call retourne du n'importe quoi (1 entry hallucinée),
        retry réussit avec une entry valide."""
        from agents.refonte.categories_llm import (
            _NewCategoriesProposal,
            propose_keywords_for_new_folders,
        )
        bad = _NewCategoriesProposal(entries=[
            _new_category_entry_lax(chemin="WRONG/PATH", groupe="informatique",
                                    priorite=5, mots_cles=["a", "b", "c"]),
        ])
        good = _NewCategoriesProposal(entries=[
            _new_category_entry_lax(chemin="X/Y", groupe="informatique",
                                    priorite=5,
                                    mots_cles=["alpha", "beta", "gamma"]),
        ])
        mock_llm = mock.MagicMock()
        mock_llm.with_structured_output.return_value.invoke.side_effect = [bad, good]

        result = propose_keywords_for_new_folders(
            llm=mock_llm,
            creations=[{"path": "X/Y", "rationale": "test"}],
            existing_groupes=["informatique"],
            groupe_inference={"X/Y": "informatique"},
            sample_entries={"informatique": []},
        )
        # 2 calls effectués (1er hallucination + retry)
        self.assertEqual(
            mock_llm.with_structured_output.return_value.invoke.call_count, 2)
        self.assertEqual(result[0]["chemin"], "X/Y")
        self.assertEqual(result[0]["mots_cles"], ["alpha", "beta", "gamma"])


class TestMergeCategoriesChanges(unittest.TestCase):

    def test_merge_categories_intermediate_plus_new(self):
        from agents.refonte.proposition_tools import _merge_categories_changes
        intermediate = {
            "informatique": [
                {"chemin": "02-INFO/Web", "priorite": 3,
                 "mots_cles": ["html"]},
            ],
            "sciences": [],
        }
        new_entries = [
            {"chemin": "02-INFO/RAG", "groupe": "informatique",
             "priorite": 5, "mots_cles": ["retrieval", "embedding", "vector"]},
            {"chemin": "01-SCIENCES/CHIMIE/Materiaux", "groupe": "sciences",
             "priorite": 6, "mots_cles": ["alloy", "polymer", "ceramic"]},
        ]
        merged = _merge_categories_changes(intermediate, new_entries)
        chemins_info = [e["chemin"] for e in merged["informatique"]]
        chemins_sci = [e["chemin"] for e in merged["sciences"]]
        self.assertIn("02-INFO/Web", chemins_info)
        self.assertIn("02-INFO/RAG", chemins_info)
        self.assertIn("01-SCIENCES/CHIMIE/Materiaux", chemins_sci)


class TestRenderRationale(unittest.TestCase):

    def test_render_rationale_categories_section(self):
        from agents.refonte.proposition_tools import _render_categories_section
        cascade_log = [
            {"type": "rename", "old": "02-INFO/Web",
             "new": "02-INFO/Web-Frontend", "n_entries": 1, "n_collisions": 0},
            {"type": "fusion", "old": ["09-BUREAU/Excel"],
             "new": "09-BUREAU/Microsoft-Excel", "n_entries": 1},
            {"type": "deletion", "old": "02-INFO/Vieux", "n_entries": 1},
        ]
        new_entries = [
            {"chemin": "02-INFO/RAG", "groupe": "informatique",
             "priorite": 5, "mots_cles": ["retrieval", "embedding", "vector"]},
        ]
        md = _render_categories_section(cascade_log, new_entries)
        self.assertIn("## CATÉGORIES", md)
        self.assertIn("### Cascades automatiques", md)
        self.assertIn("rename", md)
        self.assertIn("02-INFO/Web", md)
        self.assertIn("02-INFO/Web-Frontend", md)
        self.assertIn("fusion", md)
        self.assertIn("deletion", md)
        self.assertIn("### Nouveaux folders", md)
        self.assertIn("02-INFO/RAG", md)
        self.assertIn("retrieval", md)


if __name__ == "__main__":
    unittest.main()
