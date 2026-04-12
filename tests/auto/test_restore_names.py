"""Tests pour scripts/restore_original_names.py."""

import csv
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# Ajouter scripts/ au path pour importer restore_original_names
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))

from restore_original_names import build_mapping, restore_names  # noqa: E402


class TestBuildMapping(unittest.TestCase):
    """Tests pour build_mapping."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _write_log(self, name: str, rows: list[tuple[str, str, str]]) -> Path:
        """Écrit un log_renommage_*.csv."""
        path = self.tmp / name
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["ancien_chemin", "nouveau_chemin", "status"])
            for row in rows:
                w.writerow(row)
        return path

    def test_simple_mapping(self):
        self._write_log("log_renommage_20260101_100000.csv", [
            ("/lib/old1.pdf", "/lib/new1.pdf", "OK"),
            ("/lib/old2.pdf", "/lib/new2.pdf", "OK"),
        ])
        mapping = build_mapping(self.tmp)
        self.assertEqual(mapping["new1.pdf"], "old1.pdf")
        self.assertEqual(mapping["new2.pdf"], "old2.pdf")

    def test_skip_non_ok_status(self):
        self._write_log("log_renommage_20260101_100000.csv", [
            ("/lib/old1.pdf", "/lib/new1.pdf", "OK"),
            ("/lib/old2.pdf", "/lib/new2.pdf", "ERROR"),
        ])
        mapping = build_mapping(self.tmp)
        self.assertIn("new1.pdf", mapping)
        self.assertNotIn("new2.pdf", mapping)

    def test_most_recent_log_wins(self):
        """Si un fichier apparaît dans plusieurs logs, le plus récent gagne."""
        # Log ancien
        self._write_log("log_renommage_20260101_100000.csv", [
            ("/lib/very_old.pdf", "/lib/intermediate.pdf", "OK"),
        ])
        # Log récent : intermediate.pdf → final.pdf
        self._write_log("log_renommage_20260102_100000.csv", [
            ("/lib/intermediate.pdf", "/lib/final.pdf", "OK"),
        ])
        mapping = build_mapping(self.tmp)
        # final.pdf doit être mappé vers intermediate.pdf (log le plus récent)
        self.assertEqual(mapping["final.pdf"], "intermediate.pdf")

    def test_empty_logs_dir(self):
        mapping = build_mapping(self.tmp)
        self.assertEqual(mapping, {})


class TestRestoreNames(unittest.TestCase):
    """Tests pour restore_names."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.library = self.tmp / "library"
        self.logs = self.tmp / "logs"
        self.library.mkdir()
        self.logs.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _make_pdf(self, relative: str):
        path = self.library / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fake")

    def _write_log(self, name: str, rows: list[tuple[str, str, str]]):
        path = self.logs / name
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["ancien_chemin", "nouveau_chemin", "status"])
            for row in rows:
                w.writerow(row)

    def test_restore_flat(self):
        """Restaure un fichier renommé à plat."""
        self._make_pdf("new_name.pdf")
        self._write_log("log_renommage_20260101_100000.csv", [
            ("/any/old_name.pdf", "/any/new_name.pdf", "OK"),
        ])
        restored, _ = restore_names(self.library, self.logs, execute=True)
        self.assertEqual(restored, 1)
        self.assertTrue((self.library / "old_name.pdf").exists())
        self.assertFalse((self.library / "new_name.pdf").exists())

    def test_restore_in_subdirectory(self):
        """Restaure un fichier qui a été déplacé dans un sous-dossier après rename."""
        self._make_pdf("01-Informatique/new_name.pdf")
        self._write_log("log_renommage_20260101_100000.csv", [
            ("/lib/_INBOX/old_name.pdf", "/lib/_INBOX/new_name.pdf", "OK"),
        ])
        restored, _ = restore_names(self.library, self.logs, execute=True)
        self.assertEqual(restored, 1)
        self.assertTrue((self.library / "01-Informatique" / "old_name.pdf").exists())

    def test_dry_run_no_changes(self):
        self._make_pdf("new_name.pdf")
        self._write_log("log_renommage_20260101_100000.csv", [
            ("/any/old_name.pdf", "/any/new_name.pdf", "OK"),
        ])
        restored, _ = restore_names(self.library, self.logs, execute=False)
        self.assertEqual(restored, 1)
        # Dry-run : le fichier n'a pas été renommé
        self.assertTrue((self.library / "new_name.pdf").exists())
        self.assertFalse((self.library / "old_name.pdf").exists())

    def test_no_mapping_no_changes(self):
        self._make_pdf("some_file.pdf")
        restored, _ = restore_names(self.library, self.logs, execute=True)
        self.assertEqual(restored, 0)
        self.assertTrue((self.library / "some_file.pdf").exists())

    def test_collision_adds_suffix(self):
        """Si le nom original existe déjà, un suffixe est ajouté."""
        self._make_pdf("new_name.pdf")
        self._make_pdf("old_name.pdf")  # collision
        self._write_log("log_renommage_20260101_100000.csv", [
            ("/any/old_name.pdf", "/any/new_name.pdf", "OK"),
        ])
        restored, collisions = restore_names(self.library, self.logs, execute=True)
        self.assertEqual(restored, 1)
        self.assertEqual(collisions, 1)
        # Le fichier original existe toujours + une version suffixée
        self.assertTrue((self.library / "old_name.pdf").exists())
        self.assertTrue((self.library / "old_name (2).pdf").exists())


if __name__ == "__main__":
    unittest.main()
