#!/usr/bin/env python3
"""
Phase 3 tests — storage backends + filetype routing.

LocalBackend is exercised end-to-end (store→exists→verify→fetch, hash integrity,
tamper detection, idempotence). Routing and the registry are pure. RcloneBackend
is round-tripped for real IF `rclone` is on PATH (local-path remote), else
skipped. The Telegram backend's live path needs a session + network, so only its
guards and durability flag are checked here.

    PYTHONPATH=librarian python3 librarian/tests/test_backends.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from librarian.backends import (BackendError, LocalBackend, Locator,  # noqa: E402
                                Registry, StorageBackend)
from librarian.backends.rclone import RcloneBackend                   # noqa: E402
from librarian.backends.telegram import TelegramBackend               # noqa: E402
from librarian.hashing import full_hash                               # noqa: E402
from librarian.routing import RoutingPolicy, bucket                   # noqa: E402

_passed = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1


# ── LocalBackend round-trip + integrity ─────────────────────────────────────
def test_local() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "src.bin"
        src.write_bytes(b"librarian phase 3 payload " * 50)
        h = full_hash(src)
        lb = LocalBackend(td / "store")

        check(isinstance(lb, StorageBackend), "LocalBackend satisfies the Protocol")
        loc = lb.store(src, h)
        check(loc.backend == "local" and Path(loc.ref).exists(), "store landed")
        check(lb.exists(loc), "exists True after store")
        check(lb.verify(loc, h), "verify True for correct hash")
        check(not lb.verify(loc, "0" * 64), "verify False for wrong hash")

        out = lb.fetch(loc, td / "out" / "restored.bin")
        check(full_hash(out) == h, "fetched bytes match by content_hash")

        # idempotent (content-addressed): re-store returns the same ref, no error
        check(lb.store(src, h).ref == loc.ref, "store is idempotent")

        # tamper detection: corrupt the stored object → verify fails
        Path(loc.ref).write_bytes(b"corrupted")
        check(not lb.verify(loc, h), "verify catches a corrupted object")
        check(not lb.exists(Locator("local", str(td / "ghost"))), "absent → exists False")


# ── routing: bucket + policy + config ───────────────────────────────────────
def test_routing() -> None:
    check(bucket("a.JPG") == "photo", "jpg → photo (case-insensitive)")
    check(bucket("a.mp4") == "video", "mp4 → video")
    check(bucket("a.pdf") == "document", "pdf → document")
    check(bucket("a.mp3") == "audio", "mp3 → audio")
    check(bucket("a.xyz") == "other", "unknown → other")

    default = RoutingPolicy()
    check(default.backends_for("x.jpg") == ["gdrive", "telegram"],
          f"default routing, got {default.backends_for('x.jpg')}")

    custom = RoutingPolicy({"default": ["d"], "photo": ["box", "telegram"]})
    check(custom.backends_for("x.jpg") == ["box", "telegram"], "photo override")
    check(custom.backends_for("x.mp4") == ["d"], "falls back to default")

    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "config.toml"
        cfg.write_text(
            "[backup.routing]\n"
            'default = ["gdrive", "telegram"]\n'
            'document = ["box"]\n', encoding="utf-8")
        loaded = RoutingPolicy.load(cfg)
        check(loaded.backends_for("book.pdf") == ["box"], "loaded document route")
        check(loaded.backends_for("clip.mp4") == ["gdrive", "telegram"],
              "loaded default route")
    # missing config → defaults, no crash
    check(RoutingPolicy.load(Path(td) / "gone.toml").default == ["gdrive", "telegram"],
          "missing config → defaults")


# ── registry ────────────────────────────────────────────────────────────────
class _Fake:
    name = "fake"
    # no `durable` attr → treated as non-durable
    def store(self, p, h): return Locator("fake", "x")
    def fetch(self, loc, d): return d
    def verify(self, loc, h): return True
    def exists(self, loc): return True


def test_registry() -> None:
    with tempfile.TemporaryDirectory() as td:
        lb = LocalBackend(Path(td) / "s")
        reg = Registry({"local": lb})
        reg.register(_Fake())

        check(reg.get("local") is lb, "get returns the instance")
        check(reg.has("fake") and not reg.has("nope"), "has")
        check(reg.names() == ["fake", "local"], f"names sorted, got {reg.names()}")
        check(reg.available(["local", "gdrive", "fake"]) == ["local", "fake"],
              "available filters to registered, preserving order")
        check(reg.is_durable("local") and not reg.is_durable("fake"),
              "durability: Local gates offload, fake does not")
        try:
            reg.get("missing")
            check(False, "get(missing) should raise")
        except BackendError:
            check(True, "get(missing) raises BackendError")


# ── Telegram backend guards + flags (live path needs a session) ─────────────
def test_telegram_guards() -> None:
    check(TelegramBackend.durable is False,
          "Telegram is presence-only → never gates offload")
    try:
        TelegramBackend(None, None)
        check(False, "TelegramBackend(None, None) should raise")
    except BackendError:
        check(True, "missing client/destination raises")


# ── RcloneBackend: real round-trip via a local-path remote, if installed ────
def test_rclone() -> None:
    if not shutil.which("rclone"):
        print("    (skipped test_rclone — rclone not on PATH)")
        return
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "src.bin"
        src.write_bytes(b"rclone payload " * 100)
        h = full_hash(src)
        rb = RcloneBackend(str(td / "remote"), base="lib")   # local path as remote
        loc = rb.store(src, h)
        check(rb.exists(loc), "rclone exists after store")
        check(rb.verify(loc, h), "rclone hash verify")
        out = rb.fetch(loc, td / "out.bin")
        check(full_hash(out) == h, "rclone fetched bytes match")


def main() -> int:
    for fn in (test_local, test_routing, test_registry, test_telegram_guards,
               test_rclone):
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\nlibrarian Phase 3 — all {_passed} checks passed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as e:
        print(f"\n✗ FAILED: {e}")
        raise SystemExit(1)
