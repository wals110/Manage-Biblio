"""
Client LLM unifié pour Klodo.

Centralise tous les appels HTTP vers les APIs LLM (SiliconFlow, Ollama, etc.) :
  - Construction des headers (Authorization conditionnelle)
  - Retry avec backoff exponentiel sur rate limit (429)
  - Retry avec délai fixe sur erreurs transitoires
  - Parsing de la réponse (choices[0].message.content)
  - Logging cohérent

Usage :
    from lib.llm_client import LLMClient

    client = LLMClient(
        api_key="sk-xxx",
        endpoint="https://api.siliconflow.com/v1/chat/completions",
        model="Qwen/Qwen3-VL-32B-Instruct",
    )

    # Appel texte
    content = client.call(prompt="Quel est le thème de ce livre ?")

    # Appel multimodal (vision)
    content = client.call(
        prompt="Analyse cette couverture.",
        images_b64=["base64..."],
        max_tokens=300,
    )

Python 3.9 compatible.
"""

import time
from typing import Optional, List, Dict

from lib.logger import get_logger

log = get_logger()

try:
    import requests as req_lib
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


class LLMClient:
    """
    Client HTTP unifié pour les APIs LLM compatibles OpenAI.

    Gère le retry, le backoff, le logging et le parsing de manière cohérente
    pour tous les modules de Klodo (vision, mapper, refiner).
    """

    def __init__(self, api_key, endpoint, model,
                 timeout=30, max_retries=3, verbose=False):
        # type: (str, str, str, int, int, bool) -> None
        self.api_key = api_key
        self.endpoint = endpoint
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.verbose = verbose

    def call(self, prompt, images_b64=None,
             max_tokens=150, temperature=0.1,
             timeout=None, max_retries=None):
        # type: (str, Optional[List[str]], int, float, Optional[int], Optional[int]) -> Optional[str]
        """
        Appel LLM unifié (texte ou multimodal).

        Args:
            prompt: Le texte du prompt à envoyer.
            images_b64: Liste de strings base64 JPEG pour les appels vision.
                        Si None, appel texte pur.
            max_tokens: Nombre max de tokens en sortie (défaut 150).
            temperature: Température du modèle (défaut 0.1).
            timeout: Override du timeout pour cet appel (sinon self.timeout).
            max_retries: Override du nombre de retries (sinon self.max_retries).

        Returns:
            Le contenu textuel de la réponse (stripped), ou None en cas d'échec.
        """
        if not HAS_REQUESTS:
            log.warning("  ⚠ requests non installé (pip install requests)")
            return None

        effective_timeout = timeout if timeout is not None else self.timeout
        effective_retries = max_retries if max_retries is not None else self.max_retries

        headers = self._build_headers()
        messages = self._build_messages(prompt, images_b64)
        payload = self._build_payload(messages, max_tokens, temperature)

        return self._send_with_retry(
            payload, headers, effective_timeout, effective_retries)

    def call_messages(self, messages, max_tokens=150, temperature=0.1,
                      timeout=None, max_retries=None):
        # type: (List[Dict], int, float, Optional[int], Optional[int]) -> Optional[str]
        """
        Appel LLM avec un payload messages pré-construit.

        Utile quand l'appelant construit lui-même le format multimodal
        (ex: refiner vision avec prompt spécifique).

        Args:
            messages: Liste de messages au format OpenAI.
            max_tokens: Nombre max de tokens en sortie.
            temperature: Température du modèle.
            timeout: Override du timeout.
            max_retries: Override du nombre de retries.

        Returns:
            Le contenu textuel de la réponse, ou None.
        """
        if not HAS_REQUESTS:
            log.warning("  ⚠ requests non installé (pip install requests)")
            return None

        effective_timeout = timeout if timeout is not None else self.timeout
        effective_retries = max_retries if max_retries is not None else self.max_retries

        headers = self._build_headers()
        payload = self._build_payload(messages, max_tokens, temperature)

        return self._send_with_retry(
            payload, headers, effective_timeout, effective_retries)

    # ── Construction du payload ─────────────────────────────────────────

    def _build_headers(self):
        # type: () -> Dict[str, str]
        """Construit les headers HTTP. Authorization ajoutée si api_key fournie."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer {}".format(self.api_key)
        return headers

    def _build_messages(self, prompt, images_b64=None):
        # type: (str, Optional[List[str]]) -> List[Dict]
        """Construit le tableau messages (texte pur ou multimodal)."""
        if images_b64:
            content_parts = []  # type: List
            for img_b64 in images_b64:
                content_parts.append({
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/jpeg;base64,{}".format(img_b64),
                    },
                })
            content_parts.append({"type": "text", "text": prompt})
            return [{"role": "user", "content": content_parts}]
        else:
            return [{"role": "user", "content": prompt}]

    def _build_payload(self, messages, max_tokens, temperature):
        # type: (List[Dict], int, float) -> Dict
        """Construit le payload JSON complet."""
        return {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

    # ── Envoi avec retry ────────────────────────────────────────────────

    def _send_with_retry(self, payload, headers, timeout, max_retries):
        # type: (Dict, Dict[str, str], int, int) -> Optional[str]
        """
        Envoie la requête HTTP avec retry et backoff.

        Stratégie :
          - 429 (rate limit) : backoff exponentiel min(2^attempt * 2, 30)
          - Autres erreurs HTTP : retry après 2s
          - Timeout / exceptions : retry après 2s
          - Après max_retries tentatives, retourne None.
        """
        for attempt in range(max_retries):
            try:
                resp = req_lib.post(
                    self.endpoint,
                    headers=headers,
                    json=payload,
                    timeout=timeout,
                )

                if resp.status_code == 429:
                    wait = min(2 ** attempt * 2, 30)
                    if self.verbose:
                        log.info("  ⏳ Rate limit, attente {}s...".format(wait))
                    time.sleep(wait)
                    continue

                if resp.status_code != 200:
                    if self.verbose:
                        log.error("  ⚠ API erreur {}: {}".format(
                            resp.status_code, resp.text[:200]))
                    if attempt < max_retries - 1:
                        time.sleep(2)
                        continue
                    return None

                data = resp.json()
                return data['choices'][0]['message']['content'].strip()

            except req_lib.exceptions.Timeout:
                if self.verbose:
                    log.info("  ⏳ Timeout (tentative {}/{})".format(
                        attempt + 1, max_retries))
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                return None

            except Exception as e:
                if self.verbose:
                    log.warning("  ⚠ LLM exception: {}".format(e))
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                return None

        return None
