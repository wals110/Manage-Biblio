#!/usr/bin/env python3
"""Tests for lib/wordcheck.py — word validation for filenames."""

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from lib.wordcheck import _is_acronym_or_name, _is_known_word, contains_real_words, word_score


class TestIsKnownWord(unittest.TestCase):
    """Test individual word recognition."""

    def test_english_common(self):
        for word in ["algorithm", "programming", "computer", "science", "network"]:
            self.assertTrue(_is_known_word(word), f"{word} should be known")

    def test_french_common(self):
        for word in ["informatique", "réseau", "bibliothèque", "algorithme"]:
            self.assertTrue(_is_known_word(word), f"{word} should be known")

    def test_tech_whitelist(self):
        for word in ["kubernetes", "tensorflow", "docker", "graphql", "mongodb"]:
            self.assertTrue(_is_known_word(word), f"{word} should be in tech whitelist")

    def test_gibberish_rejected(self):
        for word in ["fh", "itei", "xyzklm", "bxcg", "gyxj", "qwrt"]:
            self.assertFalse(_is_known_word(word), f"{word} should not be known")

    def test_case_insensitive(self):
        self.assertTrue(_is_known_word("Python"))
        self.assertTrue(_is_known_word("PYTHON"))
        self.assertTrue(_is_known_word("python"))


class TestIsAcronymOrName(unittest.TestCase):
    """Test acronym and proper name detection."""

    def test_acronyms(self):
        for word in ["API", "SQL", "LLM", "PDF", "CPU", "GPU"]:
            self.assertTrue(_is_acronym_or_name(word), f"{word} should be acronym")

    def test_proper_names(self):
        for word in ["Einstein", "LaMothe", "Microsoft"]:
            self.assertTrue(_is_acronym_or_name(word), f"{word} should be name")

    def test_camel_case(self):
        for word in ["JavaScript", "PowerShell", "OpenCV"]:
            self.assertTrue(_is_acronym_or_name(word), f"{word} should be CamelCase")

    def test_lowercase_not_name(self):
        self.assertFalse(_is_acronym_or_name("fh"))
        self.assertFalse(_is_acronym_or_name("itei"))

    def test_long_uppercase_not_acronym(self):
        # 7+ chars all uppercase is not a typical acronym
        self.assertFalse(_is_acronym_or_name("XYZKLMN"))


class TestContainsRealWords(unittest.TestCase):
    """Test full filename validation."""

    # ── Gibberish must be rejected ──
    def test_gibberish_rejected(self):
        self.assertFalse(contains_real_words("fh&itei"))

    def test_gibberish_multiple_words(self):
        self.assertFalse(contains_real_words("bxcg laud gyxj"))

    def test_numeric_code_rejected(self):
        self.assertFalse(contains_real_words("00 0672318350 fm 05"))

    def test_short_gibberish(self):
        self.assertFalse(contains_real_words("xyzklm"))

    def test_empty_rejected(self):
        self.assertFalse(contains_real_words(""))

    def test_only_short_words(self):
        self.assertFalse(contains_real_words("ab cd ef"))

    # ── Real titles must be accepted ──
    def test_single_word_title(self):
        self.assertTrue(contains_real_words("Algorithms"))

    def test_multi_word_title(self):
        self.assertTrue(contains_real_words("Python Programming"))

    def test_long_title(self):
        self.assertTrue(contains_real_words("TRICKS OF THE 3D GAME PROGRAMMING GURUS"))

    def test_title_with_tech_terms(self):
        self.assertTrue(contains_real_words("Machine Learning with TensorFlow"))

    def test_title_with_framework(self):
        self.assertTrue(contains_real_words("Kubernetes in Action"))

    def test_acronym_title(self):
        self.assertTrue(contains_real_words("MCAD MCSD NET"))

    def test_proper_names(self):
        self.assertTrue(contains_real_words("Einstein"))
        self.assertTrue(contains_real_words("LaMothe"))

    def test_mixed_language(self):
        self.assertTrue(contains_real_words("Microsoft Combat Flight Simulator"))

    def test_french_title(self):
        self.assertTrue(contains_real_words("Introduction à l'informatique"))

    def test_j2ee_title(self):
        self.assertTrue(contains_real_words("J2EE Developers Handbook"))


