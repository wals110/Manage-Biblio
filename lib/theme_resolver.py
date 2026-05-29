"""C.2 — Canonisation contextuelle des thèmes via LLM texte avec titres en contexte.

Différence avec lib.theme_canonicalizer (C-light "blind") :
  - C-light : (raw_theme) → canonical, le LLM décide à l'aveugle
  - C.2 (this) : (raw_theme, N titres représentatifs) → canonical, le LLM
    désambiguïse en voyant le contexte d'usage du thème dans la bibliothèque.

Le LLM peut ainsi distinguer un thème "Logic" :
  - Si les livres taggés sont "Foundations of Computer Logic", "Logic
    Programming with Prolog" → canonical "Logic" (informatique)
  - Si "Aristotle's Metaphysics", "Philosophy of Logic" → canonical "Logic"
    aussi mais le LLM voit le contexte philosophique
  - Pas de confusion avec "Mathematics" (l'erreur principale du run C-light v1)

Pipeline (orchestré par lib.theme_canon.build_canon_table(mode="source")) :
  1. extract_themes_with_titles(profile) — collecte les 5 titres par thème
  2. resolve_themes_with_context(themes_titles, vocabulary, llm) — LLM
  3. (côté theme_canon) re-clustering via cluster_themes pour gommer les
     doublons de canoniques produits par batches différents
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from lib.theme_canon import _parse_cached_result, _vision_cache_path

# Nombre de titres représentatifs collectés par thème (décision design 2026-05-29)
TITLES_PER_THEME = 5


def extract_themes_with_titles(profile: str) -> dict[str, list[str]]:
    """Pour chaque raw_theme du vision_cache, collecte jusqu'à 5 titres
    représentatifs (les premiers rencontrés dans l'ordre du cache).

    Le contexte des titres permet au LLM (en aval) de désambiguïser le
    sens d'un thème ambigu (Logic, Statistics, Big Data, History, …).

    Args:
        profile: Nom du profil.

    Returns:
        Dict {raw_theme: [title_1, ..., title_N]} avec N ≤ 5.
        Les titres sont uniques par thème et préservent l'ordre du cache.
        Profils sans vision_cache → dict vide.
    """
    path = _vision_cache_path(profile)
    if not path.exists():
        return {}
    import json
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    by_theme: dict[str, list[str]] = defaultdict(list)
    seen: dict[str, set[str]] = defaultdict(set)

    for entry in cache.values():
        if not isinstance(entry, dict):
            continue
        result = _parse_cached_result(entry.get("result"))
        if not isinstance(result, dict):
            continue
        title = str(result.get("title") or "").strip()
        if not title:
            continue
        for t in result.get("themes", []) or []:
            name = ""
            if isinstance(t, dict):
                name = str(t.get("theme") or "").strip()
            elif isinstance(t, str):
                name = t.strip()
            if not name:
                continue
            # Skip si on a déjà 5 titres pour ce thème (premier-arrivé)
            if len(by_theme[name]) >= TITLES_PER_THEME:
                continue
            # Skip si ce titre est déjà associé au thème (dédup)
            if title in seen[name]:
                continue
            seen[name].add(title)
            by_theme[name].append(title)

    return dict(by_theme)


def count_themes(profile: str) -> dict[str, int]:
    """Compte le nombre d'occurrences de chaque raw_theme dans le vision_cache.

    Helper séparé de `extract_themes_with_titles` car les usages diffèrent :
    le count sert au vocabulary bootstrap (top-N par fréquence), les titres
    servent au contexte LLM.
    """
    path = _vision_cache_path(profile)
    if not path.exists():
        return {}
    import json
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    counts: dict[str, int] = {}
    for entry in cache.values():
        if not isinstance(entry, dict):
            continue
        result = _parse_cached_result(entry.get("result"))
        if not isinstance(result, dict):
            continue
        for t in result.get("themes", []) or []:
            name = ""
            if isinstance(t, dict):
                name = str(t.get("theme") or "").strip()
            elif isinstance(t, str):
                name = t.strip()
            if name:
                counts[name] = counts.get(name, 0) + 1
    return counts


def themes_with_titles_iter(
    themes_titles: dict[str, list[str]],
) -> Iterable[tuple[str, list[str]]]:
    """Itérateur stable (theme, titles) sur les thèmes — ordre lexico pour
    déterminisme des batches et du cache."""
    for theme in sorted(themes_titles.keys()):
        yield theme, themes_titles[theme]


# ─── D.2 — LLM resolver avec contexte titres ───────────────────────────────


class ResolvedTheme(BaseModel):
    """Décision LLM pour 1 thème (avec ses titres en contexte)."""

    raw: str = Field(description="Le thème brut tel qu'envoyé en input")
    canonical: str = Field(
        description="Le canonical choisi (depuis le vocabulaire OU raw lui-même)"
    )
    matched_existing: bool = Field(
        description="True si canonical est dans le vocabulaire de référence"
    )


class BatchResolution(BaseModel):
    """Sortie LLM pour un batch de thèmes-avec-titres."""

    mappings: list[ResolvedTheme] = Field(
        description="Un mapping par thème en input — l'ordre doit être préservé"
    )


_SYSTEM_PROMPT = """Tu es un expert en taxonomie qui consolide UNIQUEMENT les \
variantes orthographiques et synonymes EXACTS de thèmes d'une bibliothèque PDF.

═══════ CONTEXTE FOURNI ═══════

Pour chaque thème brut, tu reçois jusqu'à 5 titres représentatifs de livres
de la bibliothèque taggés avec ce thème. Utilise ces titres pour comprendre
le SENS RÉEL du thème dans son contexte d'usage avant de décider.

═══════ PRINCIPE FONDAMENTAL ═══════

Par DÉFAUT garde le thème tel quel (canonical = raw, matched_existing=False).
Ne fusionne que dans les cas listés ci-dessous.

L'erreur la plus grave est la SUR-FUSION (mélanger des concepts distincts).
Une fragmentation est facilement réparable par l'utilisateur, une sur-fusion
fait perdre de l'information de façon irréversible.

═══════ CAS OÙ TU DOIS FUSIONNER (canonical = entrée du vocabulaire) ═══════

UNIQUEMENT si le thème brut ET ses titres montrent qu'il est strictement
équivalent à un canonical du vocabulaire :

  1. Variante de casse SEULE : "Machine Learning" / "machine learning"
  2. Variante de pluriel/singulier SEULE : "Neural Networks" / "Neural Network"
  3. Orthographe US/UK SEULE : "Optimization" / "Optimisation"
  4. Acronyme strict = développé connu, CONFIRMÉ par les titres : "ML" + titres
     "Introduction to Machine Learning" → "Machine Learning". MAIS si le thème
     est "ML" et les titres parlent de "Mailing List", ne fusionne PAS.
  5. Reformulation pure SANS perte de sens : "Web Development with Java" ↔
     "Java Web Development" (mêmes mots dans un autre ordre).

═══════ CAS OÙ TU NE DOIS PAS FUSIONNER (canonical = raw) ═══════

✗ Sous-domaine ou spécialisation : "Unsupervised Machine Learning" ≠
  "Machine Learning". Garde le raw.
✗ Concept lié mais distinct : "Logic" ≠ "Mathematics". "Cognitive Science"
  ≠ "Artificial Intelligence". "Big Data" ≠ "Data Science". "Computer
  Security" ≠ "Information Security". "Penetration Testing" ≠ "Information
  Security" (cas particulier d'une catégorie n'EST PAS la catégorie).
✗ Inclusion conceptuelle : "Retro computing" ≠ "History of Science",
  même si lié.
✗ Versions, plateformes, technologies différentes : "iOS Application
  Development" ≠ "Mobile Application Development". "Quantum Physics" ≠
  "Quantum Mechanics" en physique théorique.
✗ Compositions "X for Y" ≠ "X" seul : "Statistics for Data Science" reste
  distinct de "Statistics".

═══════ UTILISATION DES TITRES ═══════

Les titres servent à 3 choses :
  a) Confirmer un acronyme ambigu : "ML" + "Pattern Recognition and Machine
     Learning" → confirmé Machine Learning. "AI" + "Artificial Insemination
     in Cattle" → PAS Artificial Intelligence.
  b) Désambiguïser un homonyme : "Logic" + titres logique formelle (livres
     "A First Course in Logic", "Mathematical Logic") → "Logic" reste
     "Logic" (pas Math, pas Philosophy).
  c) Détecter un titre individuel mal taggé comme thème : si un thème est
     exactement le titre d'un livre, garde-le tel quel.

