"""Pipeline complet de dédupli des thèmes — orchestre Phases 1+2+3 et
persiste le résultat dans `profiles/<p>/.cache/theme-canon.json`.

Ce module est le point d'entrée du chantier dédupli :
  - `extract_themes_from_vision_cache(profile)` — extrait {raw: count}
  - `build_canon_table(profile, llm)` — pipeline end-to-end + persistance
  - `load_canon_table(profile)` — lecture du mapping pour le branchement
    dans `dashboard.taxonomy._aggregate_themes_llm`
  - `canonicalize(theme, canon_table)` — résout 1 raw_theme → canonical

Le fichier `theme-canon.json` produit est consommé en runtime par
l'aggregator des thèmes : chaque raw_theme est résolu vers son canonical
avant cumul des counts. Le branchement est opt-in via la présence du
fichier — pas de feature flag YAML, pas de breaking change.

Format du fichier :
```json
{
  "version": 1,
  "built_at": "2026-05-27T...",
  "raw_count": 15376,
  "canonical_count": 12345,
  "mapping": {
    "Machine Learning": "Machine Learning",
    "Machine learning": "Machine Learning",
    "Web Application Development": "Web Application Development",
    "Web application development": "Web Application Development",
    "iOS Application Development": "iOS Application Development"
  }
}
```
"""

from __future__ import annotations

import ast
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel

from lib.theme_judge import JudgeResult, judge_clusters
from lib.theme_normalizer import cluster_themes

CANON_VERSION = 2
DEFAULT_CLUSTER_THRESHOLD = 92

# Type alias : callback de progression invoqué après chaque cluster traité.
# Signature : (clusters_done, clusters_total, phase) → None
ProgressCallback = Callable[[int, int, str], None]


def _canon_path(profile: str) -> Path:
    """Chemin du fichier de canonisation pour un profil."""
    # Import local pour éviter une dépendance circulaire au import-time
    from dashboard import data
    return data.get_project_root() / "profiles" / profile / ".cache" / "theme-canon.json"


def _vision_cache_path(profile: str) -> Path:
    from dashboard import data
    return data.get_project_root() / "profiles" / profile / ".cache" / "vision_cache.json"


