"""
Checkpoint module for Manage-Biblio pipeline.

Provides thread-safe checkpoint/resume with atomic writes.
Compatible with the existing progress_migration.json / progress_ocr.json format.

Python 3.9+
"""

import json
import os
import threading
from datetime import datetime
from typing import Callable, Dict, Optional

from lib.logger import get_logger

__version__ = "4.0.0"

log = get_logger()


class CheckpointManager:
    """
    Thread-safe checkpoint persistence with atomic writes.

    Compatible with both legacy format (v3: 'results' key) and new format (v4: 'data' key).
    """

    RETRYABLE_STATUSES = {"erreur_api", "erreur_extraction", "confiance_basse"}
    RECLASSIFIABLE_STATUSES = {"non_classifié", "renommé_seul"}

    def __init__(self, logs_dir, filename="progress.json"):
        # type: (str, str) -> None
        self.logs_dir = logs_dir
        self.filename = filename
        self._lock = threading.Lock()
        os.makedirs(logs_dir, exist_ok=True)

    @property
    def path(self):
        # type: () -> str
        return os.path.join(self.logs_dir, self.filename)

    # ── Internal (no lock) ──────────────────────────────────────────────

    def _load_raw(self):
        # type: () -> Dict[str, Dict]
        """Load checkpoint without acquiring lock (caller must hold lock)."""
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                content = json.load(f)
            if isinstance(content, dict):
                # v4 format: 'data' key
                if "data" in content:
                    return content.get("data", {})
                # v3 format: 'results' key
                if "results" in content:
                    return content.get("results", {})
            return {}
        except (json.JSONDecodeError, KeyError):
            log.warning("  ⚠ Fichier de progression corrompu \u2014 redémarrage à zéro")
            return {}

    def _save_raw(self, progress, model="", total_files=0):
        # type: (Dict[str, Dict], str, int) -> None
        """Save checkpoint without acquiring lock (caller must hold lock)."""
        tmp_path = self.path + ".tmp"
        data = {
            "version": __version__,
            "model": model,
            "last_update": datetime.now().isoformat(),
            "total_files": total_files,
            "processed": len(progress),
            "results": progress,
        }
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp_path, self.path)

    # ── Public API (thread-safe) ────────────────────────────────────────

    def load(self):
        # type: () -> Dict[str, Dict]
        """Load checkpoint. Returns {file_path: result_dict}."""
        with self._lock:
            return self._load_raw()

    def save(self, progress, model="", total_files=0):
        # type: (Dict[str, Dict], str, int) -> None
        """Save checkpoint with atomic write."""
        with self._lock:
            self._save_raw(progress, model, total_files)

    def clear(self):
        # type: () -> None
        """Delete checkpoint file."""
        with self._lock:
            if os.path.exists(self.path):
                os.remove(self.path)
                log.info("🗑  Progression précédente supprimée.")

    def retry_errors(self):
        # type: () -> int
        """Remove retryable errors from checkpoint. Returns count removed."""
        with self._lock:
            progress = self._load_raw()
            if not progress:
                log.info("  Pas de checkpoint trouvé.")
                return 0

            to_retry = [k for k, v in progress.items()
                        if v.get("status") in self.RETRYABLE_STATUSES]
            if not to_retry:
                log.error("  Aucun fichier en erreur dans le checkpoint.")
                return 0

            for k in to_retry:
                del progress[k]

            self._save_raw(progress)
            log.info("🔄 {} fichiers retirés du checkpoint (seront retraités)".format(len(to_retry)))
            return len(to_retry)

    def reclassify(self, classify_fn, min_confidence=0.5):
        # type: (Callable[[str, float], Optional[str]], float) -> int
        """
        Reclassify non_classifié/renommé_seul entries.

        classify_fn(theme, confidence) -> Optional[str] (destination path).
        Uses field names compatible with ocr_cover.py: 'theme_detecte', 'confiance'.
        """
        with self._lock:
            progress = self._load_raw()
            if not progress:
                log.info("  Pas de checkpoint trouvé pour reclassifier.")
                return 0

            reclassified = 0
            for path, result in progress.items():
                if result.get("status") not in self.RECLASSIFIABLE_STATUSES:
                    continue
                theme = result.get("theme_detecte", "")
                confidence = float(result.get("confiance", 0))
                if theme and confidence >= min_confidence:
                    dest = classify_fn(theme, confidence)
                    if dest:
                        result["destination"] = dest
                        result["score"] = confidence
                        result["mot_cle"] = "LLM:{}".format(theme)
                        result["status"] = "classifié"
                        reclassified += 1

            if reclassified > 0:
                self._save_raw(progress)
            log.info("🔄 Reclassification : {} fichiers récupérés".format(reclassified))
            return reclassified
