#!/usr/bin/env python3
"""Tests pour Phase C (C.0 — backup + journal + rollback endpoints)."""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from fastapi.testclient import TestClient  # noqa: E402

from agents.refonte import agent_backup, agent_journal  # noqa: E402
from dashboard import agent_refonte_phase_c, data  # noqa: E402


class _PhaseCBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from dashboard.app import app
        cls.client = TestClient(app)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="klodo_phase_c_"))
        (self.tmp / "profiles" / "p").mkdir(parents=True)
        self._patcher = mock.patch.object(
            data, "get_project_root", return_value=self.tmp,
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed_yamls(self, content: str = "original") -> None:
        root = self.tmp / "profiles" / "p"
        (root / "tree.yaml").write_text(f"folders: [{content}]\n",
                                         encoding="utf-8")
        (root / "theme_mapping.yaml").write_text(f"theme1: {content}\n",
                                                  encoding="utf-8")


class TestRollbackHappyPath(_PhaseCBase):
    def test_rollback_restores_yaml_and_journals_undo(self):
        self._seed_yamls("original")
        # Simule une mutation : backup + journal entry
        bid = agent_journal.generate_batch_id()
        backup_dir = agent_backup.create_backup("p", bid)
        agent_journal.append_entry(
            "p", tool="add_folder", args={"path": "X"}, result="ok",
            batch_id=bid, backup_dir=backup_dir,
        )
        # Modifie les YAML après la mutation
        self._seed_yamls("modified")
        # Rollback
        result = agent_refonte_phase_c.rollback_batch("p", bid)
        self.assertEqual(set(result["restored_files"]),
                         {"tree.yaml", "theme_mapping.yaml"})
        self.assertEqual(result["warning_newer_batches"], 0)
        # Les YAML sont restaurés
        tree = (self.tmp / "profiles" / "p" / "tree.yaml").read_text()
        self.assertIn("original", tree)
        self.assertNotIn("modified", tree)
        # Le journal contient l'undo
        batches = agent_journal.list_batches("p")
        self.assertTrue(batches[0]["rolled_back"])


class TestRollbackRejections(_PhaseCBase):
    def test_empty_profile_raises_value_error(self):
        with self.assertRaises(ValueError):
            agent_refonte_phase_c.rollback_batch("", "x")

    def test_empty_batch_id_raises_value_error(self):
        with self.assertRaises(ValueError):
            agent_refonte_phase_c.rollback_batch("p", "")

    def test_unknown_profile_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            agent_refonte_phase_c.rollback_batch("ghost", "anything")

    def test_unknown_batch_raises_rollback_error(self):
        with self.assertRaises(agent_refonte_phase_c.RollbackError) as ctx:
            agent_refonte_phase_c.rollback_batch("p", "ghost-batch-id")
        self.assertIn("not found", str(ctx.exception))

    def test_already_rolled_back_raises(self):
        self._seed_yamls()
        bid = agent_journal.generate_batch_id()
        backup_dir = agent_backup.create_backup("p", bid)
        agent_journal.append_entry(
            "p", tool="add_folder", args={}, result="ok",
            batch_id=bid, backup_dir=backup_dir,
        )
        # 1er rollback OK
        agent_refonte_phase_c.rollback_batch("p", bid)
        # 2e rollback → RollbackError "already"
        with self.assertRaises(agent_refonte_phase_c.RollbackError) as ctx:
            agent_refonte_phase_c.rollback_batch("p", bid)
        self.assertIn("already", str(ctx.exception))

    def test_missing_backup_dir_raises(self):
        """Cas où le journal a une entrée mais le backup physique a disparu."""
        self._seed_yamls()
        bid = agent_journal.generate_batch_id()
        backup_dir = agent_backup.create_backup("p", bid)
        agent_journal.append_entry(
            "p", tool="add_folder", args={}, result="ok",
            batch_id=bid, backup_dir=backup_dir,
        )
        # Supprime physiquement le backup
        shutil.rmtree(self.tmp / "profiles" / "p" / ".cache"
                      / "taxonomy-backups" / backup_dir)
        with self.assertRaises(agent_refonte_phase_c.RollbackError) as ctx:
            agent_refonte_phase_c.rollback_batch("p", bid)
        self.assertIn("no backup directory", str(ctx.exception))


class TestRollbackWarningsNewerBatches(_PhaseCBase):
    def test_warns_when_newer_batches_exist(self):
        import time
        self._seed_yamls()
        # 3 mutations successives
        bids = []
        for _ in range(3):
            bid = agent_journal.generate_batch_id()
            backup_dir = agent_backup.create_backup("p", bid)
            agent_journal.append_entry(
                "p", tool="add_folder", args={}, result="ok",
                batch_id=bid, backup_dir=backup_dir,
            )
            bids.append(bid)
            time.sleep(1.05)
        # Rollback du 1er batch → 2 plus récents existent
        result = agent_refonte_phase_c.rollback_batch("p", bids[0])
        self.assertEqual(result["warning_newer_batches"], 2)

    def test_no_warning_on_last_batch(self):
        self._seed_yamls()
        bid = agent_journal.generate_batch_id()
        backup_dir = agent_backup.create_backup("p", bid)
        agent_journal.append_entry(
            "p", tool="add_folder", args={}, result="ok",
            batch_id=bid, backup_dir=backup_dir,
        )
        result = agent_refonte_phase_c.rollback_batch("p", bid)
        self.assertEqual(result["warning_newer_batches"], 0)


# ─── Endpoints HTTP ────────────────────────────────────────────────────────


class TestEndpointBatches(_PhaseCBase):
    def test_lists_batches(self):
        bid = agent_journal.generate_batch_id()
        agent_journal.append_entry(
            "p", tool="add_folder", args={}, result="ok", batch_id=bid,
        )
        r = self.client.get("/api/agent/refonte/c/batches?profile=p")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["profile"], "p")
        self.assertEqual(len(body["batches"]), 1)
        self.assertEqual(body["batches"][0]["batch_id"], bid)

    def test_empty_when_no_journal(self):
        r = self.client.get("/api/agent/refonte/c/batches?profile=p")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["batches"], [])

    def test_400_when_no_profile(self):
        r = self.client.get("/api/agent/refonte/c/batches?profile=")
        self.assertEqual(r.status_code, 400)