def _parse_cached_result(raw: Any) -> dict | None:
    """Décode le champ `result` du vision_cache.

    Historiquement stocké de plusieurs façons :
      - dict natif (récent)
      - str JSON
      - str repr Python (`{'title': '...', ...}`) — legacy
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    try:
        if s.startswith("{'"):
            parsed = ast.literal_eval(s)
        else:
            parsed = json.loads(s)
    except (ValueError, SyntaxError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def extract_themes_from_vision_cache(profile: str) -> dict[str, int]:
    """Extrait {raw_theme: count} depuis vision_cache.json.

    Pas de filtrage par confidence — la canonisation est indépendante
    de la confiance (un thème avec faible confidence reste un thème
    à dédupliquer). Le filtrage par confidence reste géré côté
    `dashboard.taxonomy._aggregate_themes_llm`.

    Returns:
        Dict {raw_theme: count_occurrences}. Vide si pas de cache.
    """
    path = _vision_cache_path(profile)
    if not path.exists():
        return {}
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    counts: dict[str, int] = {}
    for entry in cache.values():
        if not isinstance(entry, dict):
            continue
        result = _parse_cached_result(entry.get("result"))
        if result is None:
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


def assemble_canon_mapping(
    clusters: list[dict],
    judgments: list[JudgeResult],
) -> dict[str, str]:
    """Construit `{raw_theme: canonical}` à partir des décisions LLM.

    Pour chaque cluster + sa décision :
      - members (synonymes validés)  → canonical
      - splits  (sous-domaines)      → eux-mêmes (identité)
      - variantes non mentionnées dans members ni splits → eux-mêmes
        (le LLM les a oubliées, on les préserve par défaut)

    Pour les clusters singleton (1 variante) : identity mapping.
    Pour les clusters auto-mergés : tous les raw_members → canonical
    choisi par max(len), via le JudgeResult fabriqué dans
    `judge_clusters` sans appel LLM.

    Args:
        clusters: Sortie de `cluster_themes()` — liste de dicts avec
                  `raw_members`.
        judgments: Liste de `JudgeResult`, même longueur et même ordre
                   que `clusters`.

    Returns:
        Dict {raw_theme: canonical}.
    """
    if len(clusters) != len(judgments):
        raise ValueError(
            f"clusters ({len(clusters)}) et judgments ({len(judgments)}) "
            "doivent avoir la même longueur"
        )
    mapping: dict[str, str] = {}
    for cluster, judgment in zip(clusters, judgments, strict=True):
        canonical = judgment.canonical
        # Membres validés → canonical
        member_set: set[str] = set()
        for member in judgment.members:
            mapping[member] = canonical
            member_set.add(member)
        # Splits → identité (le LLM a explicitement séparé)
        split_set: set[str] = set()
        for split in judgment.splits:
            mapping[split.theme] = split.theme
            split_set.add(split.theme)
        # Filet de sécurité : si le LLM a oublié une variante (ni
        # member ni split), on la préserve par défaut (identité). Ça
        # garantit que toute variante du cluster a au moins une entrée
        # dans le mapping.
        for raw in cluster.get("raw_members", []):
            if raw not in member_set and raw not in split_set:
                mapping.setdefault(raw, raw)
    return mapping


def build_canon_table(
    profile: str,
    llm: BaseChatModel,
    *,
    threshold: int = DEFAULT_CLUSTER_THRESHOLD,
    use_judge_cache: bool = True,
    on_progress: ProgressCallback | None = None,
    mode: str = "syntactic",
    vocabulary_top_n: int = 500,
) -> dict[str, Any]:
    """Pipeline end-to-end : extraction → clustering → LLM judge → persistance.

    Deux modes disponibles :
      - `"syntactic"` (défaut) : Phases 1+2+3 — normalisation + clustering
        fuzzy + LLM judge. Capture variantes orthographiques et acronymes.
        Réduction observée ~11% sur le profil default.
      - `"semantic"` (C-light) : vocabulaire bootstrapé + canonisation LLM
        sémantique. Capture aussi les synonymes éloignés (ML ↔ Machine
        Learning ↔ AI). Réduction attendue ~70-80%.

    Args:
        profile: Nom du profil.
        llm: ChatOpenAI configuré (typiquement get_agent_llm()).
        threshold: Seuil de similarité Phase 2 / re-clustering final (défaut 92).
        use_judge_cache: Réutilise les décisions LLM persistées (défaut True).
        on_progress: Callback `(done, total, phase)` invoqué pendant le run.
                     Phases (syntactic) : extracting → clustering → judging → done
                     Phases (semantic)  : extracting → canonicalizing → reclustering → done
        mode: "syntactic" ou "semantic".
        vocabulary_top_n: Taille du vocabulaire de référence en mode semantic.

    Returns:
        La table de canonisation complète (incluant métadonnées et clusters
        enrichis pour l'UI). Écrite sur disque dans
        `profiles/<p>/.cache/theme-canon.json`.
    """
    if mode not in ("syntactic", "semantic", "source"):
        raise ValueError(
            f"mode must be 'syntactic', 'semantic' or 'source', got {mode!r}"
        )

    if on_progress:
        on_progress(0, 0, "extracting")
    themes = extract_themes_from_vision_cache(profile)
    if not themes:
        # Profil sans vision_cache → table vide mais quand même persistée
        table = {
            "version": CANON_VERSION,
            "built_at": _now_iso(),
            "raw_count": 0,
            "canonical_count": 0,
            "mapping": {},
            "clusters": [],
            "mode": mode,
        }
        save_canon_table(profile, table)
        if on_progress:
            on_progress(0, 0, "done")
        return table

    if mode == "semantic":
        return _build_canon_table_semantic(
            profile, llm, themes,
            threshold=threshold,
            use_cache=use_judge_cache,
            vocabulary_top_n=vocabulary_top_n,
            on_progress=on_progress,
        )

    if mode == "source":
        return _build_canon_table_source(
            profile, llm, themes,
            threshold=threshold,
            use_cache=use_judge_cache,
            vocabulary_top_n=vocabulary_top_n,
            on_progress=on_progress,
        )

    # Mode syntactique (Phases 1+2+3) — comportement historique
    if on_progress:
        on_progress(0, len(themes), "clustering")
    clusters = cluster_themes(themes.keys(), threshold=threshold)
    cluster_lists = [c["raw_members"] for c in clusters]
    total_clusters = len(cluster_lists)
    if on_progress:
        on_progress(0, total_clusters, "judging")

    # Adaptation du callback : judge_clusters fait (done, total), nous
    # rajoutons la phase fixe "judging".
    def _judge_progress(done: int, total: int) -> None:
        if on_progress:
            on_progress(done, total, "judging")

    judgments = judge_clusters(
        cluster_lists, profile, llm,
        use_cache=use_judge_cache,
        on_progress=_judge_progress,
    )
    mapping = assemble_canon_mapping(clusters, judgments)

    # Pour l'UI : chaque cluster avec le canonical décidé + members + splits
    # + count cumulé (somme des occurrences des raw_themes du cluster).
    clusters_serialized = []
    for cluster, judgment in zip(clusters, judgments, strict=True):
        raw_members = cluster.get("raw_members", [])
        count_cumulative = sum(themes.get(m, 0) for m in raw_members)
        clusters_serialized.append({
            "canonical": judgment.canonical,
            "members": judgment.members,
            "splits": [
                {"theme": s.theme, "reason": s.reason}
                for s in judgment.splits
            ],
            "raw_members": raw_members,
            "count_cumulative": count_cumulative,
        })

    if on_progress:
        on_progress(total_clusters, total_clusters, "done")

    table = {
        "version": CANON_VERSION,
        "built_at": _now_iso(),
        "raw_count": len(mapping),
        "canonical_count": len(set(mapping.values())),
        "threshold": threshold,
        "clusters": clusters_serialized,
        "mapping": mapping,
        "mode": "syntactic",
    }
    save_canon_table(profile, table)
    return table


def _build_canon_table_semantic(
    profile: str,
    llm: BaseChatModel,
    themes: dict[str, int],
    *,
    threshold: int,
    use_cache: bool,
    vocabulary_top_n: int,
    on_progress: ProgressCallback | None,
) -> dict[str, Any]:
    """Pipeline du mode "semantic" (C-light).

    Étapes :
      1. build_vocabulary : top-N par fréquence comme vocabulaire de référence
      2. canonicalize_themes : LLM produit {raw_theme: canonical} en parallèle
      3. Re-clustering du résultat via cluster_themes (Phase 1+2) pour
         gommer les doublons que le LLM aurait créés dans différents batches
      4. Assemblage final + serialisation des clusters pour l'UI
    """
    from lib.theme_canonicalizer import build_vocabulary, canonicalize_themes

    # Étape 1 : vocabulaire
    vocabulary = build_vocabulary(themes, top_n=vocabulary_top_n)

    if on_progress:
        on_progress(0, 0, "canonicalizing")

    # Étape 2 : canonisation LLM
    def _cano_progress(done: int, total: int) -> None:
        if on_progress:
            on_progress(done, total, "canonicalizing")

    raw_to_canon = canonicalize_themes(
        list(themes.keys()),
        vocabulary,
        llm,
        profile,
        use_cache=use_cache,
        on_progress=_cano_progress,
    )

    if on_progress:
        on_progress(0, 0, "reclustering")

    # Étape 3 : re-clustering des canoniques produits via Phase 1+2 pour
    # gommer les doublons résiduels (variantes ortho créées par le LLM).
    # On clusterise l'ensemble des canoniques uniques.
    unique_canons = sorted(set(raw_to_canon.values()))
    canon_clusters = cluster_themes(unique_canons, threshold=threshold)

    # Pour le choix du final canonical d'un cluster fuzzy : count cumulé MAX
    # (le plus représentatif). Tie-break sur longueur décroissante (préfère
    # le plus court à count égal), puis lexico pour déterminisme.
    canon_to_count: dict[str, int] = {c: 0 for c in unique_canons}
    for raw, canon in raw_to_canon.items():
        canon_to_count[canon] = canon_to_count.get(canon, 0) + themes.get(raw, 0)

    canon_to_final: dict[str, str] = {}
    for cluster in canon_clusters:
        members = cluster.get("raw_members", [])
        if not members:
            continue
        final = max(
            members,
            key=lambda m: (canon_to_count.get(m, 0), -len(m), m),
        )
        for m in members:
            canon_to_final[m] = final

    # Mapping final raw → canonical après fusion
    final_mapping: dict[str, str] = {
        raw: canon_to_final.get(canon, canon)
        for raw, canon in raw_to_canon.items()
    }

    # Étape 4 : groupes par canonical pour l'UI (équivalent des clusters
    # du mode syntactique, format identique consommé par le panel).
    grouped: dict[str, list[str]] = {}
    for raw, final in final_mapping.items():
        grouped.setdefault(final, []).append(raw)

    clusters_serialized = []
    for canonical, raw_members in grouped.items():
        # Pour le mode sémantique, on n'a pas de "splits" — tous les raws
        # qui ont été mappés vers ce canonical sont considérés members.
        count_cumulative = sum(themes.get(m, 0) for m in raw_members)
        clusters_serialized.append({
            "canonical": canonical,
            "members": list(raw_members),
            "splits": [],
            "raw_members": list(raw_members),
            "count_cumulative": count_cumulative,
        })
    # Tri par count cumulé décroissant
    clusters_serialized.sort(key=lambda c: -c["count_cumulative"])

    if on_progress:
        on_progress(len(themes), len(themes), "done")

    table = {
        "version": CANON_VERSION,
        "built_at": _now_iso(),
        "raw_count": len(final_mapping),
        "canonical_count": len(set(final_mapping.values())),
        "threshold": threshold,
        "vocabulary_top_n": vocabulary_top_n,
        "vocabulary_size": len(vocabulary),
        "clusters": clusters_serialized,
        "mapping": final_mapping,
        "mode": "semantic",
    }
    save_canon_table(profile, table)
    return table


def _build_canon_table_source(
    profile: str,
    llm: BaseChatModel,
    themes: dict[str, int],
    *,
    threshold: int,
    use_cache: bool,
    vocabulary_top_n: int,
    on_progress: ProgressCallback | None,
) -> dict[str, Any]:
    """Pipeline du mode "source" (C.2 — canonisation contextuelle).

    Différence avec semantic (C-light) :
      - C-light : (raw_theme seul) → LLM décide à l'aveugle
      - C.2     : (raw_theme + 5 titres représentatifs) → LLM voit le contexte

    Étapes :
      1. extract_themes_with_titles : pour chaque raw_theme, collecte les
         5 titres représentatifs depuis vision_cache.json
      2. build_vocabulary : top-N par fréquence (vocabulary de référence)
      3. resolve_themes_with_context : LLM produit {raw: canonical} avec
         les titres comme contexte de désambiguïsation, parallélisé
      4. Re-clustering du résultat via cluster_themes pour gommer les
         doublons résiduels (canoniques quasi-identiques entre batches)
      5. Assemblage final
    """
    from lib.theme_canonicalizer import build_vocabulary
    from lib.theme_resolver import (
        extract_themes_with_titles,
        resolve_themes_with_context,
    )

    if on_progress:
        on_progress(0, 0, "extracting")
    themes_titles = extract_themes_with_titles(profile)

    vocabulary = build_vocabulary(themes, top_n=vocabulary_top_n)

    if on_progress:
        on_progress(0, 0, "resolving")

    def _resolver_progress(done: int, total: int) -> None:
        if on_progress:
            on_progress(done, total, "resolving")

    raw_to_canon = resolve_themes_with_context(
        themes_titles,
        vocabulary,
        llm,
        profile,
        use_cache=use_cache,
        on_progress=_resolver_progress,
    )

    if on_progress:
        on_progress(0, 0, "reclustering")

    # Re-clustering des canoniques pour gommer les doublons résiduels
    unique_canons = sorted(set(raw_to_canon.values()))
    canon_clusters = cluster_themes(unique_canons, threshold=threshold)

    # Pour le choix du final canonical d'un cluster fuzzy : on prend la
    # variante avec le plus grand count cumulé parmi les raw_themes qui
    # pointaient vers elle. On évite ainsi de choisir "Web Application
    # Development with C# and .NET" (un canonical hyper-spécifique apparu
    # 1× dans un batch) comme représentant d'un cluster contenant
    # "Web Development" qui est apparu 100 fois.
    canon_to_count: dict[str, int] = {c: 0 for c in unique_canons}
    for raw, canon in raw_to_canon.items():
        canon_to_count[canon] = canon_to_count.get(canon, 0) + themes.get(raw, 0)

    canon_to_final: dict[str, str] = {}
    for cluster in canon_clusters:
        members = cluster.get("raw_members", [])
        if not members:
            continue
        # canonical = membre avec le count cumulé MAX (le plus représentatif).
        # Tie-break sur la longueur DÉCROISSANTE (préfère le plus court à
        # count égal, ex. "Mathematics" vs "Advanced Mathematics" choisit
        # "Mathematics" en cas d'égalité). Tie final sur l'ordre lexico
        # pour déterminisme.
        final = max(
            members,
            key=lambda m: (canon_to_count.get(m, 0), -len(m), m),
        )
        for m in members:
            canon_to_final[m] = final

    final_mapping: dict[str, str] = {
        raw: canon_to_final.get(canon, canon)
        for raw, canon in raw_to_canon.items()
    }

    grouped: dict[str, list[str]] = {}
    for raw, final in final_mapping.items():
        grouped.setdefault(final, []).append(raw)

    clusters_serialized = []
    for canonical, raw_members in grouped.items():
        count_cumulative = sum(themes.get(m, 0) for m in raw_members)
        # Pour la vue UI : sample des titres représentatifs du canonical
        # (concaténation des titres de ses raw_members, limitée à 5)
        sample_titles: list[str] = []
        seen_titles: set[str] = set()
        for member in raw_members:
            for title in themes_titles.get(member, []):
                if title in seen_titles or len(sample_titles) >= 5:
                    continue
                seen_titles.add(title)
                sample_titles.append(title)
            if len(sample_titles) >= 5:
                break
        clusters_serialized.append({
            "canonical": canonical,
            "members": list(raw_members),
            "splits": [],
            "raw_members": list(raw_members),
            "count_cumulative": count_cumulative,
            "sample_titles": sample_titles,
        })
    clusters_serialized.sort(key=lambda c: -c["count_cumulative"])

    if on_progress:
        on_progress(len(themes), len(themes), "done")

    table = {
        "version": CANON_VERSION,
        "built_at": _now_iso(),
        "raw_count": len(final_mapping),
        "canonical_count": len(set(final_mapping.values())),
        "threshold": threshold,
        "vocabulary_top_n": vocabulary_top_n,
        "vocabulary_size": len(vocabulary),
        "clusters": clusters_serialized,
        "mapping": final_mapping,
        "mode": "source",
    }
    save_canon_table(profile, table)
    return table


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def load_canon_table(profile: str) -> dict[str, str] | None:
    """Charge le mapping `{raw: canonical}` pour un profil.

    Returns:
        Le mapping seul (pas les métadonnées). `None` si le fichier
        n'existe pas ou est corrompu — dans ce cas l'appelant doit
        fallback sur l'identité (pas de canonisation).
    """
    path = _canon_path(profile)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    mapping = data.get("mapping")
    if not isinstance(mapping, dict):
        return None
    return mapping


def save_canon_table(profile: str, table: dict[str, Any]) -> None:
    """Persiste la table de canonisation sur disque (atomique)."""
    path = _canon_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(table, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


def canonicalize(theme: str, canon_table: dict[str, str] | None) -> str:
    """Résout un raw_theme vers son canonical via la table de canonisation.

    Fallback identity si la table est `None` ou si le thème n'y figure pas.
    Cette fonction est appelée pour CHAQUE thème agrégé — doit rester
    hot-path safe.
    """
    if not canon_table:
        return theme
    return canon_table.get(theme, theme)
