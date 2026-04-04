"""
Tests pour lib/llm_client.py — Client LLM unifié.

Couvre : succès, retry 429, retry erreur serveur, timeout,
échec total, appel vision, call_messages, header Authorization,
connection pooling via requests.Session.
"""

import unittest
from unittest.mock import patch, MagicMock

from lib.llm_client import LLMClient


def _make_response(status_code=200, content='{"result": "ok"}'):
    """Crée un mock de réponse HTTP."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = content
    resp.json.return_value = {
        'choices': [{'message': {'content': content}}]
    }
    return resp


def _setup_mock_req(mock_req):
    """Configure mock_req pour que Session() retourne un mock avec .post = mock_req.post.

    Cela permet aux tests de continuer à vérifier mock_req.post tout en passant
    par la session (connection pooling).
    """
    mock_session = MagicMock()
    mock_session.post = mock_req.post
    mock_session.headers = {}
    mock_req.Session.return_value = mock_session
    mock_req.exceptions.Timeout = type('Timeout', (Exception,), {})
    return mock_session


class TestLLMClientSuccess(unittest.TestCase):
    """Appels réussis."""

    @patch('lib.llm_client.req_lib')
    def test_call_text_success(self, mock_req):
        """Appel texte 200 → retourne le contenu."""
        mock_session = _setup_mock_req(mock_req)
        mock_req.post.return_value = _make_response(200, 'Réponse LLM')

        client = LLMClient('sk-test', 'http://api.test/v1', 'model-1')
        result = client.call('Quel est le thème ?')

        self.assertEqual(result, 'Réponse LLM')
        mock_req.post.assert_called_once()

        # Vérifier le payload
        call_args = mock_req.post.call_args
        payload = call_args[1]['json']
        self.assertEqual(payload['model'], 'model-1')
        self.assertEqual(payload['messages'][0]['role'], 'user')
        self.assertEqual(payload['messages'][0]['content'], 'Quel est le thème ?')
        self.assertEqual(payload['max_tokens'], 150)
        self.assertEqual(payload['temperature'], 0.1)

    @patch('lib.llm_client.req_lib')
    def test_call_vision_success(self, mock_req):
        """Appel vision avec images → payload multimodal correct."""
        _setup_mock_req(mock_req)
        mock_req.post.return_value = _make_response(200, '{"title": "Test"}')

        client = LLMClient('sk-test', 'http://api.test/v1', 'model-1')
        result = client.call('Analyse cette couverture', images_b64=['base64data'])

        self.assertEqual(result, '{"title": "Test"}')

        payload = mock_req.post.call_args[1]['json']
        content_parts = payload['messages'][0]['content']
        # Premier élément = image, deuxième = texte
        self.assertEqual(content_parts[0]['type'], 'image_url')
        self.assertIn('base64data', content_parts[0]['image_url']['url'])
        self.assertEqual(content_parts[1]['type'], 'text')
        self.assertEqual(content_parts[1]['text'], 'Analyse cette couverture')

    @patch('lib.llm_client.req_lib')
    def test_call_vision_multi_images(self, mock_req):
        """Appel vision avec plusieurs images → toutes présentes dans le payload."""
        _setup_mock_req(mock_req)
        mock_req.post.return_value = _make_response(200, 'ok')

        client = LLMClient('sk-test', 'http://api.test/v1', 'model-1')
        client.call('Prompt', images_b64=['img1', 'img2', 'img3'])

        content_parts = mock_req.post.call_args[1]['json']['messages'][0]['content']
        # 3 images + 1 texte
        self.assertEqual(len(content_parts), 4)
        self.assertEqual(content_parts[0]['type'], 'image_url')
        self.assertEqual(content_parts[1]['type'], 'image_url')
        self.assertEqual(content_parts[2]['type'], 'image_url')
        self.assertEqual(content_parts[3]['type'], 'text')

    @patch('lib.llm_client.req_lib')
    def test_call_messages_success(self, mock_req):
        """call_messages avec payload pré-construit."""
        _setup_mock_req(mock_req)
        mock_req.post.return_value = _make_response(200, 'réponse')

        client = LLMClient('sk-test', 'http://api.test/v1', 'model-1')
        messages = [{"role": "user", "content": "test"}]
        result = client.call_messages(messages, max_tokens=300)

        self.assertEqual(result, 'réponse')
        payload = mock_req.post.call_args[1]['json']
        self.assertEqual(payload['max_tokens'], 300)
        self.assertEqual(payload['messages'], messages)

    @patch('lib.llm_client.req_lib')
    def test_custom_max_tokens_temperature(self, mock_req):
        """max_tokens et temperature overridés par appel."""
        _setup_mock_req(mock_req)
        mock_req.post.return_value = _make_response(200, 'ok')

        client = LLMClient('sk-test', 'http://api.test/v1', 'model-1')
        client.call('test', max_tokens=500, temperature=0.8)

        payload = mock_req.post.call_args[1]['json']
        self.assertEqual(payload['max_tokens'], 500)
        self.assertEqual(payload['temperature'], 0.8)


class TestLLMClientHeaders(unittest.TestCase):
    """Construction des headers HTTP via Session."""

    @patch('lib.llm_client.req_lib')
    def test_auth_header_with_api_key(self, mock_req):
        """Clé API fournie → header Authorization sur la session."""
        mock_session = _setup_mock_req(mock_req)
        mock_req.post.return_value = _make_response()

        client = LLMClient('sk-mykey', 'http://api.test/v1', 'model-1')
        client.call('test')

        # Les headers sont mis sur la session, pas passés à chaque post()
        self.assertEqual(mock_session.headers['Authorization'], 'Bearer sk-mykey')
        self.assertEqual(mock_session.headers['Content-Type'], 'application/json')

    @patch('lib.llm_client.req_lib')
    def test_no_auth_header_without_api_key(self, mock_req):
        """Pas de clé API → pas de header Authorization (Ollama local)."""
        mock_session = _setup_mock_req(mock_req)
        mock_req.post.return_value = _make_response()

        client = LLMClient('', 'http://localhost:11434/v1', 'model-1')
        client.call('test')

        self.assertNotIn('Authorization', mock_session.headers)

    @patch('lib.llm_client.req_lib')
    def test_no_auth_header_none_api_key(self, mock_req):
        """api_key=None → pas de header Authorization."""
        mock_session = _setup_mock_req(mock_req)
        mock_req.post.return_value = _make_response()

        client = LLMClient(None, 'http://localhost:11434/v1', 'model-1')
        client.call('test')

        self.assertNotIn('Authorization', mock_session.headers)


class TestLLMClientSession(unittest.TestCase):
    """Connection pooling via requests.Session."""

    @patch('lib.llm_client.req_lib')
    def test_session_reused_across_calls(self, mock_req):
        """La même session est réutilisée pour plusieurs appels."""
        _setup_mock_req(mock_req)
        mock_req.post.return_value = _make_response(200, 'ok')

        client = LLMClient('sk-test', 'http://api.test/v1', 'model-1')
        client.call('premier appel')
        client.call('deuxième appel')

        # Session créée une seule fois
        mock_req.Session.assert_called_once()
        # Deux appels POST sur la même session
        self.assertEqual(mock_req.post.call_count, 2)

    @patch('lib.llm_client.req_lib')
    def test_session_created_lazily(self, mock_req):
        """La session n'est pas créée au constructeur, seulement au premier appel."""
        _setup_mock_req(mock_req)

        client = LLMClient('sk-test', 'http://api.test/v1', 'model-1')
        mock_req.Session.assert_not_called()

        mock_req.post.return_value = _make_response(200, 'ok')
        client.call('test')
        mock_req.Session.assert_called_once()


