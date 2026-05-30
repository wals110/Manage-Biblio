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


_SYSTEM_PROMPT = """Tu canonises des thèmes de bibliothèque PDF. Pour chaque \
thème brut, choisis canonical :

DÉFAUT = raw (matched_existing=False). Ne fusionne QUE pour ces cas :

FUSION OK (vers une entrée du vocabulaire) :
- Variante casse/pluriel/ortho US-UK seule ("ML" → si vocab a "Machine Learning")
- Acronyme = développé, CONFIRMÉ par titres ("ML" + titres parlant de Machine
  Learning → fusion. "AI" + titres "Artificial Insemination" → identité.)
- Reformulation pure sans ajout de qualifier ("Web Dev with Java" peut être
  fusionné avec "Java Web Dev" car mêmes mots)

NE PAS FUSIONNER (canonical = raw) — INTERDICTION ABSOLUE pour ces patterns :

✗ Toute composition "X with Y", "X with Z" → reste distincte de "X" :
  "Web Development with .NET" ≠ "Web Development"
  "Web Development with ASP.NET" ≠ "Web Development"
  "Mathematics for BCPST" ≠ "Mathematics"
  "Statistics for Data Science" ≠ "Statistics"

✗ Toute composition "X (Y)" ou "X / Y" → reste distincte de "X" :
  "Mathematical Analysis (Discrete)" ≠ "Mathematical Analysis"
  "Artificial Intelligence / Machine Learning" ≠ "Artificial Intelligence"

✗ Tout qualifier ajouté ("Advanced", "Modern", "Applied", "Introduction to",
  numéro de version) → reste distinct :
  "Advanced Java Programming" ≠ "Java Programming"
  "Modern Web Development with JavaScript" ≠ "Web Development"
  "Java 8 Programming" ≠ "Java Programming"
  "Python 3 Programming" ≠ "Python Programming"

✗ Sous-domaine ou spécialisation thématique :
  "Unsupervised Machine Learning" ≠ "Machine Learning"
  "Cloud Big Data" ≠ "Cloud Computing"
  "Cloud Application Deployment" ≠ "Cloud Computing"

✗ Concepts liés mais distincts :
  Logic ≠ Math, Big Data ≠ Data Science, Penetration Testing ≠
  Information Security, Quantum Physics ≠ Quantum Mechanics

✗ Versions/plateformes différentes :
  iOS App Dev ≠ Mobile App Dev

Les titres servent à : confirmer un acronyme, désambiguïser un homonyme.
Si titres ne correspondent pas au sens du canonical → ne fusionne PAS.

PRINCIPE : sur-fusion >> sous-fusion en gravité. En cas de doute → identité.
Si le raw contient un mot que le canonical ne contient PAS (qualifier ajouté
type "with", "for", "Advanced", numéro), c'est PROBABLEMENT un sous-domaine
→ identité par défaut.

Canonical en Title Case quand fusion vers vocab. Sinon raw tel quel."""


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


def _ensure_non_streaming(llm: BaseChatModel) -> BaseChatModel:
    """Désactive streaming sur une copie du LLM si besoin (ChatOpenAI seul).

    Justification : en mode `function_calling`, le tool_call complet arrive
    en bloc → pas de chunks intermédiaires émis pendant la phase de génération.
    Avec streaming=True (défaut de get_agent_llm pour Phase B), SiliconFlow
    disconnect serveur-side à ~85-90s si aucun chunk reçu. En streaming=False,
    le serveur attend de finir et envoie tout d'un coup → pas de timeout.

    Restreint à ChatOpenAI pour ne pas perturber les mocks dans les tests
    (un MagicMock répond truthy à n'importe quel getattr).
    """
    from langchain_openai import ChatOpenAI
    if not isinstance(llm, ChatOpenAI):
        return llm
    if not getattr(llm, "streaming", False):
        return llm
    try:
        return llm.model_copy(update={"streaming": False})
    except Exception:  # noqa: BLE001
        return llm


def resolve_batch(
    batch: list[tuple[str, list[str]]],
    vocabulary: list[str],
    llm: BaseChatModel,
) -> dict[str, str]:
    """Appelle le LLM sur un batch (theme + titres) → {raw: canonical}.

    Utilise `with_structured_output(BatchResolution, method="function_calling")`
    avec streaming désactivé (cf. _ensure_non_streaming pour le rationale).

    Filet de sécurité : si le LLM omet certains thèmes → identité par défaut.
    """
    if not batch:
        return {}
    no_stream_llm = _ensure_non_streaming(llm)
    structured_llm = no_stream_llm.with_structured_output(
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
