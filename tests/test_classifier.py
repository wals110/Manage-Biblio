#!/usr/bin/env python3
"""Tests pour lib/classifier.py — classify_combined et ses améliorations."""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Ajouter la racine du projet au path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from lib.classifier import classify_by_theme, classify_combined


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════

THEME_MAPPING = {
    'Machine Learning': '02-INFORMATIQUE/05-IA-ML/Machine-Learning',
    'Python': '02-INFORMATIQUE/03-Langages-Programmation/Python',
    'Quantum Physics': '01-SCIENCES/PHYSIQUE/05-Relativite-Quantique',
    'Islam': '05-RELIGIONS/ISLAM',
    'Cooking': '08-LOISIRS/CUISINE',
    'Algebra': '01-SCIENCES/MATHEMATIQUES/01-Algebre',
}


def _make_classifier_mock(results):
    """Crée un mock de KeywordClassifier qui retourne les résultats donnés."""
    mock = MagicMock()
    mock.classify.return_value = results
    return mock


# ═══════════════════════════════════════════════════════════════════════════
# Tests classify_by_theme
# ═══════════════════════════════════════════════════════════════════════════

class TestClassifyByTheme(unittest.TestCase):

    def test_exact_match(self):
        """Match exact insensible à la casse."""
        result = classify_by_theme('machine learning', THEME_MAPPING)
        self.assertEqual(result, '02-INFORMATIQUE/05-IA-ML/Machine-Learning')

    def test_exact_match_case(self):
        """Match exact avec casse originale."""
        result = classify_by_theme('Machine Learning', THEME_MAPPING)
        self.assertEqual(result, '02-INFORMATIQUE/05-IA-ML/Machine-Learning')

    def test_substring_match(self):
        """Le thème contient une clé du mapping (substring)."""
        result = classify_by_theme('Advanced Machine Learning Techniques', THEME_MAPPING)
        self.assertEqual(result, '02-INFORMATIQUE/05-IA-ML/Machine-Learning')

    def test_reverse_substring(self):
        """La clé du mapping contient le thème (reverse substring)."""
        result = classify_by_theme('Quantum', THEME_MAPPING)
        self.assertEqual(result, '01-SCIENCES/PHYSIQUE/05-Relativite-Quantique')

    def test_no_match(self):
        """Aucun match → None."""
        result = classify_by_theme('Woodworking', THEME_MAPPING)
        self.assertIsNone(result)

    def test_empty_theme(self):
        """Thème vide → None."""
        result = classify_by_theme('', THEME_MAPPING)
        self.assertIsNone(result)

    def test_empty_mapping(self):
        """Mapping vide → None."""
        result = classify_by_theme('Python', {})
        self.assertIsNone(result)


# ═══════════════════════════════════════════════════════════════════════════
# Tests classify_combined — titre enrichi
# ═══════════════════════════════════════════════════════════════════════════

class TestClassifyCombinedEnrichedTitle(unittest.TestCase):
    """Vérifie que le titre LLM est passé au keyword classifier."""

    def test_title_used_in_keyword_classification(self):
        """Le titre détecté est inclus dans le texte envoyé au keyword classifier."""
        mock_kw = MagicMock()
        mock_kw.classify.return_value = [
            ('01-SCIENCES/MATHEMATIQUES/01-Algebre', 0.8, 'algebra'),
        ]

        vision = {'theme': 'Unknown', 'confidence': 0.3, 'title': 'Linear Algebra Done Right'}

        result = classify_combined(
            vision, '1234567890.pdf', THEME_MAPPING,
            classifier=mock_kw)

        # Vérifier que le keyword classifier a reçu le titre
        call_args = mock_kw.classify.call_args
        text_sent = call_args[0][0]  # Premier argument positionnel
        self.assertIn('Linear Algebra Done Right', text_sent)
        self.assertIn('1234567890.pdf', text_sent)

    def test_title_plus_theme_in_text(self):
        """Le thème détecté est aussi inclus dans le texte enrichi."""
        mock_kw = MagicMock()
        mock_kw.classify.return_value = [
            ('02-INFORMATIQUE/05-IA-ML/Machine-Learning', 0.7, 'neural'),
        ]

        vision = {
            'theme': 'Neural Networks',
            'confidence': 0.3,
            'title': 'Deep Learning Fundamentals',
        }

        result = classify_combined(
            vision, 'doc.pdf', THEME_MAPPING,
            classifier=mock_kw)

        text_sent = mock_kw.classify.call_args[0][0]
        self.assertIn('Deep Learning Fundamentals', text_sent)
        self.assertIn('Neural Networks', text_sent)
        self.assertIn('doc.pdf', text_sent)

    def test_filename_only_if_no_title(self):
        """Sans titre ni thème, seul le filename est passé."""
        mock_kw = MagicMock()
        mock_kw.classify.return_value = []

        vision = {'theme': '', 'confidence': 0.0, 'title': ''}

        classify_combined(
            vision, 'algebra_book.pdf', THEME_MAPPING,
            classifier=mock_kw)

        text_sent = mock_kw.classify.call_args[0][0]
        self.assertEqual(text_sent.strip(), 'algebra_book.pdf')

    def test_high_confidence_theme_wins_over_keyword(self):
        """Avec confiance >= 0.5 et match theme_mapping, le LLM theme gagne."""
        mock_kw = _make_classifier_mock([
            ('08-LOISIRS/CUISINE', 0.9, 'cooking'),
        ])

        vision = {'theme': 'Python', 'confidence': 0.9, 'title': 'Python Cookbook'}

        path, score, source = classify_combined(
            vision, 'cookbook.pdf', THEME_MAPPING,
            classifier=mock_kw)

        self.assertEqual(path, '02-INFORMATIQUE/03-Langages-Programmation/Python')
        self.assertEqual(source, 'LLM (theme)')


