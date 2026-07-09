#!/usr/bin/env python3
"""
Phase 5 tests — librarian bot + retrieval (find / serve / restore).

Covers the FTS5 `find` (search over title/caption/path/upload_date, incl. the
migration + trigger sync on later edits), the `serve` Telegram-location pick, and
`restore`'s integrity contract: bytes are re-hashed against content_hash before
success, a corrupt/wrong copy is skipped for the next backend, an OFFLOADED row
flips to BACKED_UP, and a missing copy fails safe without touching status.

    PYTHONPATH=. python3 tests/test_bot.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from librarian import roots                                       # noqa: E402
from librarian.backends import LocalBackend, Locator, Registry    # noqa: E402
from librarian.backends.base import BackendError                  # noqa: E402
from librarian.backup import backup_pass                          # noqa: E402
from librarian.bot import (RestoreOutcome, find, restore,         # noqa: E402
                           telegram_location)
from librarian.deletion import DeletionGuard                      # noqa: E402
from librarian.models import Status                               # noqa: E402
from librarian.offload import offload_pass                        # noqa: E402
from librarian.routing import RoutingPolicy                       # noqa: E402
from librarian.store import ItemStore, _fts_match                 # noqa: E402

_passed = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1


# ── fakes ───────────────────────────────────────────────────────────────────
class FakeTelegram:
    """Presence-only, non-durable — serves bytes back on fetch like the real one."""
    name = "tg"
    durable = False

    def __init__(self):
        self._blobs: dict[str, bytes] = {}

    def store(self, path, content_hash):
        self._blobs[content_hash] = Path(path).read_bytes()
        return Locator(self.name, content_hash)

    def fetch(self, locator, dest):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self._blobs[locator.ref])
        return dest

    def verify(self, locator, content_hash):
        return locator.ref in self._blobs

    def exists(self, locator):
        return locator.ref in self._blobs


class WrongBytesBackend:
    """A durable backend that hands back CORRUPT bytes — restore must reject it."""
    name = "rot"
    durable = True

    def store(self, path, content_hash):
        return Locator(self.name, content_hash)

    def fetch(self, locator, dest):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"not the original bytes")
        return dest

    def verify(self, locator, content_hash):
        return True                      # claims fine; restore hashes for real

    def exists(self, locator):
        return True


# ── helpers ─────────────────────────────────────────────────────────────────
def make(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    past = time.time() - 60
    os.utime(path, (past, past))


def setup(td: Path, files: dict[str, bytes]):
    db = str(td / "l.db")
    root = td / "Root"
    for rel, data in files.items():
        make(root / rel, data)
    s = ItemStore.open(db)
    roots.register(s, "Root", root)
    roots.scan(s, "Root")
    return s, root


def item_for(store: ItemStore, path: Path):
    return store.get(store.id_of(str(path)))


# ── _fts_match: sanitizer ─────────────────────────────────────────────────────
def test_fts_match_sanitizer() -> None:
    check(_fts_match("beach") == '"beach"*', "single token → quoted prefix")
    check(_fts_match("  beach   sunset ") == '"beach"* "sunset"*', "AND of tokens")
    check(_fts_match("") == "", "empty → empty")
    check(_fts_match("--- *** ") == "", "operator-only query → empty")
    # embedded quotes are doubled, so a stray quote can't break out of the phrase
    check(_fts_match('a"b') == '"a""b"*', "internal quote doubled")


# ── find (FTS5) ───────────────────────────────────────────────────────────────
def test_find_basic_and_path() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root = setup(td, {"Vacation/beach.jpg": b"J" * 120,
                             "Work/report.pdf": b"P" * 120})
        # path is indexed → folder/filename is findable pre-caption
        hits = find(s, "beach")
        check([Path(h.path).name for h in hits] == ["beach.jpg"], "find by filename")
        check(find(s, "vacation")[0].root == "Root", "find by folder segment")
        check(find(s, "report")[0].path.endswith("report.pdf"), "find other file")
        check(find(s, "nonexistentxyz") == [], "no match → empty")
        check(find(s, "") == [], "empty query → empty")
        s.close()


def test_find_caption_and_trigger_sync() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root = setup(td, {"a.jpg": b"A" * 120})
        item = item_for(s, root / "a.jpg")
        check(find(s, "seahorse") == [], "caption term absent before edit")
        # UPDATE items → items_fts_au trigger must re-index the new caption
        s.conn.execute("UPDATE items SET caption = ? WHERE id = ?",
                       ("a rare seahorse #ocean", item.id))
        s.conn.commit()
        hits = find(s, "seahorse")
        check(len(hits) == 1 and hits[0].id == item.id, "caption edit is searchable")
        check(len(find(s, "ocean")) == 1, "hashtag word in caption searchable")
        # DELETE → items_fts_ad trigger must drop it from the index
        s.delete(item.id)
        check(find(s, "seahorse") == [], "deleted row leaves the index")
        s.close()


def test_find_prefix() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root = setup(td, {"mountains.jpg": b"M" * 120})
        check(len(find(s, "mount")) == 1, "prefix match (mount → mountains)")
        s.close()


# ── serve: telegram-location pick ─────────────────────────────────────────────
def test_serve_location_pick() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root = setup(td, {"doc.pdf": b"D" * 200})
        disk = LocalBackend(td / "disk", name="disk")
        tg = FakeTelegram()
        reg = Registry({"disk": disk, "tg": tg})
        pol = RoutingPolicy({"document": ["disk", "tg"]})
        backup_pass(s, reg, pol)
        item = item_for(s, root / "doc.pdf")
        loc = telegram_location(s, reg, item)
        check(loc is not None and loc.backend == "tg",
              "serve picks the presence-only (Telegram) copy, not the durable one")

        # a durable-only item has no Telegram copy → serve declines
        (td / "b").mkdir()
        s2, root2 = setup(td / "b", {"only.pdf": b"O" * 200})
        reg2 = Registry({"disk": disk})
        backup_pass(s2, reg2, RoutingPolicy({"document": ["disk"]}))
        check(telegram_location(s2, reg2, item_for(s2, root2 / "only.pdf")) is None,
              "no Telegram copy → no inline serve")
        s.close(); s2.close()


# ── restore: the integrity contract ───────────────────────────────────────────
def _offloaded(td: Path):
    """A doc offloaded off disk, durable copy on disk-backend + tg fallback."""
    s, root = setup(td, {"doc.pdf": b"PDF-bytes" * 40})
    disk = LocalBackend(td / "disk", name="disk")
    tg = FakeTelegram()
    reg = Registry({"disk": disk, "tg": tg})
    pol = RoutingPolicy({"document": ["disk", "tg"]})
    backup_pass(s, reg, pol)
    offload_pass(s, reg, DeletionGuard())          # reclaim the local file
    return s, root, reg


def test_restore_happy_and_status_flip() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root, reg = _offloaded(td)
        f = root / "doc.pdf"
        check(not f.exists(), "precondition: offloaded off disk")
        check(item_for(s, f).status == Status.OFFLOADED.value, "precondition OFFLOADED")

        out_dir = td / "Downloads"
        res = restore(s, reg, item_for(s, f), out_dir)
        check(res.outcome == RestoreOutcome.RESTORED, f"restored, got {res.outcome}")
        check(res.backend == "disk", "durable backend tried first")
        landed = out_dir / "doc.pdf"
        check(landed.exists() and landed.read_bytes() == b"PDF-bytes" * 40,
              "bytes land in Downloads intact")
        check(item_for(s, f).status == Status.BACKED_UP.value,
              "OFFLOADED → BACKED_UP after verified restore")
        s.close()


def test_restore_skips_corrupt_copy() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        # durable 'rot' hands back wrong bytes; tg fallback has the real bytes.
        s, root = setup(td, {"doc.pdf": b"REAL" * 50})
        tg = FakeTelegram()
        reg = Registry({"rot": WrongBytesBackend(), "tg": tg})
        backup_pass(s, reg, RoutingPolicy({"document": ["rot", "tg"]}))
        item = item_for(s, root / "doc.pdf")

        out_dir = td / "Downloads"
        res = restore(s, reg, item, out_dir)
        check(res.outcome == RestoreOutcome.RESTORED, f"got {res.outcome}")
        check(res.backend == "tg", "corrupt durable copy skipped, fell back to tg")
        check((out_dir / "doc.pdf").read_bytes() == b"REAL" * 50, "correct bytes served")
        s.close()


def test_restore_all_corrupt_verify_fails() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root = setup(td, {"doc.pdf": b"REAL" * 50})
        reg = Registry({"rot": WrongBytesBackend()})
        backup_pass(s, reg, RoutingPolicy({"document": ["rot"]}))
        item = item_for(s, root / "doc.pdf")
        out_dir = td / "Downloads"
        res = restore(s, reg, item, out_dir)
        check(res.outcome == RestoreOutcome.VERIFY_FAILED,
              f"no copy hashes back → verify_failed, got {res.outcome}")
        check(not (out_dir / "doc.pdf").exists(), "corrupt download not left behind")
        check(item_for(s, root / "doc.pdf").status == Status.BACKED_UP.value,
              "status untouched on failed restore")
        s.close()


def test_restore_no_location() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root = setup(td, {"doc.pdf": b"X" * 120})
        item = item_for(s, root / "doc.pdf")            # never backed up anywhere
        res = restore(s, Registry({}), item, td / "Downloads")
        check(res.outcome == RestoreOutcome.NO_LOCATION, f"got {res.outcome}")
        check(not res.ok, "no_location is not ok")
        s.close()


def test_restore_fetch_failed() -> None:
    class Boom:
        name = "boom"; durable = True
        def store(self, p, h): return Locator(self.name, h)
        def fetch(self, loc, dest): raise BackendError("network down")
        def verify(self, loc, h): return True
        def exists(self, loc): return True

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root = setup(td, {"doc.pdf": b"X" * 120})
        reg = Registry({"boom": Boom()})
        backup_pass(s, reg, RoutingPolicy({"document": ["boom"]}))
        item = item_for(s, root / "doc.pdf")
        res = restore(s, reg, item, td / "Downloads")
        check(res.outcome == RestoreOutcome.FETCH_FAILED, f"got {res.outcome}")
        check("network down" in (res.error or ""), "carries the backend error")
        s.close()


def main() -> int:
    for fn in (test_fts_match_sanitizer, test_find_basic_and_path,
               test_find_caption_and_trigger_sync, test_find_prefix,
               test_serve_location_pick, test_restore_happy_and_status_flip,
               test_restore_skips_corrupt_copy, test_restore_all_corrupt_verify_fails,
               test_restore_no_location, test_restore_fetch_failed):
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\nlibrarian Phase 5 — all {_passed} checks passed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as e:
        print(f"\n✗ FAILED: {e}")
        raise SystemExit(1)
