"""Canonisation sémantique des thèmes via LLM avec vocabulaire de référence.

Alternative à Phase 1+2+3 (chantier "C-light" du backlog dédupli) : au lieu
de regrouper des variantes orthographiques, on demande à un LLM de
**catégoriser** chaque thème vers un vocabulary contrôlé. Capture les
synonymes sémantiques (ML ↔ Machine Learning ↔ AI) que la similarité de
string laisse passer.

Pipeline (orchestré par lib.theme_canon.build_canon_table(mode="semantic")) :

  1. build_vocabulary(themes_by_count, top_n=500)
     → liste des canoniques de référence (les thèmes les plus fréquents)
  2. canonicalize_themes(remaining_themes, vocabulary, llm)
     → mapping {raw_theme: canonical} via batches LLM parallélisés
  3. (côté theme_canon) re-clustering de la liste des canoniques produits
     via cluster_themes() pour gommer les doublons résiduels créés par
     le LLM dans différents batches

Cache idempotent par hash (vocabulary + batch) — ré-jouer ne ré-appelle pas.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field


class RawCanonicalPair(BaseModel):
    """Décision LLM pour 1 thème brut."""

    raw: str = Field(description="Le thème brut tel qu'envoyé en input")
    canonical: str = Field(
        description="Le canonical choisi (depuis le vocabulaire OU nouveau)"
    )
    matched_existing: bool = Field(
        description="True si canonical est dans le vocabulaire de référence, "
                    "False si le LLM a dû proposer un nouveau canonical"
    )


class BatchCanonicalization(BaseModel):
    """Sortie LLM pour un batch de thèmes."""

    mappings: list[RawCanonicalPair] = Field(
        description="Un mapping par thème en input — l'ordre doit être préservé"
    )


_SYSTEM_PROMPT = """Tu es un expert en taxonomie qui consolide UNIQUEMENT les \
variantes orthographiques et synonymes EXACTS de thèmes d'une bibliothèque PDF.

═══════ PRINCIPE FONDAMENTAL ═══════

Pour chaque thème, par DÉFAUT garde-le tel quel (canonical = raw).
Ne fusionne que dans les cas listés ci-dessous.

L'erreur la plus grave est la SUR-FUSION (mélanger des concepts distincts).
Une fragmentation est facilement réparable par l'utilisateur, une
sur-fusion fait perdre de l'information de façon irréversible.

═══════ CAS OÙ TU DOIS FUSIONNER (canonical = entrée du vocabulaire) ═══════

UNIQUEMENT si le thème brut est strictement équivalent à un canonical du
vocabulaire selon ces critères :

  1. Variante de casse SEULE : "Machine Learning" / "machine learning" /
     "MACHINE LEARNING" → "Machine Learning"
  2. Variante de pluriel/singulier SEULE : "Neural Networks" / "Neural Network"
  3. Orthographe US/UK SEULE : "Optimization" / "Optimisation"
  4. Acronyme strict = développé connu : "ML" ↔ "Machine Learning",
     "AI" ↔ "Artificial Intelligence", "SEO" ↔ "Search Engine Optimization"
  5. Reformulation pure SANS perte de sens : "Web Development with Java" ↔
     "Java Web Development" (mêmes mots dans un autre ordre)

═══════ CAS OÙ TU NE DOIS PAS FUSIONNER (canonical = raw, matched_existing=False) ═══════

✗ Sous-domaine ou spécialisation : "Unsupervised Machine Learning" ne fusionne
  PAS avec "Machine Learning" (sous-domaine ≠ parent).
✗ Concept lié mais distinct : "Logic" ne fusionne PAS avec "Mathematics".
  "Cognitive Science" ne fusionne PAS avec "Artificial Intelligence".
  "Big Data" ne fusionne PAS avec "Data Science" (méthodologie ≠ volume).
  "Computer Security" ne fusionne PAS avec "Information Security".
