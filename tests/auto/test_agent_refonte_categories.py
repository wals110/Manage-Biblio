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


if __name__ == "__main__":
    unittest.main()
