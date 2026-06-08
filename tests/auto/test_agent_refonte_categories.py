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


if __name__ == "__main__":
    unittest.main()
