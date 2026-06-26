"""
librarian.backends.base
───────────────────────
The `StorageBackend` Strategy — the seam that makes Telegram just one backend
among durable clouds. Every backend implements the same four operations so the
backup/offload/restore logic never special-cases a provider.

    store(path, content_hash)  → push bytes, return a Locator (where they landed)
    fetch(locator, dest)       → pull bytes back to dest
    verify(locator, hash)      → prove the copy is present (and, where the backend
                                 can, byte-intact)
    exists(locator)            → cheap presence check

VERIFY SEMANTICS — the load-bearing distinction behind the whole HSM design:
  A backend that can cheaply hash its stored object (LocalBackend, RcloneBackend
  via `rclone hashsum`) verifies BYTE INTEGRITY. Telegram cannot return a hash
  without downloading, so its verify() is PRESENCE-ONLY (== exists). This is
  exactly why offload (reclaiming local disk) requires a *durable, hash-verifying*
  backend — never Telegram alone. See DESIGN §4.1 / ADR-0001 D1.

A `Locator` is `(backend, ref)`: `ref` is the backend-specific address string
stored verbatim in `locations.locator`; `backend` is the `locations.backend`
column. Reconstruct one from a DB row to fetch/verify later.

Independent of the suite (no `import core`); see DESIGN §0.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Locator:
    """Address of stored bytes within ONE backend. `ref` is opaque to callers
    and persisted as-is in locations.locator."""
    backend: str
    ref:     str

    def __str__(self) -> str:
        return f"{self.backend}:{self.ref}"


class BackendError(RuntimeError):
    """A backend operation failed (network, I/O, provider error)."""


class BackendUnavailable(BackendError):
    """A backend's prerequisite is missing — the `rclone` binary, the `telethon`
    library, credentials, etc. Raised at construction so a misconfigured backend
    fails LOUD at startup rather than silently dropping a backup. (Mirrors the
    suite's hard-dep startup guards, e.g. hachoir.)"""


@runtime_checkable
class StorageBackend(Protocol):
    name: str

    def store(self, path: Path, content_hash: str) -> Locator: ...
    def fetch(self, locator: Locator, dest: Path) -> Path: ...
    def verify(self, locator: Locator, content_hash: str) -> bool: ...
    def exists(self, locator: Locator) -> bool: ...
