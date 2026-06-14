"""Proposition de hiérarchie depuis les clusters (1 appel LLM).

Les SECTIONS viennent du contenu (clusters) ; la FORME suit des conventions
imposées (cf. spec § Forme de l'arbre) : ≤ 2 niveaux, sections 1er niveau
numérotées en MAJUSCULES (01-SCIENCES), sous-dossiers TitleCase, bucket
résiduel _A-TRIER. Pas de template imposé.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)
_RESIDUAL = "_A-TRIER"

_SYSTEM = (
    "Tu proposes une arborescence de dossiers pour classer une bibliothèque, "
    "à partir de clusters de thèmes observés (avec leur volume). RÈGLES DE FORME "
    "STRICTES : profondeur 2 niveaux MAX ; sections de 1er niveau préfixées et en "
    "MAJUSCULES (ex. 01-SCIENCES, 02-INFORMATIQUE) ; sous-dossiers en TitleCase "
    "(ex. Astronomie, Deep-Learning, sans espace, tirets autorisés) ; n'invente pas "
    "de thème — chaque section couvre un ou plusieurs clusters fournis ; regroupe les "
    "petits clusters proches. Tu assignes chaque cluster (par sa forme canonique) à "
    "EXACTEMENT un dossier feuille."
)


class _Section(BaseModel):
    folder: str = Field(description="Chemin du dossier feuille, ex '02-INFORMATIQUE/Deep-Learning'")
    cluster_canonicals: list[str] = Field(description="Canoniques des clusters classés ici")


class _ProposedTaxonomy(BaseModel):
    sections: list[_Section]


def propose_taxonomy(llm: Any, clusters: list[dict]) -> tuple[list[str], dict[str, str]]:
    """Retourne (tree_folders, theme_mapping). theme_mapping = {raw_theme: folder}.

    Robuste : un cluster assigné à un canonical inconnu est ignoré ; _A-TRIER est
    toujours présent dans l'arbre (bucket résiduel).
    """
    by_canon = {c["canonical"]: c for c in clusters}
    if len(clusters) > 200:
        log.info("propose_taxonomy: %d clusters → tronqué à 200 ; les thèmes "
                 "au-delà retomberont dans %s", len(clusters), _RESIDUAL)
    payload = "\n".join(
        f"- canonical={c['canonical']!r} volume={c['count']} variantes={c['raw_members'][:4]}"
        for c in clusters[:200]
    )
    structured = llm.with_structured_output(_ProposedTaxonomy)
    try:
        result = structured.invoke(
            [{"role": "system", "content": _SYSTEM},
             {"role": "user", "content": f"Clusters observés :\n{payload}"}])
    except Exception as exc:  # noqa: BLE001 — frontière LLM
        log.warning("propose_taxonomy LLM failed: %s — fallback _A-TRIER seul", exc)
        return [_RESIDUAL], {}

    folders: set[str] = {_RESIDUAL}
    mapping: dict[str, str] = {}
    for sec in result.sections:
        folder = _sanitize_folder(sec.folder)
        if not folder:
            log.warning("propose_taxonomy: section ignorée (dossier invalide %r)", sec.folder)
            continue
        # parents implicites
        parts = folder.split("/")
        for i in range(1, len(parts) + 1):
            folders.add("/".join(parts[:i]))
        for canon in sec.cluster_canonicals:
            c = by_canon.get(canon)
            if not c:
                continue  # canonical halluciné → ignoré
            for raw in c["raw_members"]:
                mapping[raw] = folder
    return sorted(folders), mapping


def _sanitize_folder(path: str) -> str:
    """Nettoie + normalise un chemin de dossier proposé.

    - segments FS-safe (regex), ≤ 2 niveaux, rejet traversal / segment caché ;
    - rejet du nom réservé _INBOX (jamais une section de classement) ;
    - normalisation de forme : section de 1er niveau forcée en MAJUSCULES
      (convention spec). La casse des sous-dossiers reste celle du LLM (revue
      par l'utilisateur dans le brouillon) — on ne TitleCase pas pour ne pas
      casser les acronymes (NLP, IA-ML).
    Retourne "" si invalide (le cluster concerné retombe alors dans _A-TRIER).
    """
    import re
    segs = [s.strip() for s in (path or "").strip().strip("/").split("/") if s.strip()]
    segs = segs[:2]   # profondeur 2 max
    if not segs:
        return ""
    safe = []
    for s in segs:
        if s in (".", "..") or s.startswith("."):
            return ""
        if s.upper() == "_INBOX":
            return ""
        if not re.match(r"^[A-Za-zÀ-ÿ0-9 _.\-&()]+$", s):
            return ""
        safe.append(s)
    safe[0] = safe[0].upper()   # section de 1er niveau en MAJUSCULES (convention spec)
    return "/".join(safe)
