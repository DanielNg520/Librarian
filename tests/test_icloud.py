#!/usr/bin/env python3
"""
Phase 9 tests — iCloud-aware ingest.

Covers pure classification (`placeholder_state` / stub name parsing), the
injectable `materialize`, the three scan policies (report_only / materialize /
skip) over evicted files, and the offload guard that refuses to evict an
already-cloud-managed file. All OS probes are injected, so this runs anywhere.

    PYTHONPATH=. python3 tests/test_icloud.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from librarian import icloud, roots                              # noqa: E402
from librarian.icloud import ICloudState                         # noqa: E402
from librarian.backends import LocalBackend, Locator, Registry   # noqa: E402
from librarian.deletion import DeletionGuard                     # noqa: E402
from librarian.models import Status                              # noqa: E402
from librarian.offload import OffloadOutcome, offload_item       # noqa: E402
from librarian.store import ItemStore                            # noqa: E402

_passed = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1


class _FakeStat:
    """Minimal stat_result stand-in for placeholder_state."""
    def __init__(self, size: int, blocks: int):
        self.st_size = size
        self.st_blocks = blocks


def make(path: Path, data: bytes, *, age: float = 60) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    t = time.time() - age
    os.utime(path, (t, t))


# ── pure classification ─────────────────────────────────────────────────────
def test_stub_parsing() -> None:
    check(icloud.is_stub(".Report.pdf.icloud"), "recognizes a stub")
    check(not icloud.is_stub("Report.pdf"), "ordinary name is not a stub")
    check(not icloud.is_stub(".tags"), ".tags sidecar is not a stub")
    check(icloud.original_name(".My Book.epub.icloud") == "My Book.epub",
          "strips leading dot + .icloud")
    check(icloud.original_path("/a/b/.x.pdf.icloud") == Path("/a/b/x.pdf"),
          "original_path drops the placeholder")


def test_placeholder_state() -> None:
    # stub → EVICTED_STUB regardless of blocks
    check(icloud.placeholder_state(".f.pdf.icloud") == ICloudState.EVICTED_STUB,
          "stub classified as evicted stub")
    # dataless: real size, zero allocated blocks
    dataless = lambda p: _FakeStat(size=10_000, blocks=0)
    check(icloud.placeholder_state("f.pdf", stat_fn=dataless)
          == ICloudState.DATALESS, "zero-block non-empty file is dataless")
    # materialized: blocks allocated
    local = lambda p: _FakeStat(size=10_000, blocks=24)
    check(icloud.placeholder_state("f.pdf", stat_fn=local)
          == ICloudState.MATERIALIZED, "allocated file is materialized")
    # unreadable → fail-open to MATERIALIZED (don't hide a real file)
    def boom(p): raise OSError("nope")
    check(icloud.placeholder_state("f.pdf", stat_fn=boom)
          == ICloudState.MATERIALIZED, "unstattable → materialized (fail-open)")
    check(icloud.is_evicted(ICloudState.DATALESS)
          and icloud.is_evicted(ICloudState.EVICTED_STUB)
          and not icloud.is_evicted(ICloudState.MATERIALIZED),
          "is_evicted covers dataless + stub only")


def test_materialize_injected() -> None:
    with tempfile.TemporaryDirectory() as td:
        real = Path(td) / "book.pdf"
        # runner "downloads" the bytes; state_fn then reports it local.
        downloaded = {}

        def runner(target):
            make(target, b"PDF" * 100)
            downloaded["path"] = target

        ok = icloud.materialize(str(Path(td) / ".book.pdf.icloud"),
                                runner=runner,
                                state_fn=lambda p: ICloudState.MATERIALIZED)
        check(ok, "materialize returns True on success")
        check(downloaded["path"] == real,
              "materialize downloads the ORIGINAL path, not the stub")

        # runner fails → False, no raise
        def fail(target): raise RuntimeError("brctl missing")
        check(icloud.materialize(str(real), runner=fail) is False,
              "materialize fails soft when the download can't start")

        # never materializes → times out to False (injected clock, no real sleep)
        t = {"now": 0.0}
        clock = lambda: t["now"]
        def tick(_): t["now"] += 10
        check(icloud.materialize(str(real), timeout=5, runner=lambda p: None,
                                 state_fn=lambda p: ICloudState.DATALESS,
                                 sleep_fn=tick, clock_fn=clock) is False,
              "materialize times out when bytes never arrive")


# ── scan policies ───────────────────────────────────────────────────────────
def _root(td: Path):
    (td / "Root").mkdir(parents=True, exist_ok=True)
    s = ItemStore.open(str(td / "l.db"))
    roots.register(s, "Root", td / "Root")
    return s, td / "Root"


def test_scan_report_only_surfaces_dataless(monkeypatch_state) -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root = _root(td)
        make(root / "local.txt", b"HELLO" * 40)      # ordinary → ingested
        make(root / "evicted.pdf", b"PDF" * 100)      # pretend dataless
        monkeypatch_state({root / "evicted.pdf": ICloudState.DATALESS})

        rep = roots.scan(s, "Root")                   # default report_only
        check(rep.cloud_only == 1, f"dataless surfaced as cloud_only, got {rep}")
        check(rep.inserted == 1, "only the local file was ingested")
        check(s.id_of(str(root / "evicted.pdf")) is None,
              "report_only did NOT ingest (and never read) the evicted file")
        check(s.id_of(str(root / "local.txt")) is not None, "local file tracked")
        s.close()


def test_scan_skip_ignores_silently(monkeypatch_state) -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root = _root(td)
        make(root / "evicted.pdf", b"PDF" * 100)
        monkeypatch_state({root / "evicted.pdf": ICloudState.DATALESS})

        rep = roots.scan(s, "Root", icloud_policy="skip")
        check(rep.cloud_only == 0 and rep.inserted == 0,
              f"skip ignores evicted files silently, got {rep}")
        s.close()


def test_scan_materialize_downloads_then_ingests(patch_attr, monkeypatch_state) -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root = _root(td)
        f = root / "evicted.pdf"
        make(f, b"PDF" * 100)
        monkeypatch_state({f: ICloudState.DATALESS})

        # materialize() is injected to "succeed" without touching the OS.
        called = {}
        def fake_materialize(p, **kw):
            called["p"] = Path(p)
            return True
        patch_attr(icloud, "materialize", fake_materialize)

        rep = roots.scan(s, "Root", icloud_policy="materialize")
        check(called["p"] == f, "materialize was asked to download the file")
        check(rep.inserted == 1 and rep.cloud_only == 0,
              f"materialized file was ingested, got {rep}")
        check(s.id_of(str(f)) is not None, "row created after materialize")
        s.close()


def test_scan_stub_no_longer_invisible(monkeypatch_state) -> None:
    """An `.icloud` stub used to be silently skipped as a hidden file; now it is
    surfaced (report_only) rather than lost."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root = _root(td)
        # a real eviction placeholder for 'Paper.pdf'
        make(root / ".Paper.pdf.icloud", b"plist-ish-stub-bytes")
        rep = roots.scan(s, "Root")                   # report_only
        check(rep.cloud_only == 1,
              f"stub surfaced instead of skipped as hidden, got {rep}")
        s.close()