Si les titres NE CORRESPONDENT PAS au sens d'un canonical du vocabulaire,
ne fusionne PAS.

═══════ FORMAT ═══════

Le `canonical` doit être en Title Case propre quand il vient du vocabulaire.
Quand tu gardes le raw (matched_existing=False), restitue-le tel quel."""


def _stable_batch_key(
    batch: list[tuple[str, list[str]]],
    vocabulary: list[str],
) -> str:
    """Hash stable d'un batch (theme + titres) + vocab — clé de cache.

    L'ordre du batch importe (le LLM produit mappings[] ordonnés).
    Le vocab est trié pour éliminer l'influence de son ordre.
    Les titres dans chaque entrée du batch sont triés pour qu'on hit le
    cache même si l'ordre de collecte des titres varie d'une run à l'autre.
    """
    canonical = [
        (theme, sorted(titles)) for theme, titles in batch
    ]
    payload = json.dumps(
        {"batch": canonical, "vocab": sorted(vocabulary)},
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _resolver_cache_path(profile: str):
    from dashboard import data
    return (
        data.get_project_root()
        / "profiles" / profile / ".cache" / "theme-resolver.json"
    )


def _load_resolver_cache(profile: str) -> dict[str, dict]:
    path = _resolver_cache_path(profile)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_resolver_cache(profile: str, cache: dict[str, dict]) -> None:
    path = _resolver_cache_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _build_user_prompt(
    batch: list[tuple[str, list[str]]],
    vocabulary: list[str],
) -> str:
    """Construit le HumanMessage pour un batch (theme + titres)."""
    vocab_block = "\n".join(f"  - {v}" for v in vocabulary)
    entries: list[str] = []
    for i, (theme, titles) in enumerate(batch, 1):
        title_lines = "\n".join(f"    - {t!r}" for t in titles)
        if not title_lines:
            title_lines = "    (aucun titre disponible)"
        entries.append(
            f"Thème {i} : {theme!r}\n"
            f"  Titres représentatifs ({len(titles)}) :\n"
            f"{title_lines}"
        )
    entries_block = "\n\n".join(entries)
    return (
        f"Vocabulaire de référence ({len(vocabulary)} canoniques) :\n"
        f"{vocab_block}\n\n"
        f"Thèmes à canoniser ({len(batch)}) — produis un `mapping` par thème "
        "dans l'ordre :\n\n"
        f"{entries_block}"
    )


def resolve_batch(
    batch: list[tuple[str, list[str]]],
    vocabulary: list[str],
    llm: BaseChatModel,
) -> dict[str, str]:
    """Appelle le LLM sur un batch (theme + titres) → {raw: canonical}.

    Utilise `with_structured_output(BatchResolution, method="function_calling")`.
    GLM-4.7 ne supporte pas json mode → on force function_calling.

    Filet de sécurité : si le LLM omet certains thèmes → identité par défaut.
    """
    if not batch:
        return {}
    structured_llm = llm.with_structured_output(
        BatchResolution, method="function_calling",
    )
    result = structured_llm.invoke([
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=_build_user_prompt(batch, vocabulary)),
    ])
    by_raw: dict[str, str] = {}
    for pair in result.mappings:
        if pair.raw in by_raw:
            continue
        canonical = (pair.canonical or "").strip() or pair.raw
        by_raw[pair.raw] = canonical
    # Filet de sécurité : thème oublié → identité
    for theme, _ in batch:
        by_raw.setdefault(theme, theme)
    return by_raw


def resolve_themes_with_context(
    themes_titles: dict[str, list[str]],
    vocabulary: list[str],
    llm: BaseChatModel,
    profile: str,
    *,
    batch_size: int = 30,
    max_workers: int = 8,
    use_cache: bool = True,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict[str, str]:
    """Pipeline complet de canonisation contextuelle (C.2).

    Pour chaque thème, le LLM voit le thème + 5 titres représentatifs et
    décide canonical (depuis vocabulary OU lui-même). Les thèmes déjà dans
    `vocabulary` sont identité (skip LLM).

    Args:
        themes_titles: {raw_theme: [title_1, ..., title_5]} — sortie de
                       extract_themes_with_titles().
        vocabulary: Vocabulaire de référence (sortie de build_vocabulary
                    côté theme_canonicalizer).
        llm: ChatOpenAI configuré.
        profile: Pour la persistance du cache.
        batch_size: Thèmes par appel LLM (défaut 30).
        max_workers: Threads parallèles (défaut 8).
        use_cache: Désactivable pour les tests.
        on_progress: Callback `(done_batches, total_batches)`.

    Returns:
        Dict {raw_theme: canonical} couvrant TOUS les thèmes en entrée.
    """
    if not themes_titles:
        return {}

    vocab_set = set(vocabulary)
    # Identité pour thèmes déjà dans vocab — pas d'appel LLM
    result: dict[str, str] = {
        t: t for t in themes_titles if t in vocab_set
    }
    # Itère en ordre lexico stable (déterminisme batches + cache)
    to_process: list[tuple[str, list[str]]] = [
        (theme, themes_titles[theme])
        for theme in sorted(themes_titles.keys())
        if theme not in vocab_set
    ]

    if not to_process:
        if on_progress is not None:
            on_progress(0, 0)
        return result

    batches: list[list[tuple[str, list[str]]]] = [
        to_process[i:i + batch_size]
        for i in range(0, len(to_process), batch_size)
    ]
    total_batches = len(batches)

    cache = _load_resolver_cache(profile) if use_cache else {}
    lock = threading.Lock()
    cache_dirty = False
    done = 0

    def process_one(
        batch: list[tuple[str, list[str]]],
    ) -> tuple[str, dict[str, str]]:
        key = _stable_batch_key(batch, vocabulary)
        if use_cache and key in cache:
            return key, dict(cache[key])
        mapping = resolve_batch(batch, vocabulary, llm)
        return key, mapping

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_one, b): b for b in batches}
        for future in as_completed(futures):
            try:
                key, batch_mapping = future.result()
            except Exception:  # noqa: BLE001 — fallback identité par batch
                batch = futures[future]
                key = _stable_batch_key(batch, vocabulary)
                batch_mapping = {theme: theme for theme, _ in batch}
            with lock:
                result.update(batch_mapping)
                if use_cache:
                    cache[key] = batch_mapping
                    cache_dirty = True
                done += 1
                if on_progress is not None:
                    on_progress(done, total_batches)

    if cache_dirty and use_cache:
        _save_resolver_cache(profile, cache)

    return result