class TestWordScore(unittest.TestCase):
    """Test word quality scoring."""

    def test_gibberish_zero(self):
        self.assertEqual(word_score("xyzklm"), 0.0)

    def test_real_title_high(self):
        self.assertGreater(word_score("Python Programming"), 0.9)

    def test_mixed_partial(self):
        score = word_score("bxcg laud gyxj")
        self.assertLess(score, 0.5)

    def test_empty_zero(self):
        self.assertEqual(word_score(""), 0.0)

    def test_higher_score_is_better(self):
        good = word_score("Machine Learning Algorithms")
        bad = word_score("fh itei bxcg")
        self.assertGreater(good, bad)


class TestIntegrationWithRenamer(unittest.TestCase):
    """Test that is_name_clean uses wordcheck correctly."""

    def test_gibberish_not_clean(self):
        from lib.renamer import is_name_clean
        self.assertFalse(is_name_clean("'fh&itei.pdf"))

    def test_real_title_clean(self):
        from lib.renamer import is_name_clean
        self.assertTrue(is_name_clean("Algorithms.pdf"))
        self.assertTrue(is_name_clean("Python Programming.pdf"))

    def test_long_real_title_clean(self):
        from lib.renamer import is_name_clean
        self.assertTrue(is_name_clean("Machine Learning with TensorFlow.pdf"))

    def test_numeric_id_not_clean(self):
        from lib.renamer import is_name_clean
        self.assertFalse(is_name_clean("2738119042.pdf"))


class TestNamePatterns(unittest.TestCase):
    """Test name_patterns validation in is_name_clean."""

    PATTERNS = [
        r"^[A-ZÀ-Ÿ].+ - [A-ZÀ-Ÿ].+$",   # Titre - Auteur
        r"^[A-ZÀ-Ÿ][A-Za-zÀ-ÿ ]{4,}$",    # Titre seul capitalisé
    ]

    def test_no_patterns_retrocompatible(self):
        """Sans patterns, seul le wordcheck décide."""
        from lib.renamer import is_name_clean
        self.assertTrue(is_name_clean("Python Programming.pdf"))
        self.assertTrue(is_name_clean("Python Programming.pdf", name_patterns=[]))

    def test_titre_auteur_matches(self):
        from lib.renamer import is_name_clean
        self.assertTrue(is_name_clean(
            "Introduction to Algorithms - Thomas Cormen.pdf", self.PATTERNS))

    def test_titre_seul_matches(self):
        from lib.renamer import is_name_clean
        self.assertTrue(is_name_clean("Algorithms.pdf", self.PATTERNS))
        self.assertTrue(is_name_clean("Python Programming.pdf", self.PATTERNS))

    def test_scan_artifact_rejected(self):
        """Artefact de scan : vrais mots mais pas au bon format."""
        from lib.renamer import is_name_clean
        self.assertFalse(is_name_clean(
            "00 0672318350 fm 05•02•2003 2 31 PM Page i.pdf", self.PATTERNS))

    def test_lowercase_rejected(self):
        """Nom tout en minuscules ne matche aucun pattern."""
        from lib.renamer import is_name_clean
        self.assertFalse(is_name_clean(
            "introduction to algorithms.pdf", self.PATTERNS))

    def test_gibberish_still_rejected(self):
        """Gibberish rejeté par wordcheck avant même les patterns."""
        from lib.renamer import is_name_clean
        self.assertFalse(is_name_clean("'fh&itei.pdf", self.PATTERNS))

    def test_invalid_regex_does_not_crash(self):
        """Regex invalide dans les patterns ne crashe pas."""
        from lib.renamer import is_name_clean
        bad_patterns = ["[", r"^[A-Z].+ - [A-Z].+$"]
        # Le premier pattern est invalide, le second valide → doit matcher
        self.assertTrue(is_name_clean(
            "Clean Code - Robert Martin.pdf", bad_patterns))

    def test_no_match_returns_false(self):
        """Fichier avec vrais mots mais aucun pattern ne matche → dirty."""
        from lib.renamer import is_name_clean
        # Pattern très restrictif qui ne matche pas ce fichier
        strict = [r"^EXACTMATCH$"]
        self.assertFalse(is_name_clean(
            "Python Programming.pdf", strict))

    def test_pattern_with_accents(self):
        from lib.renamer import is_name_clean
        self.assertTrue(is_name_clean(
            "Économie Politique.pdf", self.PATTERNS))
        self.assertTrue(is_name_clean(
            "Économie Politique - François Dupont.pdf", self.PATTERNS))


if __name__ == "__main__":
    unittest.main()
