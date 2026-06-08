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

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from agents.refonte.proposition_tools import _groupe_from_path_prefix  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
