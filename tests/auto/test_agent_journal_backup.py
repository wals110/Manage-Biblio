#!/usr/bin/env python3
"""Tests pour agents.refonte.agent_journal + agent_backup — C.0 Phase C.

Couvre :
  - Journal append-only + read + filter by batch_id + list_batches
  - Backup create/restore/list/rotate
  - Lien journal ↔ backup via find_backup_for_batch
  - Rollback : record_undo + flag rolled_back dans list_batches
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from agents.refonte import agent_backup, agent_journal  # noqa: E402
from dashboard import data  # noqa: E402


class _AgentC0Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="klodo_agent_c0_"))
        (self.tmp / "profiles" / "p").mkdir(parents=True)
        self._patcher = mock.patch.object(
            data, "get_project_root", return_value=self.tmp,
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _profile_root(self) -> Path:
        return self.tmp / "profiles" / "p"

    def _seed_prod_yamls(self, *, tree="folders:\n  - A\n",
                        mapping="x: A\n"):
        (self._profile_root() / "tree.yaml").write_text(tree, encoding="utf-8")
        (self._profile_root() / "theme_mapping.yaml").write_text(
            mapping, encoding="utf-8",
        )


# ─── Journal ───────────────────────────────────────────────────────────────


class TestAgentJournalAppend(_AgentC0Base):
    def test_generate_batch_id_unique(self):
        b1 = agent_journal.generate_batch_id()
        b2 = agent_journal.generate_batch_id()
        self.assertNotEqual(b1, b2)
        # Format UUID4
        self.assertEqual(len(b1), 36)

    def test_append_creates_file_and_writes_one_line(self):
        bid = agent_journal.generate_batch_id()
        entry = agent_journal.append_entry(
            "p", tool="add_folder",
            args={"path": "X"}, result="ok", batch_id=bid,
            backup_dir="agent-foo-20260530-120000",
        )
        path = (self.tmp / "profiles" / "p" / ".cache"
                / "refonte" / "agent-journal.jsonl")
        self.assertTrue(path.exists())
        lines = path.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        loaded = json.loads(lines[0])
        self.assertEqual(loaded["tool"], "add_folder")
        self.assertEqual(loaded["batch_id"], bid)
        self.assertEqual(loaded["backup_dir"], "agent-foo-20260530-120000")
        # Le retour contient bien le ts
        self.assertIn("ts", entry)

    def test_append_multiple_entries(self):
        bid = agent_journal.generate_batch_id()
        for i in range(3):
            agent_journal.append_entry(
                "p", tool=f"tool_{i}", args={}, result="ok",
                batch_id=bid, backup_dir=None,
            )
        entries = agent_journal.read_journal("p")
        self.assertEqual(len(entries), 3)
        # Ordre d'écriture préservé
        self.assertEqual(entries[0]["tool"], "tool_0")
        self.assertEqual(entries[2]["tool"], "tool_2")

    def test_append_with_error(self):
        bid = agent_journal.generate_batch_id()
        agent_journal.append_entry(
            "p", tool="add_folder", args={"path": "X"},
            result="error", batch_id=bid, error="folder already exists",
        )
        entries = agent_journal.read_journal("p")
        self.assertEqual(entries[0]["result"], "error")
        self.assertEqual(entries[0]["error"], "folder already exists")

    def test_append_rejects_invalid_result(self):
        with self.assertRaises(ValueError):
            agent_journal.append_entry(
                "p", tool="x", args={}, result="weird",
                batch_id=agent_journal.generate_batch_id(),
            )

    def test_append_rejects_empty_batch_id(self):
        with self.assertRaises(ValueError):
            agent_journal.append_entry(
                "p", tool="x", args={}, result="ok", batch_id="",
            )


class TestAgentJournalRead(_AgentC0Base):
    def test_read_empty_returns_empty_list(self):
        self.assertEqual(agent_journal.read_journal("p"), [])

    def test_read_skips_corrupted_lines(self):
        path = (self.tmp / "profiles" / "p" / ".cache"
                / "refonte" / "agent-journal.jsonl")
        path.parent.mkdir(parents=True)
        path.write_text(
            '{"tool":"ok"}\n'
            'not json\n'
            '\n'  # empty line
            '{"tool":"alsoOk"}\n',
            encoding="utf-8",
        )
        entries = agent_journal.read_journal("p")
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["tool"], "ok")
        self.assertEqual(entries[1]["tool"], "alsoOk")

    def test_list_entries_recent_first_with_limit(self):
        for i in range(5):
            agent_journal.append_entry(
                "p", tool=f"tool_{i}", args={}, result="ok",
                batch_id=agent_journal.generate_batch_id(),
            )
        entries = agent_journal.list_entries("p", limit=2)
        self.assertEqual(len(entries), 2)
        # Plus récents en tête
        self.assertEqual(entries[0]["tool"], "tool_4")
        self.assertEqual(entries[1]["tool"], "tool_3")

    def test_list_entries_filter_by_batch_id(self):
        bid_a = agent_journal.generate_batch_id()
        bid_b = agent_journal.generate_batch_id()
        agent_journal.append_entry("p", tool="t1", args={}, result="ok",
                                    batch_id=bid_a)
        agent_journal.append_entry("p", tool="t2", args={}, result="ok",
                                    batch_id=bid_b)
        agent_journal.append_entry("p", tool="t3", args={}, result="ok",
                                    batch_id=bid_a)
        only_a = agent_journal.list_entries("p", batch_id=bid_a)
        self.assertEqual(len(only_a), 2)
        self.assertEqual({e["tool"] for e in only_a}, {"t1", "t3"})


class TestAgentJournalBatches(_AgentC0Base):
    def test_list_batches_groups_by_batch_id(self):
        bid_a = agent_journal.generate_batch_id()
        bid_b = agent_journal.generate_batch_id()
        agent_journal.append_entry("p", tool="add_folder",
                                    args={}, result="ok", batch_id=bid_a)
        agent_journal.append_entry("p", tool="add_theme_mapping",
                                    args={}, result="ok", batch_id=bid_a)
        agent_journal.append_entry("p", tool="rename_folder",
                                    args={}, result="error",
                                    batch_id=bid_b, error="busy")
        batches = agent_journal.list_batches("p")
        self.assertEqual(len(batches), 2)
        # Plus récent en tête : bid_b (dernier ajouté)
        self.assertEqual(batches[0]["batch_id"], bid_b)
        self.assertEqual(batches[0]["n_error"], 1)
        self.assertEqual(batches[0]["n_ok"], 0)
        # bid_a a 2 ok
        self.assertEqual(batches[1]["batch_id"], bid_a)
        self.assertEqual(batches[1]["n_ok"], 2)
        self.assertEqual(batches[1]["tools"], ["add_folder", "add_theme_mapping"])

    def test_list_batches_marks_rolled_back(self):
        bid = agent_journal.generate_batch_id()
        agent_journal.append_entry("p", tool="add_folder",
                                    args={}, result="ok", batch_id=bid,
                                    backup_dir="agent-foo-bar")
        # Undo plus tard
        agent_journal.record_undo("p", bid)
        batches = agent_journal.list_batches("p")
        # Le batch initial bid doit être marqué rolled_back, et le undo
        # est filtré (pas dans la liste des batches "normaux").
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["batch_id"], bid)
        self.assertTrue(batches[0]["rolled_back"])

    def test_record_undo_creates_new_batch_id(self):
        bid_orig = agent_journal.generate_batch_id()
        agent_journal.append_entry("p", tool="add_folder",
                                    args={}, result="ok", batch_id=bid_orig)
        undo_entry = agent_journal.record_undo("p", bid_orig)
        self.assertNotEqual(undo_entry["batch_id"], bid_orig)
        self.assertEqual(undo_entry["tool"], "undo")
        self.assertEqual(undo_entry["args"]["target_batch_id"], bid_orig)


# ─── Backup ────────────────────────────────────────────────────────────────


class TestAgentBackupCreateRestore(_AgentC0Base):
    def test_create_backup_copies_both_yamls(self):
        self._seed_prod_yamls(tree="folders: [A, B]\n",
                              mapping="theme1: A\n")
        dir_name = agent_backup.create_backup("p", "batch-1")
        # Le dossier existe avec les 2 fichiers
        backup_path = (self._profile_root() / ".cache" / "taxonomy-backups"
                       / dir_name)
        self.assertTrue(backup_path.is_dir())
        self.assertTrue((backup_path / "tree.yaml").exists())
        self.assertTrue((backup_path / "theme_mapping.yaml").exists())

    def test_create_backup_name_format(self):
        self._seed_prod_yamls()
        dir_name = agent_backup.create_backup("p", "my-batch-id")
        self.assertTrue(dir_name.startswith("agent-my-batch-id-"))
        # Suffix = "YYYYMMDD-HHMMSS" → 15 chars
        suffix = dir_name[len("agent-my-batch-id-"):]
        self.assertEqual(len(suffix), 15)

    def test_create_backup_refuses_when_no_prod_yaml(self):
        # Pas de tree.yaml ni theme_mapping.yaml dans le profil
        with self.assertRaises(agent_backup.BackupError):
            agent_backup.create_backup("p", "batch-1")
        # Aucun dossier vide laissé
        root = self._profile_root() / ".cache" / "taxonomy-backups"
        if root.exists():
            self.assertEqual(list(root.iterdir()), [])

    def test_create_backup_refuses_empty_batch_id(self):
        self._seed_prod_yamls()
        with self.assertRaises(agent_backup.BackupError):
            agent_backup.create_backup("p", "")

    def test_restore_roundtrip(self):
        self._seed_prod_yamls(tree="folders: [original]\n",
                              mapping="theme1: original\n")
        dir_name = agent_backup.create_backup("p", "batch-1")
        # Modifie les YAML
        (self._profile_root() / "tree.yaml").write_text(
            "folders: [modified]\n", encoding="utf-8")
        (self._profile_root() / "theme_mapping.yaml").write_text(
            "theme1: modified\n", encoding="utf-8")
        # Restore
        result = agent_backup.restore_backup("p", dir_name)
        self.assertEqual(set(result["restored"]),
                         {"tree.yaml", "theme_mapping.yaml"})
        # Les YAML sont remis dans l'état pré-backup
        tree = (self._profile_root() / "tree.yaml").read_text()
        self.assertIn("original", tree)
        self.assertNotIn("modified", tree)

    def test_restore_missing_backup_raises(self):
        with self.assertRaises(agent_backup.BackupError):
            agent_backup.restore_backup("p", "agent-ghost-00000000-000000")


class TestAgentBackupList(_AgentC0Base):
    def test_list_backups_sorted_recent_first(self):
        import time
        self._seed_prod_yamls()
        # Crée 3 backups successifs (ts dans le nom différents → besoin d'attendre)
        dirs = []
        for bid in ["one", "two", "three"]:
            dirs.append(agent_backup.create_backup("p", bid))
            time.sleep(1.05)  # garantir un ts différent
        listed = agent_backup.list_backups("p")
        self.assertEqual(len(listed), 3)
        # Plus récent (three) en tête
        self.assertEqual(listed[0]["batch_id"], "three")
        self.assertEqual(listed[2]["batch_id"], "one")
        # Chaque entrée a les bons champs
        self.assertEqual(set(listed[0]["files"]),
                         {"tree.yaml", "theme_mapping.yaml"})
        self.assertGreater(listed[0]["size_bytes"], 0)

    def test_list_backups_ignores_non_agent_dirs(self):
        # Un backup manuel "theme_mapping-2026.yaml" doit être ignoré
        self._seed_prod_yamls()
        agent_backup.create_backup("p", "real-batch")
        root = (self._profile_root() / ".cache" / "taxonomy-backups")
        (root / "manual-backup-12345").mkdir()
        (root / "theme_mapping-2026.yaml").write_text("foo: bar")
        listed = agent_backup.list_backups("p")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["batch_id"], "real-batch")

    def test_list_backups_empty(self):
        self.assertEqual(agent_backup.list_backups("p"), [])


class TestAgentBackupRotate(_AgentC0Base):
    def test_rotate_keeps_only_max_keep(self):
        import time
        self._seed_prod_yamls()
        for i in range(5):
            agent_backup.create_backup("p", f"batch-{i}", max_keep=999)
            time.sleep(1.05)
        # Force rotation à max_keep=2 → garde les 2 plus récents
        deleted = agent_backup.rotate_backups("p", max_keep=2)
        self.assertEqual(deleted, 3)
        listed = agent_backup.list_backups("p")
        self.assertEqual(len(listed), 2)
        self.assertEqual({b["batch_id"] for b in listed}, {"batch-3", "batch-4"})

    def test_rotate_no_op_when_under_limit(self):
        self._seed_prod_yamls()
        agent_backup.create_backup("p", "only-one", max_keep=999)
        deleted = agent_backup.rotate_backups("p", max_keep=50)
        self.assertEqual(deleted, 0)


class TestFindBackupForBatch(_AgentC0Base):
    def test_finds_when_exists(self):
        self._seed_prod_yamls()
        dir_name = agent_backup.create_backup("p", "target-batch")
        self.assertEqual(
            agent_backup.find_backup_for_batch("p", "target-batch"),
            dir_name,
        )

    def test_returns_none_when_missing(self):
        self.assertIsNone(
            agent_backup.find_backup_for_batch("p", "ghost-batch"),
        )


if __name__ == "__main__":
    unittest.main()
