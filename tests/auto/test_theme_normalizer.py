#!/usr/bin/env python3
"""Tests pour lib.theme_normalizer — Phase 1 du chantier dédupli des thèmes.

Cible : variantes orthographiques pures (casse, ponctuation, singulier/pluriel,
parenthèses, accents). On NE doit PAS fusionner des sous-domaines distincts
("Unsupervised Machine Learning" ne doit PAS matcher "Machine Learning" —
c'est le job du LLM judge à venir en Phase 3).

Cas issus du profil default (vision_cache.json réel) — `Machine Learning` 829
occurrences face à `Machine learning` 3 occurrences, etc.
"""

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from lib.theme_normalizer import (  # noqa: E402
    group_by_canonical,
    normalize_theme,
    normalize_themes_batch,
)


class TestNormalizeBasics(unittest.TestCase):
    def test_lowercase(self):
        self.assertEqual(normalize_theme("Machine Learning"), "machine learning")

    def test_already_lowercase(self):
        self.assertEqual(normalize_theme("machine learning"), "machine learning")

    def test_mixed_case_variants_collapse(self):
        # Le cas observé : "Machine Learning" (829) vs "Machine learning" (3)
        self.assertEqual(
            normalize_theme("Machine Learning"),
            normalize_theme("Machine learning"),
        )

    def test_empty_returns_empty(self):
        self.assertEqual(normalize_theme(""), "")
        self.assertEqual(normalize_theme(None), "")

    def test_non_string_input(self):
        self.assertEqual(normalize_theme(42), "")
        self.assertEqual(normalize_theme(["foo"]), "")

    def test_whitespace_only(self):
        self.assertEqual(normalize_theme("   "), "")

    def test_collapses_extra_spaces(self):
        self.assertEqual(normalize_theme("Machine   Learning"), "machine learning")
        self.assertEqual(normalize_theme("  Python  Programming  "), "python programming")


class TestNormalizeAccents(unittest.TestCase):
    def test_accent_stripped_fr(self):
        self.assertEqual(
            normalize_theme("Mathématiques Appliquées"),
            "mathematique appliquee",
        )

    def test_accent_stripped_diverse(self):
        self.assertEqual(normalize_theme("Économétrie"), "econometrie")
        self.assertEqual(normalize_theme("Naïve Bayes"), "naive baye")

    def test_accent_strip_unifies(self):
        self.assertEqual(
            normalize_theme("Économétrie"),
            normalize_theme("Econometrie"),
        )


class TestNormalizeParentheses(unittest.TestCase):
    def test_parenthetical_qualifier_stripped(self):
        # Cas réel : 'Neural Networks (Computer Science)' apparaît avec
        # juste 'Neural Networks' — la précision parenthétique est noise
        # pour la dédupli, on l'enlève intentionnellement.
        self.assertEqual(
            normalize_theme("Neural Networks (Computer Science)"),
            "neural network",
        )
        self.assertEqual(
            normalize_theme("Neural Networks (CS)"),
            "neural network",
        )

    def test_multiple_parens(self):
        self.assertEqual(
            normalize_theme("Algorithm (CS) (advanced)"),
            "algorithm",
        )


class TestNormalizePunctuation(unittest.TestCase):
    def test_strip_punctuation(self):
        self.assertEqual(normalize_theme("C++"), "c")
        self.assertEqual(normalize_theme("Web 2.0"), "web 2 0")

    def test_hyphen_preserved(self):
        # Le tiret est sémantique (e-commerce, multi-agent, fine-tuning)
        self.assertEqual(normalize_theme("E-commerce"), "e-commerce")
        self.assertEqual(normalize_theme("Multi-Agent Systems"), "multi-agent system")

    def test_slash_treated_as_separator(self):
        self.assertEqual(normalize_theme("AI/ML"), "ai ml")


class TestSingularization(unittest.TestCase):
    def test_simple_s_plural(self):
        self.assertEqual(normalize_theme("Networks"), "network")
        self.assertEqual(normalize_theme("Algorithms"), "algorithm")
        self.assertEqual(normalize_theme("Neural Networks"), "neural network")

    def test_ies_to_y(self):
        self.assertEqual(normalize_theme("Technologies"), "technology")
        self.assertEqual(normalize_theme("Companies"), "company")

    def test_ches_shes_strip_es(self):
        self.assertEqual(normalize_theme("Branches"), "branch")
        self.assertEqual(normalize_theme("Bushes"), "bush")

    def test_sses_keeps_ss(self):
        self.assertEqual(normalize_theme("Classes"), "class")
        self.assertEqual(normalize_theme("Processes"), "process")

    def test_false_plural_statistics(self):
        # Cas observé : 'Statistics' (95) — ne PAS le transformer en 'statistic'
        self.assertEqual(normalize_theme("Statistics"), "statistics")
        self.assertEqual(normalize_theme("Mathematics"), "mathematics")
        self.assertEqual(normalize_theme("Physics"), "physics")
        self.assertEqual(normalize_theme("Robotics"), "robotics")
        self.assertEqual(normalize_theme("Linguistics"), "linguistics")

    def test_false_plural_analysis(self):
        # 'Analysis' (≠ 'analyses') — pas de strip du -is
        self.assertEqual(normalize_theme("Analysis"), "analysis")
        self.assertEqual(normalize_theme("Thesis"), "thesis")
        self.assertEqual(normalize_theme("Hypothesis"), "hypothesis")

    def test_irregular_plural(self):
        self.assertEqual(normalize_theme("Children"), "child")
        self.assertEqual(normalize_theme("Criteria"), "criterion")

    def test_short_acronyms_preserved(self):
        # On ne touche pas aux acronymes courts (≤ 3 chars OU dans _KEEP_AS_IS)
        self.assertEqual(normalize_theme("ML"), "ml")
        self.assertEqual(normalize_theme("AI"), "ai")
        self.assertEqual(normalize_theme("OS"), "os")
        self.assertEqual(normalize_theme("NLP"), "nlp")

    def test_us_suffix_preserved(self):
        # 'Bus' ne doit pas devenir 'bu'
        self.assertEqual(normalize_theme("Bus"), "bus")
        self.assertEqual(normalize_theme("Virus"), "virus")


