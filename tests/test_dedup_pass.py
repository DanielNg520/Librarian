#!/usr/bin/env python3
"""
Phase 8 tests — standalone dedup pass + protection policy + no-dup-upload.

Covers `dedup_root` (DB-driven grouping, untracked-straggler hashing, winner
determinism across a re-scan, adopt vs delete-row reconciliation, dry-run,
guard refusal), `ProtectionPolicy` (pause + protected prefixes, config load),
and the backup path's dup-upload short-circuit. Standalone, no optional deps:

    PYTHONPATH=. python3 tests/test_dedup_pass.py
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
from librarian.backup import backup_item, backup_pass             # noqa: E402
from librarian.dedup import DedupReport, dedup_pass, dedup_root    # noqa: E402
from librarian.deletion import DeletionGuard, ProtectionPolicy    # noqa: E402
from librarian.models import Status                               # noqa: E402
from librarian.routing import RoutingPolicy                       # noqa: E402
from librarian.store import ItemStore                             # noqa: E402

_passed = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1


# ── fakes / helpers ─────────────────────────────────────────────────────────
class FakeTelegram:
    name = "tg"
    durable = False

    def __init__(self):
        self._have: set[str] = set()
        self.stored: list[str] = []           # content_hashes actually uploaded

    def store(self, path, content_hash, *, caption=None):
        self._have.add(content_hash)
        self.stored.append(content_hash)
        return Locator(self.name, content_hash)

    def fetch(self, locator, dest):
        return dest

    def verify(self, locator, content_hash):
        return locator.ref in self._have

    def exists(self, locator):
        return locator.ref in self._have


def make(path: Path, data: bytes, *, age: float = 60) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    t = time.time() - age
    os.utime(path, (t, t))


def open_root(td: Path):
    (td / "Root").mkdir(parents=True, exist_ok=True)
    s = ItemStore.open(str(td / "l.db"))
    roots.register(s, "Root", td / "Root")
    return s, td / "Root"


def paths_on_disk(base: Path) -> set[str]:
    return {str(p.relative_to(base)) for p in base.rglob("*") if p.is_file()
            and not p.name.startswith(".")}


# ── dedup: DB-driven group collapse (two tracked twins) ─────────────────────
def test_dedup_tracked_twins_collapse() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root = open_root(td)
        # Same bytes at two different paths. Register the OLDER one first so it
        # wins the tiebreak, THEN force a second independent row for the twin.
        make(root / "a/keep.bin", b"DUP" * 100, age=120)
        roots.scan(s, "Root")                          # keep.bin → row (discovered first)
        make(root / "b/copy.bin", b"DUP" * 100, age=60)
        # Insert a row for the twin directly (bypass ingest's collapse) to model a
        # duplicate that slipped past ingest-time collapse.
        s.add_item(path=str(root / "b/copy.bin"),
                   content_hash=s.get(s.id_of(str(root / "a/keep.bin"))).content_hash,
                   root="Root", size_bytes=300, title="copy")

        guard = DeletionGuard()
        # dry-run first: nothing changes, but the group is predicted.
        dry = dedup_root(s, guard, "Root", dry_run=True)
        check(dry.dry_run and dry.dup_groups == 1 and dry.removed == 1,
              f"dry-run predicts 1 group / 1 removal, got {dry}")
        check(paths_on_disk(root) == {"a/keep.bin", "b/copy.bin"},
              "dry-run left both files on disk")

        rep = dedup_root(s, guard, "Root", dry_run=False)
        check(rep.dup_groups == 1 and rep.removed == 1 and rep.rows_removed == 1,
              f"live: 1 group, 1 removed, loser row deleted, got {rep}")
        check(paths_on_disk(root) == {"a/keep.bin"}, "only the winner survives")
        check(s.id_of(str(root / "b/copy.bin")) is None, "loser row gone")
        check(s.id_of(str(root / "a/keep.bin")) is not None, "winner row kept")
        s.close()


# ── dedup: untracked straggler adopted into a row-less winner… and determinism
def test_dedup_untracked_and_determinism() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root = open_root(td)
        # Two byte-identical UNTRACKED files (no rows at all). Winner is decided
        # purely by the path tiebreak → deterministic across re-scans.
        make(root / "z_second.bin", b"RAW" * 100)
        make(root / "a_first.bin", b"RAW" * 100)
        guard = DeletionGuard()

        rep = dedup_root(s, guard, "Root", dry_run=False)
        check(rep.removed == 1, f"one redundant untracked copy removed, got {rep}")
        survivors = paths_on_disk(root)
        check(survivors == {"a_first.bin"},
              f"lexicographically-first path is the deterministic winner, got {survivors}")

        # Re-run: the survivor is unique now → nothing to do (idempotent, and it
        # did NOT flip to the other name and re-create the dup).
        rep2 = dedup_root(s, guard, "Root", dry_run=False)
        check(rep2.dup_groups == 0 and rep2.removed == 0,
              f"second pass is a no-op, got {rep2}")
        s.close()


def test_dedup_tracked_beats_untracked() -> None:
    """INVARIANT: a tracked+backed-up file always OUTRANKS an untracked byte-twin,
    even one whose path sorts earlier — so the row (and its backups) is never
    orphaned; the untracked stray is the one removed."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root = open_root(td)
        make(root / "tracked.bin", b"KEEP" * 100)
        roots.scan(s, "Root")
        it = s.get(s.id_of(str(root / "tracked.bin")))
        s.add_location(it.id, "tg", "msg-42")          # pretend it's backed up
        # An untracked twin whose name sorts BEFORE 'tracked.bin' — path tiebreak
        # would favor it, but has-a-row wins first, so 'tracked.bin' still keeps.
        make(root / "a_twin.bin", b"KEEP" * 100)

        rep = dedup_root(s, DeletionGuard(), "Root", dry_run=False)
        check(rep.removed == 1 and rep.rows_removed == 0,
              f"untracked stray removed, no row deleted, got {rep}")
        check(paths_on_disk(root) == {"tracked.bin"},
              "the tracked (backed-up) file survives")
        kept = s.get(it.id)
        check(kept is not None and kept.path == str(root / "tracked.bin"),
              "row untouched")
        check([l.locator for l in s.locations_for(it.id)] == ["msg-42"],
              "backup location preserved")
        s.close()


