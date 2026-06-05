#!/usr/bin/env python3
"""Tests pour agents.refonte.mutations — C.1 Phase C.

Stratégie : on mock les helpers `dashboard.taxonomy.*` pour isoler la
logique de wrapping (backup + journal). Quelques tests d'intégration
vérifient le flow complet sur un profil temporaire.
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from agents.refonte import agent_backup, agent_journal, mutations  # noqa: E402
from dashboard import data  # noqa: E402
from dashboard import taxonomy as _tax  # noqa: E402


class _MutBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="klodo_mut_"))
        (self.tmp / "profiles" / "p").mkdir(parents=True)
        self._patcher = mock.patch.object(
            data, "get_project_root", return_value=self.tmp,
        )
        self._patcher.start()
        # YAML de production minimal pour pouvoir créer un backup
        (self.tmp / "profiles" / "p" / "tree.yaml").write_text(
            "folders:\n  - A\n  - B\n", encoding="utf-8",
        )
        (self.tmp / "profiles" / "p" / "theme_mapping.yaml").write_text(
            "theme1: A\n", encoding="utf-8",
        )

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


# ─── add_folder ─────────────────────────────────────────────────────────────


class TestAddFolder(_MutBase):
    @mock.patch.object(_tax, "create_folder")
    def test_happy_path_journals_and_backups(self, mock_create):
        mock_create.return_value = {"path": "X", "created": True}
        out = mutations.add_folder("p", parent="", name="X")
        self.assertEqual(out["tool"], "add_folder")
        self.assertIsNotNone(out["backup_dir"])
        # Vérifie journal
        entries = agent_journal.read_journal("p")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["tool"], "add_folder")
        self.assertEqual(entries[0]["result"], "ok")
        # Vérifie backup existe physiquement
        self.assertIn(out["backup_dir"],
                     {b["name"] for b in agent_backup.list_backups("p")})

    @mock.patch.object(_tax, "create_folder")
    def test_taxonomy_error_journals_error_and_raises(self, mock_create):
        mock_create.side_effect = ValueError("folder exists")
        with self.assertRaises(mutations.MutationError):
            mutations.add_folder("p", parent="", name="X")
        entries = agent_journal.read_journal("p")
        self.assertEqual(entries[0]["result"], "error")
        self.assertIn("folder exists", entries[0]["error"])

    @mock.patch.object(_tax, "create_folder")
    def test_uses_provided_batch_id(self, mock_create):
        mock_create.return_value = {}
        bid = agent_journal.generate_batch_id()
        out = mutations.add_folder("p", parent="", name="X", batch_id=bid)
        self.assertEqual(out["batch_id"], bid)


# ─── add_theme_mapping ──────────────────────────────────────────────────────


class TestAddThemeMapping(_MutBase):
    @mock.patch.object(_tax, "add_mapping")
    def test_happy_path(self, mock_add):
        mock_add.return_value = {"theme": "ML", "folder": "X"}
        out = mutations.add_theme_mapping("p", theme="ML", folder="X")
        self.assertEqual(out["tool"], "add_theme_mapping")
        mock_add.assert_called_once_with("p", "ML", "X")
        entries = agent_journal.read_journal("p")
        self.assertEqual(entries[0]["tool"], "add_theme_mapping")
        self.assertEqual(entries[0]["args"], {"theme": "ML", "folder": "X"})


# ─── rename_folder ──────────────────────────────────────────────────────────


class TestRenameFolder(_MutBase):
    @mock.patch.object(_tax, "rename_folder")
    def test_happy_path(self, mock_rename):
        mock_rename.return_value = {"old_path": "A", "new_path": "Z"}
        out = mutations.rename_folder("p", old_path="A", new_name="Z")
        self.assertEqual(out["tool"], "rename_folder")
        mock_rename.assert_called_once_with("p", "A", "Z")


# ─── bulk_move_files ────────────────────────────────────────────────────────


class TestBulkMoveFiles(_MutBase):
    @mock.patch.object(_tax, "move_file")
    def test_all_succeed(self, mock_move):
        mock_move.return_value = {}
        out = mutations.bulk_move_files(
            "p", rel_paths=["a.pdf", "b.pdf", "c.pdf"], dest_folder="X",
        )
        self.assertEqual(out["result_data"]["moved"],
                         ["a.pdf", "b.pdf", "c.pdf"])
        self.assertEqual(out["result_data"]["errors"], [])
        # 3 appels move_file
        self.assertEqual(mock_move.call_count, 3)
        # 1 entrée journal globale (pas N)
        entries = agent_journal.read_journal("p")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["result"], "ok")

    @mock.patch.object(_tax, "move_file")
    def test_partial_failures_continue_and_journal_error(self, mock_move):
        def fake_move(prof, rel, dest):
            if rel == "b.pdf":
                raise ValueError("file not found")
            return {}
        mock_move.side_effect = fake_move
        out = mutations.bulk_move_files(
            "p", rel_paths=["a.pdf", "b.pdf", "c.pdf"], dest_folder="X",
        )
        self.assertEqual(out["result_data"]["moved"], ["a.pdf", "c.pdf"])
        self.assertEqual(len(out["result_data"]["errors"]), 1)
        self.assertEqual(out["result_data"]["errors"][0]["path"], "b.pdf")
        entries = agent_journal.read_journal("p")
        self.assertEqual(entries[0]["result"], "error")
        self.assertIn("1 of 3", entries[0]["error"])


# ─── merge_folders ──────────────────────────────────────────────────────────


class TestMergeFolders(_MutBase):
    @mock.patch.object(_tax, "delete_folder")
    @mock.patch.object(_tax, "move_file")
    @mock.patch.object(_tax, "list_files_in_folder")
    @mock.patch.object(_tax, "create_folder")
    @mock.patch.object(_tax, "get_snapshot")
    def test_happy_path_creates_dest_and_merges_sources(
        self, mock_snap, mock_create, mock_list, mock_move, mock_delete,
    ):
        # dest_path absent dans la snapshot
        mock_snap.return_value = {"folders": [{"path": "A"}, {"path": "B"}]}
        mock_list.return_value = {"files": [
            {"rel_path": "A/file1.pdf"},
            {"rel_path": "A/file2.pdf"},
        ]}
        mock_create.return_value = {}
        mock_move.return_value = {}
        mock_delete.return_value = {}

        out = mutations.merge_folders(
            "p", src_paths=["A"], dest_path="MERGED",
        )
        self.assertTrue(out["result_data"]["created_dest"])
        self.assertEqual(out["result_data"]["sources_merged"], ["A"])
        # create_folder appelé pour MERGED
        mock_create.assert_called_once_with("p", "", "MERGED")
        # move_file appelé pour les 2 fichiers de A
        self.assertEqual(mock_move.call_count, 2)
        # delete_folder appelé pour A
        mock_delete.assert_called_once_with("p", "A", force=True)

    @mock.patch.object(_tax, "get_snapshot")
    def test_dest_already_exists_skips_create(self, mock_snap):
        mock_snap.return_value = {"folders": [{"path": "MERGED"}]}
        with mock.patch.object(_tax, "create_folder") as mock_create, \
             mock.patch.object(_tax, "list_files_in_folder",
                               return_value={"files": []}), \
             mock.patch.object(_tax, "delete_folder"):
            out = mutations.merge_folders(
                "p", src_paths=["A"], dest_path="MERGED",
            )
            self.assertFalse(out["result_data"]["created_dest"])
            mock_create.assert_not_called()


# ─── Batch grouping ─────────────────────────────────────────────────────────


class TestBatchGrouping(_MutBase):
    """Plusieurs mutations dans le même batch partagent UN seul backup."""

    @mock.patch.object(_tax, "add_mapping")
    @mock.patch.object(_tax, "create_folder")
    def test_shared_backup_dir(self, mock_create, mock_add):
        mock_create.return_value = {}
        mock_add.return_value = {}
        bid = agent_journal.generate_batch_id()
        r1 = mutations.add_folder("p", parent="", name="X", batch_id=bid)
        r2 = mutations.add_theme_mapping("p", theme="t", folder="X",
                                          batch_id=bid)
        # Même backup_dir pour les 2 mutations du batch
        self.assertEqual(r1["backup_dir"], r2["backup_dir"])
        # Mais 2 entrées dans le journal
        entries = agent_journal.read_journal("p")
        self.assertEqual(len(entries), 2)
        # 1 seul backup physique
        self.assertEqual(len(agent_backup.list_backups("p")), 1)

    @mock.patch.object(_tax, "create_folder")
    def test_separate_batches_have_separate_backups(self, mock_create):
        mock_create.return_value = {}
        r1 = mutations.add_folder("p", parent="", name="X")
        r2 = mutations.add_folder("p", parent="", name="Y")
        # batch_id et backup_dir différents
        self.assertNotEqual(r1["batch_id"], r2["batch_id"])
        self.assertNotEqual(r1["backup_dir"], r2["backup_dir"])


if __name__ == "__main__":
    unittest.main()
