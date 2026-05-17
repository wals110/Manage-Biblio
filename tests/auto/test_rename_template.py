#!/usr/bin/env python3
"""Tests for lib/rename_template.py — template parsing, rendering,
sanitization, truncation."""

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from lib.rename_template import (  # noqa: E402
    DEFAULT_DROP_CHARS,
    DEFAULT_MAX_LENGTH,
    DEFAULT_REPLACE_CHARS,
    TemplateError,
    parse_template,
    render_template,
    render_with_fallback,
    sanitize,
    truncate_with_ext,
)


# ─── Parser ──────────────────────────────────────────────────────────────


class TestParseTemplate(unittest.TestCase):

    def test_empty_string(self):
        self.assertEqual(parse_template(""), [])

    def test_pure_literal(self):
        blocks = parse_template("constant.pdf")
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].kind, "literal")
        self.assertEqual(blocks[0].text, "constant.pdf")

    def test_single_var(self):
        blocks = parse_template("{title}")
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].kind, "var")
        self.assertEqual(blocks[0].name, "title")

    def test_var_then_literal(self):
        blocks = parse_template("{title}.pdf")
        self.assertEqual([b.kind for b in blocks], ["var", "literal"])
        self.assertEqual(blocks[1].text, ".pdf")

    def test_optional_block_simple(self):
        blocks = parse_template("{title}{ - author}")
        self.assertEqual([b.kind for b in blocks], ["var", "optional"])
        opt = blocks[1]
        self.assertEqual(opt.name, "author")
        self.assertEqual(opt.prefix, " - ")
        self.assertEqual(opt.suffix, "")

    def test_optional_block_with_suffix(self):
        blocks = parse_template("{title}{ (year)}")
        opt = blocks[1]
        self.assertEqual(opt.name, "year")
        self.assertEqual(opt.prefix, " (")
        self.assertEqual(opt.suffix, ")")

    def test_three_blocks_chained(self):
        blocks = parse_template("{title}{ - author}{ (year)}")
        self.assertEqual([b.kind for b in blocks],
                         ["var", "optional", "optional"])
        self.assertEqual([b.name for b in blocks if b.kind != "literal"],
                         ["title", "author", "year"])

    def test_unknown_variable_raises(self):
        with self.assertRaises(TemplateError) as cm:
            parse_template("{nope}")
        self.assertIn("no known variable", str(cm.exception))

    def test_unmatched_open_brace_raises(self):
        with self.assertRaises(TemplateError):
            parse_template("{title")

    def test_unmatched_close_brace_raises(self):
        with self.assertRaises(TemplateError):
            parse_template("title}")

    def test_nested_braces_raise(self):
        with self.assertRaises(TemplateError) as cm:
            parse_template("{title}{ ({year})}")
        self.assertIn("nested braces", str(cm.exception))

    def test_empty_braces_raise(self):
        with self.assertRaises(TemplateError):
            parse_template("{title}{}")

    def test_two_variables_in_one_optional_block_raises(self):
        with self.assertRaises(TemplateError) as cm:
            parse_template("{ - author year}")  # both 'author' and 'year'
        self.assertIn("multiple variables", str(cm.exception))


# ─── Renderer ────────────────────────────────────────────────────────────


class TestRenderTemplate(unittest.TestCase):

    def test_basic_substitution(self):
        r = render_template("{title}", {"title": "Foo"})
        self.assertTrue(r.is_valid)
        self.assertEqual(r.new_name, "Foo.pdf")
        self.assertEqual(r.new_stem, "Foo")

    def test_full_template_all_vars_present(self):
        r = render_template(
            "{title}{ - author}{ (year)}",
            {"title": "Hands-On ML", "author": "Géron", "year": "2019"},
        )
        self.assertEqual(r.new_name, "Hands-On ML - Géron (2019).pdf")

    def test_optional_drops_block_when_var_empty(self):
        r = render_template(
            "{title}{ - author}", {"title": "Solo", "author": ""},
        )
        self.assertEqual(r.new_name, "Solo.pdf")

    def test_optional_drops_block_when_var_missing(self):
        # Same behaviour: missing key = empty
        r = render_template(
            "{title}{ - author}", {"title": "Solo"},
        )
        self.assertEqual(r.new_name, "Solo.pdf")

    def test_required_var_empty_fails(self):
        r = render_template("{title}", {"title": ""})
        self.assertFalse(r.is_valid)
        self.assertTrue(any("title" in i for i in r.issues))

    def test_required_var_missing_fails(self):
        r = render_template("{title}", {})
        self.assertFalse(r.is_valid)

    def test_extension_param_used(self):
        r = render_template("{title}", {"title": "X"}, extension=".epub")
        self.assertTrue(r.new_name.endswith(".epub"))

    def test_parse_error_returned_in_result(self):
        r = render_template("{nope}", {"nope": "x"})
        self.assertFalse(r.is_valid)
        self.assertTrue(any("parse error" in i for i in r.issues))

    def test_values_are_trimmed(self):
        r = render_template("{title}", {"title": "  Foo  "})
        self.assertEqual(r.new_stem, "Foo")

    def test_non_string_value_coerced(self):
        r = render_template("{year}", {"year": 2019})  # int
        self.assertEqual(r.new_stem, "2019")


