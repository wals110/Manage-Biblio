"""Tests pour lib/pattern_detector.py — détection de patterns de nommage."""

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from lib.pattern_detector import (
    PatternResult,
    _build_prompt,
    _parse_response,
    detect_pattern,
    test_coverage,
)


VALID_JSON = json.dumps({
    "regex": r"^[A-ZÀ-Ÿ].+ - [A-ZÀ-Ÿ].+$",
    "description": "Titre - Auteur capitalisés",
    "confidence": 0.92,
    "examples_matched": ["Algorithms - Cormen", "Clean Code - Martin", "Deep Learning - Goodfellow"],
    "examples_rejected": ["12345.pdf", "random_file", "abc"],
})


class TestBuildPrompt(unittest.TestCase):
    """Tests pour _build_prompt."""

    def test_all_stems_in_prompt(self):
        stems = ["Introduction to Algorithms", "Clean Code", "Deep Learning"]
        prompt = _build_prompt(stems)
        for s in stems:
            self.assertIn(s, prompt)

    def test_prompt_asks_for_json(self):
        prompt = _build_prompt(["test"])
        self.assertIn("JSON", prompt)
        self.assertIn("regex", prompt)


class TestParseResponse(unittest.TestCase):
    """Tests pour _parse_response."""

    def test_valid_json(self):
        result = _parse_response(VALID_JSON)
        self.assertIsNotNone(result)
        self.assertIn("regex", result)
        self.assertEqual(result["confidence"], 0.92)

    def test_markdown_fenced_json(self):
        fenced = f"```json\n{VALID_JSON}\n```"
        result = _parse_response(fenced)
        self.assertIsNotNone(result)
        self.assertIn("regex", result)

    def test_json_with_surrounding_text(self):
        text = f"Voici le pattern :\n{VALID_JSON}\nJ'espère que cela aide."
        result = _parse_response(text)
        self.assertIsNotNone(result)

    def test_invalid_json(self):
        self.assertIsNone(_parse_response("pas du json"))

    def test_empty_input(self):
        self.assertIsNone(_parse_response(""))
        self.assertIsNone(_parse_response(None))


class TestDetectPattern(unittest.TestCase):
    """Tests pour detect_pattern."""

    @patch("lib.pattern_detector.LLMClient")
    def test_returns_pattern_result(self, mock_cls):
        mock_client = MagicMock()
        mock_client.call.return_value = VALID_JSON
        mock_cls.return_value = mock_client

        result = detect_pattern(
            ["Algo - Cormen.pdf", "Code - Martin.pdf"],
            "key", "endpoint", "model"
        )
        self.assertIsInstance(result, PatternResult)
        self.assertEqual(result.confidence, 0.92)
        self.assertIn("Titre", result.description)

    @patch("lib.pattern_detector.LLMClient")
    def test_regex_is_valid(self, mock_cls):
        mock_client = MagicMock()
        mock_client.call.return_value = VALID_JSON
        mock_cls.return_value = mock_client

        result = detect_pattern(["a.pdf", "b.pdf"], "k", "e", "m")
        # La regex doit compiler sans erreur
        re.compile(result.regex)

    @patch("lib.pattern_detector.LLMClient")
    def test_strips_pdf_extension(self, mock_cls):
        """Le prompt doit contenir les stems sans .pdf."""
        mock_client = MagicMock()
        mock_client.call.return_value = VALID_JSON
        mock_cls.return_value = mock_client

        detect_pattern(["Test File.pdf", "Other.pdf"], "k", "e", "m")
        call_args = mock_client.call.call_args
        prompt = call_args[1].get("prompt") or call_args[0][0]
        self.assertIn("Test File", prompt)
        # Les stems ne doivent pas contenir .pdf (le texte du prompt peut)
        self.assertNotIn("  - Test File.pdf", prompt)
        self.assertNotIn("  - Other.pdf", prompt)

    @patch("lib.pattern_detector.LLMClient")
    def test_llm_returns_none(self, mock_cls):
        mock_client = MagicMock()
        mock_client.call.return_value = None
        mock_cls.return_value = mock_client

        result = detect_pattern(["a.pdf", "b.pdf"], "k", "e", "m")
        self.assertIsNone(result)

    @patch("lib.pattern_detector.LLMClient")
    def test_invalid_json_from_llm(self, mock_cls):
        mock_client = MagicMock()
        mock_client.call.return_value = "ceci n'est pas du JSON"
        mock_cls.return_value = mock_client

        result = detect_pattern(["a.pdf", "b.pdf"], "k", "e", "m")
        self.assertIsNone(result)

    @patch("lib.pattern_detector.LLMClient")
    def test_invalid_regex_from_llm(self, mock_cls):
        """Regex invalide du LLM → retourne None."""
        mock_client = MagicMock()
        bad_json = json.dumps({
            "regex": "[",  # regex invalide
            "description": "test",
            "confidence": 0.5,
            "examples_matched": [],
            "examples_rejected": [],
        })
        mock_client.call.return_value = bad_json
        mock_cls.return_value = mock_client

        result = detect_pattern(["a.pdf", "b.pdf"], "k", "e", "m")
        self.assertIsNone(result)

    def test_too_few_files(self):
        """Moins de 2 fichiers → retourne None."""
        result = detect_pattern(["seul.pdf"], "k", "e", "m")
        self.assertIsNone(result)

    @patch("lib.pattern_detector.LLMClient")
    def test_markdown_fences_handled(self, mock_cls):
        mock_client = MagicMock()
        mock_client.call.return_value = f"```json\n{VALID_JSON}\n```"
        mock_cls.return_value = mock_client

        result = detect_pattern(["a.pdf", "b.pdf"], "k", "e", "m")
        self.assertIsNotNone(result)
        self.assertEqual(result.confidence, 0.92)


