#!/usr/bin/env python3
"""
Phase 7 tests — generalized captions + the send seam.

Covers the shared caption spine (`_compose.folder_lines`), the generic-file
captioner, the `captioning.compose` dispatcher (photo / book-enriched /
book-unenriched-fallback / generic), and the backup send seam: the composed
caption reaches the fast-access (Telegram) backend and is ignored by durable
backends. Standalone, no optional deps. Run:

    PYTHONPATH=. python3 tests/test_send_caption.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from librarian import captioning, roots                           # noqa: E402
from librarian.backends import LocalBackend, Locator, Registry    # noqa: E402
from librarian.backup import backup_item, _caption_for            # noqa: E402
from librarian.captioning.generic import compose_generic_caption  # noqa: E402
from librarian.models import Status                               # noqa: E402
from librarian.routing import RoutingPolicy                       # noqa: E402
from librarian.store import ItemStore                             # noqa: E402
from librarian.tags import TagResolver                            # noqa: E402

_passed = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1


# ── fakes / helpers ─────────────────────────────────────────────────────────
class FakeTelegram:
    """Presence-only fast tier; records the caption each store() saw."""
    name = "tg"
    durable = False

    def __init__(self):
        self._have: set[str] = set()
        self.captions: list[str | None] = []

    def store(self, path, content_hash, *, caption=None):
        self._have.add(content_hash)
        self.captions.append(caption)
        return Locator(self.name, content_hash)

    def fetch(self, locator, dest):
        return dest

    def verify(self, locator, content_hash):
        return locator.ref in self._have

    def exists(self, locator):
        return locator.ref in self._have


def make(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    past = time.time() - 60
    os.utime(path, (past, past))


# ── shared spine: generic folder lines match photo's ────────────────────────
def test_generic_folder_lines() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "Root"
        vid = root / "talks" / "2024" / "keynote.mp4"
        make(vid, b"MP4" * 80)

        cap = compose_generic_caption(vid, root)
        lines = cap.splitlines()
        # date line (mtime) + description + hashtag line
        check(len(lines) == 3, f"generic caption = 3 lines, got {lines!r}")
        check(lines[1] == "talks · 2024", f"description line, got {lines[1]!r}")
        # '2024' is all-digit → dropped by the slug rule (Telegram ignores #123),
        # so it stays in the human description but not the hashtag line.
        check(lines[2] == "#talks", f"hashtag line, got {lines[2]!r}")
        # root-level file → date only (no folder segments below the root)
        top = root / "loose.mp4"
        make(top, b"MP4" * 80)
        check("\n" not in compose_generic_caption(top, root),
              "root-level generic file = date only")


def test_generic_layers_base_and_sidecar_tags() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "Root"
        doc = root / "notes" / "handout.docx"
        make(doc, b"DOC" * 80)
        (root / "notes" / ".tags").write_text("#archive shared\n")

        cap = compose_generic_caption(doc, root, resolver=TagResolver(str(root)),
                                      base_tags=["Fav"])
        tagline = cap.splitlines()[-1]
        # union in layer order: base tag → segment → sidecar tags, de-duped
        check(tagline == "#fav #notes #archive #shared",
              f"layered tags, got {tagline!r}")


# ── dispatcher routing ──────────────────────────────────────────────────────
def test_compose_dispatch() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "Root"
        photo = root / "trip" / "IMG.jpg"
        book = root / "shelf" / "novel.epub"
        other = root / "misc" / "clip.mp4"
        for p in (photo, book, other):
            make(p, b"X" * 200)

        # photo → identical to the EXIF-aware photo composer
        check(captioning.compose(photo, root)
              == captioning.compose_caption(photo, root),
              "dispatch(photo) routes to compose_caption")

        # book WITH an enriched caption → that caption verbatim (ISBN pass output)
        enriched = "The Novel\nJane Doe · 2020\nISBN 9780000000000\nshelf\n#shelf"
        check(captioning.compose(book, root, book_caption=enriched) == enriched,
              "dispatch(book, enriched) returns the stored caption verbatim")

        # book WITHOUT a caption → generic fallback (never ships caption-less)
        check(captioning.compose(book, root)
              == compose_generic_caption(book, root),
              "dispatch(book, un-enriched) falls back to generic")

        # any other type → generic
        check(captioning.compose(other, root)
              == compose_generic_caption(other, root),
              "dispatch(other) routes to generic")


# ── the send seam: caption composed once, reaches TG, ignored by durable ────
def test_send_seam_caption_reaches_telegram() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        root = td / "Root"
        vid = root / "talks" / "2024" / "keynote.mp4"
        make(vid, b"MP4" * 200)

        s = ItemStore.open(str(td / "l.db"))
        roots.register(s, "Root", root, tags="Fav")
        roots.scan(s, "Root")

        tg = FakeTelegram()
        disk = LocalBackend(td / "disk", name="disk")
        reg = Registry({"disk": disk, "tg": tg})
        pol = RoutingPolicy({"video": ["disk", "tg"]})

        item = s.get(s.id_of(str(vid)))
        # sanity: the composer sees the root's base tag + folder taxonomy
        cap = _caption_for(s, item)
        check(cap is not None and "talks · 2024" in cap
              and "#fav" in cap and "#talks" in cap,
              f"composed caption carries taxonomy, got {cap!r}")

        out = backup_item(s, reg, pol, item)
        check(out.value == "backed_up", f"backed up, got {out}")
        # Telegram (fast tier) got the caption exactly once…
        check(tg.captions == [cap],
              f"telegram store saw the composed caption, got {tg.captions!r}")
        # …and the durable copy stored fine while ignoring the caption.
        item = s.get(s.id_of(str(vid)))
        locs = {l.backend for l in s.locations_for(item.id)}
        check(locs == {"disk", "tg"}, f"both backends stored, got {locs}")
        s.close()


def test_send_seam_caption_fail_soft() -> None:
    """A file with no registered root → caption composes to None, delivery still
    proceeds (a missing caption must never block a backup)."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        root = td / "Root"
        f = root / "x.mp4"
        make(f, b"MP4" * 200)
        s = ItemStore.open(str(td / "l.db"))
        roots.register(s, "Root", root)
        roots.scan(s, "Root")

        item = s.get(s.id_of(str(f)))
        # forge an item whose root is unknown → compose must fail soft to None
        broken = item.__class__(**{**item.__dict__, "root": "ghost"})
        check(_caption_for(s, broken) is None,
              "unknown root → caption None (fail-soft), no raise")
        s.close()


def main() -> None:
    for t in (test_generic_folder_lines,
              test_generic_layers_base_and_sidecar_tags,
              test_compose_dispatch,
              test_send_seam_caption_reaches_telegram,
              test_send_seam_caption_fail_soft):
        t()
        print(f"  ✓ {t.__name__}")
    print(f"\nlibrarian Phase 7 — all {_passed} checks passed.")


if __name__ == "__main__":
    main()
