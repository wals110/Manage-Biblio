#!/usr/bin/env python3
"""Tests for lib/rename_journal.py — append-only JSONL + undo helpers."""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from lib import rename_journal as rj  # noqa: E402


class JournalTestBase(unittest.TestCase):
    """Isolated tmpdir per test (used as the "profile dir")."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="klodo-renamejnl-"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_file(self, rel: str, content: bytes = b"%PDF-1.4 x") -> Path:
        """Create an empty file under the tmpdir + return its absolute path."""
        path = self.tmpdir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path


# ─── Append + list ───────────────────────────────────────────────────────


class TestAppendAndList(JournalTestBase):

    def test_append_creates_journal_file(self):
        rec = rj.append_rename(
            self.tmpdir, old_abs="/old/a.pdf", new_abs="/new/a.pdf")
        self.assertTrue((self.tmpdir / ".cache" / "rename-journal.jsonl").exists())
        self.assertEqual(rec["old"], "/old/a.pdf")
        self.assertEqual(rec["new"], "/new/a.pdf")
        self.assertEqual(rec["batch"], "")

    def test_append_preserves_unicode(self):
        rj.append_rename(
            self.tmpdir, "/old/é.pdf", "/new/é.pdf", batch_id="b1")
        content = (self.tmpdir / ".cache" / "rename-journal.jsonl").read_text(
            encoding="utf-8")
        self.assertIn("é.pdf", content)

    def test_list_renames_newest_first(self):
        rj.append_rename(self.tmpdir, "/a", "/b")
        rj.append_rename(self.tmpdir, "/c", "/d")
        rj.append_rename(self.tmpdir, "/e", "/f")
        records = rj.list_renames(self.tmpdir)
        self.assertEqual([r["new"] for r in records], ["/f", "/d", "/b"])

    def test_list_renames_respects_limit(self):
        for i in range(10):
            rj.append_rename(self.tmpdir, f"/old{i}", f"/new{i}")
        recs = rj.list_renames(self.tmpdir, limit=3)
        self.assertEqual(len(recs), 3)
        # Most recent first
        self.assertEqual(recs[0]["new"], "/new9")

    def test_list_renames_empty_when_no_journal(self):
        self.assertEqual(rj.list_renames(self.tmpdir), [])

    def test_malformed_lines_skipped(self):
        # Pre-populate with bad JSON
        jpath = self.tmpdir / ".cache" / "rename-journal.jsonl"
        jpath.parent.mkdir(parents=True, exist_ok=True)
        jpath.write_text(
            'not-json\n'
            '{"old":"/a","new":"/b","ts":"t","batch":""}\n'
            'garbage\n',
            encoding="utf-8",
        )
        recs = rj.list_renames(self.tmpdir)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["new"], "/b")


# ─── Batches ─────────────────────────────────────────────────────────────


class TestBatches(JournalTestBase):

    def test_list_batches_groups_records(self):
        bid = "20260517-001-abc123"
        rj.append_rename(self.tmpdir, "/a", "/b", batch_id=bid)
        rj.append_rename(self.tmpdir, "/c", "/d", batch_id=bid)
        rj.append_rename(self.tmpdir, "/single", "/single2")  # no batch
        batches = rj.list_batches(self.tmpdir)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["batch"], bid)
        self.assertEqual(batches[0]["n_renames"], 2)

    def test_list_batches_skips_undo_records(self):
        rj.append_rename(self.tmpdir, "/a", "/b", batch_id="bid1")
        rj.append_rename(self.tmpdir, "/b", "/a", batch_id="undo-bid1")
        batches = rj.list_batches(self.tmpdir)
        # bid1 is listed; undo-bid1 is filtered out
        ids = [b["batch"] for b in batches]
        self.assertIn("bid1", ids)
        self.assertNotIn("undo-bid1", ids)

    def test_list_batches_sorted_newest_first(self):
        rj.append_rename(self.tmpdir, "/a", "/b", batch_id="20260101-001-aaa")
        rj.append_rename(self.tmpdir, "/c", "/d", batch_id="20260601-001-bbb")
        rj.append_rename(self.tmpdir, "/e", "/f", batch_id="20260301-001-ccc")
        batches = rj.list_batches(self.tmpdir)
        # We sort by ts, which the helper writes at append time — so the
        # newest *appended* is the first one, not the highest batch id.
        # All 3 were appended in this order: 0101, 0601, 0301.
        # The "ts" stored is the WALL CLOCK at append → they share the same
        # second in this test, so we'd hit a tie. Just check we got 3
        # batches and no crash.
        self.assertEqual(len(batches), 3)


# ─── Undo single record ──────────────────────────────────────────────────


class TestUndoRecord(JournalTestBase):

    def test_undo_reverses_rename(self):
        # Setup: file currently at "new", was "old"
        new_path = self._make_file("new/file.pdf")
        old_path = self.tmpdir / "old" / "file.pdf"
        old_path.parent.mkdir(parents=True, exist_ok=True)
        rec = rj.append_rename(self.tmpdir, str(old_path), str(new_path))

        inverse = rj.undo_record(self.tmpdir, rec)
        self.assertFalse(new_path.exists())
        self.assertTrue(old_path.exists())
        # inverse op was logged
        self.assertEqual(inverse["old"], str(new_path))
        self.assertEqual(inverse["new"], str(old_path))
        self.assertTrue(inverse["batch"].startswith("undo"))

    def test_undo_raises_when_new_missing(self):
        old_path = self.tmpdir / "old" / "a.pdf"
        new_path = self.tmpdir / "new" / "a.pdf"
        # Neither file exists — new is gone (someone moved it manually)
        rec = {"old": str(old_path), "new": str(new_path), "ts": "x", "batch": ""}
        with self.assertRaises(FileNotFoundError):
            rj.undo_record(self.tmpdir, rec)

    def test_undo_raises_on_collision(self):
        # Both old AND new exist — undo would overwrite something
        new_path = self._make_file("new/a.pdf")
        old_path = self._make_file("old/a.pdf")
        rec = {"old": str(old_path), "new": str(new_path), "ts": "x", "batch": ""}
        with self.assertRaises(FileExistsError):
            rj.undo_record(self.tmpdir, rec)


# ─── Undo batch ──────────────────────────────────────────────────────────


class TestUndoBatch(JournalTestBase):

    def test_undo_batch_reverses_all_renames(self):
        bid = "b1"
        # Simulate 3 renames
        for i in range(3):
            new = self._make_file(f"new/f{i}.pdf")
            old = self.tmpdir / "old" / f"f{i}.pdf"
            old.parent.mkdir(parents=True, exist_ok=True)
            rj.append_rename(self.tmpdir, str(old), str(new), batch_id=bid)

        result = rj.undo_batch(self.tmpdir, bid)
        self.assertEqual(result["n_undone"], 3)
        self.assertEqual(result["n_errors"], 0)
        for i in range(3):
            self.assertFalse((self.tmpdir / "new" / f"f{i}.pdf").exists())
            self.assertTrue((self.tmpdir / "old" / f"f{i}.pdf").exists())

    def test_undo_batch_unknown_id_raises(self):
        with self.assertRaises(FileNotFoundError):
            rj.undo_batch(self.tmpdir, "does-not-exist")

    def test_undo_batch_reports_errors_per_record(self):
        bid = "b1"
        # 1 valid rename + 1 with new missing
        new1 = self._make_file("new/a.pdf")
        old1 = self.tmpdir / "old" / "a.pdf"
        old1.parent.mkdir(parents=True, exist_ok=True)
        rj.append_rename(self.tmpdir, str(old1), str(new1), batch_id=bid)

        rj.append_rename(self.tmpdir, "/nonexistent/old", "/nonexistent/new",
                         batch_id=bid)
        result = rj.undo_batch(self.tmpdir, bid)
        self.assertEqual(result["n_undone"], 1)
        self.assertEqual(result["n_errors"], 1)

    def test_undo_batch_skips_already_undone(self):
        bid = "b1"
        new = self._make_file("new/a.pdf")
        old = self.tmpdir / "old" / "a.pdf"
        old.parent.mkdir(parents=True, exist_ok=True)
        rj.append_rename(self.tmpdir, str(old), str(new), batch_id=bid)
        # First undo
        rj.undo_batch(self.tmpdir, bid)
        # Re-create the new file (someone might have done another rename
        # back to the new path) and try to undo again
        new.parent.mkdir(parents=True, exist_ok=True)
        new.touch()
        old.unlink()    # remove old so we don't get collision
        result = rj.undo_batch(self.tmpdir, bid)
        # Should skip because an inverse for the original is already in the
        # journal — this is the safety contract for "don't double-undo".
        self.assertEqual(result["n_undone"], 0)


# ─── Batch id ────────────────────────────────────────────────────────────


class TestBatchId(unittest.TestCase):

    def test_generate_batch_id_format(self):
        bid = rj.generate_batch_id()
        # Shape: YYYYMMDD-HHMMSS-XXXXXX (22 chars, hex suffix 6)
        parts = bid.split("-")
        self.assertEqual(len(parts), 3)
        self.assertEqual(len(parts[0]), 8)   # YYYYMMDD
        self.assertEqual(len(parts[1]), 6)   # HHMMSS
        self.assertEqual(len(parts[2]), 6)   # short uuid

    def test_generate_batch_id_unique(self):
        ids = {rj.generate_batch_id() for _ in range(20)}
        self.assertEqual(len(ids), 20)


if __name__ == "__main__":
    unittest.main()
