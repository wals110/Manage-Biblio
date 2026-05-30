"""LLM judge sur les clusters fuzzy de Phase 2 — tranche les cas ambigus.

Cible : les clusters multi-variantes où `cluster_themes()` a regroupé des
formes canoniques distinctes par similarité fuzzy. Beaucoup de ces clusters
contiennent des **sous-domaines** que `token_sort_ratio` confond avec leur
parent (ex. "Java EE Development" + "JavaFX Development" mélangés).

Pipeline :
  1. should_auto_merge() : skip LLM si toutes les paires du cluster ont
     similarité ≥ 97 (vrais doublons orthographiques, fusion sûre).
  2. judge_cluster(cluster, llm) : 1 appel LLM avec sortie structurée
     Pydantic → JudgeResult (canonical + members + splits).
  3. judge_clusters(clusters, profile, llm) : applique 1+2 sur la liste,
     avec cache idempotent dans `profiles/<p>/.cache/theme-judge.json`.

Le cache est calé sur un hash stable du cluster (tri lexicographique), donc
re-jouer la dédupli sur les mêmes inputs ne ré-appelle pas le LLM.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field
from rapidfuzz import fuzz


class JudgeSplit(BaseModel):
    """Une variante du cluster séparée par le LLM judge."""

    theme: str = Field(description="La variante brute à séparer")
    reason: str = Field(
        description="Justification courte (sous-domaine, version, contexte distinct)"
    )


class JudgeResult(BaseModel):
    """Décision du LLM judge sur un cluster fuzzy.

    Le LLM choisit un canonical propre (Title Case), liste les variantes
    confirmées comme synonymes, et sépare celles qui sont des
    sous-domaines distincts.
    """

    canonical: str = Field(
        description="Nom canonique propre en Title Case (ex. 'Machine Learning')"
    )
    members: list[str] = Field(
        description="Variantes brutes confirmées comme synonymes du canonical"
    )
    splits: list[JudgeSplit] = Field(
        default_factory=list,
        description="Variantes à séparer du cluster (sous-domaines distincts)",
    )


_SYSTEM_PROMPT = """Tu es un expert en taxonomie qui déduplique des thèmes d'une bibliothèque PDF technique.

Ton job : on te donne un GROUPE de variantes candidates à fusion. Pour chaque variante, décide si elle représente :
  - Le MÊME concept que les autres (à fusionner) → "members"
  - Un SOUS-DOMAINE ou un concept DISTINCT → "splits"

Règles fondamentales (à appliquer strictement) :
  ✓ FUSIONNER si :
    - Variante de casse : "Machine Learning" + "machine learning"
    - Variante de pluriel : "Neural Networks" + "Neural Network"
    - Orthographe US/UK : "Optimization" + "Optimisation"
    - Acronyme = développé : "SEO" + "Search Engine Optimization"
    - Reformulation neutre : "Web Development with Java" + "Java Web Development"

  ✗ SÉPARER (splits) si :
    - Sous-domaine : "Machine Learning" vs "Unsupervised Machine Learning"
    - Version produit : "Windows 10" vs "Windows XP"
    - Techno différente : "Java EE Development" vs "JavaFX Development"
    - Contexte distinct : "Network Security" vs "Computer Security"
    - Specialisation : "Game Programming" vs "2D Game Programming"

Choisis le `canonical` en Title Case propre (pas en minuscule, pas en MAJUSCULES).

