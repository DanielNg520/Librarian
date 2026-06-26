#!/usr/bin/env python3
"""
Phase 0 tests for librarian.tags — folder hashtags.

Standalone, no test framework needed (matches the suite's `python tests/...`
convention). Run:

    PYTHONPATH=librarian python librarian/tests/test_tags.py
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

# Allow running from the repo root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from librarian.tags import TagResolver, parse_tags_file, slugify_tag  # noqa: E402

_passed = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ── slugify_tag ────────────────────────────────────────────────────────────
def test_slugify() -> None:
    check(slugify_tag("bathroom selfie") == "bathroom_selfie", "space → _")
    check(slugify_tag("#Beach") == "beach", "strip # + lowercase")
    check(slugify_tag("foo-bar") == "foo_bar", "hyphen → _ (Telegram-safe)")
    check(slugify_tag("a  b\tc") == "a_b_c", "runs of ws/non-alnum collapse")
    check(slugify_tag("  outdoor  ") == "outdoor", "trim")
    check(slugify_tag("café") == "caf", "non-ascii folded out (conservative)")
    check(slugify_tag("123") is None, "all-digit rejected")
    check(slugify_tag("") is None, "empty rejected")
    check(slugify_tag("#") is None, "bare # rejected")
    check(slugify_tag(None) is None, "None rejected")
    check(slugify_tag("__x__") == "x", "edge underscores trimmed")


# ── parse_tags_file ────────────────────────────────────────────────────────
def test_parse() -> None:
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / ".tags"
        write(f, "#beach #sunset\n// a comment\n\noutdoor\nBeach\n")
        # multi-tag line, comment + blank skipped, dedup (#beach vs Beach)
        check(parse_tags_file(f) == ["beach", "sunset", "outdoor"],
              f"parse/order/dedup, got {parse_tags_file(f)}")
        check(parse_tags_file(Path(td) / "nope.tags") == [], "missing file → []")


# ── inheritance + boundary ─────────────────────────────────────────────────
def test_inheritance() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "Photos"
        write(root / ".tags", "#trip")
        write(root / "selfie" / ".tags", "#selfie")
        write(root / "selfie" / "outdoor" / ".tags", "#outdoor #trip")  # dup #trip
        img = root / "selfie" / "outdoor" / "IMG_1.jpg"
        write(img, "x")

        r = TagResolver(root)
        check(bool(r) is True, "resolver with abs root is truthy")
        # outermost-first, deduped: trip (root), selfie, outdoor
        check(r.tags_for(img) == ["trip", "selfie", "outdoor"],
              f"layered order/dedup, got {r.tags_for(img)}")

        # a file directly in root inherits only root's tags
        top = root / "top.jpg"
        write(top, "x")
        check(r.tags_for(top) == ["trip"], f"root-level, got {r.tags_for(top)}")

        # boundary: a file OUTSIDE the root yields nothing, even if it has .tags
        outside = Path(td) / "Other" / "x.jpg"
        write(outside.parent / ".tags", "#secret")
        write(outside, "x")
        check(r.tags_for(outside) == [], "file outside root → no tags (boundary)")


# ── no-op resolver ─────────────────────────────────────────────────────────
def test_noop() -> None:
    check(bool(TagResolver(None)) is False, "None root → falsy no-op")
    check(bool(TagResolver("")) is False, "empty root → falsy no-op")
    check(bool(TagResolver("relative/dir")) is False, "relative root → no-op")
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "f.jpg"
        write(f, "x")
        check(TagResolver(None).apply("cap", f) == "cap", "no-op apply unchanged")
        check(TagResolver(None).tags_for(f) == [], "no-op tags_for empty")


# ── apply (caption composition + de-dup) ───────────────────────────────────
def test_apply() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "P"
        write(root / ".tags", "#beach #sunset")
        img = root / "IMG.jpg"
        write(img, "x")
        r = TagResolver(root)

        check(r.apply("Nice day", img) == "Nice day\n#beach #sunset",
              f"append line, got {r.apply('Nice day', img)!r}")
        check(r.apply(None, img) == "#beach #sunset", "empty caption → tags alone")
        check(r.apply("", img) == "#beach #sunset", "blank caption → tags alone")
        # caption already carries #beach → not doubled, only #sunset added
        check(r.apply("at the #beach", img) == "at the #beach\n#sunset",
              f"no-double existing tag, got {r.apply('at the #beach', img)!r}")
        # word-boundary: #beaches must NOT suppress #beach
        check(r.apply("love #beaches", img) == "love #beaches\n#beach #sunset",
              f"word-boundary de-dup, got {r.apply('love #beaches', img)!r}")

        # a folder with no tags leaves the caption untouched
        empty = Path(td) / "Q"
        f2 = empty / "y.jpg"
        write(f2, "x")
        r2 = TagResolver(empty)
        check(r2.apply("hello", f2) == "hello", "no tags → caption unchanged")


# ── mtime hot-reload ───────────────────────────────────────────────────────
def test_reload() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "P"
        tagfile = root / ".tags"
        write(tagfile, "#one")
        img = root / "z.jpg"
        write(img, "x")
        r = TagResolver(root)
        check(r.tags_for(img) == ["one"], "initial read")

        # edit the file; bump mtime to defeat coarse timestamp resolution
        write(tagfile, "#one #two")
        import os
        future = time.time() + 5
        os.utime(tagfile, (future, future))
        check(r.tags_for(img) == ["one", "two"], "re-read after mtime moved")

        # deleting the file falls back to no tags
        tagfile.unlink()
        check(r.tags_for(img) == [], "removed .tags → no tags")


def main() -> int:
    for fn in (test_slugify, test_parse, test_inheritance, test_noop,
               test_apply, test_reload):
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\nlibrarian.tags — all {_passed} checks passed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as e:
        print(f"\n✗ FAILED: {e}")
        raise SystemExit(1)