# ─── Fallback ────────────────────────────────────────────────────────────


class TestRenderWithFallback(unittest.TestCase):

    def test_primary_succeeds_no_fallback(self):
        r = render_with_fallback(
            "{title}{ - author}", "{title}",
            {"title": "A", "author": "B"},
        )
        self.assertTrue(r.is_valid)
        self.assertFalse(r.used_fallback)

    def test_primary_fails_fallback_succeeds(self):
        # primary requires author; falls back to title-only
        r = render_with_fallback(
            "{author} - {title}", "{title}",
            {"title": "A", "author": ""},
        )
        self.assertTrue(r.is_valid)
        self.assertTrue(r.used_fallback)
        self.assertEqual(r.new_stem, "A")

    def test_both_fail(self):
        r = render_with_fallback(
            "{title}", "{title}", {"title": ""},
        )
        self.assertFalse(r.is_valid)
        # primary issues should be preserved
        self.assertTrue(any("primary failed" in i for i in r.issues))


# ─── Sanitize ────────────────────────────────────────────────────────────


class TestSanitize(unittest.TestCase):

    def test_replace_chars_defaults(self):
        out = sanitize("Pop/Rock: A History")
        self.assertNotIn("/", out)
        self.assertNotIn(":", out)
        self.assertIn("-", out)   # replacement
        self.assertIn("—", out)   # em-dash replacement for ':'

    def test_drop_chars_defaults(self):
        out = sanitize("What? <maybe> *yes*")
        for ch in DEFAULT_DROP_CHARS:
            self.assertNotIn(ch, out)

    def test_collapse_spaces(self):
        out = sanitize("  hello   world  ")
        self.assertEqual(out, "hello world")

    def test_strip_dots(self):
        out = sanitize("...title...")
        self.assertEqual(out, "title")

    def test_custom_replace(self):
        out = sanitize("foo/bar", {"replace_chars": {"/": "_"}})
        self.assertEqual(out, "foo_bar")

    def test_disable_collapse(self):
        out = sanitize("a  b", {"collapse_spaces": False})
        self.assertEqual(out, "a  b")

    def test_unicode_nfkc_default(self):
        # NFKC folds compatibility chars: ﬁ (U+FB01) → fi
        out = sanitize("ﬁle")
        self.assertEqual(out, "file")

    def test_disable_unicode_normalize(self):
        out = sanitize("ﬁle", {"normalize_unicode": None})
        self.assertEqual(out, "ﬁle")

    def test_drop_then_collapse(self):
        # When chars are dropped between two words, the resulting double
        # space is collapsed.
        out = sanitize('a"b"c')  # " is in default drop set
        self.assertEqual(out, "abc")


# ─── Truncate ────────────────────────────────────────────────────────────


class TestTruncate(unittest.TestCase):

    def test_short_passes_through(self):
        stem, was = truncate_with_ext("short title", ".pdf", 180)
        self.assertEqual(stem, "short title")
        self.assertFalse(was)

    def test_cut_at_word_boundary(self):
        # 100-char budget: "Very long title here ..." → cut at the last space
        # within the budget, provided it's not too aggressive (≥70% of budget).
        title = "abc def ghi jkl mno pqr stu vwx yz1 234 567 890"
        stem, was = truncate_with_ext(title, ".pdf", 25)  # budget 21
        self.assertTrue(was)
        self.assertLessEqual(len(stem), 21)
        # Should not end on partial word
        self.assertFalse(stem.endswith(" "))

    def test_cut_brutal_when_no_good_word_boundary(self):
        # Single long token: no space to cut on → brute truncation
        title = "X" * 200
        stem, was = truncate_with_ext(title, ".pdf", 30)
        self.assertTrue(was)
        self.assertEqual(len(stem), 30 - 4)  # budget = 30 - len('.pdf')

    def test_extension_preserved_in_render(self):
        # Via the high-level render call: truncation should fit under
        # max_length AND preserve the extension. Word-boundary cuts may
        # leave a few chars unused — that's fine, the contract is "≤ max".
        long_title = "word " * 100  # 500 chars
        r = render_template(
            "{title}",
            {"title": long_title.strip()},
            extension=".epub",
            max_length=50,
        )
        self.assertTrue(r.is_valid)
        self.assertLessEqual(len(r.new_name), 50)
        self.assertTrue(r.new_name.endswith(".epub"))
        self.assertIn("truncated", " ".join(r.issues))


# ─── Defaults sanity ─────────────────────────────────────────────────────


class TestDefaults(unittest.TestCase):

    def test_default_replace_map_is_safe(self):
        # Replacements must NOT introduce FS-invalid chars
        for repl in DEFAULT_REPLACE_CHARS.values():
            for ch in DEFAULT_DROP_CHARS:
                self.assertNotIn(ch, repl)

    def test_default_max_length_reasonable(self):
        self.assertGreaterEqual(DEFAULT_MAX_LENGTH, 100)
        self.assertLessEqual(DEFAULT_MAX_LENGTH, 255)


if __name__ == "__main__":
    unittest.main()
