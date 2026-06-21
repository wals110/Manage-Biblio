"""Proposition de hiérarchie depuis les clusters (1 appel LLM).

Les SECTIONS viennent du contenu (clusters) ; la FORME suit des conventions —
imposées par défaut mais **paramétrables** par l'utilisateur à l'onboarding
(profondeur, numérotation des sections, langue des noms, granularité). Pas de
template figé : ce sont des conventions, pas une bibliothèque de gabarits.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)
_RESIDUAL = "_A-TRIER"

# Options de taxonomie (défauts = conventions historiques de la spec).
DEFAULT_OPTIONS: dict[str, Any] = {
    "max_depth": 2,             # 2 ou 3 niveaux
    "numbered_sections": True,  # 01-SCIENCES vs SCIENCES
    "folder_language": "auto",  # auto | fr | en
    "granularity": "auto",      # auto | compact | detailed
}


def _normalize_options(options: dict | None) -> dict[str, Any]:
    """Fusionne avec les défauts + borne les valeurs invalides."""
    o = dict(DEFAULT_OPTIONS)
    if options:
        o.update({k: options[k] for k in DEFAULT_OPTIONS if k in options})
    try:
        o["max_depth"] = int(o.get("max_depth", 2))
    except (TypeError, ValueError):
        o["max_depth"] = 2
    if o["max_depth"] not in (2, 3):
        o["max_depth"] = 2
    o["numbered_sections"] = bool(o.get("numbered_sections", True))
    if o["folder_language"] not in ("auto", "fr", "en"):
        o["folder_language"] = "auto"
    if o["granularity"] not in ("auto", "compact", "detailed"):
        o["granularity"] = "auto"
    return o


def _build_system(opts: dict[str, Any]) -> str:
    """Construit le prompt système selon les options de forme."""
    depth = opts["max_depth"]
    depth_rule = f"profondeur {depth} niveau{'x' if depth > 1 else ''} MAX"
    if opts["numbered_sections"]:
        sec_rule = ("sections de 1er niveau PRÉFIXÉES (numéro à 2 chiffres) et en "
                    "MAJUSCULES (ex. 01-SCIENCES, 02-INFORMATIQUE)")
    else:
        sec_rule = ("sections de 1er niveau en MAJUSCULES, SANS préfixe numérique "
                    "(ex. SCIENCES, INFORMATIQUE)")
    sub_rule = ("sous-dossiers en TitleCase (ex. Astronomie, Deep-Learning, sans "
                "espace, tirets autorisés)")
    lang = {"fr": "Nomme les dossiers en FRANÇAIS.",
            "en": "Name folders in ENGLISH.",
            "auto": "Nomme les dossiers dans la langue dominante du corpus."}[opts["folder_language"]]
    gran = {"compact": "Préfère PEU de grandes sections (regroupe largement).",
            "detailed": "Crée des sections plutôt FINES et spécialisées.",
            "auto": ""}[opts["granularity"]]
    parts = [
        "Tu proposes une arborescence de dossiers pour classer une bibliothèque, à "
        "partir de clusters de thèmes observés (avec leur volume).",
        f"RÈGLES DE FORME STRICTES : {depth_rule} ; {sec_rule} ; {sub_rule}.",
        lang,
    ]
    if gran:
        parts.append(gran)
    parts.append(
        "N'invente pas de thème — chaque section couvre un ou plusieurs clusters "
        "fournis ; regroupe les petits clusters proches. Tu assignes chaque cluster "
        "(par sa forme canonique) à EXACTEMENT un dossier feuille.")
    return " ".join(parts)


class _Section(BaseModel):
    folder: str = Field(description="Chemin du dossier feuille, ex '02-INFORMATIQUE/Deep-Learning'")
    cluster_canonicals: list[str] = Field(description="Canoniques des clusters classés ici")


class _ProposedTaxonomy(BaseModel):
    sections: list[_Section]


def propose_taxonomy(llm: Any, clusters: list[dict],
                     options: dict | None = None) -> tuple[list[str], dict[str, str]]:
    """Retourne (tree_folders, theme_mapping). theme_mapping = {raw_theme: folder}.

    `options` : profondeur/numérotation/langue/granularité (cf. DEFAULT_OPTIONS).
    Robuste : un cluster assigné à un canonical inconnu est ignoré ; _A-TRIER est
    toujours présent dans l'arbre (bucket résiduel).
    """
    opts = _normalize_options(options)
    by_canon = {c["canonical"]: c for c in clusters}
    if len(clusters) > 200:
        log.info("propose_taxonomy: %d clusters → tronqué à 200 ; les thèmes "
                 "au-delà retomberont dans %s", len(clusters), _RESIDUAL)
    payload = "\n".join(
        f"- canonical={c['canonical']!r} volume={c['count']} variantes={c['raw_members'][:4]}"
        for c in clusters[:200]
    )
    # method="function_calling" : compatible avec tout modèle tool-capable, alors
    # que le json-mode par défaut casse sur la famille GLM (20024 "Json mode is
    # not supported"). Cohérent avec lib/theme_judge + theme_canonicalizer.
    structured = llm.with_structured_output(_ProposedTaxonomy, method="function_calling")
    try:
        result = structured.invoke(
            [{"role": "system", "content": _build_system(opts)},
             {"role": "user", "content": f"Clusters observés :\n{payload}"}])
    except Exception as exc:  # noqa: BLE001 — frontière LLM
        log.warning("propose_taxonomy LLM failed: %s — fallback _A-TRIER seul", exc)
        return [_RESIDUAL], {}

    folders: set[str] = {_RESIDUAL}
    mapping: dict[str, str] = {}
    for sec in result.sections:
        folder = _sanitize_folder(sec.folder, max_depth=opts["max_depth"])
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


def _sanitize_folder(path: str, max_depth: int = 2) -> str:
    """Nettoie + normalise un chemin de dossier proposé.

    - segments FS-safe (regex), profondeur ≤ `max_depth`, rejet traversal /
      segment caché ;
    - rejet du nom réservé _INBOX (jamais une section de classement) ;
    - normalisation de forme : section de 1er niveau forcée en MAJUSCULES. La
      casse des sous-dossiers reste celle du LLM (revue par l'utilisateur) — on
      ne TitleCase pas pour ne pas casser les acronymes (NLP, IA-ML).
    Retourne "" si invalide (le cluster concerné retombe alors dans _A-TRIER).
    """
    segs = [s.strip() for s in (path or "").strip().strip("/").split("/") if s.strip()]
    segs = segs[:max_depth]   # profondeur bornée par les options
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
    safe[0] = safe[0].upper()   # section de 1er niveau en MAJUSCULES
    return "/".join(safe)