# ═══════════════════════════════════════════════════════════════════════════
# Tests classify_combined — LLM Mapper avec pdf_path
# ═══════════════════════════════════════════════════════════════════════════

class TestClassifyCombinedMapperPdfPath(unittest.TestCase):
    """Vérifie que pdf_path est transmis au LLM Mapper."""

    def test_pdf_path_passed_to_mapper(self):
        """Le pdf_path est transmis au mapper.resolve()."""
        mock_mapper = MagicMock()
        mock_mapper.resolve.return_value = '02-INFORMATIQUE/05-IA-ML/Machine-Learning'

        vision = {'theme': 'Mechatronics', 'confidence': 0.8, 'title': 'Intro Mechatronics'}

        path, score, source = classify_combined(
            vision, 'mechatronics.pdf', THEME_MAPPING,
            llm_mapper=mock_mapper,
            pdf_path='/chemin/vers/mechatronics.pdf')

        # Vérifier que resolve a été appelé avec pdf_path
        mock_mapper.resolve.assert_called_once_with(
            'Mechatronics',
            title='Intro Mechatronics',
            filename='mechatronics.pdf',
            pdf_path='/chemin/vers/mechatronics.pdf',
        )
        self.assertEqual(source, 'LLM (mapper)')

    def test_mapper_not_called_if_theme_matches(self):
        """Si le theme_mapping matche, le mapper n'est pas appelé."""
        mock_mapper = MagicMock()

        vision = {'theme': 'Machine Learning', 'confidence': 0.9, 'title': 'ML Book'}

        classify_combined(
            vision, 'ml.pdf', THEME_MAPPING,
            llm_mapper=mock_mapper)

        mock_mapper.resolve.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# Tests classify_combined — flux complet
# ═══════════════════════════════════════════════════════════════════════════

class TestClassifyCombinedFlow(unittest.TestCase):

    def test_failed_when_nothing_matches(self):
        """Aucun match → FAILED."""
        vision = {'theme': '', 'confidence': 0.0}
        path, score, source = classify_combined(
            vision, 'random.pdf', THEME_MAPPING)
        self.assertIsNone(path)
        self.assertEqual(source, 'FAILED')

    def test_low_confidence_fallback(self):
        """Confiance basse mais thème matche → LLM (fallback)."""
        vision = {'theme': 'Python', 'confidence': 0.3}
        path, score, source = classify_combined(
            vision, 'doc.pdf', THEME_MAPPING)
        self.assertEqual(path, '02-INFORMATIQUE/03-Langages-Programmation/Python')
        self.assertEqual(source, 'LLM (fallback)')
        self.assertAlmostEqual(score, 0.3)

    def test_keyword_fallback_when_theme_not_in_mapping(self):
        """Thème inconnu + confiance basse → keyword classifier."""
        mock_kw = _make_classifier_mock([
            ('05-RELIGIONS/ISLAM', 0.6, 'quran'),
        ])

        vision = {'theme': 'Unknown', 'confidence': 0.3, 'title': 'The Quran Explained'}

        path, score, source = classify_combined(
            vision, '9781234.pdf', THEME_MAPPING,
            classifier=mock_kw)

        self.assertEqual(path, '05-RELIGIONS/ISLAM')
        self.assertIn('Keyword', source)


# ═══════════════════════════════════════════════════════════════════════════
# Tests LLMMapper — vision et escalade
# ═══════════════════════════════════════════════════════════════════════════

