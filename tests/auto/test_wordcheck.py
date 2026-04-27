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


if __name__ == "__main__":
    unittest.main()
