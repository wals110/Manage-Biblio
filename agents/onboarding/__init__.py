"""Agent Onboarding — bootstrap d'un profil depuis un répertoire brut.

Pipeline (pas un agent ReAct) : Scan & estimation → Analyse & proposition.
Voir docs/superpowers/specs/2026-06-14-onboarding-agent-design.md
"""

from agents.onboarding.proposition import build_proposal, write_proposal  # noqa: F401
from agents.onboarding.scan import estimate_cost, scan_directory  # noqa: F401
