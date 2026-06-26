"""
librarian.backends
───────────────────
Pluggable storage backends behind one `StorageBackend` Strategy: Telegram (fast
access) and durable clouds via rclone, plus a local content-addressed tier. See
base.py for the contract and the verify-semantics that drive the offload gate.
"""

from __future__ import annotations

from .base import (
    BackendError,
    BackendUnavailable,
    Locator,
    StorageBackend,
)
from .local import LocalBackend
from .registry import Registry

__all__ = [
    "BackendError",
    "BackendUnavailable",
    "Locator",
    "StorageBackend",
    "LocalBackend",
    "Registry",
]