PRINCIPE DE PRUDENCE : si tu hésites sur une variante, mets-la dans `splits`.
La sur-fusion est plus dommageable que la sous-fusion — un cluster qui mélange
des concepts différents fausse durablement la taxonomie."""


def _stable_cluster_key(variants: list[str]) -> str:
    """Hash stable d'un cluster — indépendant de l'ordre des variantes.

    Utilisé comme clé de cache : si on re-juge exactement les mêmes
    variantes (peu importe l'ordre), on retrouve la décision précédente.
    """
    canonical = "|".join(sorted(variants))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def should_auto_merge(variants: list[str], threshold: int = 97) -> bool:
    """True si TOUTES les paires du cluster ont similarité ≥ threshold.

    Skip le LLM pour les évidences (typos purs, variantes de casse ratées
    par Phase 1 normalisation, orthographe US/UK collée). Threshold 97
    capture les vrais homonymes sans risque de faux positif.

    Comparaison case-insensitive : "Machine Learning" vs "Machine learning"
    score 100 ici (la casse finale est tranchée par le LLM judge sur les
    clusters ambigus, et par max(len) sur les clusters auto-mergés).

    Note : on exige la similarité min sur TOUTES les paires (pas la moyenne)
    pour qu'un cluster de 5 variantes avec 1 outlier ne soit pas auto-mergé.
    """
    n = len(variants)
    if n < 2:
        return True
    lowered = [v.lower().strip() for v in variants]
    for i in range(n):
        for j in range(i + 1, n):
            if fuzz.token_sort_ratio(lowered[i], lowered[j]) < threshold:
                return False
    return True


def judge_cluster(variants: list[str], llm: BaseChatModel) -> JudgeResult:
    """Appelle le LLM pour trancher members vs splits sur un cluster.

    Utilise `with_structured_output(JudgeResult, method="function_calling")`.
    On force `function_calling` car le mode JSON par défaut n'est pas
    supporté par GLM-4.7 sur SiliconFlow (BadRequest 20024 "Json mode is
    not supported for this model"). Le function calling l'est, comme on
    l'utilise déjà côté Phase B.

    Args:
        variants: Liste des variantes brutes (telles que retournées par
                  le LLM Vision, casse préservée).
        llm: Instance ChatOpenAI déjà configurée (typiquement issue de
             agents.llm.get_agent_llm()).

    Returns:
        JudgeResult avec canonical, members, splits.
    """
    structured_llm = llm.with_structured_output(JudgeResult, method="function_calling")
    user_prompt = (
        f"Voici {len(variants)} variantes candidates à fusion :\n\n"
        f"{json.dumps(variants, ensure_ascii=False, indent=2)}\n\n"
        "Tranche : quel canonical, quels members synonymes, quelles "
        "variantes à séparer (splits)."
    )
    return structured_llm.invoke([
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=user_prompt),
    ])


def _judge_cache_path(profile: str) -> Path:
    """Chemin du cache des décisions LLM judge pour un profil."""
    # Import local pour éviter une dépendance circulaire au import-time
    from dashboard import data
    return data.get_project_root() / "profiles" / profile / ".cache" / "theme-judge.json"


def _load_judge_cache(profile: str) -> dict[str, dict]:
    path = _judge_cache_path(profile)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_judge_cache(profile: str, cache: dict[str, dict]) -> None:
    path = _judge_cache_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def judge_clusters(
    clusters: list[list[str]],
    profile: str,
    llm: BaseChatModel,
    *,
    auto_merge_threshold: int = 97,
    use_cache: bool = True,
    on_progress: Callable[[int, int], None] | None = None,
    max_workers: int = 8,
) -> list[JudgeResult]:
    """Pipeline complet : skip auto + cache + LLM judge (parallélisé).

    Pour chaque cluster :
      - 1 variante seulement → JudgeResult trivial (members = [variante]).
      - 2+ variantes très proches (similarité ≥ auto_merge_threshold sur
        toutes les paires) → JudgeResult auto-fusionné, pas d'appel LLM.
      - 2+ variantes ambigües → cache lookup, sinon appel LLM, persistance.

    Les appels LLM (le seul travail coûteux) sont parallélisés sur un
    ThreadPoolExecutor — chaque appel est indépendant (Pydantic structured
    output sans état partagé). Sur ~700 clusters LLM, parallélisation 8x
    fait passer un run de ~3h à ~25 min.

    Args:
        clusters: Sortie de cluster_themes() — liste de listes de variantes.
        profile: Profil pour le chemin du cache.
        llm: ChatOpenAI prêt (typiquement get_agent_llm()).
        auto_merge_threshold: Seuil pour skip LLM (défaut 97).
        use_cache: Désactivable pour les tests.
        on_progress: Callback `(done, total)` invoqué après chaque cluster
                     (utile pour status.json en runtime UI/dashboard).
                     Thread-safe (appelé sous lock).
        max_workers: Nombre de threads pour les appels LLM (défaut 8).
                     Mettre 1 pour désactiver la parallélisation (tests).

    Returns:
        Liste de JudgeResult, un par cluster d'entrée (ordre préservé).
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    cache = _load_judge_cache(profile) if use_cache else {}
    total = len(clusters)
    results: list[JudgeResult | None] = [None] * total
    needs_llm: list[tuple[int, list[str], str]] = []  # (index, variants, cache_key)

    # Étape 1 : traitement instantané (singleton / auto-merge / cache hit)
    # — en série, c'est sub-milliseconde par cluster.
    done = 0
    for i, variants in enumerate(clusters):
        if len(variants) < 2:
            results[i] = JudgeResult(
                canonical=variants[0] if variants else "",
                members=list(variants),
            )
            done += 1
        elif should_auto_merge(variants, threshold=auto_merge_threshold):
            results[i] = JudgeResult(
                canonical=max(variants, key=len),
                members=list(variants),
            )
            done += 1
        else:
            key = _stable_cluster_key(variants)
            if use_cache and key in cache:
                results[i] = JudgeResult(**cache[key])
                done += 1
            else:
                needs_llm.append((i, variants, key))

    # Notifie la progression après le pré-tri (souvent des dizaines de
    # milliers de clusters instantanés traités d'un coup).
    if on_progress is not None:
        on_progress(done, total)

    if not needs_llm:
        return results  # type: ignore[return-value] — tous remplis

    # Étape 2 : appels LLM parallélisés
    lock = threading.Lock()
    cache_dirty = False
    done_counter = done  # capturé par closure

    def judge_one(item: tuple[int, list[str], str]) -> tuple[int, str, JudgeResult]:
        idx, variants, key = item
        result = judge_cluster(variants, llm)
        return idx, key, result

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(judge_one, item): item for item in needs_llm}
        for future in as_completed(futures):
            try:
                idx, key, result = future.result()
            except Exception:  # noqa: BLE001
                # Sur erreur LLM (timeout, parsing, ...) on enregistre un
                # JudgeResult fallback identité — le pipeline continue.
                item = futures[future]
                idx, variants, key = item
                result = JudgeResult(
                    canonical=variants[0] if variants else "",
                    members=list(variants[:1]),
                )
            results[idx] = result
            with lock:
                if use_cache:
                    cache[key] = result.model_dump()
                    cache_dirty = True
                done_counter += 1
                if on_progress is not None:
                    on_progress(done_counter, total)

    if cache_dirty and use_cache:
        _save_judge_cache(profile, cache)

    return results  # type: ignore[return-value] — tous remplis
