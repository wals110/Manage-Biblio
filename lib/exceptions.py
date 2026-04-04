"""
Hiérarchie d'exceptions pour Klodo.

Toutes les exceptions métier héritent de KlodoError, ce qui permet
de les attraper d'un seul bloc dans le point d'entrée (klodo.py).
"""


class KlodoError(Exception):
    """Exception de base pour toutes les erreurs Klodo."""


class ConfigError(KlodoError):
    """Erreur de configuration : profil manquant, YAML invalide, chemin absent."""


class LLMError(KlodoError):
    """Erreur lors d'un appel LLM (API inaccessible, réponse invalide)."""


class ClassificationError(KlodoError):
    """Erreur dans le pipeline de classification."""


class SafetyError(KlodoError):
    """Violation de sécurité (inbox == target, inbox == fallback, etc.)."""
