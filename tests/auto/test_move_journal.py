#!/usr/bin/env python3
"""Tests for lib/move_journal.py — append-only JSONL + undo helpers."""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from lib import move_journal as mj  # noqa: E402


class JournalTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="klodo-movejnl-"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_file(self, rel: str) -> Path:
        path = self.tmpdir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.4 x")
        return path


class TestAppendAndRead(JournalTestBase):
    def test_append_creates_journal_file(self):
        rec = mj.append_move(self.tmpdir, old_abs="/old/a.pdf", new_abs="/new/a.pdf", batch_id="b1")
        self.assertTrue((self.tmpdir / ".cache" / "move-journal.jsonl").exists())
        self.assertEqual(rec["old"], "/old/a.pdf")
        self.assertEqual(rec["new"], "/new/a.pdf")
        self.assertEqual(rec["batch"], "b1")
        self.assertIn("ts", rec)

    def test_read_journal_returns_records_oldest_first(self):
        mj.append_move(self.tmpdir, "/o/1.pdf", "/n/1.pdf", batch_id="b1")
        mj.append_move(self.tmpdir, "/o/2.pdf", "/n/2.pdf", batch_id="b1")
        recs = mj.read_journal(self.tmpdir)
        self.assertEqual([r["old"] for r in recs], ["/o/1.pdf", "/o/2.pdf"])

    def test_read_journal_skips_malformed_lines(self):
        mj.append_move(self.tmpdir, "/o/1.pdf", "/n/1.pdf", batch_id="b1")
        with open(self.tmpdir / ".cache" / "move-journal.jsonl", "a", encoding="utf-8") as f:
            f.write("{broken json\n")
        self.assertEqual(len(mj.read_journal(self.tmpdir)), 1)

    def test_read_journal_empty_when_missing(self):
        self.assertEqual(mj.read_journal(self.tmpdir), [])


class TestUndoRecord(JournalTestBase):
    def test_undo_moves_file_back_and_journals_inverse(self):
        old = self.tmpdir / "A" / "f.pdf"
        new = self._make_file("B/f.pdf")
        rec = {"old": str(old), "new": str(new), "batch": "b1", "ts": "t"}
        mj.undo_record(self.tmpdir, rec)
        self.assertTrue(old.exists())
        self.assertFalse(new.exists())
        last = mj.read_journal(self.tmpdir)[-1]
        self.assertEqual(last["batch"], "undo-b1")
        self.assertEqual(last["old"], str(new))
        self.assertEqual(last["new"], str(old))

    def test_undo_raises_when_new_missing(self):
        rec = {"old": str(self.tmpdir / "A" / "f.pdf"),
               "new": str(self.tmpdir / "B" / "gone.pdf"), "batch": "b1"}
        with self.assertRaises(FileNotFoundError):
            mj.undo_record(self.tmpdir, rec)

    def test_undo_raises_on_collision_at_old(self):
        old = self._make_file("A/f.pdf")
        new = self._make_file("B/f.pdf")
        rec = {"old": str(old), "new": str(new), "batch": "b1"}
        with self.assertRaises(FileExistsError):
            mj.undo_record(self.tmpdir, rec)


class TestUndoBatch(JournalTestBase):
    def _move_and_journal(self, src_rel: str, dst_rel: str, batch: str) -> None:
        src = self._make_file(src_rel)
        dst = self.tmpdir / dst_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.rename(src, dst)
        mj.append_move(self.tmpdir, str(src), str(dst), batch_id=batch)

    def test_undo_batch_reverses_all_in_reverse_order(self):
        self._move_and_journal("A/1.pdf", "X/1.pdf", "b1")
        self._move_and_journal("A/2.pdf", "X/2.pdf", "b1")
        result = mj.undo_batch(self.tmpdir, "b1")
        self.assertEqual(result["n_undone"], 2)
        self.assertEqual(result["n_failed"], 0)
        self.assertTrue((self.tmpdir / "A" / "1.pdf").exists())
        self.assertTrue((self.tmpdir / "A" / "2.pdf").exists())

    def test_undo_batch_skips_failures_and_reports(self):
        self._move_and_journal("A/1.pdf", "X/1.pdf", "b1")
        self._move_and_journal("A/2.pdf", "X/2.pdf", "b1")
        (self.tmpdir / "X" / "2.pdf").unlink()  # fichier disparu → échec individuel
        result = mj.undo_batch(self.tmpdir, "b1")
        self.assertEqual(result["n_undone"], 1)
        self.assertEqual(result["n_failed"], 1)
        self.assertEqual(len(result["failures"]), 1)
        self.assertIn("2.pdf", result["failures"][0]["new"])

    def test_undo_batch_ignores_other_batches_and_undos(self):
        self._move_and_journal("A/1.pdf", "X/1.pdf", "b1")
        self._move_and_journal("A/2.pdf", "X/2.pdf", "b2")
        result = mj.undo_batch(self.tmpdir, "b1")
        self.assertEqual(result["n_undone"], 1)
        self.assertTrue((self.tmpdir / "X" / "2.pdf").exists())  # b2 intact
        # Re-undo du même batch : les records déjà annulés échouent (new absent)
        result2 = mj.undo_batch(self.tmpdir, "b1")
        self.assertEqual(result2["n_undone"], 0)


if __name__ == "__main__":
    unittest.main()
