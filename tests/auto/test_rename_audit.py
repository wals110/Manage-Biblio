#!/usr/bin/env python3
"""Tests for dashboard/rename.py — scan + categorize + suggest."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from dashboard import data, rename  # noqa: E402


class RenameAuditTestBase(unittest.TestCase):
    """Isolated profile + target with vision_cache wired up."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="klodo-renaud-"))
        self.profile_name = "test"
        self.profile_dir = self.tmpdir / "profiles" / self.profile_name
        self.target = self.tmpdir / "library"
        self.target.mkdir(parents=True, exist_ok=True)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        # profile.yaml with target wired
        (self.profile_dir / "profile.yaml").write_text(yaml.safe_dump({
            "name": "test",
            "target": str(self.target),
            "llm": {"model": "Qwen/Qwen3-VL-32B-Instruct"},
            "defaults": {"pages": 2},
        }))
        self._patcher = mock.patch.object(
            data, "get_project_root", return_value=self.tmpdir,
        )
        self._patcher.start()
        rename.reset_cache()

    def tearDown(self):
        self._patcher.stop()
        rename.reset_cache()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _add_file_with_cache(
        self,
        rel: str,
        title: str,
        author: str = "",
        confidence: float = 0.95,
    ) -> Path:
        """Write a unique-content PDF + add the corresponding vision_cache entry."""
        from lib import vision_cache as vc
        abs_path = self.target / rel
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        # Unique content so the cache_key is distinct per file
        abs_path.write_bytes(f"%PDF-1.4 {rel}".encode())
        key = vc.compute_cache_key(
            str(abs_path),
            model="Qwen/Qwen3-VL-32B-Instruct",
            n_pages=2,
        )
        assert key is not None
        cache_path = self.profile_dir / ".cache" / "vision_cache.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache: dict = {}
        if cache_path.exists():
            try:
                cache = json.loads(cache_path.read_text())
            except (OSError, json.JSONDecodeError):
                cache = {}
        cache[key] = {
            "result": {
                "title": title,
                "author": author,
                "confidence": confidence,
            },
            "model": "Qwen/Qwen3-VL-32B-Instruct",
            "prompt_version": "v3",
        }
        cache_path.write_text(json.dumps(cache))
        return abs_path

    def _set_rename_config(self, **overrides):
        """Patch profile.yaml's rename block."""
        cfg = yaml.safe_load((self.profile_dir / "profile.yaml").read_text())
        cfg["rename"] = overrides
        (self.profile_dir / "profile.yaml").write_text(yaml.safe_dump(cfg))


# ─── Config resolution ───────────────────────────────────────────────────


class TestRenameConfig(RenameAuditTestBase):

    def test_defaults_when_block_missing(self):
        cfg = rename.get_rename_config(self.profile_name)
        self.assertEqual(cfg["template"], rename.DEFAULT_TEMPLATE)
        self.assertEqual(cfg["fallback"], rename.DEFAULT_FALLBACK)
        self.assertEqual(cfg["max_length"], rename.DEFAULT_MAX_LENGTH)

    def test_overrides_picked_up(self):
        self._set_rename_config(
            template="{title} - {author}", fallback="{title}",
            max_length=120, min_title_confidence=0.5,
        )
        cfg = rename.get_rename_config(self.profile_name)
        self.assertEqual(cfg["template"], "{title} - {author}")
        self.assertEqual(cfg["max_length"], 120)
        self.assertEqual(cfg["min_title_confidence"], 0.5)


# ─── Audit ───────────────────────────────────────────────────────────────


class TestRenameAuditBasics(RenameAuditTestBase):

    def test_empty_lib_yields_empty(self):
        r = rename.rename_audit(self.profile_name)
        self.assertEqual(r["stats"]["n_total"], 0)
        self.assertEqual(r["candidates"], [])

    def test_file_without_cache_marked_no_metadata(self):
        # File but no cache entry
        (self.target / "alpha.pdf").write_bytes(b"%PDF unique alpha")
        r = rename.rename_audit(self.profile_name)
        self.assertEqual(r["stats"]["n_total"], 1)
        self.assertEqual(r["stats"]["n_no_metadata"], 1)
        self.assertEqual(r["stats"]["n_with_title"], 0)

    def test_low_confidence_skipped(self):
        self._add_file_with_cache("foo.pdf", title="The Real Title",
                                  confidence=0.4)
        # Default min_title_confidence is 0.85 → file is in the cache but
        # below the threshold. Should be counted in n_low_confidence (not
        # n_no_metadata, which is reserved for files with no cache hit).
        r = rename.rename_audit(self.profile_name)
        self.assertEqual(r["stats"]["n_low_confidence"], 1)
        self.assertEqual(r["stats"]["n_no_metadata"], 0)
        self.assertEqual(r["stats"]["n_with_title"], 0)

    def test_no_metadata_and_low_confidence_distinct(self):
        # Two files: one with no cache entry at all, one below threshold.
        # They must land in different stat buckets.
        (self.target / "no_cache.pdf").write_bytes(b"%PDF no cache here")
        self._add_file_with_cache(
            "low_conf.pdf", title="Real Title", confidence=0.3)
        r = rename.rename_audit(self.profile_name)
        self.assertEqual(r["stats"]["n_no_metadata"], 1)
        self.assertEqual(r["stats"]["n_low_confidence"], 1)
        self.assertEqual(r["stats"]["n_total"], 2)

    def test_high_confidence_audited(self):
        self._add_file_with_cache(
            "rubbish.pdf",
            title="Hands-On Machine Learning",
            author="Aurélien Géron",
            confidence=0.95,
        )
        r = rename.rename_audit(self.profile_name)
        self.assertEqual(r["stats"]["n_with_title"], 1)
        c = r["candidates"][0]
        self.assertEqual(c["current_name"], "rubbish.pdf")
        self.assertEqual(
            c["suggested_name"],
            "Hands-On Machine Learning - Aurélien Géron.pdf",
        )
        self.assertEqual(c["title"], "Hands-On Machine Learning")
        self.assertEqual(c["author"], "Aurélien Géron")


# ─── Categorization ──────────────────────────────────────────────────────


class TestCategorization(RenameAuditTestBase):

    def test_placeholder_detected(self):
        self._add_file_with_cache(
            "Title Author.pdf",
            title="Real Book", author="Real Author",
        )
        r = rename.rename_audit(self.profile_name)
        cat = r["candidates"][0]["category"]
        self.assertEqual(cat, "placeholder")
        self.assertEqual(r["stats"]["n_placeholder"], 1)

    def test_placeholder_with_suffix_number_detected(self):
        self._add_file_with_cache(
            "Title Author (7).pdf",
            title="Real Book", author="Real Author",
        )
        r = rename.rename_audit(self.profile_name)
        self.assertEqual(r["candidates"][0]["category"], "placeholder")

    def test_divergent_detected(self):
        self._add_file_with_cache(
            "abcd.pdf",
            title="Hands-On Machine Learning",
            author="Aurélien Géron",
        )
        r = rename.rename_audit(self.profile_name)
        c = r["candidates"][0]
        self.assertEqual(c["category"], "divergent")
        self.assertLess(c["similarity"], 0.4)

    def test_minor_case_detected(self):
        # Same title but different casing → similarity moderate-high
        self._add_file_with_cache(
            "hands-on machine learning.pdf",
            title="Hands-On Machine Learning",
            author="",
        )
        self._set_rename_config(template="{title}", fallback="{title}",
                                min_title_confidence=0.5)
        r = rename.rename_audit(self.profile_name)
        c = r["candidates"][0]
        self.assertEqual(c["category"], "ok")
        self.assertGreaterEqual(c["similarity"], 0.7)

    def test_ok_when_filename_matches_template_output(self):
        self._add_file_with_cache(
            "Hands-On Machine Learning - Aurélien Géron.pdf",
            title="Hands-On Machine Learning",
            author="Aurélien Géron",
        )
        r = rename.rename_audit(self.profile_name)
        c = r["candidates"][0]
        self.assertEqual(c["category"], "ok")


