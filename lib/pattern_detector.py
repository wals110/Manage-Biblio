"""
Pattern Detector — détection automatique de patterns de nommage via LLM.

Analyse un échantillon de noms de fichiers fournis par l'utilisateur,
envoie un unique appel LLM pour identifier le pattern commun,
et teste la couverture sur la bibliothèque complète.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from lib.llm_client import LLMClient
from lib.logger import get_logger

log = get_logger()


@dataclass
class PatternResult:
    """Résultat de la détection de pattern."""

    regex: str
    description: str
    confidence: float
    examples_matched: list[str]
    examples_rejected: list[str]
    coverage_count: int = 0
    coverage_total: int = 0
    false_positive_samples: list[str] = field(default_factory=list)


def _build_prompt(stems: list[str]) -> str:
    """Construit le prompt LLM pour la détection de pattern."""
    filenames_block = "\n".join(f"  - {s}" for s in stems)
    return f"""Tu es un expert en gestion de bibliothèque PDF.

Voici une liste de noms de fichiers (sans extension .pdf) qui partagent une convention de nommage :

{filenames_block}

Analyse ces noms et propose UN pattern regex Python qui capture leur structure commune.

Réponds UNIQUEMENT avec un objet JSON valide, rien d'autre :
{{
  "regex": "le pattern regex Python compatible re.search()",
  "description": "description courte en français du pattern",
  "confidence": 0.85,
  "examples_matched": ["exemple1", "exemple2", "exemple3"],
  "examples_rejected": ["contre-exemple1", "contre-exemple2", "contre-exemple3"]
}}

Règles :
- Le regex doit fonctionner avec re.search() sur le nom sans extension .pdf
- Utilise des classes Unicode pour les accents : [A-ZÀ-Ÿ] et [A-Za-zÀ-ÿ]
- La description doit être concise (ex: "Titre - Auteur capitalisés")
- confidence : 0.9+ seulement si le pattern est évident et non ambigu
- examples_matched : 3 noms qui correspondent au pattern
- examples_rejected : 3 noms qui ne correspondent PAS"""


def _parse_response(raw: str) -> dict | None:
    """Parse la réponse JSON du LLM, gère les fences markdown."""
    if not raw:
        return None

    text = raw.strip()

    # Retirer les fences markdown ```json ... ```
    fence_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()

    # Extraire le premier objet JSON
    json_match = re.search(r'\{.*\}', text, re.DOTALL)
    if not json_match:
        return None

    try:
        return json.loads(json_match.group())
    except json.JSONDecodeError:
        return None


def detect_pattern(
    filenames: list[str],
    api_key: str,
    endpoint: str,
    model: str,
    max_tokens: int = 800,
    verbose: bool = False,
) -> PatternResult | None:
    """Détecte un pattern de nommage à partir d'un échantillon de fichiers.

    Args:
        filenames: Liste de noms de fichiers (avec ou sans .pdf).
        api_key: Clé API LLM.
        endpoint: URL de l'endpoint LLM.
        model: Nom du modèle LLM.
        max_tokens: Tokens max pour la réponse.
        verbose: Mode verbeux.

    Returns:
        PatternResult ou None si la détection échoue.
    """
    if len(filenames) < 2:
        log.warning("  ⚠ Il faut au moins 2 fichiers pour détecter un pattern.")
        return None

    # Travailler sur les stems (sans extension)
    stems = [Path(f).stem for f in filenames]

    prompt = _build_prompt(stems)
    if verbose:
        log.info("  📝 Prompt envoyé au LLM (%d fichiers)", len(stems))

    client = LLMClient(
        api_key=api_key, endpoint=endpoint, model=model, verbose=verbose
    )
    raw = client.call(prompt, max_tokens=max_tokens)

    if raw is None:
        log.warning("  ⚠ Le LLM n'a pas répondu.")
        return None

    data = _parse_response(raw)
    if data is None:
        log.warning("  ⚠ Réponse JSON invalide du LLM : %s", raw[:200])
        return None

    regex = data.get("regex", "")
    if not regex:
        log.warning("  ⚠ Pas de regex dans la réponse du LLM.")
        return None

    # Valider la regex
    try:
        re.compile(regex)
    except re.error as e:
        log.warning("  ⚠ Regex invalide reçue du LLM : %s (%s)", regex, e)
        return None

    return PatternResult(
        regex=regex,
        description=data.get("description", ""),
        confidence=float(data.get("confidence", 0.0)),
        examples_matched=data.get("examples_matched", []),
        examples_rejected=data.get("examples_rejected", []),
    )


def test_coverage(
    result: PatternResult,
    all_filenames: list[str],
    input_stems: list[str] | None = None,
    max_false_positive_samples: int = 10,
) -> PatternResult:
    """Teste la couverture d'un pattern sur une bibliothèque complète.

    Args:
        result: Le PatternResult à tester.
        all_filenames: Tous les noms de fichiers de la bibliothèque.
        input_stems: Les stems fournis par l'utilisateur (pour filtrer les faux positifs).
        max_false_positive_samples: Nombre max d'exemples de faux positifs.

    Returns:
        Le PatternResult mis à jour avec les stats de couverture.
    """
    pattern = re.compile(result.regex)
    input_set = set(s.lower() for s in (input_stems or []))

    matched = 0
    false_positives: list[str] = []

    for f in all_filenames:
        stem = Path(f).stem
        if pattern.search(stem):
            matched += 1
            # Faux positif = matche mais n'était pas dans la sélection utilisateur
            if input_set and stem.lower() not in input_set:
                if len(false_positives) < max_false_positive_samples:
                    false_positives.append(stem)

    result.coverage_count = matched
    result.coverage_total = len(all_filenames)
    result.false_positive_samples = false_positives

    return result
