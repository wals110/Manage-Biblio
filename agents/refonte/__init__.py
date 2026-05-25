"""Agent de refonte de taxonomie — Phases A (Diagnostic) / B (Proposition) / C (Dialog).

Voir docs/refonte-agent-spec.md pour la spec complète.

Implémentation incrémentale :
    A.1 — Scaffolding LangGraph (ce module)
    A.2 — Outils read-only (tools.py, à venir)
    A.3 — Agent diagnostic (LLM, à venir)
    A.4 — Endpoint + UI (dashboard/, à venir)
    A.5 — Validation sur biblio réelle
"""

from agents.refonte.diagnostic import build_diagnostic_graph
from agents.refonte.state import RefonteState
from agents.refonte.tools import (
    compute_folder_overlap,
    count_files_per_folder,
    find_orphan_themes,
    get_classifier_breakdown,
    list_folders,
    list_themes_per_folder,
    list_vision_themes,
    read_theme_mapping,
)

__all__ = [
    "RefonteState",
    "build_diagnostic_graph",
    "compute_folder_overlap",
    "count_files_per_folder",
    "find_orphan_themes",
    "get_classifier_breakdown",
    "list_folders",
    "list_themes_per_folder",
    "list_vision_themes",
    "read_theme_mapping",
]