# ─── Sort order ──────────────────────────────────────────────────────────


class TestSortOrder(RenameAuditTestBase):

    def test_placeholder_before_divergent_before_ok(self):
        self._add_file_with_cache("Hands-On Machine Learning - Aurélien Géron.pdf",
                                   title="Hands-On Machine Learning",
                                   author="Aurélien Géron")
        self._add_file_with_cache("xyzzy.pdf",
                                   title="A Totally Different Title",
                                   author="Other Author")
        self._add_file_with_cache("Title Author.pdf",
                                   title="Whatever", author="Whoever")
        r = rename.rename_audit(self.profile_name)
        cats = [c["category"] for c in r["candidates"]]
        # placeholder first, then divergent, then ok last
        self.assertEqual(cats[0], "placeholder")
        self.assertEqual(cats[-1], "ok")


# ─── Cache ───────────────────────────────────────────────────────────────


class TestAuditCache(RenameAuditTestBase):

    def test_cached_second_call(self):
        self._add_file_with_cache("a.pdf", title="A", author="B")
        r1 = rename.rename_audit(self.profile_name)
        # Add a 2nd file but don't reset cache → audit should NOT see it
        self._add_file_with_cache("c.pdf", title="C", author="D")
        r2 = rename.rename_audit(self.profile_name)
        self.assertEqual(r1["stats"]["n_total"], r2["stats"]["n_total"])

    def test_force_reload_bypasses_cache(self):
        self._add_file_with_cache("a.pdf", title="A", author="B")
        rename.rename_audit(self.profile_name)
        self._add_file_with_cache("c.pdf", title="C", author="D")
        r = rename.rename_audit(self.profile_name, force_reload=True)
        self.assertEqual(r["stats"]["n_total"], 2)

    def test_reset_cache_clears(self):
        self._add_file_with_cache("a.pdf", title="A", author="B")
        rename.rename_audit(self.profile_name)
        rename.reset_cache(self.profile_name)
        self._add_file_with_cache("c.pdf", title="C", author="D")
        r = rename.rename_audit(self.profile_name)
        self.assertEqual(r["stats"]["n_total"], 2)


# ─── Similarity util ─────────────────────────────────────────────────────


class TestSimilarity(unittest.TestCase):

    def test_identical_strings(self):
        self.assertEqual(rename._jaccard_3grams("abc", "abc"), 1.0)

    def test_empty_strings(self):
        self.assertEqual(rename._jaccard_3grams("", ""), 0.0)
        self.assertEqual(rename._jaccard_3grams("x", ""), 0.0)

    def test_case_insensitive(self):
        s = rename._jaccard_3grams("Hello World", "hello world")
        self.assertEqual(s, 1.0)

    def test_punctuation_ignored(self):
        s = rename._jaccard_3grams("hands-on ml", "hands on ml")
        self.assertGreater(s, 0.9)

    def test_completely_different(self):
        s = rename._jaccard_3grams("abcdefg", "xyzwvut")
        self.assertEqual(s, 0.0)


# ─── Endpoint ────────────────────────────────────────────────────────────


class TestRenameAuditEndpoint(RenameAuditTestBase):

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient

        from dashboard.app import app
        self.client = TestClient(app)

    def test_endpoint_basic(self):
        r = self.client.get(f"/api/rename/audit?profile={self.profile_name}")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("candidates", body)
        self.assertIn("stats", body)
        self.assertIn("config", body)

    def test_endpoint_force_reload(self):
        self._add_file_with_cache("a.pdf", title="A", author="B")
        # First call caches
        self.client.get(f"/api/rename/audit?profile={self.profile_name}")
        # Add file
        self._add_file_with_cache("c.pdf", title="C", author="D")
        # Without force → cache hit (still 1)
        r1 = self.client.get(f"/api/rename/audit?profile={self.profile_name}")
        self.assertEqual(r1.json()["stats"]["n_total"], 1)
        # With force → re-scan (2)
        r2 = self.client.get(
            f"/api/rename/audit?profile={self.profile_name}&force=true")
        self.assertEqual(r2.json()["stats"]["n_total"], 2)


class TestCommitRename(RenameAuditTestBase):
    """commit_rename(): FS rename + journal append + cache invalidation."""

    def test_happy_path(self):
        self._add_file_with_cache("ugly_name.pdf", title="Clean Title",
                                   author="An Author")
        r = rename.commit_rename(
            self.profile_name, "ugly_name.pdf", "Clean Title - An Author.pdf",
        )
        self.assertTrue(r["ok"])
        self.assertEqual(r["new_rel_path"], "Clean Title - An Author.pdf")
        # FS state
        self.assertFalse((self.target / "ugly_name.pdf").exists())
        self.assertTrue((self.target / "Clean Title - An Author.pdf").exists())
        # Journal got the entry
        from lib import rename_journal
        records = rename_journal.list_renames(self.profile_dir)
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["old"].endswith("ugly_name.pdf"))
        self.assertTrue(
            records[0]["new"].endswith("Clean Title - An Author.pdf"))

    def test_unchanged_when_new_equals_old(self):
        self._add_file_with_cache("same.pdf", title="x")
        r = rename.commit_rename(self.profile_name, "same.pdf", "same.pdf")
        self.assertTrue(r["ok"])
        self.assertTrue(r.get("unchanged"))
        # No journal entry written for a no-op
        from lib import rename_journal
        self.assertEqual(rename_journal.list_renames(self.profile_dir), [])

    def test_collision_refused_409(self):
        self._add_file_with_cache("a.pdf", title="A")
        # Pre-create the target so the rename collides
        (self.target / "b.pdf").write_bytes(b"%PDF other")
        with self.assertRaises(rename.RenameError) as cm:
            rename.commit_rename(self.profile_name, "a.pdf", "b.pdf")
        self.assertEqual(cm.exception.status, 409)
        # Source file still in place
        self.assertTrue((self.target / "a.pdf").exists())

    def test_missing_source_404(self):
        with self.assertRaises(rename.RenameError) as cm:
            rename.commit_rename(
                self.profile_name, "nope.pdf", "anything.pdf")
        self.assertEqual(cm.exception.status, 404)

    def test_validates_new_name_empty(self):
        self._add_file_with_cache("a.pdf", title="A")
        with self.assertRaises(rename.RenameError) as cm:
            rename.commit_rename(self.profile_name, "a.pdf", "   ")
        self.assertEqual(cm.exception.status, 400)
        self.assertIn("vide", str(cm.exception))

    def test_validates_new_name_separators(self):
        self._add_file_with_cache("a.pdf", title="A")
        for bad in ("foo/bar.pdf", "..\\b.pdf", "x:y.pdf", "x?.pdf"):
            with self.assertRaises(rename.RenameError) as cm:
                rename.commit_rename(self.profile_name, "a.pdf", bad)
            self.assertEqual(cm.exception.status, 400,
                             f"expected 400 for {bad!r}")

    def test_validates_new_name_length(self):
        self._add_file_with_cache("a.pdf", title="A")
        too_long = "x" * 300 + ".pdf"
        with self.assertRaises(rename.RenameError) as cm:
            rename.commit_rename(self.profile_name, "a.pdf", too_long)
        self.assertEqual(cm.exception.status, 400)

    def test_path_traversal_blocked(self):
        self._add_file_with_cache("a.pdf", title="A")
        # rel_path that escapes target
        with self.assertRaises(rename.RenameError) as cm:
            rename.commit_rename(
                self.profile_name, "../outside.pdf", "x.pdf")
        # Either 400 (escapes target) or 404 (doesn't exist) — both acceptable
        self.assertIn(cm.exception.status, (400, 404))

    def test_subdirectory_preserved(self):
        """The rename keeps the file in its current directory; only the
        basename changes."""
        self._add_file_with_cache(
            "01-SCIENCES/PHYSIQUE/ugly.pdf", title="Pretty")
        rename.commit_rename(
            self.profile_name,
            "01-SCIENCES/PHYSIQUE/ugly.pdf",
            "Pretty.pdf",
        )
        self.assertFalse((self.target / "01-SCIENCES/PHYSIQUE/ugly.pdf").exists())
        self.assertTrue((self.target / "01-SCIENCES/PHYSIQUE/Pretty.pdf").exists())

    def test_case_only_rename_allowed_on_case_insensitive_fs(self):
        """Renaming `foo.pdf` → `Foo.pdf` must NOT raise 409 collision.

        On case-insensitive filesystems (APFS / HFS+ default on macOS),
        the destination path "exists" because it's the same inode as the
        source. The collision guard must use samefile() to tell case
        renames apart from real collisions. On case-sensitive FS (Linux
        ext4) the destination doesn't exist anyway — same outcome.
        """
        self._add_file_with_cache("foo.pdf", title="Foo")
        r = rename.commit_rename(self.profile_name, "foo.pdf", "Foo.pdf")
        self.assertTrue(r["ok"])
        self.assertEqual(r["new_rel_path"], "Foo.pdf")
        # The file is now at Foo.pdf (case may be merged on APFS; what
        # matters is no 409 was raised and the journal has an entry).
        from lib import rename_journal
        records = rename_journal.list_renames(self.profile_dir)
        self.assertEqual(len(records), 1)

    def test_caches_invalidated_after_rename(self):
        self._add_file_with_cache("a.pdf", title="A")
        # Prime the audit cache
        r1 = rename.rename_audit(self.profile_name)
        self.assertEqual(r1["stats"]["n_total"], 1)
        # Apply rename
        rename.commit_rename(self.profile_name, "a.pdf", "B.pdf")
        # Audit cache should now reflect the renamed file (re-built)
        r2 = rename.rename_audit(self.profile_name)
        names = {c["current_name"] for c in r2["candidates"]}
        self.assertIn("B.pdf", names)
        self.assertNotIn("a.pdf", names)


