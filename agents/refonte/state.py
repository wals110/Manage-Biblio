"""État de l'agent Refonte — partagé entre les phases A/B/C.

À ce stade (A.1 scaffolding), le state est volontairement minimal. Les phases
suivantes (A.2 outils, A.3 LLM) viendront enrichir avec `tool_results`,
`anomalies`, `report_markdown`, etc.
"""

from typing import TypedDict


class RefonteState(TypedDict, total=False):
    """État partagé entre les nœuds du graphe de refonte.

    `total=False` car les champs sont remplis progressivement par les nœuds
    (le graphe commence avec uniquement `profile` et `phase`).

    Attributes:
        profile: Nom du profil Klodo cible (ex. "default", "test")
        phase: Phase courante de l'agent — "A" (diagnostic), "B" (proposition), "C" (dialog)
        run_id: Identifiant unique de la run (UUID), sert de clé pour .cache/refonte/<run_id>/
        status: État de progression — "pending" | "running" | "done" | "error"
        error: Message d'erreur si status == "error"
    """

    profile: str
    phase: str
    run_id: str
    status: str
    error: str
