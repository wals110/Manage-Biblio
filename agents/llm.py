"""Factory LLM partagée entre tous les agents Klodo.

Par défaut, pointe sur SiliconFlow avec DeepSeek-V3.2-Exp (bon ratio
raisonnement/prix, ~$0.27/$1.10 par M tokens, function calling natif).

Le modèle est surchargeable via la variable d'env `KLODO_AGENT_MODEL`
(par ex. pour tester GLM-4.6 ou Qwen3 free tier).

L'endpoint est compatible OpenAI — `langchain_openai.ChatOpenAI` est
le client adéquat (pas besoin de wrapper custom).
"""

from __future__ import annotations

import os

from langchain_openai import ChatOpenAI

DEFAULT_MODEL = "deepseek-ai/DeepSeek-V3.2-Exp"
DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"


def get_agent_llm(
    model: str | None = None,
    *,
    temperature: float = 0.2,
    base_url: str | None = None,
    api_key: str | None = None,
) -> ChatOpenAI:
    """Construit un ChatOpenAI configuré pour SiliconFlow.

    Args:
        model: ID du modèle (ex. "deepseek-ai/DeepSeek-V3.2-Exp"). Si None,
               lit `KLODO_AGENT_MODEL` puis fallback sur DEFAULT_MODEL.
        temperature: Default 0.2 — assez bas pour raisonnement structuré +
                     un peu de variabilité pour les itérations utilisateur.
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
    )
