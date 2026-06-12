"""
Tests pour LLMMapper.resolve_batch — résolution de N thèmes en 1 appel LLM.

Aucun appel réseau réel : `_call_llm` est mocké partout.
"""

import unittest
from unittest import mock

from lib.llm_mapper import LLMMapper

FOLDERS = [
    "01-SCIENCES/CHIMIE",
    "01-SCIENCES/MATHS/Algebre",
    "01-SCIENCES/PHYSIQUE",
    "02-INFORMATIQUE/05-IA-ML",
]


def make_mapper():
    return LLMMapper(folders=list(FOLDERS), api_key="x",
                     endpoint="https://example.test/v1/chat/completions",
                     model="test-model")


class TestResolveBatch(unittest.TestCase):

    def test_two_themes_one_call(self):
        """(a) 2 thèmes → 1 appel _call_llm, dict avec les 2 folders validés."""
        resp = ('[{"theme":"Colloid Science","folder":"01-SCIENCES/CHIMIE","confidence":0.9},'
                '{"theme":"Group Theory","folder":"01-SCIENCES/MATHS/Algebre","confidence":0.95}]')
        mapper = make_mapper()
        with mock.patch.object(LLMMapper, "_call_llm", return_value=resp) as m:
            result = mapper.resolve_batch(["Colloid Science", "Group Theory"])
        self.assertEqual(m.call_count, 1)
        self.assertEqual(result["Colloid Science"]["folder"], "01-SCIENCES/CHIMIE")
        self.assertEqual(result["Group Theory"]["folder"], "01-SCIENCES/MATHS/Algebre")
        self.assertAlmostEqual(result["Colloid Science"]["confidence"], 0.9)

    def test_invalid_folder_ignored(self):
        """(b) un folder inexistant → ce thème est ignoré."""
        resp = ('[{"theme":"Colloid Science","folder":"01-SCIENCES/CHIMIE","confidence":0.9},'
                '{"theme":"Unknown","folder":"99-INEXISTANT/NOPE","confidence":0.95}]')
        mapper = make_mapper()
        with mock.patch.object(LLMMapper, "_call_llm", return_value=resp):
            result = mapper.resolve_batch(["Colloid Science", "Unknown"])
        self.assertIn("Colloid Science", result)
        self.assertNotIn("Unknown", result)

    def test_low_confidence_ignored(self):
        """(c) confidence < seuil → ignoré."""
        resp = ('[{"theme":"Colloid Science","folder":"01-SCIENCES/CHIMIE","confidence":0.9},'
                '{"theme":"Maybe","folder":"01-SCIENCES/PHYSIQUE","confidence":0.3}]')
        mapper = make_mapper()
        with mock.patch.object(LLMMapper, "_call_llm", return_value=resp):
            result = mapper.resolve_batch(["Colloid Science", "Maybe"])
        self.assertIn("Colloid Science", result)
        self.assertNotIn("Maybe", result)

    def test_chunking_three_calls(self):
        """(d) 90 thèmes, chunk_size=40 → 3 appels _call_llm."""
        themes = ["Theme {}".format(i) for i in range(90)]
        mapper = make_mapper()
        with mock.patch.object(LLMMapper, "_call_llm", return_value="[]") as m:
            mapper.resolve_batch(themes, chunk_size=40)
        self.assertEqual(m.call_count, 3)

    def test_unparsable_chunk_does_not_break_others(self):
        """(e) un chunk inparsable n'empêche pas les autres."""
        good = '[{"theme":"Theme 0","folder":"01-SCIENCES/CHIMIE","confidence":0.9}]'
        bad = "ceci n'est pas du JSON { du tout ["
        good2 = '[{"theme":"Theme 1","folder":"01-SCIENCES/PHYSIQUE","confidence":0.9}]'
        # 3 chunks de 1 thème chacun → 3 appels
        themes = ["Theme 0", "Theme 1bad", "Theme 1"]
        mapper = make_mapper()
        with mock.patch.object(LLMMapper, "_call_llm", side_effect=[good, bad, good2]):
            result = mapper.resolve_batch(themes, chunk_size=1)
        self.assertEqual(result.get("Theme 0", {}).get("folder"), "01-SCIENCES/CHIMIE")
        self.assertEqual(result.get("Theme 1", {}).get("folder"), "01-SCIENCES/PHYSIQUE")
        self.assertNotIn("Theme 1bad", result)

    def test_case_insensitive_theme_match(self):
        """(f) LLM renvoie le thème en minuscule → matché, clé = casse d'origine."""
        resp = '[{"theme":"colloid science","folder":"01-SCIENCES/CHIMIE","confidence":0.9}]'
        mapper = make_mapper()
        with mock.patch.object(LLMMapper, "_call_llm", return_value=resp):
            result = mapper.resolve_batch(["Colloid Science"])
        self.assertIn("Colloid Science", result)
        self.assertNotIn("colloid science", result)
        self.assertEqual(result["Colloid Science"]["folder"], "01-SCIENCES/CHIMIE")

    def test_titles_hint_passed_to_prompt(self):
        """Le titre d'indice est interpolé dans le prompt batch."""
        resp = '[{"theme":"Colloid Science","folder":"01-SCIENCES/CHIMIE","confidence":0.9}]'
        captured = {}

        def fake_call(self, prompt, *a, **k):
            captured["prompt"] = prompt
            return resp

        with mock.patch.object(LLMMapper, "_call_llm", new=fake_call):
            mapper = make_mapper()
            mapper.resolve_batch(["Colloid Science"],
                                 titles={"Colloid Science": "Introduction to Colloids"})
        self.assertIn("Introduction to Colloids", captured["prompt"])

    def test_no_folder_marker_ignored(self):
        """Le marqueur _AUCUN → thème non résolu."""
        resp = '[{"theme":"Random","folder":"_AUCUN","confidence":0.9}]'
        mapper = make_mapper()
        with mock.patch.object(LLMMapper, "_call_llm", return_value=resp):
            result = mapper.resolve_batch(["Random"])
        self.assertEqual(result, {})

    def test_empty_themes(self):
        """Liste vide → dict vide, aucun appel."""
        mapper = make_mapper()
        with mock.patch.object(LLMMapper, "_call_llm", return_value="[]") as m:
            result = mapper.resolve_batch([])
        self.assertEqual(result, {})
        self.assertEqual(m.call_count, 0)


if __name__ == "__main__":
    unittest.main()
