#!/usr/bin/env python3
"""
Worker-cycle tests — the heal pass (self-healing) and run_once wiring the three
passes (heal → backup → offload) together across their seams.

The heal pass is safety-critical in both directions, so both are tested hard:
it must repair a genuinely-stale claim automatically (drop → re-arm → re-backup
in the same cycle), and it must NOT drop a claim on a transient backend error
or judge a presence-only backend.

    PYTHONPATH=librarian python3 librarian/tests/test_worker.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from librarian import roots                                        # noqa: E402
from librarian.backends import LocalBackend, Locator, Registry     # noqa: E402
from librarian.backends.base import BackendError                   # noqa: E402
from librarian.backup import backup_pass                           # noqa: E402
from librarian.deletion import DeletionGuard                       # noqa: E402
from librarian.heal import HealOutcome, heal_item, heal_pass       # noqa: E402
from librarian.models import Status                                # noqa: E402
from librarian.routing import RoutingPolicy                        # noqa: E402
from librarian.store import ItemStore                              # noqa: E402
from librarian.worker import CycleReport, full_cycle, run_once     # noqa: E402

_passed = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1


class FakeTelegram:
    """Presence-only, non-durable — like the real Telegram backend."""
    name = "tg"
    durable = False

    def __init__(self):
        self._have: set[str] = set()

    def store(self, path, content_hash, *, caption=None):
        self._have.add(content_hash)
        return Locator(self.name, content_hash)

    def fetch(self, locator, dest):
        return dest

    def verify(self, locator, content_hash):
        return locator.ref in self._have

    def exists(self, locator):
        return locator.ref in self._have


class ErroringBackend:
    """Durable backend whose verify ERRORS (network down) — heal must keep
    the claim."""
    name = "flaky"
    durable = True

    def store(self, path, content_hash, *, caption=None):
        return Locator(self.name, content_hash)

    def fetch(self, locator, dest):
        return dest

    def verify(self, locator, content_hash):
        raise BackendError("network down")

    def exists(self, locator):
        return False


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


# ── heal: intact copies just get verified_at refreshed ──────────────────────
def test_heal_intact() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root = setup(td, {"doc.pdf": b"PDF" * 200})
        reg = Registry({"disk": LocalBackend(td / "disk", name="disk"),
                        "tg": FakeTelegram()})
        backup_pass(s, reg, RoutingPolicy({"document": ["disk", "tg"]}))

        rep = heal_pass(s, reg)
        check(rep.intact == 1 and rep.rearmed == 0 and rep.lost == 0,
              f"intact copy stays intact, got {rep}")
        doc = item_for(s, root / "doc.pdf")
        check(doc.status == Status.BACKED_UP.value, "status untouched")
        locs = {l.backend: l for l in s.locations_for(doc.id)}
        check(locs["disk"].verified_at is not None, "verified_at refreshed")
        s.close()


# ── heal: stale durable claim → drop + re-arm + same-cycle re-backup ────────
def test_heal_rearm_and_reship() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root = setup(td, {"doc.pdf": b"PDF" * 200})
        disk = LocalBackend(td / "disk", name="disk")
        reg = Registry({"disk": disk, "tg": FakeTelegram()})
        pol = RoutingPolicy({"document": ["disk", "tg"]})
        backup_pass(s, reg, pol)

        doc = item_for(s, root / "doc.pdf")
        disk_loc = {l.backend: l for l in s.locations_for(doc.id)}["disk"]
        Path(disk_loc.locator).unlink()          # the NAS lost the object

        out = heal_item(s, reg, item_for(s, root / "doc.pdf"))
        check(out == HealOutcome.REARMED, f"stale claim → rearmed, got {out}")
        doc = item_for(s, root / "doc.pdf")
        check(doc.status == Status.PENDING.value, "re-armed to pending")
        check("disk" not in {l.backend for l in s.locations_for(doc.id)},
              "stale claim dropped from DB")
        check("tg" in {l.backend for l in s.locations_for(doc.id)},
              "presence-only claim never judged/dropped")

        # The very next cycle re-ships ONLY the missing backend and converges.
        h, b, _ = run_once(s, reg, pol, DeletionGuard(), heal=True)
        check(b.backed_up == 1, f"re-shipped in the next cycle, got {b}")
        doc = item_for(s, root / "doc.pdf")
        check(doc.status == Status.BACKED_UP.value, "converged to backed_up")
        check(disk.verify(Locator("disk", str(disk._path_for(doc.content_hash))),
                          doc.content_hash), "durable copy restored on disk")
        s.close()


# ── heal: corruption (not absence) also drops the claim ─────────────────────
def test_heal_corruption() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root = setup(td, {"doc.pdf": b"PDF" * 200})
        reg = Registry({"disk": LocalBackend(td / "disk", name="disk")})
        backup_pass(s, reg, RoutingPolicy({"document": ["disk"]}))
        doc = item_for(s, root / "doc.pdf")
        loc = s.locations_for(doc.id)[0]
        Path(loc.locator).write_bytes(b"bit-rot")
        rep = heal_pass(s, reg)
        check(rep.rearmed == 1, f"corrupt copy → rearmed, got {rep}")
        check(s.locations_for(doc.id) == [], "corrupt claim dropped")
        s.close()


# ── heal: transient verify ERROR keeps the claim (never amplify an outage) ──
def test_heal_transient_error_keeps_claim() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root = setup(td, {"doc.pdf": b"PDF" * 200})
        reg = Registry({"flaky": ErroringBackend()})
        backup_pass(s, reg, RoutingPolicy({"document": ["flaky"]}))
        # store succeeded but flaky is durable → verify errors... so backup
        # can't have converged; place the claim/status by hand to isolate heal.
        doc = item_for(s, root / "doc.pdf")
        s.add_location(doc.id, "flaky", doc.content_hash)
        s.set_status(doc.id, Status.BACKED_UP)

        rep = heal_pass(s, reg)
        check(rep.intact == 1 and rep.rearmed == 0,
              f"error → claim kept (counts as intact), got {rep}")
        doc = item_for(s, root / "doc.pdf")
        check({l.backend for l in s.locations_for(doc.id)} == {"flaky"},
              "claim NOT dropped on a transient error")
        check(doc.status == Status.BACKED_UP.value, "status untouched")
        s.close()


# ── heal: offloaded item that lost every durable copy is reported LOST ──────
def test_heal_lost_detection() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root = setup(td, {"doc.pdf": b"PDF" * 200})
        disk = LocalBackend(td / "disk", name="disk")
        reg = Registry({"disk": disk})
        backup_pass(s, reg, RoutingPolicy({"document": ["disk"]}))
        rep_off = None
        from librarian.offload import offload_pass
        rep_off = offload_pass(s, reg, DeletionGuard())
        check(rep_off.offloaded == 1, "offloaded first")

        doc = item_for(s, root / "doc.pdf")
        loc = s.locations_for(doc.id)[0]
        Path(loc.locator).unlink()               # the ONLY durable copy dies
        rep = heal_pass(s, reg)
        check(rep.lost == 1 and rep.lost_ids == [doc.id],
              f"loss detected loudly, got {rep}")
        check(item_for(s, root / "doc.pdf").status == Status.OFFLOADED.value,
              "status left for manual recovery (no false re-arm)")
        s.close()


# ── run_once: full cycle heal → backup → offload across the seams ───────────
def test_run_once_full_cycle() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root = setup(td, {"doc.pdf": b"PDF" * 200, "pic.jpg": b"JPG" * 200})
        reg = Registry({"disk": LocalBackend(td / "disk", name="disk"),
                        "tg": FakeTelegram()})
        pol = RoutingPolicy({"document": ["disk", "tg"], "photo": ["tg"]})
        h, b, o = run_once(s, reg, pol, DeletionGuard(),
                           heal=True, offload=True)
        check(h is not None and h.intact == 0, "first cycle: nothing to heal yet")
        check(b.backed_up == 2, f"both backed up, got {b}")
        check(o is not None and o.offloaded == 1,
              f"durable doc offloaded, got {o}")
        check(not (root / "doc.pdf").exists(), "doc reclaimed")
        check((root / "pic.jpg").exists(), "tg-only pic NEVER reclaimed")

        # Second cycle: idempotent — heal sees the offloaded doc intact and the
        # tg-only pic skipped (no durable claim is judgeable).
        h2, b2, o2 = run_once(s, reg, pol, DeletionGuard(),
                              heal=True, offload=True)
        check(h2.intact == 1 and h2.skipped == 1 and b2.backed_up == 0
              and o2.offloaded == 0,
              f"steady state, got {h2} / {b2} / {o2}")
        s.close()


# ── full_cycle: every pass wired end-to-end, fail-soft per stage ────────────
def test_full_cycle_end_to_end() -> None:
    """discover → enrich (book caption) → dedup (collapse a stray) → backup →
    offload, all in ONE call, every seam exercised for real."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        root = td / "Root"
        # a book, a photo, and an untracked byte-twin of the book
        make(root / "shelf" / "novel.pdf", b"%PDF" + b"B" * 400)
        make(root / "shelf" / "novel copy.pdf", b"%PDF" + b"B" * 400)
        make(root / "pics" / "pic.jpg", b"JPG" * 200)
        s = ItemStore.open(str(td / "l.db"))
        roots.register(s, "Root", root)

        reg = Registry({"disk": LocalBackend(td / "disk", name="disk"),
                        "tg": FakeTelegram()})
        pol = RoutingPolicy({"default": ["disk", "tg"]})

        rep = full_cycle(s, reg, pol, DeletionGuard(),
                         dedup=True, offload=True, enrich_online=False,
                         enrich_ocr=False)
        check(isinstance(rep, CycleReport) and rep.ok,
              f"cycle clean, got errors={rep.errors}")
        check(len(rep.scans) == 1 and rep.scans[0].inserted >= 2,
              f"scan discovered the files, got {rep.scans}")
        # ingest-time collapse killed the byte-twin: exactly ONE row holds those
        # bytes (2 items total: book + pic), and offload later reclaimed both.
        check(s.count_by_status() == 2,
              f"twin collapsed to one row, got {s.count_by_status()} items")
        # the book got a caption BEFORE backup shipped it
        check(rep.enrich is not None and rep.enrich.enriched >= 1,
              f"book enriched in-cycle, got {rep.enrich}")
        book_id = (s.id_of(str(root / "shelf" / "novel.pdf"))
                   or s.id_of(str(root / "shelf" / "novel copy.pdf")))
        check(book_id is not None and s.get(book_id).caption is not None,
              "book caption written pre-send")
        check(rep.backup is not None and rep.backup.backed_up == 2,
              f"both items backed up, got {rep.backup}")
        check(rep.offload is not None and rep.offload.offloaded == 2,
              f"both offloaded after durable verify, got {rep.offload}")

        # steady state: a second cycle is a no-op and still clean
        rep2 = full_cycle(s, reg, pol, DeletionGuard(),
                          dedup=True, offload=True, enrich_online=False,
                          enrich_ocr=False)
        check(rep2.ok and rep2.backup.backed_up == 0
              and rep2.offload.offloaded == 0,
              f"second cycle idempotent, got {rep2}")
        s.close()