✗ Inclusion conceptuelle : un thème qui est un EXEMPLE ou un CAS PARTICULIER
  de l'autre ne fusionne PAS. "Penetration Testing" est un CAS de
  "Information Security" — garde-le séparé.
✗ Versions, plateformes, technologies différentes : "iOS Application
  Development" ne fusionne PAS avec "Mobile Application Development".
  "Quantum Physics" ne fusionne PAS avec "Quantum Mechanics" (concepts proches
  mais distincts en physique théorique).
✗ Compositions : "X for Y" ne fusionne pas avec "X" tout seul. "Statistics
  for Data Science" reste distinct de "Statistics".
✗ Titres de livres ou cours présents comme thèmes : laisse tels quels.

═══════ TEST DE FUSION ═══════

Avant de fusionner X → Y, demande-toi : "Si je voulais ranger un livre dans
un dossier Y, les livres taggés X iraient-ils SANS EXCEPTION dans ce dossier
ET INVERSEMENT ?". Si non, NE FUSIONNE PAS.

═══════ FORMAT ═══════

Le `canonical` doit être en Title Case propre quand il vient du vocabulaire.
Quand tu gardes le raw (matched_existing=False), restitue-le tel quel."""


def build_vocabulary(
    themes_by_count: dict[str, int],
    *,
    top_n: int = 500,
) -> list[str]:
    """Sélectionne le top-N par fréquence comme vocabulaire de référence.

    L'intuition : les thèmes les plus fréquents sont déjà les "concepts
    principaux" de la biblio. Les utiliser comme canoniques garantit qu'on
    fusionne vers des termes humainement reconnaissables (pas vers des
    raretés).

    Args:
        themes_by_count: {raw_theme: count_occurrences}.
        top_n: Taille du vocabulaire (défaut 500).

    Returns:
        Liste de thèmes (les plus fréquents en premier), bornée à top_n.
        Doublons retirés en préservant l'ordre.
    """
    if not themes_by_count:
        return []
    sorted_themes = sorted(
        themes_by_count.items(),
        key=lambda kv: (-kv[1], kv[0].lower()),
    )
    return [t for t, _ in sorted_themes[:top_n]]


def _stable_batch_key(batch: list[str], vocabulary: list[str]) -> str:
    """Hash stable d'un batch + vocab pour le cache.

    L'ordre du batch importe (le LLM produit des `mappings[]` ordonnés),
    mais le vocab est trié pour éliminer l'influence de son ordre.
    """
    payload = json.dumps(
        {"batch": batch, "vocab": sorted(vocabulary)},
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _canonicalizer_cache_path(profile: str) -> Path:
    from dashboard import data
    return (
        data.get_project_root()
        / "profiles" / profile / ".cache" / "theme-canonicalizer.json"
    )


def _load_canonicalizer_cache(profile: str) -> dict[str, dict]:
    path = _canonicalizer_cache_path(profile)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_canonicalizer_cache(profile: str, cache: dict[str, dict]) -> None:
    path = _canonicalizer_cache_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _build_user_prompt(batch: list[str], vocabulary: list[str]) -> str:
    """Construit le HumanMessage pour un batch."""
    vocab_block = "\n".join(f"  - {v}" for v in vocabulary)
    batch_block = "\n".join(f"  - {t!r}" for t in batch)
    return (
        f"Vocabulaire de référence ({len(vocabulary)} canoniques) :\n"
        f"{vocab_block}\n\n"
        f"Thèmes à canoniser ({len(batch)}) — produis un `mapping` par thème, "
        "dans l'ordre :\n"
        f"{batch_block}"
    )


def canonicalize_batch(
    batch: list[str],
    vocabulary: list[str],
    llm: BaseChatModel,
) -> dict[str, str]:
    """Appelle le LLM sur un batch et retourne {raw: canonical}.

    Utilise `with_structured_output(BatchCanonicalization,
    method="function_calling")` — GLM-4.7 ne supporte pas json mode.

    Si le LLM omet certains thèmes du batch (rare), ils sont fallback
    identité (raw → raw) pour ne pas casser le pipeline.
    """
    if not batch:
        return {}
    structured_llm = llm.with_structured_output(
        BatchCanonicalization, method="function_calling",
    )
    result = structured_llm.invoke([
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=_build_user_prompt(batch, vocabulary)),
    ])
    by_raw: dict[str, str] = {}
    for pair in result.mappings:
        if pair.raw in by_raw:
            continue  # déduplique si le LLM répète
        canonical = (pair.canonical or "").strip() or pair.raw
        by_raw[pair.raw] = canonical
    # Filet de sécurité : tout thème oublié → identité
    for raw in batch:
        by_raw.setdefault(raw, raw)
    return by_raw


def canonicalize_themes(
    themes: Iterable[str],
    vocabulary: list[str],
    llm: BaseChatModel,
    profile: str,
    *,
    batch_size: int = 30,
    max_workers: int = 8,
    use_cache: bool = True,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict[str, str]:
    """Pipeline complet de canonisation sémantique d'une liste de thèmes.

    Pour chaque thème, le LLM choisit un canonical depuis `vocabulary` ou
    en propose un nouveau. Les thèmes déjà dans `vocabulary` sont mappés
    vers eux-mêmes (no-op, pas d'appel LLM).

    Args:
        themes: Tous les thèmes raw à canoniser (peut inclure les
                vocabulaires — ils sont skip).
        vocabulary: Le vocabulaire de référence (sortie de build_vocabulary).
        llm: ChatOpenAI configuré (typiquement get_agent_llm()).
        profile: Pour la persistance du cache.
        batch_size: Nombre de thèmes par appel LLM (défaut 30).
        max_workers: Threads parallèles (défaut 8).
        use_cache: Désactivable pour les tests.
        on_progress: Callback `(done, total)`. `total` = nb de batches
                     (pas de thèmes individuels — trop fine-grained).

    Returns:
        Dict {raw_theme: canonical} couvrant TOUS les thèmes d'entrée.
        Les thèmes du vocabulary mappent vers eux-mêmes par construction.
    """
    themes_list = [t for t in themes if t]
    if not themes_list:
        return {}

    vocab_set = set(vocabulary)
    # Identity mapping pour les thèmes déjà dans le vocab — pas d'appel LLM
    result: dict[str, str] = {t: t for t in themes_list if t in vocab_set}
    to_process = [t for t in themes_list if t not in vocab_set]

    if not to_process:
        if on_progress is not None:
            on_progress(0, 0)
        return result

    # Batches
    batches: list[list[str]] = [
        to_process[i:i + batch_size]
        for i in range(0, len(to_process), batch_size)
    ]
    total_batches = len(batches)

    cache = _load_canonicalizer_cache(profile) if use_cache else {}
    lock = threading.Lock()
    cache_dirty = False
    done = 0

    def process_one(batch: list[str]) -> tuple[str, dict[str, str]]:
        key = _stable_batch_key(batch, vocabulary)
        if use_cache and key in cache:
            return key, dict(cache[key])
        mapping = canonicalize_batch(batch, vocabulary, llm)
        return key, mapping

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_one, b): b for b in batches}
        for future in as_completed(futures):
            try:
                key, batch_mapping = future.result()
            except Exception:  # noqa: BLE001 — fallback identité
                batch = futures[future]
                key = _stable_batch_key(batch, vocabulary)
                batch_mapping = {t: t for t in batch}
            with lock:
                result.update(batch_mapping)
                if use_cache:
                    cache[key] = batch_mapping
                    cache_dirty = True
                done += 1
                if on_progress is not None:
                    on_progress(done, total_batches)

    if cache_dirty and use_cache:
        _save_canonicalizer_cache(profile, cache)

    return result
