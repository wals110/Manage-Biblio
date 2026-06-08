"""Module dédié au prompt + parsing du LLM pour proposer les mots-clés
des nouveaux folders dans categories.yaml (Phase B step 3).

Le LLM reçoit un contexte focalisé : la liste des nouveaux folders avec
leur rationale + 2-3 exemples d'entries existantes par groupe.

Sortie validée par Pydantic (structured output via LangChain).
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class _NewCategoryEntry(BaseModel):
    chemin: str
    groupe: str
    priorite: int = Field(ge=1, le=99, default=5)
    mots_cles: list[str] = Field(min_length=3, max_length=15)


class _NewCategoriesProposal(BaseModel):
    entries: list[_NewCategoryEntry]


def propose_keywords_for_new_folders(
    llm: Any,
    creations: list[dict],
    existing_groupes: list[str],
    groupe_inference: dict[str, str],
    sample_entries: dict[str, list[dict]],
) -> list[dict]:
    """Appelle le LLM 1 fois avec un contexte focalisé pour proposer les
    mots-clés des nouveaux folders. Skip l'appel si `creations` est vide.

    Retourne une liste de dicts au format categories.yaml :
        [{chemin, groupe, priorite, mots_cles}, ...]
    """
    if not creations:
        return []
    # Reste implémenté dans les tâches suivantes.
    raise NotImplementedError("LLM call not implemented yet")
