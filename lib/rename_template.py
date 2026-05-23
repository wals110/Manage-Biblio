"""Filename template rendering with conditional blocks + sanitization.

Used to generate a clean filename from LLM-extracted metadata (title /
author / year / lang / theme). Designed to be config-driven via
profile.yaml.

Template syntax
---------------

A template is a regular string with two block types:

- ``{varname}``         — REQUIRED variable. Rendering fails if the value
                          is empty.
- ``{ ...var... }``     — OPTIONAL block. Inside a block, the variable
                          name appears as a BARE word (no inner braces).
                          The block is rendered ONLY when its variable is
                          non-empty; otherwise the whole block (including
                          its static parts) drops out.

Examples:

    "{title}"                     → "Foo"
    "{title}{ - author}"          → "Foo - Bar" or "Foo" if author empty
    "{title}{ (year)}"            → "Foo (2024)" or "Foo" if year empty
    "{author}{ - title}{ (year)}" → "Bar - Foo (2024)"

Note: nested braces are NOT supported — write ``{ (year)}`` not
``{ ({year})}``. The parser rejects nested-brace forms loudly.

Known variables: ``title``, ``author``, ``year``, ``lang``, ``theme``.
Other names raise TemplateError at parse time so typos fail loudly.

Sanitization pipeline
---------------------

1. Unicode normalization (default ``NFKC``, can be disabled with ``None``).
2. Character replacement (``replace_chars``, e.g. ``"/" → "-"``).
3. Character drop (``drop_chars``, e.g. ``?*<>|"\\``).
4. Whitespace collapse (multiple spaces → single, leading/trailing trim).
5. Length truncation that preserves the extension and prefers cuts at
   word boundaries when not too aggressive.

The defaults target HFS+/exFAT safety on macOS + portability hint for
Windows-shared paths.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Known template variables. Extending requires updating callers too.
KNOWN_VARS = frozenset({"title", "author", "year", "lang", "theme"})

# Filesystem-safe defaults
DEFAULT_REPLACE_CHARS: dict[str, str] = {"/": "-", ":": "—"}
DEFAULT_DROP_CHARS: str = '?*<>|"\\'
DEFAULT_MAX_LENGTH: int = 180
DEFAULT_NORMALIZE: str | None = "NFKC"


class TemplateError(ValueError):
    """Raised on malformed templates (unmatched braces, unknown variable)."""


@dataclass
class TemplateBlock:
    """One parsed unit of a template: a literal string, a required
    variable, or an optional block (prefix + var + suffix)."""

    kind: str           # "literal" | "var" | "optional"
    name: str = ""      # variable name for var/optional; ignored for literal
    text: str = ""      # raw text for literal
    prefix: str = ""    # for optional: static text before the var
    suffix: str = ""    # for optional: static text after the var


@dataclass
class RenderResult:
    new_stem: str = ""               # rendered base name, no extension
    new_name: str = ""               # full name including extension
    is_valid: bool = False           # passed validation + non-empty
    issues: list[str] = field(default_factory=list)
    used_fallback: bool = False


# ─── Parser ──────────────────────────────────────────────────────────────


def parse_template(template: str) -> list[TemplateBlock]:
    """Split a template string into ordered blocks.

    Raises TemplateError on:
      - unmatched ``{`` or ``}``
      - unknown variable name
      - empty ``{}`` (no variable)
    Nested braces are NOT supported.
    """
    if "{" not in template:
        # Still validate: a stray '}' in a pure-literal template is suspicious.
        if "}" in template:
            raise TemplateError(
                f"unmatched '}}' at position {template.index('}')}")
        return [TemplateBlock(kind="literal", text=template)] if template else []

    blocks: list[TemplateBlock] = []
    i = 0
    n = len(template)
    while i < n:
        ch = template[i]
        if ch == "}":
            raise TemplateError(f"unmatched '}}' at position {i}")
        if ch != "{":
            j = template.find("{", i)
            if j == -1:
                blocks.append(TemplateBlock(kind="literal", text=template[i:]))
                break
            blocks.append(TemplateBlock(kind="literal", text=template[i:j]))
            i = j
            continue

        # We're at '{' — find the matching '}'
        j = template.find("}", i + 1)
        if j == -1:
            raise TemplateError(f"unmatched '{{' at position {i}")
        if "{" in template[i + 1: j]:
            raise TemplateError(
                f"nested braces not supported at position {i} — "
                f"write '{{ (year)}}' instead of '{{ ({{year}})}}'")
        inner = template[i + 1: j]
        if not inner:
            raise TemplateError(f"empty '{{}}' at position {i}")

        stripped = inner.strip()
        if stripped in KNOWN_VARS:
            # Plain variable block: "{title}"
            blocks.append(TemplateBlock(kind="var", name=stripped))
        else:
            # Optional block: must contain exactly one known var as a word
            var_name: str | None = None
            var_start: int = -1
            for v in KNOWN_VARS:
                m = re.search(r"(?<![A-Za-z0-9_])" + v + r"(?![A-Za-z0-9_])",
                              inner)
                if m:
                    if var_name is not None:
                        raise TemplateError(
                            f"optional block {inner!r} contains multiple "
                            f"variables — only one is supported per block")
                    var_name = v
                    var_start = m.start()
            if var_name is None:
                raise TemplateError(
                    f"optional block {inner!r} has no known variable "
                    f"(known: {sorted(KNOWN_VARS)})")
            prefix = inner[:var_start]
            suffix = inner[var_start + len(var_name):]
            blocks.append(TemplateBlock(
                kind="optional", name=var_name,
                prefix=prefix, suffix=suffix,
            ))
        i = j + 1

    return blocks


# ─── Renderer ────────────────────────────────────────────────────────────


def _value_of(metadata: dict[str, object], name: str) -> str:
    """Coerce metadata lookup to a clean string (trimmed)."""
    v = metadata.get(name)
    if v is None:
        return ""
    return str(v).strip()


def render_template(
    template: str,
    metadata: dict[str, object],
    extension: str = ".pdf",
    sanitize_cfg: dict | None = None,
    max_length: int = DEFAULT_MAX_LENGTH,
) -> RenderResult:
    """Render a template against metadata, returning RenderResult.

    Returns is_valid=False on any of:
      - template parse error
      - required variable empty
      - all-empty result after sanitization
    """
    try:
        blocks = parse_template(template)
    except TemplateError as e:
        return RenderResult(is_valid=False, issues=[f"parse error: {e}"])

    parts: list[str] = []
    issues: list[str] = []
    for b in blocks:
        if b.kind == "literal":
            parts.append(b.text)
        elif b.kind == "var":
            val = _value_of(metadata, b.name)
            if not val:
                return RenderResult(
                    is_valid=False,
                    issues=[f"required variable {b.name!r} is empty"],
                )
            parts.append(val)
        elif b.kind == "optional":
            val = _value_of(metadata, b.name)
            if val:
                parts.append(b.prefix + val + b.suffix)
            # else: drop the entire block (including prefix/suffix)

    raw = "".join(parts)
    cleaned = sanitize(raw, sanitize_cfg or {})
    if not cleaned:
        return RenderResult(
            is_valid=False,
            issues=["empty result after sanitization"],
        )

    new_stem, truncated = truncate_with_ext(cleaned, extension, max_length)
    if truncated:
        issues.append(f"truncated to {max_length} chars")

    return RenderResult(
        new_stem=new_stem,
        new_name=new_stem + extension,
        is_valid=True,
        issues=issues,
    )


def render_with_fallback(
    template: str,
    fallback: str,
    metadata: dict[str, object],
    extension: str = ".pdf",
    sanitize_cfg: dict | None = None,
    max_length: int = DEFAULT_MAX_LENGTH,
) -> RenderResult:
    """Try the primary template; on failure, retry with the fallback.

    The fallback should be conservative (e.g. ``"{title}"`` alone) so it
    rescues the common case where author/year are missing.
    """
    primary = render_template(
        template, metadata, extension, sanitize_cfg, max_length)
    if primary.is_valid:
        return primary
    fb = render_template(
        fallback, metadata, extension, sanitize_cfg, max_length)
    fb.used_fallback = True
    # Preserve the primary diagnostics so the caller knows why we fell back
    fb.issues = ["primary failed: " + "; ".join(primary.issues)] + fb.issues
    return fb


# ─── Sanitization ────────────────────────────────────────────────────────


def sanitize(name: str, cfg: dict | None = None) -> str:
    """Apply Unicode + char + whitespace cleanups in 4 deterministic passes."""
    cfg = cfg or {}

    # 1. Unicode normalization (NFKC by default folds compatibility chars).
    norm = cfg.get("normalize_unicode", DEFAULT_NORMALIZE)
    if norm:
        name = unicodedata.normalize(norm, name)

    # 2. Replace specific chars.
    replace_map: dict[str, str] = cfg.get("replace_chars", DEFAULT_REPLACE_CHARS)
    if replace_map:
        for ch, repl in replace_map.items():
            name = name.replace(ch, repl)

    # 3. Drop chars listed in drop_chars (typed as a string for ergonomics).
    drop = cfg.get("drop_chars", DEFAULT_DROP_CHARS)
    if drop:
        for ch in drop:
            name = name.replace(ch, "")

    # 4. Collapse whitespace + trim. Multiple consecutive spaces (often
    #    appearing after a drop) become a single space.
    if cfg.get("collapse_spaces", True):
        name = re.sub(r"\s+", " ", name).strip()
    # Also strip surrounding dots which look ugly and confuse some FS.
    name = name.strip(".")

    return name


def truncate_with_ext(
    stem: str,
    extension: str,
    max_length: int,
) -> tuple[str, bool]:
    """Truncate ``stem`` so that ``stem + extension`` fits in max_length.

    Cuts at the last whitespace before the budget when it sits in the
    last 30% of the available range (avoids cutting in the middle of a
    word). Returns (final_stem, was_truncated).
    """
    if max_length <= 0:
        return "", True
    budget = max_length - len(extension)
    if budget <= 0:
        # Extension alone is already over budget: do best-effort
        return stem[:max_length], True
    if len(stem) <= budget:
        return stem, False
    cut = stem[:budget]
    last_space = cut.rfind(" ")
    # Only cut at word boundary if the truncation isn't too aggressive
    # (don't lose >30% of the budget just to land on a space).
    if last_space != -1 and last_space >= int(budget * 0.7):
        cut = cut[:last_space]
    return cut.rstrip(), True
