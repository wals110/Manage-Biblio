"""Proposition de hiérarchie depuis les clusters (1 appel LLM).

Les SECTIONS viennent du contenu (clusters) ; la FORME suit des conventions —
imposées par défaut mais **paramétrables** par l'utilisateur à l'onboarding :
profondeur (min/max), numérotation des sections, casse des noms, séparateur des
mots composés, langue, granularité. Pas de template figé : des conventions.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)
_RESIDUAL = "_A-TRIER"

# Options de taxonomie (défauts = conventions lisibles : Titre, tirets, ≤ 2 niv.).
DEFAULT_OPTIONS: dict[str, Any] = {
    "min_depth": 1,             # 1..3 (borne basse — best-effort via prompt)
    "max_depth": 2,             # 1..3 (borne haute — appliquée par le sanitizer)
    "numbered_sections": True,  # 01-Sciences vs Sciences
    "folder_case": "title",     # title | upper | lower
    "word_separator": "-",      # "-" | "_" | "none" (mots composés)
    "folder_language": "auto",  # auto | fr | en
    "granularity": "auto",      # auto | compact | detailed
}


def _normalize_options(options: dict | None) -> dict[str, Any]:
    """Fusionne avec les défauts + borne les valeurs invalides (min ≤ max)."""
    o = dict(DEFAULT_OPTIONS)
    if options:
        o.update({k: options[k] for k in DEFAULT_OPTIONS if k in options})

    def _depth(v: Any, default: int) -> int:
        try:
            v = int(v)
        except (TypeError, ValueError):
            return default
        return v if v in (1, 2, 3) else default

    o["min_depth"] = _depth(o["min_depth"], 1)
    o["max_depth"] = _depth(o["max_depth"], 2)
    if o["min_depth"] > o["max_depth"]:
        o["min_depth"] = o["max_depth"]
    o["numbered_sections"] = bool(o.get("numbered_sections", True))
    if o["folder_case"] not in ("title", "upper", "lower"):
        o["folder_case"] = "title"
    if o["word_separator"] not in ("-", "_", "none"):
        o["word_separator"] = "-"
    if o["folder_language"] not in ("auto", "fr", "en"):
        o["folder_language"] = "auto"
    if o["granularity"] not in ("auto", "compact", "detailed"):
        o["granularity"] = "auto"
    return o


def _build_system(opts: dict[str, Any]) -> str:
    """Construit le prompt système selon les options de forme."""
    lo, hi = opts["min_depth"], opts["max_depth"]
    depth_rule = (f"profondeur EXACTEMENT {hi} niveau{'x' if hi > 1 else ''}"
                  if lo == hi else f"profondeur entre {lo} et {hi} niveaux")
    num_rule = ("sections de 1er niveau préfixées d'un numéro à 2 chiffres (01-, 02-…)"
                if opts["numbered_sections"] else "pas de préfixe numérique sur les sections")
    case_rule = {
        "upper": "noms de dossiers en MAJUSCULES",
        "lower": "noms de dossiers en minuscules",
        "title": "noms de dossiers en Casse Titre (1re lettre majuscule, acronymes préservés)",
    }[opts["folder_case"]]
    sep_rule = {
        "-": "mots composés reliés par des tirets (ex. Deep-Learning)",
        "_": "mots composés reliés par des underscores (ex. Deep_Learning)",
        "none": "mots composés collés sans séparateur (ex. DeepLearning)",
    }[opts["word_separator"]]
    lang = {
        "fr": "Nomme les dossiers en FRANÇAIS.",
        "en": "Name folders in ENGLISH.",
        "auto": "Nomme les dossiers dans la langue dominante du corpus.",
    }[opts["folder_language"]]
    gran = {
        "compact": "Préfère PEU de grandes sections (regroupe largement).",
        "detailed": "Crée des sections plutôt FINES et spécialisées.",
        "auto": "",
    }[opts["granularity"]]
    parts = [
        "Tu proposes une arborescence de dossiers pour classer une bibliothèque, à "
        "partir de clusters de thèmes observés (avec leur volume).",
        f"RÈGLES DE FORME : {depth_rule} ; {num_rule} ; {case_rule} ; {sep_rule}.",
        lang,
    ]
    if gran:
        parts.append(gran)
    parts.append(
        "Les sections de 1er niveau ont des noms DISTINCTS : si plusieurs clusters "
        "relèvent du même domaine, regroupe-les dans UNE section avec des sous-dossiers "
        "(JAMAIS des sections homonymes numérotées différemment, ex. interdit : "
        "01-Informatique ET 02-Informatique).")
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

    `options` : forme de la taxonomie (cf. DEFAULT_OPTIONS). Robuste : un cluster
    assigné à un canonical inconnu est ignoré ; _A-TRIER toujours présent.
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
    section_canon: dict[str, str] = {}   # nom de section (sans n°, MAJ) → segment canonique
    for sec in result.sections:
        folder = _sanitize_folder(sec.folder, opts)
        if not folder:
            log.warning("propose_taxonomy: section ignorée (dossier invalide %r)", sec.folder)
            continue
        parts = folder.split("/")
        # Consolide les sections HOMONYMES (ex. 01-/02-/03-INFORMATIQUE → 01-…) :
        # plusieurs clusters d'un même domaine doivent partager UNE section avec
        # des sous-dossiers, pas plusieurs sections homonymes numérotées différemment.
        if opts["numbered_sections"]:
            base = _strip_num_prefix(parts[0]).upper()
            parts[0] = section_canon.setdefault(base, parts[0])
            folder = "/".join(parts)
        for i in range(1, len(parts) + 1):
            folders.add("/".join(parts[:i]))
        for canon in sec.cluster_canonicals:
            c = by_canon.get(canon)
            if not c:
                continue  # canonical halluciné → ignoré
            for raw in c["raw_members"]:
                mapping[raw] = folder
    return sorted(folders), mapping


def _strip_num_prefix(seg: str) -> str:
    """Retire un préfixe numérique de section formaté ('01-Foo' → 'Foo')."""
    m = re.match(r"^\d{1,3}-(.+)$", seg)
    return m.group(1) if m else seg


def _format_segment(seg: str, case: str, sep: str, is_section: bool, numbered: bool) -> str:
    """Applique casse + séparateur à un segment de dossier.

    Préserve/normalise un préfixe numérique de section (`01-`) ; découpe le reste
    en mots (espaces/-/_), applique la casse (`title` préserve les acronymes en
    ne touchant que les mots tout-minuscule), puis rejoint avec le séparateur.
    """
    rest = seg.strip()
    prefix = ""
    m = re.match(r"^(\d{1,3})[\s\-_]+(.+)$", rest)
    if m:
        rest = m.group(2)
        if is_section and numbered:
            prefix = m.group(1).zfill(2) + "-"
    words = [w for w in re.split(r"[\s\-_]+", rest) if w]
    if case == "upper":
        words = [w.upper() for w in words]
    elif case == "lower":
        words = [w.lower() for w in words]
    else:  # title — n'altère que les mots tout-minuscule (acronymes NLP/IA-ML préservés)
        words = [(w[:1].upper() + w[1:]) if w.islower() else w for w in words]
    joined = ("" if sep == "none" else sep).join(words)
    return prefix + joined


def _sanitize_folder(path: str, opts: dict[str, Any]) -> str:
    """Nettoie + normalise un chemin de dossier proposé selon `opts`.

    - segments FS-safe (regex), profondeur ≤ `max_depth`, rejet traversal /
      segment caché / nom réservé `_INBOX` ;
    - casse + séparateur appliqués par `_format_segment`.
    Retourne "" si invalide (le cluster concerné retombe alors dans _A-TRIER).
    """
    segs = [s.strip() for s in (path or "").strip().strip("/").split("/") if s.strip()]
    segs = segs[:opts["max_depth"]]
    if not segs:
        return ""
    out = []
    for i, s in enumerate(segs):
        if s in (".", "..") or s.startswith("."):
            return ""
        if s.upper() == "_INBOX":
            return ""
        if not re.match(r"^[A-Za-zÀ-ÿ0-9 _.\-&()]+$", s):
            return ""
        formatted = _format_segment(s, opts["folder_case"], opts["word_separator"],
                                    i == 0, opts["numbered_sections"])
        if not formatted:
            return ""
        out.append(formatted)
    return "/".join(out)