# ── protection policy ───────────────────────────────────────────────────────
def test_protection_policy() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        shielded = td / "Root" / "safe"
        shielded.mkdir(parents=True)
        f = shielded / "x.bin"
        f.write_bytes(b"z" * 200)

        # prefix protection
        pol = ProtectionPolicy(protected_prefixes=[str(shielded)])
        check(pol.is_protected(f), "file under protected prefix is shielded")
        check(not pol.is_protected(td / "Root" / "other.bin"),
              "file outside prefix is deletable")
        # a guard using the policy refuses the delete (file stays)
        g = DeletionGuard(policy=pol)
        check(g.delete(f, reason="test") is False and f.exists(),
              "guard refuses a protected path, file kept")

        # global pause blocks EVERYTHING
        paused = DeletionGuard(policy=ProtectionPolicy(pause=True))
        check(paused.delete(td / "Root" / "other.bin", reason="t") is False,
              "pause shields every path")


def test_protection_load_from_config() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root = open_root(td)
        cfg = td / "config.toml"
        cfg.write_text('[protect]\npause = false\nroots = ["Root"]\n')

        pol = ProtectionPolicy.load(store=s, path=cfg)
        f = root / "deep" / "a.bin"
        make(f, b"q" * 100)
        check(pol.is_protected(f),
              "root NAME in config resolves to its folder → shielded")
        check(not pol.pause, "pause parsed as false")
        # missing config → nothing protected
        empty = ProtectionPolicy.load(path=td / "nope.toml")
        check(not empty.is_protected(f), "absent config protects nothing")
        s.close()


def test_dedup_respects_guard() -> None:
    """A protected root must have its duplicates KEPT (guard refuses)."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root = open_root(td)
        make(root / "a.bin", b"D" * 200)
        make(root / "b.bin", b"D" * 200)
        pol = ProtectionPolicy(protected_prefixes=[str(root)])
        rep = dedup_root(s, DeletionGuard(policy=pol), "Root", dry_run=False)
        check(rep.dup_groups == 1 and rep.removed == 0 and rep.refused == 1,
              f"protected root → dup found but nothing removed, got {rep}")
        check(paths_on_disk(root) == {"a.bin", "b.bin"}, "both copies kept")
        s.close()


# ── avoid dup upload ────────────────────────────────────────────────────────
def test_no_dup_upload() -> None:
    """Two DISTINCT rows with identical bytes: the first uploads; the second
    reuses the existing backend copies instead of re-uploading."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root = open_root(td)
        make(root / "one.bin", b"SAME" * 100)
        roots.scan(s, "Root")
        first = s.get(s.id_of(str(root / "one.bin")))
        # a second, independent row with the SAME content_hash (distinct path)
        make(root / "two.bin", b"SAME" * 100)
        s.add_item(path=str(root / "two.bin"), content_hash=first.content_hash,
                   root="Root", size_bytes=first.size_bytes, title="two")

        disk = LocalBackend(td / "disk", name="disk")
        tg = FakeTelegram()
        reg = Registry({"disk": disk, "tg": tg})
        pol = RoutingPolicy({"default": ["disk", "tg"]})

        out1 = backup_item(s, reg, pol, first)
        check(out1 == out1.__class__.BACKED_UP, f"first backed up, got {out1}")
        check(tg.stored == [first.content_hash], "first item uploaded once to tg")

        two = s.get(s.id_of(str(root / "two.bin")))
        out2 = backup_item(s, reg, pol, two)
        check(out2 == out2.__class__.BACKED_UP, f"second backed up, got {out2}")
        # No SECOND telegram upload — the existing copy's ref was reused.
        check(tg.stored == [first.content_hash],
              f"no re-upload of identical bytes, tg.stored={tg.stored}")
        # …yet the second row still records both locations (reused refs).
        locs = {l.backend for l in s.locations_for(two.id)}
        check(locs == {"disk", "tg"}, f"second row has both locations, got {locs}")
        s.close()


# ── dedup_pass over all roots ───────────────────────────────────────────────
def test_dedup_pass_all_roots() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root = open_root(td)
        make(root / "a.bin", b"P" * 200)
        make(root / "b.bin", b"P" * 200)
        reps = dedup_pass(s, DeletionGuard(), dry_run=True)
        check(len(reps) == 1 and isinstance(reps[0], DedupReport),
              "dedup_pass returns one report per root")
        check(reps[0].removed == 1, f"planned removal counted, got {reps[0]}")
        s.close()


def main() -> None:
    for t in (test_dedup_tracked_twins_collapse,
              test_dedup_untracked_and_determinism,
              test_dedup_tracked_beats_untracked,
              test_protection_policy,
              test_protection_load_from_config,
              test_dedup_respects_guard,
              test_no_dup_upload,
              test_dedup_pass_all_roots):
        t()
        print(f"  ✓ {t.__name__}")
    print(f"\nlibrarian Phase 8 — all {_passed} checks passed.")


if __name__ == "__main__":
    main()
