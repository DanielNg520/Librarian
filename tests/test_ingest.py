#!/usr/bin/env python3
"""
Phase 1 tests — librarian.db spine: schema, store, ingest (stabilize→hash→
dedup→insert), locations, and root scan.

Standalone, no framework. Run:

    PYTHONPATH=librarian python3 librarian/tests/test_ingest.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from librarian import roots, schema                              # noqa: E402
from librarian.ingest import IngestOutcome, register_file        # noqa: E402
from librarian.models import Status                              # noqa: E402
from librarian.store import ItemStore                            # noqa: E402

_passed = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1


def write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    # Age the file 60s into the past so stability.is_stable takes the quiescent
    # fast path (no 1.5s probe sleep) — keeps the suite fast and tests the
    # realistic "folder has been quiet" path.
    past = time.time() - 60
    os.utime(path, (past, past))


# ── schema: fresh + idempotent reopen ──────────────────────────────────────
def test_schema(db: str) -> None:
    s = ItemStore.open(db)
    tables = {r["name"] for r in s.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    check({"items", "locations", "roots", "metadata"} <= tables,
          f"all tables created, got {tables}")
    check(s.conn.execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION,
          "user_version == SCHEMA_VERSION")
    s.close()
    # Reopen must be a no-op (no crash, same version, rows persist).
    s2 = ItemStore.open(db)
    check(s2.conn.execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION,
          "reopen keeps version")
    s2.close()


# ── ingest: insert, universal hash, dedup, already-known ───────────────────
def test_ingest_dedup(db: str, work: Path) -> None:
    s = ItemStore.open(db)
    a = work / "a.bin"
    b = work / "b.bin"          # byte-identical to a (a duplicate)
    c = work / "c.bin"          # distinct content
    write(a, b"X" * 500)
    write(b, b"X" * 500)
    write(c, b"Y" * 500)

    r_a = register_file(s, a, root="T")
    check(r_a.outcome == IngestOutcome.INSERTED, f"a inserted, got {r_a.outcome}")
    check(r_a.content_hash is not None, "every row carries content_hash")
    check(s.get(r_a.item_id).content_hash == r_a.content_hash, "hash stamped on row")
    check(s.get(r_a.item_id).title == "a", "title defaults to stem")

    r_b = register_file(s, b, root="T")
    check(r_b.outcome == IngestOutcome.DEDUP_DROPPED,
          f"byte-twin dropped, got {r_b.outcome}")
    check(not b.exists(), "duplicate file physically removed")
    check(a.exists(), "tracked original kept")

    r_c = register_file(s, c, root="T")
    check(r_c.outcome == IngestOutcome.INSERTED, "distinct content inserted")

    # exactly two rows (a, c); the duplicate never created a second row
    check(s.count_by_status() == 2, f"two rows total, got {s.count_by_status()}")
    check(s.count_by_status(Status.PENDING) == 2, "both pending")

    # re-ingest an already-tracked path
    r_a2 = register_file(s, a, root="T")
    check(r_a2.outcome == IngestOutcome.ALREADY_KNOWN, "re-ingest is already_known")
    check(s.count_by_status() == 2, "no new row on re-ingest")
    s.close()


# ── ingest: unstable + unreadable skipped ──────────────────────────────────
def test_ingest_skips(db: str, work: Path) -> None:
    s = ItemStore.open(db)
    work.mkdir(parents=True, exist_ok=True)
    tiny = work / "tiny.bin"
    tiny.write_bytes(b"x")                          # under MIN_FILE_BYTES
    os.utime(tiny, (time.time() - 60, time.time() - 60))
    check(register_file(s, tiny).outcome == IngestOutcome.UNSTABLE,
          "sub-minimum file is unstable/skipped")
    check(register_file(s, work / "ghost.bin").outcome == IngestOutcome.UNSTABLE,
          "missing file is skipped, not crashed")
    s.close()


# ── locations: add + read back ─────────────────────────────────────────────
def test_locations(db: str, work: Path) -> None:
    s = ItemStore.open(db)
    f = work / "loc.bin"
    write(f, b"Z" * 300)
    iid = register_file(s, f, root="T").item_id
    s.add_location(iid, "telegram", "msg:42")
    s.add_location(iid, "gdrive", "drive:abc", verified_at="2026-06-26T00:00:00Z")
    locs = {l.backend: l for l in s.locations_for(iid)}
    check(set(locs) == {"telegram", "gdrive"}, f"two locations, got {set(locs)}")
    check(locs["gdrive"].verified_at == "2026-06-26T00:00:00Z", "verified_at stored")
    # upsert: same (item,backend) updates rather than duplicates
    s.add_location(iid, "telegram", "msg:99")
    check(s.locations_for(iid).__len__() == 2, "upsert, not duplicate")
    check({l.backend: l.locator for l in s.locations_for(iid)}["telegram"] == "msg:99",
          "locator updated on conflict")
    # CASCADE: deleting the item removes its locations
    s.delete(iid)
    check(s.locations_for(iid) == [], "locations CASCADE-deleted with item")
    s.close()


# ── roots: register + idempotent scan + dedup across the tree ──────────────
def test_roots_scan(db: str, work: Path) -> None:
    s = ItemStore.open(db)
    base = work / "Photos"
    write(base / "trip" / "img1.jpg", b"A" * 400)
    write(base / "trip" / "img2.jpg", b"B" * 400)
    write(base / "dup_of_img1.jpg", b"A" * 400)        # duplicate of img1
    write(base / ".tags", b"#should_be_ignored_hidden_file_padding_xxxxxxxxxx")
    (base / "sub").mkdir(parents=True, exist_ok=True)

    roots.register(s, "Photos", base)
    rep = roots.scan(s, "Photos")
    check(rep.scanned == 3, f"3 non-hidden files scanned, got {rep.scanned}")
    check(rep.inserted == 2, f"2 distinct inserted, got {rep.inserted}")
    check(rep.dropped == 1, f"1 duplicate dropped, got {rep.dropped}")
    check(s.count_by_status() == 2, "two distinct rows after scan")

    # second scan is fully idempotent
    rep2 = roots.scan(s, "Photos")
    check(rep2.known == 2 and rep2.inserted == 0,
          f"re-scan all known, got {rep2}")

    # bad names rejected; duplicate root rejected
    for bad in ("", "a/b", "x:y"):
        try:
            roots.register(s, bad, base)
            check(False, f"bad name {bad!r} should raise")
        except roots.RootError:
            check(True, f"bad name {bad!r} rejected")
    try:
        roots.register(s, "Photos", base)
        check(False, "duplicate root should raise")
    except roots.RootError:
        check(True, "duplicate root rejected")
    s.close()


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        # Each test gets its OWN db file — isolation, so row counts are absolute.
        for i, (fn, extra) in enumerate((
            (test_schema, ()),
            (test_ingest_dedup, (work / "w1",)),
            (test_ingest_skips, (work / "w2",)),
            (test_locations, (work / "w3",)),
            (test_roots_scan, (work / "w4",)),
        )):
            db = str(work / f"db{i}.db")
            fn(db, *extra)
            print(f"  ✓ {fn.__name__}")
    print(f"\nlibrarian Phase 1 — all {_passed} checks passed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as e:
        print(f"\n✗ FAILED: {e}")
        raise SystemExit(1)
