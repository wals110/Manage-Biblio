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

    from langchain_core.messages import HumanMessage, SystemMessage

    valid_paths = {c["path"] for c in creations}
    valid_groupes = set(existing_groupes) | {"autres"}

    def _fallback_entries() -> list[dict]:
        return [
            {
                "chemin": c["path"],
                "groupe": groupe_inference.get(c["path"], "autres"),
                "priorite": 99,
                "mots_cles": [],
            }
            for c in creations
        ]

    system_prompt = (
        "Tu génères des entries pour `categories.yaml` de Klodo (outil de "
        "classification PDF). Pour chaque nouveau folder dans la liste, "
        "propose 3 à 15 mots-clés représentatifs et une priorité (1=haute, "
        "99=basse). Le groupe est déjà inféré, garde-le. Le chemin doit "
        "rester strictement identique."
    )
    user_lines = []
    user_lines.append("Nouveaux folders à enrichir :\n")
    for c in creations:
        groupe = groupe_inference.get(c["path"], "autres")
        user_lines.append(f"- {c['path']} (groupe : {groupe})")
        user_lines.append(f"  rationale : {c.get('rationale', '')}")
    user_lines.append("\nExemples d'entries existantes (1-shot) :")
    for groupe, samples in sample_entries.items():
        for s in samples[:2]:
            user_lines.append(
                f"- groupe={groupe} chemin={s.get('chemin', '')} "
                f"priorite={s.get('priorite', 5)} "
                f"mots_cles={s.get('mots_cles', [])[:5]}"
            )

    # function_calling : compatible tout modèle tool-capable (le json-mode par
    # défaut casse sur GLM — cf. lib/theme_judge / theme_canonicalizer).
    structured = llm.with_structured_output(_NewCategoriesProposal, method="function_calling")
    result = None
    last_error: str = ""
    for attempt in range(2):  # 1 try + 1 retry
        extra_user = ""
        if attempt == 1 and last_error:
            extra_user = (
                f"\n\nLE PRÉCÉDENT ESSAI A ÉCHOUÉ : {last_error}. "
                "Reprends en t'assurant que chaque `chemin` figure EXACTEMENT "
                "dans la liste fournie et que `groupe` est dans la liste autorisée."
            )
        try:
            result = structured.invoke([
                SystemMessage(content=system_prompt),
                HumanMessage(content="\n".join(user_lines) + extra_user),
            ])
        except Exception as exc:
            log.warning("LLM call failed for categories proposal: %s", exc)
            return _fallback_entries()

        # Vérifie la validité avant de quitter la boucle
        invalid = [
            e for e in result.entries
            if e.chemin not in valid_paths or e.groupe not in valid_groupes
        ]
        if not invalid:
            break
        last_error = (
            f"{len(invalid)} entries hallucinées (chemin ou groupe invalide)"
        )
        result = None  # force retry

    if result is None:
        return _fallback_entries()

    # Post-validation : drop entries hallucinées
    out: list[dict] = []
    seen_paths: set[str] = set()
    for entry in result.entries:
        if entry.chemin not in valid_paths:
            log.warning("LLM hallucinated chemin %r — skipping", entry.chemin)
            continue
        if entry.groupe not in valid_groupes:
            log.warning("LLM hallucinated groupe %r — skipping", entry.groupe)
            continue
        out.append({
            "chemin": entry.chemin,
            "groupe": entry.groupe,
            "priorite": entry.priorite,
            "mots_cles": list(entry.mots_cles),
        })
        seen_paths.add(entry.chemin)

    # Compléter avec fallback pour les créations non couvertes
    for c in creations:
        if c["path"] not in seen_paths:
            out.append({
                "chemin": c["path"],
                "groupe": groupe_inference.get(c["path"], "autres"),
                "priorite": 99,
                "mots_cles": [],
            })
    return out