class TestLLMClientRetry429(unittest.TestCase):
    """Rate limit 429 → backoff exponentiel → retry."""

    @patch('lib.llm_client.time')
    @patch('lib.llm_client.req_lib')
    def test_429_then_success(self, mock_req, mock_time):
        """429 au premier appel → backoff → succès au deuxième."""
        _setup_mock_req(mock_req)
        resp_429 = _make_response(429)
        resp_200 = _make_response(200, 'succès')
        mock_req.post.side_effect = [resp_429, resp_200]

        client = LLMClient('sk-test', 'http://api.test/v1', 'model-1')
        result = client.call('test')

        self.assertEqual(result, 'succès')
        self.assertEqual(mock_req.post.call_count, 2)
        # Backoff : 2^0 * 2 = 2 secondes
        mock_time.sleep.assert_called_with(2)

    @patch('lib.llm_client.time')
    @patch('lib.llm_client.req_lib')
    def test_429_backoff_escalation(self, mock_req, mock_time):
        """429 trois fois → backoff 2s, 4s, puis None."""
        _setup_mock_req(mock_req)
        resp_429 = _make_response(429)
        mock_req.post.return_value = resp_429

        client = LLMClient('sk-test', 'http://api.test/v1', 'model-1',
                           max_retries=3)
        result = client.call('test')

        self.assertIsNone(result)
        self.assertEqual(mock_req.post.call_count, 3)
        # Backoff : 2s, 4s, 8s
        calls = [c[0][0] for c in mock_time.sleep.call_args_list]
        self.assertEqual(calls, [2, 4, 8])

    @patch('lib.llm_client.time')
    @patch('lib.llm_client.req_lib')
    def test_429_backoff_capped_at_30(self, mock_req, mock_time):
        """Le backoff est plafonné à 30 secondes."""
        _setup_mock_req(mock_req)
        resp_429 = _make_response(429)
        mock_req.post.return_value = resp_429

        client = LLMClient('sk-test', 'http://api.test/v1', 'model-1',
                           max_retries=6)
        client.call('test')

        calls = [c[0][0] for c in mock_time.sleep.call_args_list]
        # 2, 4, 8, 16, 30, 30 (plafonné)
        self.assertEqual(calls, [2, 4, 8, 16, 30, 30])