class TestCommitRenameEndpoint(RenameAuditTestBase):

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient

        from dashboard.app import app
        self.client = TestClient(app)

    def test_endpoint_happy(self):
        self._add_file_with_cache("a.pdf", title="A")
        r = self.client.post("/api/rename/file", json={
            "profile": self.profile_name,
            "rel_path": "a.pdf",
            "new_name": "B.pdf",
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["new_rel_path"], "B.pdf")

    def test_endpoint_collision_409(self):
        self._add_file_with_cache("a.pdf", title="A")
        (self.target / "b.pdf").write_bytes(b"%PDF")
        r = self.client.post("/api/rename/file", json={
            "profile": self.profile_name,
            "rel_path": "a.pdf",
            "new_name": "b.pdf",
        })
        self.assertEqual(r.status_code, 409)

    def test_endpoint_missing_404(self):
        r = self.client.post("/api/rename/file", json={
            "profile": self.profile_name,
            "rel_path": "nope.pdf",
            "new_name": "x.pdf",
        })
        self.assertEqual(r.status_code, 404)

    def test_endpoint_bad_name_400(self):
        self._add_file_with_cache("a.pdf", title="A")
        r = self.client.post("/api/rename/file", json={
            "profile": self.profile_name,
            "rel_path": "a.pdf",
            "new_name": "foo/bar.pdf",
        })
        self.assertEqual(r.status_code, 400)


# ─── Journal viewing + single undo (PR4A) ────────────────────────────────


class TestGetJournal(RenameAuditTestBase):

    def test_empty_journal(self):
        j = rename.get_journal(self.profile_name)
        self.assertEqual(j["records"], [])
        self.assertEqual(j["batches"], [])
        self.assertEqual(j["n_total"], 0)
        self.assertEqual(j["n_active"], 0)

    def test_records_newest_first_with_rel_paths(self):
        self._add_file_with_cache("a.pdf", title="A")
        self._add_file_with_cache("b.pdf", title="B")
        rename.commit_rename(self.profile_name, "a.pdf", "A.pdf")
        rename.commit_rename(self.profile_name, "b.pdf", "B.pdf")
        j = rename.get_journal(self.profile_name)
        self.assertEqual(len(j["records"]), 2)
        # Newest first — B was renamed after A
        self.assertEqual(j["records"][0]["new_rel"], "B.pdf")
        self.assertEqual(j["records"][1]["new_rel"], "A.pdf")
        # Flags
        self.assertFalse(j["records"][0]["is_undo"])
        self.assertFalse(j["records"][0]["is_undone"])

    def test_is_undone_flag_after_undo(self):
        self._add_file_with_cache("a.pdf", title="A")
        rename.commit_rename(self.profile_name, "a.pdf", "A.pdf")
        j_before = rename.get_journal(self.profile_name)
        rec = j_before["records"][0]
        rename.undo_single_rename(
            self.profile_name, rec["ts"], rec["old"], rec["new"])
        j_after = rename.get_journal(self.profile_name)
        # The original record now flagged as undone; the inverse appears
        originals = [r for r in j_after["records"] if not r["is_undo"]]
        self.assertEqual(len(originals), 1)
        self.assertTrue(originals[0]["is_undone"])
        # And the active count drops to 0
        self.assertEqual(j_after["n_active"], 0)

    def test_is_undone_respects_journal_order(self):
        """A rename done AFTER an earlier file landed at the same path
        must NOT be flagged as already-undone just because that earlier
        path appears as an inverse op's `old`. Order in the journal
        is the source of truth.

        Reproduces user-reported bug 2026-05-17: a fresh bulk was
        displayed as "déjà annulé" because old pre-PR4A undo records
        happened to share path names with the new bulk's targets.
        """
        # Step 1 — rename + undo (creates an inverse op whose old =
        # "renamed-target.pdf")
        self._add_file_with_cache("source.pdf", title="X")
        rename.commit_rename(
            self.profile_name, "source.pdf", "renamed-target.pdf")
        j1 = rename.get_journal(self.profile_name)
        rename.undo_single_rename(
            self.profile_name,
            j1["records"][0]["ts"],
            j1["records"][0]["old"],
            j1["records"][0]["new"],
        )
        # Step 2 — re-do the same rename. The new record's new field is
        # again "renamed-target.pdf" — same path as the earlier undo's old.
        rename.commit_rename(
            self.profile_name, "source.pdf", "renamed-target.pdf")
        # Now query the journal: the FRESH record must NOT be is_undone
        j2 = rename.get_journal(self.profile_name)
        forwards = [r for r in j2["records"] if not r["is_undo"]]
        # Two forward records: the older one IS undone (its inverse
        # exists), the newer one is NOT (newest-first ordering).
        self.assertEqual(len(forwards), 2)
        self.assertFalse(forwards[0]["is_undone"],
                         "Most recent rename must not be flagged undone")
        self.assertTrue(forwards[1]["is_undone"],
                        "Original rename should be flagged undone")
        # And n_active counts the freshly-redone rename
        self.assertEqual(j2["n_active"], 1)

    def test_batches_excludes_legacy_undo_records(self):
        """Pre-PR4A individual undo records are tagged batch="undo"
        (no batch_id suffix). They must not surface as a fake batch in
        the "Par lot" view — only forward batches should appear.

        Reproduces user-reported bug 2026-05-17: the modal showed
        "batch undo · 4 renommage(s)" because the legacy "undo" tag
        slipped past the `startswith("undo-")` filter.
        """
        # Manually craft a legacy-style journal so we don't have to
        # roundtrip through the PR3 commit/undo path.
        from lib import rename_journal
        rename_journal.append_rename(
            self.profile_dir,
            old_abs=str(self.target / "Foo.pdf"),
            new_abs=str(self.target / "Bar.pdf"),
            batch_id="",        # single rename, no batch
        )
        rename_journal.append_rename(
            self.profile_dir,
            old_abs=str(self.target / "Bar.pdf"),
            new_abs=str(self.target / "Foo.pdf"),
            batch_id="undo",    # legacy inverse op
        )
        # And a real batch alongside
        self._add_file_with_cache("source.pdf", title="X")
        rename.commit_rename_bulk(self.profile_name, items=[
            {"rel_path": "source.pdf", "new_name": "Renamed.pdf"},
        ])
        j = rename.get_journal(self.profile_name)
        batch_ids = {b["batch"] for b in j["batches"]}
        self.assertNotIn("undo", batch_ids,
                         "Legacy 'undo' must not surface as a batch")
        self.assertNotIn("", batch_ids,
                         "Un-batched singles must not surface either")
        # The real PR4 batch IS present (its id starts with a timestamp
        # like 20260517-...)
        self.assertTrue(any(b["batch"] and not b["batch"].startswith("undo")
                            for b in j["batches"]))

    def test_limit_caps_records(self):
        # Generate 5 renames
        for i in range(5):
            self._add_file_with_cache(f"f{i}.pdf", title=f"T{i}")
            rename.commit_rename(self.profile_name, f"f{i}.pdf", f"F{i}.pdf")
        j = rename.get_journal(self.profile_name, limit=3)
        self.assertEqual(len(j["records"]), 3)
        # newest-first → F4, F3, F2
        self.assertEqual(j["records"][0]["new_rel"], "F4.pdf")


class TestUndoSingle(RenameAuditTestBase):

    def _rename_and_get_record(self, old_rel: str, new_name: str) -> dict:
        self._add_file_with_cache(old_rel, title=new_name.replace(".pdf", ""))
        rename.commit_rename(self.profile_name, old_rel, new_name)
        return rename.get_journal(self.profile_name)["records"][0]

    def test_happy_path(self):
        rec = self._rename_and_get_record("ugly.pdf", "Clean.pdf")
        # FS state before undo
        self.assertTrue((self.target / "Clean.pdf").exists())
        self.assertFalse((self.target / "ugly.pdf").exists())
        r = rename.undo_single_rename(
            self.profile_name, rec["ts"], rec["old"], rec["new"])
        self.assertTrue(r["ok"])
        # FS state after undo — file is back at original path
        self.assertFalse((self.target / "Clean.pdf").exists())
        self.assertTrue((self.target / "ugly.pdf").exists())
        # Inverse entry recorded in the journal
        self.assertEqual(r["inverse_entry"]["new"],
                         str((self.target / "ugly.pdf").resolve()))

    def test_unknown_record_404(self):
        with self.assertRaises(rename.RenameError) as cm:
            rename.undo_single_rename(
                self.profile_name,
                ts="2999-01-01T00:00:00",
                old_abs=str(self.target / "ghost-old.pdf"),
                new_abs=str(self.target / "ghost-new.pdf"),
            )
        self.assertEqual(cm.exception.status, 404)

    def test_already_undone_409(self):
        rec = self._rename_and_get_record("a.pdf", "A.pdf")
        rename.undo_single_rename(
            self.profile_name, rec["ts"], rec["old"], rec["new"])
        # Second undo on the same record must refuse
        with self.assertRaises(rename.RenameError) as cm:
            rename.undo_single_rename(
                self.profile_name, rec["ts"], rec["old"], rec["new"])
        self.assertEqual(cm.exception.status, 409)
        self.assertIn("déjà", str(cm.exception))

    def test_cross_profile_path_rejected(self):
        # A record from "elsewhere" — path is outside the profile target.
        with self.assertRaises(rename.RenameError) as cm:
            rename.undo_single_rename(
                self.profile_name,
                ts="2026-01-01T00:00:00",
                old_abs="/etc/passwd-old",
                new_abs="/etc/passwd",
            )
        self.assertEqual(cm.exception.status, 400)

    def test_collision_during_undo_409(self):
        # Use truly distinct names so the squatter file can't be the
        # same inode as the renamed file on case-insensitive FS.
        rec = self._rename_and_get_record("ugly.pdf", "renamed.pdf")
        # Re-create a different file at the original path so undo can't
        # move back without overwriting it.
        (self.target / "ugly.pdf").write_bytes(b"%PDF squatter content")
        with self.assertRaises(rename.RenameError) as cm:
            rename.undo_single_rename(
                self.profile_name, rec["ts"], rec["old"], rec["new"])
        self.assertEqual(cm.exception.status, 409)

    def test_missing_disk_file_404(self):
        rec = self._rename_and_get_record("a.pdf", "A.pdf")
        # Someone removed the renamed file externally
        (self.target / "A.pdf").unlink()
        with self.assertRaises(rename.RenameError) as cm:
            rename.undo_single_rename(
                self.profile_name, rec["ts"], rec["old"], rec["new"])
        self.assertEqual(cm.exception.status, 404)

    def test_undo_respects_journal_order_not_just_path_match(self):
        """If a file has been renamed → undone → renamed-again to the
        same target, undoing the SECOND rename must NOT be refused as
        already-undone just because an earlier (now-stale) undo record
        has its `old` equal to this rename's `new`.

        Reproduces user-reported bug 2026-05-17: the modal correctly
        showed records as actionable (get_journal respects order) but
        the undo endpoint refused with 409 because undo_single_rename
        used the older path-only check.
        """
        # Step 1 — rename + undo it
        self._add_file_with_cache("src.pdf", title="Foo")
        rename.commit_rename(self.profile_name, "src.pdf", "Renamed.pdf")
        j1 = rename.get_journal(self.profile_name)
        first_rec = j1["records"][0]
        rename.undo_single_rename(
            self.profile_name,
            first_rec["ts"], first_rec["old"], first_rec["new"])
        # Step 2 — re-do the same rename. The journal now has:
        #   line 1: old=src.pdf, new=Renamed.pdf (original)
        #   line 2: old=Renamed.pdf, new=src.pdf (undo of line 1)
        #   line 3: old=src.pdf, new=Renamed.pdf (re-do)
        # The fresh re-do MUST be undoable.
        rename.commit_rename(self.profile_name, "src.pdf", "Renamed.pdf")
        j2 = rename.get_journal(self.profile_name)
        # records is newest-first; the freshest forward is at index 0
        forwards = [r for r in j2["records"] if not r["is_undo"]]
        latest_forward = forwards[0]   # newest first
        # The fresh re-do must NOT be flagged as undone (the buggy check
        # would have, because an earlier inverse op's old matches its new).
        self.assertFalse(latest_forward["is_undone"])
        # The earlier original IS undone (its inverse exists)
        self.assertTrue(forwards[1]["is_undone"])

        # Now try to undo it — this used to raise 409 with the buggy check
        result = rename.undo_single_rename(
            self.profile_name,
            latest_forward["ts"],
            latest_forward["old"],
            latest_forward["new"])
        self.assertTrue(result["ok"])
        # And the file is back at src.pdf
        self.assertTrue((self.target / "src.pdf").exists())

    def test_caches_invalidated_after_undo(self):
        rec = self._rename_and_get_record("a.pdf", "A.pdf")
        # Prime the audit cache after the rename
        r1 = rename.rename_audit(self.profile_name)
        names_before = {c["current_name"] for c in r1["candidates"]}
        self.assertIn("A.pdf", names_before)
        rename.undo_single_rename(
            self.profile_name, rec["ts"], rec["old"], rec["new"])
        # Audit should now reflect the restored name
        r2 = rename.rename_audit(self.profile_name)
        names_after = {c["current_name"] for c in r2["candidates"]}
        self.assertIn("a.pdf", names_after)
        self.assertNotIn("A.pdf", names_after)


class TestJournalEndpoints(RenameAuditTestBase):

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient

        from dashboard.app import app
        self.client = TestClient(app)

    def test_get_journal_endpoint(self):
        self._add_file_with_cache("a.pdf", title="A")
        rename.commit_rename(self.profile_name, "a.pdf", "A.pdf")
        r = self.client.get(
            f"/api/rename/journal?profile={self.profile_name}")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body["records"]), 1)
        self.assertEqual(body["records"][0]["new_rel"], "A.pdf")
        self.assertEqual(body["n_active"], 1)

    def test_undo_record_endpoint_happy(self):
        self._add_file_with_cache("a.pdf", title="A")
        rename.commit_rename(self.profile_name, "a.pdf", "A.pdf")
        j = self.client.get(
            f"/api/rename/journal?profile={self.profile_name}").json()
        rec = j["records"][0]
        r = self.client.post("/api/rename/undo/record", json={
            "profile": self.profile_name,
            "ts": rec["ts"],
            "old": rec["old"],
            "new": rec["new"],
        })
        self.assertEqual(r.status_code, 200)
        self.assertTrue((self.target / "a.pdf").exists())

    def test_undo_record_endpoint_404(self):
        r = self.client.post("/api/rename/undo/record", json={
            "profile": self.profile_name,
            "ts": "2999-01-01T00:00:00",
            "old": str(self.target / "ghost-old.pdf"),
            "new": str(self.target / "ghost-new.pdf"),
        })
        self.assertEqual(r.status_code, 404)

    def test_undo_record_endpoint_400_outside_target(self):
        r = self.client.post("/api/rename/undo/record", json={
            "profile": self.profile_name,
            "ts": "2026-01-01T00:00:00",
            "old": "/etc/passwd-old",
            "new": "/etc/passwd",
        })
        self.assertEqual(r.status_code, 400)


