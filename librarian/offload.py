"""
librarian.offload
─────────────────
Hierarchical storage management: reclaim local disk for files that are safely
backed up, leaving NO placeholder — the items row + locations IS the record, and
the Telegram bot serves retrieval on demand.

THE INTEGRITY GATE (the whole point of this module):
  A local file is unlinked ONLY after a DURABLE, hash-verifying backend (Local /
  rclone — never Telegram, whose verify is presence-only) confirms the copy is
  present AND byte-intact RIGHT NOW. We re-verify immediately before the unlink
  and never trust a stale `locations.verified_at`. A dead Telegram account can
  therefore never cost a file, and bit-rot in a cloud copy blocks the delete.

Mirrors the suite's ship-and-delete discipline in reverse: "stub only once the
durable copy is confirmed present", routed through the one DeletionGuard.

CRASH-SAFE / IDEMPOTENT: if the file is already gone but a durable copy verifies,
the row simply converges to OFFLOADED. Re-running the pass is always safe.

Independent of the suite (no `import core`); see DESIGN §0.
"""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .backends.base import BackendError, Locator
from .backends.registry import Registry
from .deletion import DeletionGuard
from .models import Status
from .store import ItemStore, now_iso

log = logging.getLogger(__name__)


class OffloadOutcome(str, Enum):
    OFFLOADED     = "offloaded"        # file unlinked, row → OFFLOADED
    ALREADY_GONE  = "already_gone"     # file absent but durable-verified → OFFLOADED
    WOULD_OFFLOAD = "would_offload"    # dry-run: gate passed, not deleted
    UNVERIFIED    = "unverified"       # no durable backend verifies → kept (safe)
    NOT_BACKED_UP = "not_backed_up"    # wrong status → skipped
    REFUSED       = "refused"          # DeletionGuard refused (protected) → kept


def _durable_verified(store: ItemStore, registry: Registry, item):
    """Return a (backend, locator) whose DURABLE backend verifies the item's
    bytes present+intact RIGHT NOW, or None. Re-verifies live — never trusts a
    stored verified_at."""
    if not item.content_hash:
        return None
    for loc in store.locations_for(item.id):
        if not registry.has(loc.backend) or not registry.is_durable(loc.backend):
            continue
        backend = registry.get(loc.backend)
        locator = Locator(loc.backend, loc.locator)
        try:
            if backend.verify(locator, item.content_hash):
                return backend, locator
        except BackendError as e:
            log.warning("offload: verify error id=%d on %s: %s",
                        item.id, loc.backend, e)
    return None


def offload_item(store: ItemStore, registry: Registry, guard: DeletionGuard,
                 item, *, dry_run: bool = False) -> OffloadOutcome:
    """Offload one item, enforcing the durable-verified gate."""
    if item.status != Status.BACKED_UP.value:
        return OffloadOutcome.NOT_BACKED_UP

    verified = _durable_verified(store, registry, item)
    if verified is None:
        return OffloadOutcome.UNVERIFIED            # no durable copy → NEVER delete

    _backend, locator = verified
    # Refresh verified_at now that we've confirmed it live.
    if not dry_run:
        store.add_location(item.id, locator.backend, locator.ref,
                           verified_at=now_iso())

    p = Path(item.path)
    if not p.exists():                              # crash-safe convergence
        if not dry_run:
            store.set_status(item.id, Status.OFFLOADED)
        return OffloadOutcome.ALREADY_GONE

    if dry_run:
        return OffloadOutcome.WOULD_OFFLOAD

    reason = f"offload (durable copy on {locator.backend} verified)"
    if guard.delete(p, reason=reason):
        store.set_status(item.id, Status.OFFLOADED)
        return OffloadOutcome.OFFLOADED
    return OffloadOutcome.REFUSED                   # guard said no → leave intact


@dataclass
class OffloadReport:
    offloaded:     int = 0
    already_gone:  int = 0
    would_offload: int = 0
    unverified:    int = 0
    not_backed_up: int = 0
    refused:       int = 0
    bytes_freed:   int = 0

    def record(self, outcome: OffloadOutcome, item) -> None:
        setattr(self, outcome.value, getattr(self, outcome.value) + 1)
        if outcome in (OffloadOutcome.OFFLOADED, OffloadOutcome.WOULD_OFFLOAD):
            self.bytes_freed += item.size_bytes or 0

    def __str__(self) -> str:
        mb = self.bytes_freed / (1024 * 1024)
        return (f"offload: offloaded={self.offloaded} gone={self.already_gone} "
                f"unverified={self.unverified} refused={self.refused} "
                f"freed={mb:.1f}MB")


def disk_free(path: str | Path) -> int:
    """Free bytes on the filesystem holding `path`."""
    return shutil.disk_usage(Path(path)).free


def offload_pass(store: ItemStore, registry: Registry, guard: DeletionGuard, *,
                 older_than_days: float = 0.0,
                 target_free_bytes: int | None = None,
                 free_path: str | Path | None = None,
                 dry_run: bool = False) -> OffloadReport:
    """Offload BACKED_UP items, oldest on-disk first.

    Selection: files whose on-disk age ≥ `older_than_days`. When
    `target_free_bytes` + `free_path` are given, offload oldest-first and STOP
    once the filesystem has at least that much free (disk-pressure mode)."""
    now = time.time()
    candidates: list[tuple[object, float]] = []
    for item in store.items_by_status(Status.BACKED_UP):
        try:
            age_days = (now - Path(item.path).stat().st_mtime) / 86400.0
        except OSError:
            age_days = float("inf")                 # file gone → still let it converge
        if age_days >= older_than_days:
            candidates.append((item, age_days))
    candidates.sort(key=lambda t: t[1], reverse=True)   # oldest first

    report = OffloadReport()
    for item, _age in candidates:
        if (target_free_bytes is not None and free_path is not None
                and disk_free(free_path) >= target_free_bytes):
            break                                   # enough space reclaimed
        outcome = offload_item(store, registry, guard, item, dry_run=dry_run)
        report.record(outcome, item)
    log.info("%s", report)
    return report