class TestLLMMapperVision(unittest.TestCase):
    """Teste l'escalade vision dans le LLM Mapper."""

    def _make_mapper(self, vision=False):
        """Crée un LLMMapper avec des paramètres de test."""
        from lib.llm_mapper import LLMMapper
        return LLMMapper(
            folders=['01-SCIENCES/PHYSIQUE', '02-INFORMATIQUE/05-IA-ML/Machine-Learning'],
            api_key='test-key',
            endpoint='http://test/api',
            model='test-model',
            min_confidence=0.6,
            verbose=False,
            vision=vision,
        )

    def test_vision_flag_stored(self):
        """Le flag vision est stocké correctement."""
        mapper = self._make_mapper(vision=True)
        self.assertTrue(mapper.vision)
        mapper2 = self._make_mapper(vision=False)
        self.assertFalse(mapper2.vision)

    def test_vision_stats_initialized(self):
        """Les compteurs vision sont initialisés à 0."""
        mapper = self._make_mapper()
        self.assertEqual(mapper.vision_calls, 0)
        self.assertEqual(mapper.vision_successes, 0)

    @patch('lib.llm_client.req_lib')
    def test_resolve_without_vision_no_escalade(self, mock_req):
        """Sans vision=True, pas d'escalade même si pdf_path fourni."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'choices': [{'message': {'content': '{"folder": "_AUCUN", "confidence": 0.0, "reason": "unknown"}'}}]
        }
        mock_req.post.return_value = mock_resp

        mapper = self._make_mapper(vision=False)
        # Mock _try_vision_mapper pour vérifier qu'il n'est PAS appelé
        mapper._try_vision_mapper = MagicMock()
        result = mapper.resolve('Unknown Topic', pdf_path='/test.pdf')
        self.assertIsNone(result)
        mapper._try_vision_mapper.assert_not_called()

    @patch('lib.llm_client.req_lib')
    def test_resolve_with_vision_escalade(self, mock_req):
        """Avec vision=True et pdf_path, escalade après échec texte."""
        # Premier appel (texte) → _AUCUN
        text_resp = MagicMock()
        text_resp.status_code = 200
        text_resp.json.return_value = {
            'choices': [{'message': {'content': '{"folder": "_AUCUN", "confidence": 0.0, "reason": "unknown"}'}}]
        }
        mock_req.post.return_value = text_resp

        mapper = self._make_mapper(vision=True)

        # Mock _try_vision_mapper pour retourner un résultat vision
        vision_result = {
            'folder': '01-SCIENCES/PHYSIQUE',
            'confidence': 0.85,
            'reason': 'physics textbook cover',
        }
        mapper._try_vision_mapper = MagicMock(return_value=vision_result)

        result = mapper.resolve('Unknown Physics', pdf_path='/test.pdf')

        self.assertEqual(result, '01-SCIENCES/PHYSIQUE')
        mapper._try_vision_mapper.assert_called_once_with('/test.pdf')
        self.assertEqual(mapper.vision_successes, 1)

    @patch('lib.llm_client.req_lib')
    def test_resolve_vision_also_fails(self, mock_req):
        """Vision activée mais échoue aussi → suggestion générée."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'choices': [{'message': {'content': '{"folder": "_AUCUN", "confidence": 0.0, "reason": "unknown"}'}}]
        }
        mock_req.post.return_value = mock_resp

        mapper = self._make_mapper(vision=True)
        mapper._try_vision_mapper = MagicMock(return_value=None)

        result = mapper.resolve('Totally Unknown', title='Mystery Book',
                                filename='mystery.pdf', pdf_path='/test.pdf')

        self.assertIsNone(result)
        mapper._try_vision_mapper.assert_called_once()

    @patch('lib.llm_client.req_lib')
    def test_resolve_text_success_no_vision(self, mock_req):
        """Si le text mapper réussit, pas d'escalade vision."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'choices': [{'message': {'content': '{"folder": "02-INFORMATIQUE/05-IA-ML/Machine-Learning", "confidence": 0.9, "reason": "ML book"}'}}]
        }
        mock_req.post.return_value = mock_resp

        mapper = self._make_mapper(vision=True)
        mapper._try_vision_mapper = MagicMock()

        result = mapper.resolve('Deep Learning', pdf_path='/test.pdf')

        self.assertEqual(result, '02-INFORMATIQUE/05-IA-ML/Machine-Learning')
        mapper._try_vision_mapper.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# Tests parser CLI — --vision sur classify et process
# ═══════════════════════════════════════════════════════════════════════════

class TestClassifyVisionParser(unittest.TestCase):

    def test_classify_vision_flag(self):
        """--vision est accepté par classify."""
        from klodo import _build_parser
        parser = _build_parser()
        args = parser.parse_args(['classify', '/tmp/test', '--vision'])
        self.assertTrue(args.vision)

    def test_classify_no_vision_default(self):
        """Sans --vision, le flag est False."""
        from klodo import _build_parser
        parser = _build_parser()
        args = parser.parse_args(['classify', '/tmp/test'])
        self.assertFalse(args.vision)

    def test_process_vision_flag(self):
        """--vision est accepté par process."""
        from klodo import _build_parser
        parser = _build_parser()
        args = parser.parse_args(['process', '/tmp/test', '--vision'])
        self.assertTrue(args.vision)


if __name__ == '__main__':
    unittest.main()