class TestTestCoverage(unittest.TestCase):
    """Tests pour test_coverage."""

    def _make_result(self, regex: str = r"^[A-Z].+ - [A-Z].+$") -> PatternResult:
        return PatternResult(
            regex=regex,
            description="test",
            confidence=0.9,
            examples_matched=[],
            examples_rejected=[],
        )

    def test_coverage_count_correct(self):
        result = self._make_result()
        filenames = [
            "Algorithms - Cormen.pdf",
            "Clean Code - Martin.pdf",
            "random_file.pdf",
            "12345.pdf",
        ]
        test_coverage(result, filenames)
        self.assertEqual(result.coverage_count, 2)
        self.assertEqual(result.coverage_total, 4)

    def test_false_positives_capped(self):
        result = self._make_result(r".*")  # Matche tout
        filenames = [f"File_{i}.pdf" for i in range(100)]
        test_coverage(result, filenames, input_stems=["File_0"], max_false_positive_samples=5)
        self.assertLessEqual(len(result.false_positive_samples), 5)

    def test_input_not_in_false_positives(self):
        result = self._make_result()
        input_stems = ["Algorithms - Cormen"]
        filenames = ["Algorithms - Cormen.pdf", "Other - Author.pdf"]
        test_coverage(result, filenames, input_stems=input_stems)
        # "Algorithms - Cormen" ne doit pas être dans les faux positifs
        self.assertNotIn("Algorithms - Cormen", result.false_positive_samples)

    def test_no_files_zero_coverage(self):
        result = self._make_result()
        test_coverage(result, [])
        self.assertEqual(result.coverage_count, 0)
        self.assertEqual(result.coverage_total, 0)


class TestInjectPattern(unittest.TestCase):
    """Tests pour l'injection de pattern dans profile.yaml."""

    def test_append_to_existing_patterns(self):
        """Ajoute un pattern à une liste existante."""
        # Import ici pour éviter les imports circulaires en contexte test
        from commands.detect import _inject_pattern

        with tempfile.TemporaryDirectory() as tmp:
            yaml_path = Path(tmp) / "profile.yaml"
            yaml_path.write_text(
                "name: test\n"
                "rename:\n"
                '  name_patterns:\n'
                '    - "^[A-Z].+$"\n'
                "defaults:\n"
                "  workers: 5\n",
                encoding="utf-8",
            )
            result = PatternResult(
                regex=r"^[A-Z].+ - [A-Z].+$",
                description="Titre - Auteur",
                confidence=0.9,
                examples_matched=[], examples_rejected=[],
            )
            _inject_pattern(result, Path(tmp))
            content = yaml_path.read_text(encoding="utf-8")
            self.assertIn(r'^[A-Z].+ - [A-Z].+$', content)
            self.assertIn(r'^[A-Z].+$', content)  # L'ancien est toujours là

    def test_creates_rename_key_if_absent(self):
        from commands.detect import _inject_pattern

        with tempfile.TemporaryDirectory() as tmp:
            yaml_path = Path(tmp) / "profile.yaml"
            yaml_path.write_text(
                "name: test\n"
                "defaults:\n"
                "  workers: 5\n",
                encoding="utf-8",
            )
            result = PatternResult(
                regex=r"^Test$",
                description="test",
                confidence=0.5,
                examples_matched=[], examples_rejected=[],
            )
            _inject_pattern(result, Path(tmp))
            content = yaml_path.read_text(encoding="utf-8")
            self.assertIn("name_patterns:", content)
            self.assertIn("^Test$", content)

    def test_no_duplicate_injection(self):
        from commands.detect import _inject_pattern

        with tempfile.TemporaryDirectory() as tmp:
            yaml_path = Path(tmp) / "profile.yaml"
            yaml_path.write_text(
                "name: test\n"
                "rename:\n"
                '  name_patterns:\n'
                '    - "^[A-Z].+$"\n',
                encoding="utf-8",
            )
            result = PatternResult(
                regex=r"^[A-Z].+$",
                description="déjà là",
                confidence=0.9,
                examples_matched=[], examples_rejected=[],
            )
            _inject_pattern(result, Path(tmp))
            content = yaml_path.read_text(encoding="utf-8")
            # Le pattern ne doit apparaître qu'une fois
            self.assertEqual(content.count("^[A-Z].+$"), 1)


if __name__ == "__main__":
    unittest.main()
