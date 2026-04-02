"""
Logger module for Manage-Biblio project.

Provides a configured logger with:
- Console output with emoji formatting (like current prints)
- Optional file logging (in logs/ directory)
- Log levels: DEBUG (verbose), INFO (normal), WARNING, ERROR
"""

import os
import sys
import logging
from typing import Optional


# ── Formateurs ──

class ConsoleFormatter(logging.Formatter):
    """Formateur console : garde les emojis, pas de timestamp."""

    def format(self, record):
        # type: (logging.LogRecord) -> str
        return record.getMessage()


class FileFormatter(logging.Formatter):
    """Formateur fichier : timestamp + level + message (sans emojis)."""

    EMOJI_CHARS = set('📚🔍🤖🧵💾💰🚀📁📋✅❌⚠💥🧹⏭🔄✏️📝🔇👤🎯🧠⚡💡📊')

    def format(self, record):
        # type: (logging.LogRecord) -> str
        msg = record.getMessage()
        # Retirer les emojis pour le fichier log
        cleaned = ''.join(c for c in msg if c not in self.EMOJI_CHARS)
        # Nettoyer les espaces doubles
        while '  ' in cleaned:
            cleaned = cleaned.replace('  ', ' ')
        record.msg = cleaned.strip()
        record.args = None
        return super().format(record)


# ── Singleton logger ──

_logger = None  # type: Optional[logging.Logger]


def setup_logger(verbose=False, log_file=None):
    # type: (bool, Optional[str]) -> logging.Logger
    """Configure et retourne le logger biblio.

    Args:
        verbose: Si True, niveau DEBUG. Sinon INFO.
        log_file: Chemin optionnel vers un fichier de log.

    Returns:
        Le logger configuré.
    """
    global _logger

    if _logger is not None:
        # Mettre à jour le niveau si demandé
        level = logging.DEBUG if verbose else logging.INFO
        _logger.setLevel(level)
        for handler in _logger.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                handler.setLevel(level)
        return _logger

    logger = logging.getLogger('biblio')
    level = logging.DEBUG if verbose else logging.INFO
    logger.setLevel(level)
    logger.propagate = False

    # Handler console
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(ConsoleFormatter())
    logger.addHandler(console)

    # Handler fichier (optionnel)
    if log_file:
        log_dir = os.path.dirname(log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding='utf-8')
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(FileFormatter(
            fmt='%(asctime)s [%(levelname)-7s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        ))
        logger.addHandler(fh)

    _logger = logger
    return logger


def get_logger():
    # type: () -> logging.Logger
    """Retourne le logger biblio. Le crée avec les défauts si non initialisé."""
    global _logger
    if _logger is None:
        return setup_logger()
    return _logger