# ─── Bulk rename + batch undo (PR4B) ─────────────────────────────────────


class TestCommitRenameBulk(RenameAuditTestBase):

    def _seed_three(self):
        # Use names with NO case-only equivalence to their targets so the
        # tests behave the same on case-sensitive (Linux ext4) and
        # case-insensitive (macOS APFS) filesystems.
        self._add_file_with_cache("alpha-src.pdf", title="A")
        self._add_file_with_cache("beta-src.pdf", title="B")
        self._add_file_with_cache("gamma-src.pdf", title="C")

    def test_happy_path_all_succeed(self):
        self._seed_three()
        r = rename.commit_rename_bulk(self.profile_name, items=[
            {"rel_path": "alpha-src.pdf", "new_name": "Alpha.pdf"},
            {"rel_path": "beta-src.pdf", "new_name": "Beta.pdf"},
            {"rel_path": "gamma-src.pdf", "new_name": "Gamma.pdf"},
        ])
        self.assertTrue(r["ok"])
        self.assertEqual(r["n_renamed"], 3)
        self.assertEqual(r["n_errors"], 0)
        self.assertTrue(r["batch_id"])
        # FS state
        for new in ("Alpha.pdf", "Beta.pdf", "Gamma.pdf"):
            self.assertTrue((self.target / new).exists())

    def test_shared_batch_id(self):
        self._seed_three()
        r = rename.commit_rename_bulk(self.profile_name, items=[
            {"rel_path": "alpha-src.pdf", "new_name": "Alpha.pdf"},
            {"rel_path": "beta-src.pdf", "new_name": "Beta.pdf"},
        ])
        bid = r["batch_id"]
        # Every success carries the same batch_id in its journal entry
        for s in r["successes"]:
            self.assertEqual(s["journal_entry"]["batch"], bid)

    def test_partial_failure_does_not_abort_batch(self):
        self._seed_three()
        # Pre-create a TRULY distinct file (different inode) for the
        # collision case so APFS case-insensitivity doesn't make it the
        # same inode as beta-src.pdf.
        (self.target / "Beta.pdf").write_bytes(b"%PDF squatter content")
        r = rename.commit_rename_bulk(self.profile_name, items=[
            {"rel_path": "alpha-src.pdf", "new_name": "Alpha.pdf"},    # OK
            {"rel_path": "beta-src.pdf",  "new_name": "Beta.pdf"},     # 409
            {"rel_path": "missing.pdf",   "new_name": "X.pdf"},        # 404
            {"rel_path": "gamma-src.pdf", "new_name": "foo/bar.pdf"},  # 400
        ])
        self.assertEqual(r["n_renamed"], 1)
        self.assertEqual(r["n_errors"], 3)
        # Each error preserves the rel_path so the UI can highlight rows
        rels = {e["rel_path"] for e in r["errors"]}
        self.assertIn("beta-src.pdf", rels)
        self.assertIn("missing.pdf", rels)
        self.assertIn("gamma-src.pdf", rels)

    def test_empty_items_400(self):
        with self.assertRaises(rename.RenameError) as cm:
            rename.commit_rename_bulk(self.profile_name, items=[])
        self.assertEqual(cm.exception.status, 400)

    def test_cache_invalidated_once_after_bulk(self):
        self._seed_three()
        r1 = rename.rename_audit(self.profile_name)
        self.assertEqual(r1["stats"]["n_total"], 3)
        rename.commit_rename_bulk(self.profile_name, items=[
            {"rel_path": "alpha-src.pdf", "new_name": "Alpha.pdf"},
            {"rel_path": "beta-src.pdf", "new_name": "Beta.pdf"},
        ])
        r2 = rename.rename_audit(self.profile_name)
        names = {c["current_name"] for c in r2["candidates"]}
        self.assertIn("Alpha.pdf", names)
        self.assertIn("Beta.pdf", names)
        self.assertNotIn("alpha-src.pdf", names)