# ── offload guard ───────────────────────────────────────────────────────────
def test_offload_refuses_cloud_managed(monkeypatch_state) -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        s, root = _root(td)
        f = root / "big.bin"
        make(f, b"DATA" * 500)
        roots.scan(s, "Root")                         # ingested while local
        it = s.get(s.id_of(str(f)))
        # simulate a durable backup + BACKED_UP status
        disk = LocalBackend(td / "disk", name="disk")
        loc = disk.store(f, it.content_hash)
        s.add_location(it.id, "disk", loc.ref, verified_at="2026-01-01T00:00:00Z")
        s.set_status(it.id, Status.BACKED_UP)
        reg = Registry({"disk": disk})

        # Now iCloud has evicted the local bytes → offload must NOT unlink it.
        monkeypatch_state({f: ICloudState.DATALESS})
        it = s.get(it.id)
        out = offload_item(s, reg, DeletionGuard(), it)
        check(out == OffloadOutcome.CLOUD_MANAGED,
              f"offload leaves an iCloud-evicted file alone, got {out}")
        check(f.exists(), "the (placeholder) file was not deleted")
        check(s.get(it.id).status == Status.BACKED_UP.value,
              "status unchanged (still backed up, still present)")
        s.close()


# ── tiny injection helpers (avoid a pytest dependency) ──────────────────────
def _run(test, **fixtures) -> None:
    test(**fixtures)


def main() -> None:
    # a monkeypatch shim: set attr, remember originals, restore after each test
    class Patcher:
        def __init__(self): self._undo = []
        def __call__(self, obj, name, val):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, val)
        def restore(self):
            for obj, name, val in reversed(self._undo):
                setattr(obj, name, val)

    def state_patcher(patch):
        def apply(mapping):
            def fake(path, **kw):
                return mapping.get(Path(path),
                                   icloud.ICloudState.MATERIALIZED)
            patch(icloud, "placeholder_state", fake)
        return apply

    tests = [
        (test_stub_parsing, {}),
        (test_placeholder_state, {}),
        (test_materialize_injected, {}),
        (test_scan_report_only_surfaces_dataless, "state"),
        (test_scan_skip_ignores_silently, "state"),
        (test_scan_materialize_downloads_then_ingests, "both"),
        (test_scan_stub_no_longer_invisible, "state"),
        (test_offload_refuses_cloud_managed, "state"),
    ]
    for test, kind in tests:
        patch = Patcher()
        try:
            if kind == "state":
                test(monkeypatch_state=state_patcher(patch))
            elif kind == "both":
                test(patch_attr=patch, monkeypatch_state=state_patcher(patch))
            else:
                test()
        finally:
            patch.restore()
        print(f"  ✓ {test.__name__}")
    print(f"\nlibrarian Phase 9 — all {_passed} checks passed.")


if __name__ == "__main__":
    main()
