"""Factory LLM partagée entre tous les agents Klodo.

Par défaut, pointe sur SiliconFlow avec GLM-4.7 (stable, tool-use propre,
suit bien les prompts FR — testé 2026-05-25 face à des `APIConnectionError`
récurrents sur DeepSeek-V3.2 côté SiliconFlow).

Le modèle est surchargeable via la variable d'env `KLODO_AGENT_MODEL`
(par ex. pour repasser sur DeepSeek-V3.2 quand l'instabilité côté
SiliconFlow sera résorbée).

L'endpoint est compatible OpenAI — `langchain_openai.ChatOpenAI` est
le client adéquat (pas besoin de wrapper custom).
"""

from __future__ import annotations

import os

from langchain_openai import ChatOpenAI

DEFAULT_MODEL = "zai-org/GLM-4.7"  # stable + tool-use FR (2026-05-25)
# Choix de modèle (mis à jour 2026-05-25 après plusieurs erreurs DeepSeek) :
#   - GLM-4.7 (défaut courant) — stable, tool-use FR propre, ~9s/réponse libre,
#     ~2s en tool-call. Pas d'APIConnectionError observée.
#   - GLM-5 / GLM-5.1 : alternatives plus récentes, OK en tool-use, à utiliser
#     si GLM-4.7 régresse.
#   - DeepSeek-V3.2 : tool-use excellent quand ça marche mais
#     `APIConnectionError` récurrent côté SiliconFlow en mai 2026 (cf. run
#     47377f35 — 7 LLM calls puis erreur).
#   - DeepSeek-V3.2-Exp : désactivé sans préavis sur SiliconFlow (piège -Exp).
#   - DeepSeek-V3.1 : régresse vers le chinois sur prompts multi-tour
#     complexes (a produit un problème d'algo chinois au lieu du rapport).
# Test rapide en cas de doute : `curl https://api.siliconflow.com/v1/models`
# pour voir la liste live, puis vérifier que le modèle suit un prompt FR.
DEFAULT_BASE_URL = "https://api.siliconflow.com/v1"
DEFAULT_TIMEOUT_S = 180  # 3 min par appel LLM — write_report peut prendre 60-120s, marge pour SiliconFlow lent


def get_agent_llm(
    model: str | None = None,
    *,
    temperature: float = 0.1,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: float | None = None,
) -> ChatOpenAI:
    """Construit un ChatOpenAI configuré pour SiliconFlow.

    Args:
        model: ID du modèle (ex. "zai-org/GLM-4.7"). Si None,
               lit `KLODO_AGENT_MODEL` puis fallback sur DEFAULT_MODEL.
        temperature: Default 0.1 — bas pour raisonnement structuré + consistance
                     entre runs (à 0.2 on observait une variance importante de
                     verbosité entre 2 runs successifs sur le même profil).
        base_url: Override pour tester un autre endpoint compatible OpenAI.
                  Default = SiliconFlow.
        api_key: Override pour tests. Default = `$SILICONFLOW_API_KEY`.

    Returns:
        ChatOpenAI prêt à `bind_tools(tools).invoke(messages)`.

    Raises:
        RuntimeError: Si aucune clé API disponible.
    """
    resolved_model = model or os.environ.get("KLODO_AGENT_MODEL", DEFAULT_MODEL)
    resolved_base = base_url or DEFAULT_BASE_URL
    resolved_key = api_key or os.environ.get("SILICONFLOW_API_KEY")
    if not resolved_key:
        raise RuntimeError(
            "SILICONFLOW_API_KEY env var is required for agent LLM "
            "(or pass api_key= explicitly)."
        )
    return ChatOpenAI(
        model=resolved_model,
        base_url=resolved_base,
        api_key=resolved_key,
        temperature=temperature,
        timeout=timeout if timeout is not None else DEFAULT_TIMEOUT_S,
        max_retries=1,
    )