class TestUndoBatch(RenameAuditTestBase):

    def _bulk_three(self) -> str:
        # Distinct names again so undo's FS state check works regardless
        # of FS case sensitivity.
        self._add_file_with_cache("alpha-src.pdf", title="A")
        self._add_file_with_cache("beta-src.pdf", title="B")
        self._add_file_with_cache("gamma-src.pdf", title="C")
        r = rename.commit_rename_bulk(self.profile_name, items=[
            {"rel_path": "alpha-src.pdf", "new_name": "Alpha.pdf"},
            {"rel_path": "beta-src.pdf", "new_name": "Beta.pdf"},
            {"rel_path": "gamma-src.pdf", "new_name": "Gamma.pdf"},
        ])
        return r["batch_id"]

    def test_undo_batch_restores_all(self):
        bid = self._bulk_three()
        summary = rename.undo_batch_for_profile(self.profile_name, bid)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["n_undone"], 3)
        self.assertEqual(summary["n_errors"], 0)
        for old in ("alpha-src.pdf", "beta-src.pdf", "gamma-src.pdf"):
            self.assertTrue((self.target / old).exists())
        for new in ("Alpha.pdf", "Beta.pdf", "Gamma.pdf"):
            self.assertFalse((self.target / new).exists())

    def test_undo_batch_unknown_id_404(self):
        with self.assertRaises(rename.RenameError) as cm:
            rename.undo_batch_for_profile(
                self.profile_name, "20990101-000000-ffffff")
        self.assertEqual(cm.exception.status, 404)

    def test_undo_batch_empty_id_400(self):
        with self.assertRaises(rename.RenameError) as cm:
            rename.undo_batch_for_profile(self.profile_name, "")
        self.assertEqual(cm.exception.status, 400)

    def test_undo_batch_partial_with_external_collision(self):
        bid = self._bulk_three()
        # Re-create a squatter at one of the original paths (using the
        # original distinct name so it's a real second inode, not the
        # same file on case-insensitive FS).
        (self.target / "beta-src.pdf").write_bytes(b"%PDF squatter content")
        summary = rename.undo_batch_for_profile(self.profile_name, bid)
        self.assertTrue(summary["ok"])
        # 2 undone, 1 errored (the one with the squatter)
        self.assertEqual(summary["n_undone"], 2)
        self.assertEqual(summary["n_errors"], 1)

    def test_undo_batch_idempotent_second_run(self):
        bid = self._bulk_three()
        rename.undo_batch_for_profile(self.profile_name, bid)
        # Second call: all records are already undone — n_undone=0
        summary = rename.undo_batch_for_profile(self.profile_name, bid)
        self.assertEqual(summary["n_undone"], 0)

    def test_cache_invalidated_after_undo_batch(self):
        bid = self._bulk_three()
        r1 = rename.rename_audit(self.profile_name)
        names1 = {c["current_name"] for c in r1["candidates"]}
        self.assertIn("Alpha.pdf", names1)
        rename.undo_batch_for_profile(self.profile_name, bid)
        r2 = rename.rename_audit(self.profile_name)
        names2 = {c["current_name"] for c in r2["candidates"]}
        self.assertIn("alpha-src.pdf", names2)
        self.assertNotIn("Alpha.pdf", names2)


