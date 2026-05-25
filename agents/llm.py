"""Factory LLM partagée entre tous les agents Klodo.

Par défaut, pointe sur SiliconFlow avec DeepSeek-V3.1 (version stable, bon
ratio raisonnement/prix, ~$0.27/$1.10 par M tokens, function calling natif).

Le modèle est surchargeable via la variable d'env `KLODO_AGENT_MODEL`
(par ex. pour tester GLM-4.6 ou Qwen3 free tier).

L'endpoint est compatible OpenAI — `langchain_openai.ChatOpenAI` est
le client adéquat (pas besoin de wrapper custom).
"""

from __future__ import annotations

import os

from langchain_openai import ChatOpenAI

DEFAULT_MODEL = "deepseek-ai/DeepSeek-V3.2"  # version stable optimisée tool-use
# Choix de modèle (mis à jour 2026-05-25 après tests réels) :
#   - V3.2 (stable, recommandé) — fine-tuné tool-use, suit bien les prompts FR
#   - V3.2-Exp : DÉSACTIVÉ ce soir sans préavis (le piège des -Exp)
#   - V3.1 : régresse vers le chinois et hallucine sur prompts multi-tour
#     complexes (testé → produit un problème d'algo chinois au lieu du
#     rapport demandé)
#   - V4-Pro : très récent, peut servir en backup (tool-use OK)
#   - GLM-4.7 / GLM-5 : alternatives non-DeepSeek si SiliconFlow coupe
#     toute la famille DeepSeek (peu probable mais possible)
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
        model: ID du modèle (ex. "deepseek-ai/DeepSeek-V3.1"). Si None,
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
