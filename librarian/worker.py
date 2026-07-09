"""
librarian.worker
────────────────
Orchestrates one maintenance cycle: (optionally) HEAL stale backup claims, back
up everything PENDING, THEN (optionally) offload to reclaim disk. Order matters
— heal first so an item whose durable copy went bad is re-shipped in the SAME
cycle, and back up before you delete.

This is the cycle body. A long-running daemon (scheduling, heartbeat) wraps
`run_once`; that wrapper lands with the bot/service in a later phase. Keeping the
cycle a plain function makes it trivially testable and safe to invoke by hand.

Independent of the suite (no `import core`); see DESIGN §0.
"""

from __future__ import annotations

import logging

from .backup import BackupReport, backup_pass, DEFAULT_MAX_RETRIES
from .backends.registry import Registry
from .deletion import DeletionGuard
from .heal import HealReport, heal_pass
from .offload import OffloadReport, offload_pass
from .routing import RoutingPolicy
from .store import ItemStore

log = logging.getLogger(__name__)


def run_once(
    store: ItemStore,
    registry: Registry,
    policy: RoutingPolicy,
    guard: DeletionGuard,
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    heal: bool = False,
    offload: bool = False,
    older_than_days: float = 0.0,
    target_free_bytes: int | None = None,
    free_path: "str | None" = None,
    dry_run: bool = False,
) -> "tuple[HealReport | None, BackupReport, OffloadReport | None]":
    """One cycle: heal_pass iff `heal` is set, then backup_pass, then
    offload_pass iff `offload` is set. Returns (heal report-or-None,
    backup report, offload report-or-None)."""
    h = heal_pass(store, registry) if heal else None
    b = backup_pass(store, registry, policy, max_retries=max_retries)
    o = None
    if offload:
        o = offload_pass(store, registry, guard,
                         older_than_days=older_than_days,
                         target_free_bytes=target_free_bytes,
                         free_path=free_path, dry_run=dry_run)
    return h, b, o