class TestStopwords(unittest.TestCase):
    def test_strips_english_stopwords(self):
        self.assertEqual(normalize_theme("Theory of Computation"), "theory computation")
        self.assertEqual(normalize_theme("Introduction to Algorithms"), "introduction algorithm")

    def test_strips_french_stopwords(self):
        self.assertEqual(normalize_theme("Théorie des Graphes"), "theorie graphe")
        self.assertEqual(normalize_theme("Le Machine Learning"), "machine learning")

    def test_does_not_strip_content_words(self):
        # 'Programming' ne doit pas être stripped
        self.assertEqual(normalize_theme("Functional Programming"), "functional programming")


class TestRealWorldDuplicates(unittest.TestCase):
    """Cas observés dans le profil default — chaque pair doit collapse."""

    def assert_same_canonical(self, a, b):
        ca, cb = normalize_theme(a), normalize_theme(b)
        self.assertEqual(ca, cb, f"{a!r} → {ca!r} != {b!r} → {cb!r}")

    def test_machine_learning_case(self):
        self.assert_same_canonical("Machine Learning", "Machine learning")

    def test_python_programming_case(self):
        self.assert_same_canonical("Python Programming", "Python programming")

    def test_neural_networks_plural(self):
        self.assert_same_canonical("Neural Networks", "Neural Network")

    def test_artificial_neural_networks(self):
        # Variante avec qualifier — ne PAS fusionner avec Neural Networks
        ca = normalize_theme("Neural Networks")
        cb = normalize_theme("Artificial Neural Networks")
        self.assertNotEqual(ca, cb, "Artificial Neural Networks ≠ Neural Networks (sous-domaine)")


class TestSubDomainsNotMerged(unittest.TestCase):
    """Les sous-domaines (ajoutant des mots qualifiants) ne doivent PAS
    fusionner avec le terme parent — c'est le job du LLM judge plus tard."""

    def test_unsupervised_ml(self):
        self.assertNotEqual(
            normalize_theme("Machine Learning"),
            normalize_theme("Unsupervised Machine Learning"),
        )

    def test_deep_learning_specializations(self):
        self.assertNotEqual(
            normalize_theme("Deep Learning"),
            normalize_theme("Deep Learning for Computer Vision"),
        )

    def test_data_science_specializations(self):
        self.assertNotEqual(
            normalize_theme("Data Science"),
            normalize_theme("Spatial Data Science"),
        )


class TestBatchAndGroup(unittest.TestCase):
    def test_normalize_themes_batch(self):
        themes = ["Machine Learning", "Machine learning", "Deep Learning"]
        out = normalize_themes_batch(themes)
        self.assertEqual(out["Machine Learning"], "machine learning")
        self.assertEqual(out["Machine learning"], "machine learning")
        self.assertEqual(out["Deep Learning"], "deep learning")

    def test_batch_skips_empty(self):
        out = normalize_themes_batch(["Foo", "", None, "Bar"])  # type: ignore[list-item]
        self.assertIn("Foo", out)
        self.assertIn("Bar", out)
        self.assertNotIn("", out)
        self.assertNotIn(None, out)

    def test_group_by_canonical(self):
        themes = [
            "Machine Learning", "Machine learning",
            "Neural Networks", "Neural Network",
            "Deep Learning",
        ]
        groups = group_by_canonical(themes)
        self.assertEqual(set(groups.keys()), {"machine learning", "neural network", "deep learning"})
        self.assertEqual(set(groups["machine learning"]), {"Machine Learning", "Machine learning"})
        self.assertEqual(set(groups["neural network"]), {"Neural Networks", "Neural Network"})
        self.assertEqual(groups["deep learning"], ["Deep Learning"])

    def test_group_skips_empty_canonical(self):
        # Un input qui se normalise à "" doit être skip
        groups = group_by_canonical(["   ", "Real Theme"])
        self.assertEqual(list(groups.keys()), ["real theme"])


if __name__ == "__main__":
    unittest.main()
