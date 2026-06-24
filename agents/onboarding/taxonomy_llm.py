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
_DIVERS = {"fr": "Divers", "en": "Misc", "auto": "Divers"}
# granularity → règles de la passe 2 (None = passe 2 désactivée)
_GRANULARITY: dict[str, dict | None] = {
    "compact":  {"skip": 6,  "low": 3, "high": 6,  "cap": 8},
    "auto":     {"skip": 12, "low": 6, "high": 12, "cap": 16},
    "detailed": None,
}

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


def _collides(sub: str, sec_fmt: str) -> bool:
    """True si `sub` est vide, identique à la section, ou réservé (`INBOX`/`_INBOX`).

    NB : « Général » et « Divers » ne sont PAS des collisions — ce sont des noms de
    dossier de repli/débordement légitimes (fallback dernier recours et bucket de
    `_enforce_cap`). Les rejeter ici casserait le plafonnement.
    """
    return (not sub) or sub.upper() == sec_fmt.upper() or sub.upper() in ("INBOX", "_INBOX")


def _macro_system(opts: dict[str, Any], existing: str, low: int, high: int) -> str:
    """Prompt système de la passe 2 (regroupement en grands thèmes). Miroir de
    `_assign_system`. Contient « GRANDS THÈMES » pour être distingué de la passe 1.
    """
    lang = {
        "fr": "Donne les noms de grands thèmes en FRANÇAIS.",
        "en": "Give broad-theme names in ENGLISH.",
        "auto": "Donne les noms dans la langue dominante du corpus.",
    }[opts["folder_language"]]
    return (
        "Tu regroupes des thèmes spécialisés en GRANDS THÈMES (sous-domaines larges). "
        f"Vise {low} à {high} grands thèmes au total. Chaque thème porte un NUMÉRO ; "
        "pour CHAQUE thème, renvoie son NUMÉRO (`index`) et le grand thème (`section`) "
        "auquel il appartient. RÉUTILISE en priorité un grand thème déjà créé : "
        f"{existing}. Crée un nouveau grand thème seulement si nécessaire. "
        "NE nomme PAS un grand thème comme la section elle-même ni « Général/Divers » "
        "(noms réservés). " + lang
        + " Assigne TOUS les thèmes, du premier au dernier."
    )


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


