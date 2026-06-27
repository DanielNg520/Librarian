#!/usr/bin/env python3
"""
Phase 4 tests — backup fan-out + offload (HSM).

The offload gate is the integrity-critical bit, so it's tested hard: a durable
backend must verify the bytes LIVE before any delete; a Telegram-only file is
never offloaded; corruption of the durable copy blocks the delete; the guard can
refuse; dry-run and crash-safe convergence behave.

    PYTHONPATH=librarian python3 librarian/tests/test_backup.py
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
from librarian.backup import BackupOutcome, backup_item, backup_pass  # noqa: E402
from librarian.deletion import DeletionGuard                      # noqa: E402
from librarian.models import Status                               # noqa: E402
from librarian.offload import (OffloadOutcome, offload_item,      # noqa: E402
                               offload_pass)
from librarian.routing import RoutingPolicy                       # noqa: E402
from librarian.store import ItemStore                             # noqa: E402

_passed = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1


# ── fakes ───────────────────────────────────────────────────────────────────
class FakeTelegram:
    """Presence-only, non-durable — like the real Telegram backend."""
    name = "tg"
    durable = False

    def __init__(self):
        self._have: set[str] = set()

    def store(self, path, content_hash):
        self._have.add(content_hash)
        return Locator(self.name, content_hash)

    def fetch(self, locator, dest):
        return dest

    def verify(self, locator, content_hash):
        return locator.ref in self._have

    def exists(self, locator):
        return locator.ref in self._have


class FailingBackend:
    name = "boom"
    durable = True

    def store(self, path, content_hash):
        raise BackendError("boom always fails")

    def fetch(self, locator, dest):
        return dest

    def verify(self, locator, content_hash):
        return False

    def exists(self, locator):
        return False


# ── helpers ─────────────────────────────────────────────────────────────────
def make(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    past = time.time() - 60
    os.utime(path, (past, past))


def setup(td: Path, files: dict[str, bytes]):
    """Fresh store + registered root + scanned files. Returns (store, root)."""
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


# ── backup fan-out ──────────────────────────────────────────────────────────
def test_backup_fanout() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root = setup(td, {"a/doc.pdf": b"PDF" * 80, "a/pic.jpg": b"JPG" * 80})
        disk = LocalBackend(td / "disk", name="disk")
        reg = Registry({"disk": disk, "tg": FakeTelegram()})
        pol = RoutingPolicy({"document": ["disk", "tg"], "photo": ["tg"]})

        rep = backup_pass(s, reg, pol)
        check(rep.backed_up == 2, f"both backed up, got {rep}")

        doc = item_for(s, root / "a/doc.pdf")
        check(doc.status == Status.BACKED_UP.value, "pdf backed_up")
        locs = {l.backend: l for l in s.locations_for(doc.id)}
        check(set(locs) == {"disk", "tg"}, "pdf has both locations")
        check(locs["disk"].verified_at is not None, "durable copy verified_at set")
        check(locs["tg"].verified_at is None, "presence-only copy not verified")

        pic = item_for(s, root / "a/pic.jpg")
        check(pic.status == Status.BACKED_UP.value, "jpg backed_up (tg only)")
        check([l.backend for l in s.locations_for(pic.id)] == ["tg"], "jpg tg only")
        s.close()


# ── partial failure → retry, then resume without double-store ───────────────
def test_backup_partial_resume() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root = setup(td, {"doc.pdf": b"PDF" * 80})
        disk = LocalBackend(td / "disk", name="disk")
        reg = Registry({"disk": disk, "boom": FailingBackend()})
        pol = RoutingPolicy({"document": ["disk", "boom"]})

        doc = item_for(s, root / "doc.pdf")
        out1 = backup_item(s, reg, pol, doc)
        check(out1 == BackupOutcome.RETRY, f"partial fail → retry, got {out1}")
        doc = item_for(s, root / "doc.pdf")
        check(doc.status == Status.PENDING.value and doc.attempts == 1,
              "stays pending, attempts bumped")
        check([l.backend for l in s.locations_for(doc.id)] == ["disk"],
              "successful copy recorded")

        # 'fix' boom by registering a working backend under the same name
        reg.register(LocalBackend(td / "boom", name="boom"))
        out2 = backup_item(s, reg, pol, item_for(s, root / "doc.pdf"))
        check(out2 == BackupOutcome.BACKED_UP, f"resume → backed_up, got {out2}")
        doc = item_for(s, root / "doc.pdf")
        check({l.backend for l in s.locations_for(doc.id)} == {"disk", "boom"},
              "both locations after resume (disk not double-stored)")
        s.close()


def test_backup_max_retries() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root = setup(td, {"doc.pdf": b"PDF" * 80})
        reg = Registry({"boom": FailingBackend()})
        pol = RoutingPolicy({"document": ["boom"]})
        doc = item_for(s, root / "doc.pdf")
        check(backup_item(s, reg, pol, doc, max_retries=2) == BackupOutcome.RETRY,
              "attempt 1 → retry")
        check(backup_item(s, reg, pol, item_for(s, root / "doc.pdf"),
                          max_retries=2) == BackupOutcome.FAILED, "attempt 2 → failed")
        check(item_for(s, root / "doc.pdf").status == Status.FAILED.value, "FAILED")
        s.close()


# ── offload: the integrity gate ─────────────────────────────────────────────
def _backed_up_doc(td: Path):
    """A doc.pdf backed up to a durable disk + presence-only tg."""
    s, root = setup(td, {"doc.pdf": b"PDF" * 200})
    disk = LocalBackend(td / "disk", name="disk")
    reg = Registry({"disk": disk, "tg": FakeTelegram()})
    pol = RoutingPolicy({"document": ["disk", "tg"]})
    backup_pass(s, reg, pol)
    guard = DeletionGuard()
    return s, root, reg, guard, disk


def test_offload_happy() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root, reg, guard, _disk = _backed_up_doc(td)
        f = root / "doc.pdf"
        out = offload_item(s, reg, guard, item_for(s, f))
        check(out == OffloadOutcome.OFFLOADED, f"offloaded, got {out}")
        check(not f.exists(), "local file reclaimed")
        check(not (f.parent / (f.name + ".stub")).exists(), "no placeholder left")
        check(item_for(s, f).status == Status.OFFLOADED.value, "row OFFLOADED")
        check(len(s.locations_for(item_for(s, f).id)) == 2, "locations preserved")
        s.close()


def test_offload_telegram_only_never() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root = setup(td, {"pic.jpg": b"JPG" * 200})
        reg = Registry({"tg": FakeTelegram()})
        pol = RoutingPolicy({"photo": ["tg"]})
        backup_pass(s, reg, pol)
        f = root / "pic.jpg"
        out = offload_item(s, reg, DeletionGuard(), item_for(s, f))
        check(out == OffloadOutcome.UNVERIFIED, f"tg-only not offloaded, got {out}")
        check(f.exists(), "telegram-only file NEVER deleted")
        check(item_for(s, f).status == Status.BACKED_UP.value, "still backed_up")
        s.close()


def test_offload_corruption_blocks() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root, reg, guard, _disk = _backed_up_doc(td)
        f = root / "doc.pdf"
        # corrupt the durable stored object → live re-verify must fail
        disk_loc = {l.backend: l for l in s.locations_for(item_for(s, f).id)}["disk"]
        Path(disk_loc.locator).write_bytes(b"corrupted now")
        out = offload_item(s, reg, guard, item_for(s, f))
        check(out == OffloadOutcome.UNVERIFIED, f"corruption blocks, got {out}")
        check(f.exists(), "file kept when durable copy fails live verify")
        s.close()


def test_offload_crash_safe() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root, reg, guard, _disk = _backed_up_doc(td)
        f = root / "doc.pdf"
        f.unlink()                       # simulate crash after delete, before status flip
        out = offload_item(s, reg, guard, item_for(s, f))
        check(out == OffloadOutcome.ALREADY_GONE, f"converges, got {out}")
        check(item_for(s, f).status == Status.OFFLOADED.value, "row converges to OFFLOADED")
        s.close()


def test_offload_guard_refuses() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root, reg, _g, _disk = _backed_up_doc(td)
        f = root / "doc.pdf"
        guard = DeletionGuard(protect=lambda p: True)   # everything protected
        out = offload_item(s, reg, guard, item_for(s, f))
        check(out == OffloadOutcome.REFUSED, f"guard refused, got {out}")
        check(f.exists() and item_for(s, f).status == Status.BACKED_UP.value,
              "protected file kept, status unchanged")
        s.close()


def test_offload_dry_run_and_age() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root, reg, guard, _disk = _backed_up_doc(td)
        f = root / "doc.pdf"
        # dry-run: gate passes but nothing deleted
        out = offload_item(s, reg, guard, item_for(s, f), dry_run=True)
        check(out == OffloadOutcome.WOULD_OFFLOAD and f.exists(),
              "dry-run keeps the file")
        check(item_for(s, f).status == Status.BACKED_UP.value, "dry-run no status change")
        # age filter: file is ~60s old, threshold 1 day → not selected
        rep = offload_pass(s, reg, guard, older_than_days=1.0)
        check(rep.offloaded == 0 and f.exists(), "young file excluded by age")
        # no age filter → offloaded
        rep2 = offload_pass(s, reg, guard, older_than_days=0.0)
        check(rep2.offloaded == 1 and not f.exists(), "offload_pass reclaims it")
        s.close()


def main() -> int:
    for fn in (test_backup_fanout, test_backup_partial_resume,
               test_backup_max_retries, test_offload_happy,
               test_offload_telegram_only_never, test_offload_corruption_blocks,
               test_offload_crash_safe, test_offload_guard_refuses,
               test_offload_dry_run_and_age):
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\nlibrarian Phase 4 — all {_passed} checks passed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as e:
        print(f"\n✗ FAILED: {e}")
        raise SystemExit(1)