class TestBulkEndpoints(RenameAuditTestBase):

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient

        from dashboard.app import app
        self.client = TestClient(app)

    def test_bulk_endpoint_happy(self):
        self._add_file_with_cache("alpha-src.pdf", title="A")
        self._add_file_with_cache("beta-src.pdf", title="B")
        r = self.client.post("/api/rename/bulk", json={
            "profile": self.profile_name,
            "items": [
                {"rel_path": "alpha-src.pdf", "new_name": "Alpha.pdf"},
                {"rel_path": "beta-src.pdf",  "new_name": "Beta.pdf"},
            ],
        })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["n_renamed"], 2)
        self.assertTrue(body["batch_id"])

    def test_bulk_endpoint_empty_items_400(self):
        r = self.client.post("/api/rename/bulk", json={
            "profile": self.profile_name, "items": [],
        })
        self.assertEqual(r.status_code, 400)

    def test_undo_batch_endpoint_happy(self):
        self._add_file_with_cache("alpha-src.pdf", title="A")
        self._add_file_with_cache("beta-src.pdf", title="B")
        bulk = self.client.post("/api/rename/bulk", json={
            "profile": self.profile_name,
            "items": [
                {"rel_path": "alpha-src.pdf", "new_name": "Alpha.pdf"},
                {"rel_path": "beta-src.pdf",  "new_name": "Beta.pdf"},
            ],
        }).json()
        r = self.client.post("/api/rename/undo/batch", json={
            "profile": self.profile_name,
            "batch_id": bulk["batch_id"],
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["n_undone"], 2)

    def test_undo_batch_endpoint_404(self):
        r = self.client.post("/api/rename/undo/batch", json={
            "profile": self.profile_name,
            "batch_id": "20990101-000000-ffffff",
        })
        self.assertEqual(r.status_code, 404)


# ─── Targeted cache invalidation (perf — no full rescan after rename) ───


class TestTargetedAuditPatch(RenameAuditTestBase):
    """commit_rename / commit_rename_bulk / undo_* must mutate the audit
    cache in place instead of dropping it (the previous behaviour forced
    a full os.walk + 18k-file rescan after every single rename).

    Each test verifies BOTH that the patched cache is correct AND that
    no rebuild happened (we replace ``_audit_cache`` with a sentinel
    after the rename and check it's still there)."""

    def test_single_rename_patches_in_place(self):
        self._add_file_with_cache("ugly.pdf", title="Clean Title")
        # Prime the cache
        audit_before = rename.rename_audit(self.profile_name)
        cached = rename._audit_cache[self.profile_name]
        self.assertIs(audit_before, cached, "Cached object reference")
        # Sentinel so we detect a full rebuild
        cached["_sentinel"] = "must-survive-patch"

        rename.commit_rename(self.profile_name, "ugly.pdf", "Clean Title.pdf")

        cached_after = rename._audit_cache[self.profile_name]
        # Same dict instance → mutated in place, not replaced
        self.assertIs(cached_after, cached)
        self.assertEqual(cached_after.get("_sentinel"), "must-survive-patch")
        # And the candidate now reflects the new name
        names = {c["current_name"] for c in cached_after["candidates"]}
        self.assertIn("Clean Title.pdf", names)
        self.assertNotIn("ugly.pdf", names)

    def test_patch_keeps_stats_accurate(self):
        # Three placeholders + one already-OK
        self._add_file_with_cache("Title Author.pdf",
                                   title="Real Book", author="X")
        self._add_file_with_cache("Title Author (2).pdf",
                                   title="Other Book", author="Y")
        self._add_file_with_cache("Real Book - X.pdf",
                                   title="Real Book", author="X")
        rename.rename_audit(self.profile_name)
        stats_before = dict(rename._audit_cache[self.profile_name]["stats"])
        self.assertEqual(stats_before["n_placeholder"], 2)

        # Rename one placeholder → it now matches "Real Book - X" template
        # (which is "ok"). Stats must decrement placeholder, increment ok.
        rename.commit_rename(
            self.profile_name, "Title Author.pdf", "Real Book - X (v2).pdf")
        stats_after = rename._audit_cache[self.profile_name]["stats"]
        self.assertEqual(stats_after["n_placeholder"],
                         stats_before["n_placeholder"] - 1)
        # n_with_title unchanged (file count is the same, just relabeled)
        self.assertEqual(stats_after["n_with_title"],
                         stats_before["n_with_title"])
        # n_total unchanged
        self.assertEqual(stats_after["n_total"], stats_before["n_total"])

    def test_patch_result_matches_full_rebuild(self):
        # Same lib, two ways to reach the post-rename state — must match
        self._add_file_with_cache("a.pdf", title="A book", author="A")
        self._add_file_with_cache("b.pdf", title="B book", author="B")
        self._add_file_with_cache("placeholder.pdf",
                                   title="Real C", author="C")
        rename.rename_audit(self.profile_name)
        rename.commit_rename(
            self.profile_name, "placeholder.pdf", "Real C - C.pdf")
        patched = dict(rename._audit_cache[self.profile_name])
        patched_candidates = [
            {**c} for c in patched["candidates"]
        ]
        patched_stats = dict(patched["stats"])

        # Force a full rebuild and compare
        rename.reset_cache(self.profile_name)
        rebuilt = rename.rename_audit(
            self.profile_name, force_reload=True)
        rebuilt_candidates = [{**c} for c in rebuilt["candidates"]]
        rebuilt_stats = dict(rebuilt["stats"])

        # Same order, same content
        self.assertEqual(
            [c["rel_path"] for c in patched_candidates],
            [c["rel_path"] for c in rebuilt_candidates])
        self.assertEqual(patched_stats, rebuilt_stats)

    def test_bulk_rename_single_sort_in_place(self):
        for i in range(5):
            self._add_file_with_cache(
                f"Title Author ({i}).pdf",
                title=f"Real {i}", author="Author")
        rename.rename_audit(self.profile_name)
        cached = rename._audit_cache[self.profile_name]
        cached["_sentinel"] = "bulk-survivor"

        rename.commit_rename_bulk(
            self.profile_name,
            items=[{"rel_path": f"Title Author ({i}).pdf",
                    "new_name": f"Real {i} - Author.pdf"} for i in range(5)],
        )
        # No rebuild happened: same dict + sentinel survives
        self.assertIs(rename._audit_cache[self.profile_name], cached)
        self.assertEqual(cached.get("_sentinel"), "bulk-survivor")
        names = {c["current_name"] for c in cached["candidates"]}
        for i in range(5):
            self.assertIn(f"Real {i} - Author.pdf", names)
            self.assertNotIn(f"Title Author ({i}).pdf", names)

    def test_undo_patches_in_place(self):
        self._add_file_with_cache("orig.pdf", title="Renamed", author="X")
        # Audit before rename so the cache exists to be patched
        rename.rename_audit(self.profile_name)
        rename.commit_rename(
            self.profile_name, "orig.pdf", "Renamed - X.pdf")
        cached = rename._audit_cache[self.profile_name]
        cached["_sentinel"] = "undo-survivor"
        # Find the journal entry to undo
        j = rename.get_journal(self.profile_name)
        rec = j["records"][0]
        rename.undo_single_rename(
            self.profile_name, rec["ts"], rec["old"], rec["new"])
        # Same dict, sentinel survives
        self.assertIs(rename._audit_cache[self.profile_name], cached)
        self.assertEqual(cached.get("_sentinel"), "undo-survivor")
        names = {c["current_name"] for c in cached["candidates"]}
        self.assertIn("orig.pdf", names)
        self.assertNotIn("Renamed - X.pdf", names)

    def test_falls_back_when_no_audit_loaded(self):
        # No audit done — _audit_cache is empty. Patch should be a no-op,
        # and the next audit should rebuild fresh (and be correct).
        self._add_file_with_cache("foo.pdf", title="Foo", author="X")
        # Sanity: no cache yet
        self.assertNotIn(self.profile_name, rename._audit_cache)
        rename.commit_rename(
            self.profile_name, "foo.pdf", "Foo - X.pdf")
        # Still no cache (no audit was loaded, so no patch happened)
        self.assertNotIn(self.profile_name, rename._audit_cache)
        # The lazy rebuild on next read produces a correct audit
        r = rename.rename_audit(self.profile_name)
        names = {c["current_name"] for c in r["candidates"]}
        self.assertIn("Foo - X.pdf", names)

    def test_falls_back_when_old_entry_not_in_candidates(self):
        # File audited as n_no_metadata (no cache hit) — its rename can't
        # be safely patched because we don't know its bucket. Fall back
        # to a full rebuild (cache dropped).
        # Setup: a file with no vision_cache entry
        (self.target / "untracked.pdf").write_bytes(b"%PDF noop")
        # Add another file WITH cache so the audit has something to scan
        self._add_file_with_cache("tracked.pdf", title="Tracked", author="X")
        rename.rename_audit(self.profile_name)
        cached = rename._audit_cache[self.profile_name]
        cached["_sentinel"] = "should-be-dropped"
        # Rename the untracked file
        rename.commit_rename(
            self.profile_name, "untracked.pdf", "Different.pdf")
        # Cache was dropped — sentinel is gone (either key absent or new dict)
        # n_no_metadata files don't actually appear in candidates.
        # The patch logic recognises this and drops the profile cache.
        self.assertNotIn(self.profile_name, rename._audit_cache)
        # And the lazy rebuild on the next read produces a correct audit.
        # This is the assertion the original test was missing: confirming
        # that the drop-then-rebuild cycle actually yields the right state
        # (not just that the drop happened).
        r = rename.rename_audit(self.profile_name)
        names = {c["current_name"] for c in r["candidates"]}
        # 'tracked.pdf' still audited (cache key untouched), the rename
        # to 'Different.pdf' is visible. The renamed file lands in
        # n_no_metadata since it has no vision_cache entry, but n_total
        # still counts it.
        self.assertIn("tracked.pdf", names)
        self.assertNotIn("untracked.pdf", names)
        self.assertEqual(r["stats"]["n_total"], 2)
        self.assertEqual(r["stats"]["n_no_metadata"], 1)


# ─── Follow-up regression coverage (PR review #137) ─────────────────────


class TestTargetedAuditPatchRegressions(RenameAuditTestBase):
    """Plug the test-coverage gaps surfaced by the post-merge code review:

      - bulk path stats consistency (I2)
      - undo_batch_for_profile in-place patch (I1)
      - stats restored to pre-call state on _apply_patch_to_audit
        failure, regardless of caller behaviour (C1)
    """

    def test_bulk_rename_updates_stats_per_pair(self):
        # 4 placeholders that will all land in "ok" after rename. Verify
        # n_placeholder drops by 4 and n_ok rises by 4 — proves the
        # per-pair stat mutations aren't dropped, doubled, or off-by-one.
        for i in range(4):
            self._add_file_with_cache(
                f"Title Author ({i}).pdf",
                title=f"Real Book {i}", author=f"Author {i}")
        rename.rename_audit(self.profile_name)
        before = dict(rename._audit_cache[self.profile_name]["stats"])
        self.assertEqual(before["n_placeholder"], 4)
        self.assertEqual(before["n_ok"], 0)

        rename.commit_rename_bulk(
            self.profile_name,
            items=[{"rel_path": f"Title Author ({i}).pdf",
                    "new_name": f"Real Book {i} - Author {i}.pdf"}
                   for i in range(4)],
        )
        after = rename._audit_cache[self.profile_name]["stats"]
        self.assertEqual(after["n_placeholder"], 0)
        self.assertEqual(after["n_ok"], 4)
        # n_with_title + n_total preserved (files were renamed, not added)
        self.assertEqual(after["n_with_title"], before["n_with_title"])
        self.assertEqual(after["n_total"], before["n_total"])

    def test_undo_batch_patches_in_place(self):
        # Mirror of test_undo_patches_in_place but for the batch path.
        # Sets a sentinel, runs a bulk + undo_batch, asserts the cache
        # dict is the SAME instance and the sentinel survives.
        for i in range(3):
            self._add_file_with_cache(
                f"orig-{i}.pdf",
                title=f"Renamed {i}", author=f"Author {i}")
        rename.rename_audit(self.profile_name)
        # Do the bulk
        result = rename.commit_rename_bulk(
            self.profile_name,
            items=[{"rel_path": f"orig-{i}.pdf",
                    "new_name": f"Renamed {i} - Author {i}.pdf"}
                   for i in range(3)],
        )
        batch_id = result["batch_id"]
        # Now mark the cache and undo the batch
        cached = rename._audit_cache[self.profile_name]
        cached["_sentinel"] = "batch-undo-survivor"
        summary = rename.undo_batch_for_profile(self.profile_name, batch_id)
        self.assertEqual(summary["n_undone"], 3)
        # Same dict instance — patched in place, not rebuilt
        self.assertIs(rename._audit_cache[self.profile_name], cached)
        self.assertEqual(cached.get("_sentinel"), "batch-undo-survivor")
        # And the files are back at their original names
        names = {c["current_name"] for c in cached["candidates"]}
        for i in range(3):
            self.assertIn(f"orig-{i}.pdf", names)
            self.assertNotIn(f"Renamed {i} - Author {i}.pdf", names)

    def test_undo_batch_consistency_with_full_rebuild(self):
        # Same lib + same actions, two ways to reach the final state:
        # via in-place patch vs via force_reload. Stats and candidates
        # must be identical.
        for i in range(3):
            self._add_file_with_cache(
                f"src-{i}.pdf", title=f"T{i}", author=f"A{i}")
        rename.rename_audit(self.profile_name)
        r = rename.commit_rename_bulk(
            self.profile_name,
            items=[{"rel_path": f"src-{i}.pdf",
                    "new_name": f"T{i} - A{i}.pdf"} for i in range(3)],
        )
        rename.undo_batch_for_profile(self.profile_name, r["batch_id"])
        patched_stats = dict(
            rename._audit_cache[self.profile_name]["stats"])
        patched_rel_paths = [
            c["rel_path"]
            for c in rename._audit_cache[self.profile_name]["candidates"]
        ]
        # Force a clean rebuild and compare
        rebuilt = rename.rename_audit(
            self.profile_name, force_reload=True)
        self.assertEqual(patched_stats, dict(rebuilt["stats"]))
        self.assertEqual(
            patched_rel_paths,
            [c["rel_path"] for c in rebuilt["candidates"]])

    def test_apply_patch_stats_restored_on_bail(self):
        # Direct exercise of _apply_patch_to_audit's bail path. Verifies
        # the C1 fix: stats must be back to their pre-call state when
        # the function returns False — regardless of whether the caller
        # subsequently drops the cache.
        self._add_file_with_cache("a.pdf", title="AAA", author="X")
        self._add_file_with_cache("b.pdf", title="BBB", author="Y")
        rename.rename_audit(self.profile_name)
        cached = rename._audit_cache[self.profile_name]
        stats_before = dict(cached["stats"])
        candidates_before_count = len(cached["candidates"])

        ctx = rename._profile_audit_context(self.profile_name)
        self.assertIsNotNone(ctx)
        # Force a bail: second pair points to a file that doesn't exist
        # on disk, AFTER the first pair has already decremented stats.
        pairs = [
            ("a.pdf", "AAA - X.pdf"),               # would succeed
            ("b.pdf", "this-file-does-not-exist.pdf"),  # bails here
        ]
        ok = rename._apply_patch_to_audit(cached, pairs, ctx)
        self.assertFalse(ok)
        # Stats restored to exactly the pre-call values — no half-mutation
        self.assertEqual(dict(cached["stats"]), stats_before)
        # Candidates unchanged in count (the list rebuild only happens on
        # the True path; on bail, candidates is the same list reference)
        self.assertEqual(len(cached["candidates"]), candidates_before_count)


# ─── User category overrides (feature/rename-status-override) ───────────


class TestSetOverrides(RenameAuditTestBase):
    """User-facing semantic: 'this divergent is actually fine, mark it ok'.
    Persists in profile/.cache/rename-overrides.json keyed by cache_key
    (MD5-of-head), so the override survives renames automatically."""

    def test_set_override_changes_category(self):
        # A truly divergent file (random name vs. specific title)
        self._add_file_with_cache(
            "abcd.pdf",
            title="Hands-On Machine Learning",
            author="Aurélien Géron",
        )
        r1 = rename.rename_audit(self.profile_name)
        c = r1["candidates"][0]
        self.assertEqual(c["category"], "divergent")
        self.assertFalse(c["is_overridden"])

        result = rename.set_overrides(
            self.profile_name,
            items=[{"rel_path": "abcd.pdf", "category": "ok"}],
        )
        self.assertEqual(result["n_set"], 1)
        self.assertEqual(result["n_errors"], 0)

        r2 = rename.rename_audit(self.profile_name)
        c2 = r2["candidates"][0]
        self.assertEqual(c2["category"], "ok")
        self.assertEqual(c2["auto_category"], "divergent")
        self.assertTrue(c2["is_overridden"])
        # Stats reflect the override
        self.assertEqual(r2["stats"]["n_ok"], 1)
        self.assertEqual(r2["stats"]["n_divergent"], 0)

    def test_clear_override_restores_auto_category(self):
        self._add_file_with_cache("xyz.pdf",
                                   title="Some Real Title", author="Author")
        rename.rename_audit(self.profile_name)
        rename.set_overrides(
            self.profile_name,
            items=[{"rel_path": "xyz.pdf", "category": "ok"}])
        # Now clear
        result = rename.set_overrides(
            self.profile_name,
            items=[{"rel_path": "xyz.pdf"}],
            clear=True)
        self.assertEqual(result["n_set"], 1)
        self.assertTrue(result["successes"][0]["cleared"])

        r = rename.rename_audit(self.profile_name)
        c = r["candidates"][0]
        self.assertFalse(c["is_overridden"])
        self.assertEqual(c["category"], c["auto_category"])

    def test_override_persists_to_disk(self):
        self._add_file_with_cache("f.pdf",
                                   title="Title", author="Author")
        rename.set_overrides(
            self.profile_name,
            items=[{"rel_path": "f.pdf", "category": "ok"}])
        path = rename._overrides_path(self.profile_name)
        self.assertTrue(path.exists())
        on_disk = json.loads(path.read_text())
        self.assertEqual(len(on_disk), 1)
        first = next(iter(on_disk.values()))
        self.assertEqual(first["category"], "ok")
        self.assertIn("ts", first)

    def test_override_survives_rename(self):
        # The override is keyed by cache_key (MD5 of file head bytes),
        # not by rel_path. Renaming the file MUST keep the override
        # active under the new path.
        self._add_file_with_cache(
            "Title Author.pdf",
            title="Real Book", author="Some Author")  # placeholder
        rename.set_overrides(
            self.profile_name,
            items=[{"rel_path": "Title Author.pdf", "category": "ok"}])
        r1 = rename.rename_audit(self.profile_name)
        self.assertEqual(r1["candidates"][0]["category"], "ok")
        self.assertTrue(r1["candidates"][0]["is_overridden"])

        # Now rename the file
        rename.commit_rename(
            self.profile_name, "Title Author.pdf", "Real Book - Some Author.pdf")
        # The renamed file MUST still be marked overridden
        r2 = rename.rename_audit(self.profile_name)
        c = r2["candidates"][0]
        self.assertEqual(c["current_name"], "Real Book - Some Author.pdf")
        self.assertTrue(c["is_overridden"])
        self.assertEqual(c["category"], "ok")

    def test_only_ok_category_allowed_in_mvp(self):
        self._add_file_with_cache("f.pdf",
                                   title="Title", author="Author")
        result = rename.set_overrides(
            self.profile_name,
            items=[{"rel_path": "f.pdf", "category": "placeholder"}])
        self.assertEqual(result["n_set"], 0)
        self.assertEqual(result["n_errors"], 1)
        self.assertEqual(result["errors"][0]["status"], 400)
        self.assertIn("non autorisée", result["errors"][0]["error"])

    def test_path_traversal_refused(self):
        self._add_file_with_cache("f.pdf",
                                   title="Title", author="Author")
        result = rename.set_overrides(
            self.profile_name,
            items=[{"rel_path": "../outside.pdf", "category": "ok"}])
        self.assertEqual(result["n_set"], 0)
        # The resolved path lands outside target, or doesn't exist —
        # either way it's a 400 or 404.
        self.assertEqual(result["n_errors"], 1)
        self.assertIn(result["errors"][0]["status"], (400, 404))

    def test_empty_items_400(self):
        with self.assertRaises(rename.RenameError) as cm:
            rename.set_overrides(self.profile_name, items=[])
        self.assertEqual(cm.exception.status, 400)

    def test_bulk_override_mixed_outcomes(self):
        self._add_file_with_cache("a.pdf",
                                   title="A book", author="A")
        self._add_file_with_cache("b.pdf",
                                   title="B book", author="B")
        result = rename.set_overrides(
            self.profile_name,
            items=[
                {"rel_path": "a.pdf", "category": "ok"},          # OK
                {"rel_path": "b.pdf", "category": "placeholder"}, # bad
                {"rel_path": "ghost.pdf", "category": "ok"},      # 404
            ])
        self.assertEqual(result["n_set"], 1)
        self.assertEqual(result["n_errors"], 2)
        self.assertEqual(result["successes"][0]["rel_path"], "a.pdf")


class TestOverrideEndpoints(RenameAuditTestBase):

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient

        from dashboard.app import app
        self.client = TestClient(app)

    def test_endpoint_set_happy(self):
        self._add_file_with_cache("a.pdf",
                                   title="A book", author="A")
        r = self.client.post("/api/rename/override", json={
            "profile": self.profile_name,
            "items": [{"rel_path": "a.pdf", "category": "ok"}],
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["n_set"], 1)

    def test_endpoint_clear(self):
        self._add_file_with_cache("a.pdf",
                                   title="A book", author="A")
        self.client.post("/api/rename/override", json={
            "profile": self.profile_name,
            "items": [{"rel_path": "a.pdf", "category": "ok"}],
        })
        r = self.client.post("/api/rename/override", json={
            "profile": self.profile_name,
            "items": [{"rel_path": "a.pdf"}],
            "clear": True,
        })
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["successes"][0]["cleared"])

    def test_endpoint_empty_items_400(self):
        r = self.client.post("/api/rename/override", json={
            "profile": self.profile_name,
            "items": [],
        })
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