def _assign_macro_themes(llm: Any, section_clusters: list[dict], opts: dict[str, Any],
                         low: int, high: int) -> dict[str, str]:
    """Regroupe les clusters d'UNE section en grands thèmes. Par lots, matching par
    NUMÉRO (le LLM reformule la canonical). Réutilise les grands thèmes entre lots.
    Retourne {canonical: grand_thème}.
    """
    structured = llm.with_structured_output(_Assignments, method="function_calling")
    assigned: dict[str, str] = {}
    macros_seen: list[str] = []
    for start in range(0, len(section_clusters), _CHUNK):
        batch = section_clusters[start:start + _CHUNK]
        existing = ", ".join(macros_seen[:60]) or "(aucun encore — crée les premiers)"
        payload = "\n".join(f"{i + 1}. {c['canonical']} (volume {c['count']})"
                            for i, c in enumerate(batch))
        try:
            res = structured.invoke(
                [{"role": "system", "content": _macro_system(opts, existing, low, high)},
                 {"role": "user", "content": f"Thèmes à classer :\n{payload}"}])
        except Exception as exc:  # noqa: BLE001 — frontière LLM
            log.warning("assign_macro_themes: lot %d échoué: %s", start // _CHUNK, exc)
            continue
        for a in res.items:
            idx = a.index - 1
            if not (0 <= idx < len(batch)):
                continue
            macro = (a.section or "").strip()
            if not macro:
                continue
            assigned[batch[idx]["canonical"]] = macro
            if macro not in macros_seen:
                macros_seen.append(macro)
    return assigned


def _enforce_cap(macro_map: dict[str, str], section_clusters: list[dict],
                 cap: int, lang: str) -> dict[str, str]:
    """Plafonne le nb de grands thèmes d'une section (cf. spec §4). Déterministe.

    ≤ cap → inchangé. Sinon : tri `(-volume, nom)`, garde les `cap-1` plus gros,
    fusionne le reste dans `_DIVERS[lang]` (clé idempotente). Si « Divers » devient
    le plus gros bucket → dégrade la section au grain fin (chaque thème = son canonical).

    `cap ≥ 1` requis (en pratique 8 ou 16 depuis `_GRANULARITY`) ; `cap=0` donnerait
    `ordered[:-1]` (silencieusement faux) — non atteint par les appelants actuels.
    """
    vol: dict[str, int] = {}
    for c in section_clusters:
        m = macro_map.get(c["canonical"])
        if m:
            vol[m] = vol.get(m, 0) + int(c["count"])
    if len(vol) <= cap:
        return macro_map
    ordered = sorted(vol, key=lambda m: (-vol[m], m))      # tie-break déterministe
    keep = set(ordered[:cap - 1])
    divers = _DIVERS[lang]
    out: dict[str, str] = {}
    for c in section_clusters:
        m = macro_map.get(c["canonical"])
        if not m:
            continue
        out[c["canonical"]] = m if m in keep else divers
    divers_vol = sum(int(c["count"]) for c in section_clusters
                     if out.get(c["canonical"]) == divers)
    if divers_vol > vol[ordered[0]]:                       # « Divers » dominant → grain fin
        return {c["canonical"]: c["canonical"] for c in section_clusters}
    return out


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

    Deux passes : (1) `_assign_sections` assigne un domaine à chaque cluster ;
    (2) `_assign_macro_themes` regroupe, par section engorgée, les thèmes en grands
    thèmes (pilotée par `granularity`). Le grand thème devient le sous-dossier ; les
    thèmes fins y sont absorbés. Garde-fous : skip des petites sections, `_enforce_cap`,
    repli grain-fin avant « Général ». `_A-TRIER` toujours présent.
    """
    opts = _normalize_options(options)
    if not clusters:
        return [_RESIDUAL], {}
    assigned = _assign_sections(llm, clusters, opts)
    if not assigned:
        log.warning("propose_taxonomy: aucune assignation LLM — fallback _A-TRIER seul")
        return [_RESIDUAL], {}

    # Passe 2 : regrouper les thèmes en grands thèmes, par section.
    gran = _GRANULARITY.get(opts["granularity"])   # None si detailed
    by_section: dict[str, list[dict]] = {}
    for c in clusters:
        sec = assigned.get(c["canonical"])
        if sec:
            by_section.setdefault(sec, []).append(c)
    macro_of: dict[str, str] = {}
    for sec, sec_clusters in by_section.items():
        if gran is None or len(sec_clusters) <= gran["skip"]:
            for c in sec_clusters:                 # petite section / detailed → grain fin
                macro_of[c["canonical"]] = c["canonical"]
            continue
        try:
            m = _assign_macro_themes(llm, sec_clusters, opts, gran["low"], gran["high"])
        except Exception as exc:  # noqa: BLE001 — frontière LLM
            log.warning("propose_taxonomy: passe 2 échouée section %r: %s", sec, exc)
            m = {}
        m = _enforce_cap(m, sec_clusters, gran["cap"], opts["folder_language"])
        for c in sec_clusters:                     # trou d'index → grain fin
            macro_of[c["canonical"]] = m.get(c["canonical"]) or c["canonical"]

    folders: set[str] = {_RESIDUAL}
    mapping: dict[str, str] = {}
    section_num: dict[str, str] = {}   # nom de section (MAJ) → numéro "NN"
    case, sep = opts["folder_case"], opts["word_separator"]
    for c in clusters:
        sec_raw = assigned.get(c["canonical"])
        if not sec_raw:
            continue                               # non assigné → reste orphelin
        sec_fmt = _fmt(sec_raw, case, sep)
        if not sec_fmt or sec_fmt.upper() in ("INBOX", "_INBOX"):
            continue
        if opts["numbered_sections"]:
            key = sec_fmt.upper()
            if key not in section_num:
                section_num[key] = f"{len(section_num) + 1:02d}"
            section_seg = f"{section_num[key]}-{sec_fmt}"
        else:
            section_seg = sec_fmt
        parts = [section_seg]
        if opts["max_depth"] >= 2:                 # SECTION/GrandThème (profondeur 2)
            sub = _fmt(macro_of.get(c["canonical"], c["canonical"]), case, sep)
            if _collides(sub, sec_fmt):
                sub = _fmt(c["canonical"], case, sep)             # 1) repli grain fin distinct
                if _collides(sub, sec_fmt):
                    sub = _fmt(_GENERAL[opts["folder_language"]], case, sep)   # 2) dernier recours
            parts.append(sub)
        folder = "/".join(parts)
        for i in range(1, len(parts) + 1):
            folders.add("/".join(parts[:i]))
        for raw in c["raw_members"]:
            mapping[raw] = folder
    return sorted(folders), mapping