class TestLLMClientRetryErrors(unittest.TestCase):
    """Erreurs serveur et timeout → retry avec délai fixe."""

    @patch('lib.llm_client.time')
    @patch('lib.llm_client.req_lib')
    def test_500_then_success(self, mock_req, mock_time):
        """Erreur 500 → retry 2s → succès."""
        _setup_mock_req(mock_req)
        resp_500 = _make_response(500, 'Internal Server Error')
        resp_200 = _make_response(200, 'ok')
        mock_req.post.side_effect = [resp_500, resp_200]

        client = LLMClient('sk-test', 'http://api.test/v1', 'model-1')
        result = client.call('test')

        self.assertEqual(result, 'ok')
        self.assertEqual(mock_req.post.call_count, 2)
        mock_time.sleep.assert_called_with(2)

    @patch('lib.llm_client.time')
    @patch('lib.llm_client.req_lib')
    def test_timeout_then_success(self, mock_req, mock_time):
        """Timeout → retry 2s → succès."""
        mock_session = _setup_mock_req(mock_req)
        mock_req.post.side_effect = [
            mock_req.exceptions.Timeout('timeout'),
            _make_response(200, 'ok'),
        ]

        client = LLMClient('sk-test', 'http://api.test/v1', 'model-1')
        result = client.call('test')

        self.assertEqual(result, 'ok')
        self.assertEqual(mock_req.post.call_count, 2)
        mock_time.sleep.assert_called_with(2)

    @patch('lib.llm_client.time')
    @patch('lib.llm_client.req_lib')
    def test_generic_exception_then_success(self, mock_req, mock_time):
        """Exception réseau → retry 2s → succès."""
        _setup_mock_req(mock_req)
        mock_req.post.side_effect = [
            ConnectionError('network down'),
            _make_response(200, 'ok'),
        ]

        client = LLMClient('sk-test', 'http://api.test/v1', 'model-1')
        result = client.call('test')

        self.assertEqual(result, 'ok')
        mock_time.sleep.assert_called_with(2)

    @patch('lib.llm_client.time')
    @patch('lib.llm_client.req_lib')
    def test_all_retries_exhausted(self, mock_req, mock_time):
        """3 erreurs consécutives → None."""
        _setup_mock_req(mock_req)
        resp_500 = _make_response(500, 'error')
        mock_req.post.return_value = resp_500

        client = LLMClient('sk-test', 'http://api.test/v1', 'model-1',
                           max_retries=3)
        result = client.call('test')

        self.assertIsNone(result)
        self.assertEqual(mock_req.post.call_count, 3)

    @patch('lib.llm_client.time')
    @patch('lib.llm_client.req_lib')
    def test_mixed_errors_then_success(self, mock_req, mock_time):
        """429 → timeout → 200 → succès."""
        _setup_mock_req(mock_req)
        mock_req.post.side_effect = [
            _make_response(429),
            mock_req.exceptions.Timeout('timeout'),
            _make_response(200, 'enfin'),
        ]

        client = LLMClient('sk-test', 'http://api.test/v1', 'model-1')
        result = client.call('test')

        self.assertEqual(result, 'enfin')
        self.assertEqual(mock_req.post.call_count, 3)