def test_full_cycle_stage_crash_is_contained() -> None:
    """A crashing stage is recorded, NOT raised — and backup still runs."""
    import librarian.worker as worker_mod
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root = setup(td, {"doc.pdf": b"PDF" * 200})
        reg = Registry({"disk": LocalBackend(td / "disk", name="disk")})
        pol = RoutingPolicy({"document": ["disk"]})

        orig = worker_mod.heal_pass
        worker_mod.heal_pass = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("heal exploded"))
        try:
            rep = full_cycle(s, reg, pol, DeletionGuard(), scan=False,
                             enrich=False)
        finally:
            worker_mod.heal_pass = orig
        check(not rep.ok and any("heal" in e for e in rep.errors),
              f"crash recorded, got {rep.errors}")
        check(rep.backup is not None and rep.backup.backed_up == 1,
              f"backup still shipped despite the heal crash, got {rep.backup}")
        s.close()


def main() -> int:
    for fn in (test_heal_intact, test_heal_rearm_and_reship,
               test_heal_corruption, test_heal_transient_error_keeps_claim,
               test_heal_lost_detection, test_run_once_full_cycle,
               test_full_cycle_end_to_end,
               test_full_cycle_stage_crash_is_contained):
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\nlibrarian worker/heal — all {_passed} checks passed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as e:
        print(f"\n✗ FAILED: {e}")
        raise SystemExit(1)
