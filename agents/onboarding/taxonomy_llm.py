"""Proposition de hiérarchie depuis les clusters de thèmes.

Approche **par lots** (scalable) : le LLM assigne un DOMAINE (section de 1er
niveau) à CHAQUE cluster, en traitant les clusters par paquets et en réutilisant
les domaines déjà créés. La structure finale est `SECTION / Thème` (profondeur 2
par construction → `min_depth=2` respecté), chaque thème devenant un sous-dossier.

La FORME suit des conventions paramétrables (profondeur, numérotation, casse,
séparateur, langue, granularité). Pas de template figé.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)
_RESIDUAL = "_A-TRIER"
_CHUNK = 40          # nb de clusters par appel LLM (assignation de domaine)
_GENERAL = {"fr": "Général", "en": "General", "auto": "Général"}

# Options de taxonomie (défauts = conventions lisibles : Titre, tirets, ≤ 2 niv.).
DEFAULT_OPTIONS: dict[str, Any] = {
    "min_depth": 1,             # 1..3 (borne basse)
    "max_depth": 2,             # 1..3 (≥ 2 → structure SECTION/Thème)
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


def _assign_system(opts: dict[str, Any], existing: str) -> str:
    """Prompt système pour l'assignation d'un domaine à un lot de thèmes."""
    lang = {
        "fr": "Donne les noms de domaines en FRANÇAIS.",
        "en": "Give domain names in ENGLISH.",
        "auto": "Donne les noms de domaines dans la langue dominante du corpus.",
    }[opts["folder_language"]]
    gran = {
        "compact": "Regroupe LARGEMENT : peu de grands domaines génériques.",
        "detailed": "Sois plus FIN : crée des domaines spécialisés quand c'est pertinent.",
        "auto": "",
    }[opts["granularity"]]
    return (
        "Tu classes des thèmes de documents dans des DOMAINES (les sections de 1er "
        "niveau d'une bibliothèque). Chaque thème porte un NUMÉRO. Pour CHAQUE thème, "
        "renvoie son NUMÉRO (`index`) et le domaine large auquel il appartient. "
        f"RÉUTILISE en priorité un domaine déjà existant : {existing}. "
        "Ne crée un nouveau domaine que si aucun existant ne convient. "
        + lang + (" " + gran if gran else "")
        + " Assigne TOUS les thèmes — une réponse par numéro, du premier au dernier."
    )


class _Assign(BaseModel):
    index: int = Field(description="numéro du thème dans la liste (1, 2, 3, …)")
    section: str = Field(description="nom du domaine large (ex. Informatique, Mathématiques, Sciences)")


class _Assignments(BaseModel):
    items: list[_Assign]


def _assign_sections(llm: Any, clusters: list[dict], opts: dict[str, Any]) -> dict[str, str]:
    """Assigne un domaine à chaque cluster, par lots. Retourne {canonical: domaine}.

    Matching par NUMÉRO (pas par texte) : le LLM reformule souvent la forme
    canonique (casse/traduction) → un match exact en perdrait la plupart.
    """
    structured = llm.with_structured_output(_Assignments, method="function_calling")
    assigned: dict[str, str] = {}
    sections_seen: list[str] = []
    for start in range(0, len(clusters), _CHUNK):
        batch = clusters[start:start + _CHUNK]
        existing = ", ".join(sections_seen[:60]) or "(aucun encore — crée les premiers)"
        payload = "\n".join(f"{i + 1}. {c['canonical']} (volume {c['count']})"
                            for i, c in enumerate(batch))
        try:
            res = structured.invoke(
                [{"role": "system", "content": _assign_system(opts, existing)},
                 {"role": "user", "content": f"Thèmes à classer :\n{payload}"}])
        except Exception as exc:  # noqa: BLE001 — frontière LLM
            log.warning("assign_sections: lot %d échoué: %s", start // _CHUNK, exc)
            continue
        for a in res.items:
            idx = a.index - 1
            if not (0 <= idx < len(batch)):
                continue
            sec = (a.section or "").strip()
            if not sec:
                continue
            assigned[batch[idx]["canonical"]] = sec
            if sec not in sections_seen:
                sections_seen.append(sec)
    return assigned


def _fmt(name: str, case: str, sep: str) -> str:
    """Formate un nom de dossier (casse + séparateur, sans préfixe numérique).

    Découpe en mots, retire les caractères non FS-safe, applique la casse (`title`
    préserve les acronymes), rejoint avec le séparateur. "" si rien d'exploitable.
    """
    rest = (name or "").strip()
    m = re.match(r"^\d{1,3}[\s\-_]+(.+)$", rest)   # retire un préfixe numérique éventuel
    if m:
        rest = m.group(1)
    clean: list[str] = []
    for w in re.split(r"[\s_\-]+", rest):
        w = re.sub(r"[^A-Za-zÀ-ÿ0-9.&()]", "", w)
        if not w:
            continue
        if case == "upper":
            w = w.upper()
        elif case == "lower":
            w = w.lower()
        elif w.islower():        # title — n'altère que les mots tout-minuscule
            w = w[:1].upper() + w[1:]
        clean.append(w)
    if not clean:
        return ""
    return ("" if sep == "none" else sep).join(clean)


def propose_taxonomy(llm: Any, clusters: list[dict],
                     options: dict | None = None) -> tuple[list[str], dict[str, str]]:
    """Retourne (tree_folders, theme_mapping). theme_mapping = {raw_theme: folder}.

    Structure `SECTION/Thème` : le LLM assigne un domaine à chaque cluster (par
    lots → couverture complète), le thème devient le sous-dossier (profondeur 2
    si `max_depth ≥ 2`). Sections numérotées de façon déterministe (homonymes
    consolidés). `_A-TRIER` toujours présent ; fallback si le LLM échoue partout.
    """
    opts = _normalize_options(options)
    if not clusters:
        return [_RESIDUAL], {}
    assigned = _assign_sections(llm, clusters, opts)
    if not assigned:
        log.warning("propose_taxonomy: aucune assignation LLM — fallback _A-TRIER seul")
        return [_RESIDUAL], {}

    folders: set[str] = {_RESIDUAL}
    mapping: dict[str, str] = {}
    section_num: dict[str, str] = {}   # nom de section (MAJ) → numéro "NN"
    case, sep = opts["folder_case"], opts["word_separator"]
    for c in clusters:
        sec_raw = assigned.get(c["canonical"])
        if not sec_raw:
            continue                                   # non assigné → reste orphelin
        sec_fmt = _fmt(sec_raw, case, sep)
        if not sec_fmt or sec_fmt.upper() in ("INBOX", "_INBOX"):
            continue   # nom de section réservé → on saute (le thème reste orphelin)
        if opts["numbered_sections"]:
            key = sec_fmt.upper()
            if key not in section_num:
                section_num[key] = f"{len(section_num) + 1:02d}"
            section_seg = f"{section_num[key]}-{sec_fmt}"
        else:
            section_seg = sec_fmt
        parts = [section_seg]
        if opts["max_depth"] >= 2:                     # SECTION/Thème (profondeur 2)
            sub = _fmt(c["canonical"], case, sep)
            if not sub or sub.upper() == sec_fmt.upper() or sub.upper() in ("INBOX", "_INBOX"):
                sub = _fmt(_GENERAL[opts["folder_language"]], case, sep)
            parts.append(sub)
        folder = "/".join(parts)
        for i in range(1, len(parts) + 1):
            folders.add("/".join(parts[:i]))
        for raw in c["raw_members"]:
            mapping[raw] = folder
    return sorted(folders), mapping
