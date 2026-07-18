#!/usr/bin/env python3
"""
Phase 11 tests — bootstrap (config → wired objects) + the `librarian` CLI.

Bootstrap: backend construction from `[backends.*]` (local + rclone-shaped +
unknown/malformed sections fail-soft), assemble() wiring routing + protection.
CLI: every subcommand driven through `main(argv)` against an env-pointed temp
DB/config, asserting real effects (rows, files, exit codes) not just output.

    PYTHONPATH=. python3 tests/test_cli.py
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from librarian import bootstrap                                  # noqa: E402
from librarian.cli import main as cli                            # noqa: E402
from librarian.store import ItemStore                            # noqa: E402

_passed = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1


def make(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    past = time.time() - 60
    os.utime(path, (past, past))


@contextlib.contextmanager
def env(td: Path, config_text: str | None = None):
    """Point $LIBRARIAN_DB / $LIBRARIAN_CONFIG at per-test files."""
    old_db = os.environ.get("LIBRARIAN_DB")
    old_cfg = os.environ.get("LIBRARIAN_CONFIG")
    os.environ["LIBRARIAN_DB"] = str(td / "l.db")
    cfg = td / "config.toml"
    if config_text is not None:
        cfg.write_text(config_text)
    os.environ["LIBRARIAN_CONFIG"] = str(cfg)
    try:
        yield
    finally:
        for k, v in (("LIBRARIAN_DB", old_db), ("LIBRARIAN_CONFIG", old_cfg)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def run(*argv: str) -> "tuple[int, str]":
    """Invoke the CLI in-process, capturing stdout."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli(list(argv))
    return code, out.getvalue()


# ── bootstrap ───────────────────────────────────────────────────────────────
def test_bootstrap_registry() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        cfg = td / "c.toml"
        cfg.write_text(f"""
[backends.disk]
type = "local"
path = "{td / 'store'}"

[backends.mystery]
type = "warp-drive"

[backends.broken]
type = "local"
# missing 'path' key

[backends.notatable]
type = "rclone"
remote = 123
""")
        reg = bootstrap.registry_from_config(cfg)
        # only the well-formed local backend survives; the rest are loud skips
        check(reg.names() == ["disk"], f"fail-soft construction, got {reg.names()}")
        check(reg.is_durable("disk"), "local backend registered durable")
        # empty/missing config → empty registry, no crash
        check(bootstrap.registry_from_config(td / "nope.toml").names() == [],
              "missing config → empty registry")


def test_bootstrap_assemble() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "Root").mkdir()
        cfg = td / "c.toml"
        cfg.write_text(f"""
[backends.disk]
type = "local"
path = "{td / 'store'}"

[backup.routing]
default = ["disk"]

[protect]
roots = ["Root"]
""")
        s = ItemStore.open(str(td / "l.db"))
        from librarian import roots as roots_mod
        roots_mod.register(s, "Root", td / "Root")
        registry, policy, guard = bootstrap.assemble(s, config=cfg)
        check(registry.names() == ["disk"], "assemble builds the registry")
        check(policy.default == ["disk"], "assemble loads routing")
        f = td / "Root" / "x.bin"
        f.write_bytes(b"z" * 200)
        check(guard.delete(f, reason="t") is False and f.exists(),
              "assemble wires the protection policy into the guard")
        s.close()


# ── CLI end-to-end ──────────────────────────────────────────────────────────
def test_cli_root_scan_status_find() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        make(td / "Books" / "physics" / "Quantum Theory.pdf", b"%PDF" + b"Q" * 300)
        with env(td, config_text=""):
            code, out = run("root", "add", "Books", str(td / "Books"))
            check(code == 0 and "registered" in out, f"root add, got {out!r}")
            code, out = run("root", "list")
            check(code == 0 and "Books" in out, "root list shows it")

            code, out = run("scan")
            check(code == 0 and "inserted=1" in out, f"scan ingests, got {out!r}")

            code, out = run("status")
            check(code == 0 and "items: 1" in out and "pending" in out,
                  f"status counts, got {out!r}")
            check("NONE configured" in out, "status is honest about no backends")

            code, out = run("find", "quantum")
            check(code == 0 and "Quantum Theory" in out, f"find hits, got {out!r}")
            code, _ = run("find", "zzzznothing")
            check(code == 1, "find miss exits 1")

            code, out = run("root", "remove", "Books")
            check(code == 0, "root remove")
            code, _ = run("root", "remove", "Books")
            check(code == 1, "removing a missing root exits 1")


def test_cli_cycle_and_dedup() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        make(td / "Root" / "a" / "doc.bin", b"D" * 400)
        make(td / "Root" / "b" / "doc copy.bin", b"D" * 400)   # byte-twin
        cfg = f"""
[backends.disk]
type = "local"
path = "{td / 'store'}"

[backup.routing]
default = ["disk"]
"""
        with env(td, config_text=cfg):
            run("root", "add", "Root", str(td / "Root"))

            # full cycle: scan collapses the twin at ingest, backup ships to disk
            code, out = run("cycle")
            check(code == 0, f"cycle clean, got {out!r}")
            check("backed_up=1" in out, f"one unique item shipped, got {out!r}")

            # dedup default is dry-run and says so
            code, out = run("dedup")
            check(code == 0 and "dry run" in out.lower(), f"dedup dry, got {out!r}")

            # offload stage reclaims after durable verify
            code, out = run("cycle", "--offload")
            check(code == 0 and "offloaded=1" in out,
                  f"offload reclaims, got {out!r}")
            remaining = [p for p in (td / "Root").rglob("*") if p.is_file()]
            check(remaining == [], f"local bytes reclaimed, got {remaining}")

            code, out = run("status")
            check("offloaded" in out and "items: 1" in out,
                  f"status reflects offload, got {out!r}")


def test_cli_scan_missing_root_and_bad_config() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        with env(td, config_text="this is [not toml"):
            code, out = run("scan")
            check(code == 1 and "no roots" in out, "scan with no roots exits 1")
            # malformed config never crashes a command
            code, out = run("status")
            check(code == 0, f"status survives malformed config, got {out!r}")


def main() -> None:
    for t in (test_bootstrap_registry, test_bootstrap_assemble,
              test_cli_root_scan_status_find, test_cli_cycle_and_dedup,
              test_cli_scan_missing_root_and_bad_config):
        t()
        print(f"  ✓ {t.__name__}")
    print(f"\nlibrarian Phase 11 — all {_passed} checks passed.")


if __name__ == "__main__":
    main()