class TestLLMClientOverrides(unittest.TestCase):
    """Override de timeout et max_retries par appel."""

    @patch('lib.llm_client.req_lib')
    def test_timeout_override(self, mock_req):
        """timeout passé à call() override celui du constructeur."""
        _setup_mock_req(mock_req)
        mock_req.post.return_value = _make_response(200, 'ok')

        client = LLMClient('sk-test', 'http://api.test/v1', 'model-1',
                           timeout=30)
        client.call('test', timeout=10)

        self.assertEqual(mock_req.post.call_args[1]['timeout'], 10)

    @patch('lib.llm_client.req_lib')
    def test_default_timeout_used(self, mock_req):
        """Sans override, le timeout du constructeur est utilisé."""
        _setup_mock_req(mock_req)
        mock_req.post.return_value = _make_response(200, 'ok')

        client = LLMClient('sk-test', 'http://api.test/v1', 'model-1',
                           timeout=45)
        client.call('test')

        self.assertEqual(mock_req.post.call_args[1]['timeout'], 45)

    @patch('lib.llm_client.time')
    @patch('lib.llm_client.req_lib')
    def test_max_retries_override(self, mock_req, mock_time):
        """max_retries passé à call() override celui du constructeur."""
        _setup_mock_req(mock_req)
        mock_req.post.return_value = _make_response(500, 'error')

        client = LLMClient('sk-test', 'http://api.test/v1', 'model-1',
                           max_retries=3)
        client.call('test', max_retries=1)

        # Un seul appel, pas de retry
        self.assertEqual(mock_req.post.call_count, 1)


class TestLLMClientEdgeCases(unittest.TestCase):
    """Cas limites."""

    @patch('lib.llm_client.HAS_REQUESTS', False)
    def test_no_requests_library(self):
        """Sans la lib requests → retourne None."""
        client = LLMClient('sk-test', 'http://api.test/v1', 'model-1')
        result = client.call('test')
        self.assertIsNone(result)

    @patch('lib.llm_client.HAS_REQUESTS', False)
    def test_no_requests_library_call_messages(self):
        """Sans la lib requests → call_messages retourne None."""
        client = LLMClient('sk-test', 'http://api.test/v1', 'model-1')
        result = client.call_messages([{"role": "user", "content": "test"}])
        self.assertIsNone(result)

    @patch('lib.llm_client.req_lib')
    def test_response_stripped(self, mock_req):
        """Le contenu de la réponse est stripped (espaces, newlines)."""
        _setup_mock_req(mock_req)
        mock_req.post.return_value = _make_response(200, '  résultat\n\n ')

        client = LLMClient('sk-test', 'http://api.test/v1', 'model-1')
        result = client.call('test')

        self.assertEqual(result, 'résultat')


if __name__ == '__main__':
    unittest.main()