class TestEndpointEntries(_PhaseCBase):
    def test_lists_entries_filtered_by_batch(self):
        bid_a = agent_journal.generate_batch_id()
        bid_b = agent_journal.generate_batch_id()
        agent_journal.append_entry("p", tool="t1", args={}, result="ok",
                                    batch_id=bid_a)
        agent_journal.append_entry("p", tool="t2", args={}, result="ok",
                                    batch_id=bid_b)
        r = self.client.get(
            f"/api/agent/refonte/c/entries?profile=p&batch_id={bid_a}",
        )
        self.assertEqual(r.status_code, 200)
        entries = r.json()["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["tool"], "t1")


class TestEndpointRollback(_PhaseCBase):
    def _make_batch(self) -> str:
        self._seed_yamls()
        bid = agent_journal.generate_batch_id()
        backup_dir = agent_backup.create_backup("p", bid)
        agent_journal.append_entry(
            "p", tool="add_folder", args={}, result="ok",
            batch_id=bid, backup_dir=backup_dir,
        )
        return bid

    def test_happy_path(self):
        bid = self._make_batch()
        r = self.client.post(
            "/api/agent/refonte/c/rollback",
            json={"profile": "p", "batch_id": bid},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["batch_id"], bid)

    def test_400_missing_profile(self):
        r = self.client.post(
            "/api/agent/refonte/c/rollback",
            json={"batch_id": "x"},
        )
        self.assertEqual(r.status_code, 400)

    def test_404_unknown_batch(self):
        r = self.client.post(
            "/api/agent/refonte/c/rollback",
            json={"profile": "p", "batch_id": "ghost"},
        )
        self.assertEqual(r.status_code, 404)

    def test_409_already_rolled_back(self):
        bid = self._make_batch()
        # 1er rollback
        self.client.post(
            "/api/agent/refonte/c/rollback",
            json={"profile": "p", "batch_id": bid},
        )
        # 2e rollback → 409
        r = self.client.post(
            "/api/agent/refonte/c/rollback",
            json={"profile": "p", "batch_id": bid},
        )
        self.assertEqual(r.status_code, 409)


if __name__ == "__main__":
    unittest.main()
