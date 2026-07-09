"""
librarian.heal
──────────────
The self-healing pass: re-verify, LIVE, every durable claim the DB holds for
BACKED_UP / OFFLOADED items, and repair what it can automatically.

WHY: `locations` rows are claims made in the past. Disks die, cloud objects
bit-rot, a human deletes a folder on the NAS. Left alone, a stale claim is a
silent lie — offload would still refuse to delete (it re-verifies live), but a
BACKED_UP item could sit for months believing it has a durable copy it no
longer has. This pass closes that loop:

  - A durable claim that verifies live gets its `verified_at` refreshed.
  - A durable claim that POSITIVELY fails verify (backend reachable, bytes
    absent or corrupt) is DROPPED — the DB stops lying.
  - A BACKED_UP item that lost a claim while its file is still on disk is
    RE-ARMED to PENDING: the very next backup pass re-ships only the missing
    backends (backup_item skips locations already recorded). Detection → repair
    is fully automatic, no human in the loop.
  - An OFFLOADED item (local bytes already reclaimed) left with NO verifying
    durable claim is reported LOST — loudly. Bytes can't be conjured back; this
    is the one outcome that needs a human, so it is never silent.

TRANSIENT-SAFE: a verify that ERRORS (network down, backend misbehaving) never
drops a claim — self-healing must not amplify an outage into data-record loss.
Only a clean, reachable "no/corrupt" does. Presence-only backends (Telegram)
are never judged here: their verify is too weak to justify dropping a claim.

Idempotent and safe to run on any cadence; `worker.run_once(heal=True)` runs it
before the backup pass so a re-armed item is re-shipped in the same cycle.

Independent of the suite (no `import core`); see DESIGN §0.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .backends.base import BackendError, Locator
from .backends.registry import Registry
from .models import Status
from .store import ItemStore, now_iso

log = logging.getLogger(__name__)


class HealOutcome(str, Enum):
    INTACT   = "intact"     # every judged durable claim verified live
    REARMED  = "rearmed"    # stale claim dropped; file on disk → PENDING re-backup
    DEGRADED = "degraded"   # stale claim dropped; another durable copy still verifies
    LOST     = "lost"       # no durable copy verifies and local bytes are gone
    SKIPPED  = "skipped"    # nothing judgeable (no hash / no durable claims)


def heal_item(store: ItemStore, registry: Registry, item) -> HealOutcome:
    """Re-verify one item's durable claims and repair its record. See module
    docstring for the outcome ladder."""
    if not item.content_hash:
        return HealOutcome.SKIPPED

    judged = dropped = verified = 0
    for loc in store.locations_for(item.id):
        if not registry.has(loc.backend) or not registry.is_durable(loc.backend):
            continue                    # unregistered/presence-only: never judged
        judged += 1
        try:
            ok = registry.get(loc.backend).verify(
                Locator(loc.backend, loc.locator), item.content_hash)
        except BackendError as e:       # transient — NEVER drop a claim on an error
            log.warning("heal: verify errored id=%d on %s (claim kept): %s",
                        item.id, loc.backend, e)
            continue
        if ok:
            verified += 1
            store.add_location(item.id, loc.backend, loc.locator,
                               verified_at=now_iso())
        else:
            dropped += 1
            store.remove_location(item.id, loc.backend)
            log.warning("heal: dropped stale claim id=%d on %s "
                        "(bytes absent or corrupt)", item.id, loc.backend)

    if judged == 0:
        return HealOutcome.SKIPPED
    if dropped == 0:
        return HealOutcome.INTACT

    # Something was lost. If the local file is still here, re-arm: the next
    # backup pass re-ships only the missing backends.
    if Path(item.path).exists():
        if item.status == Status.BACKED_UP.value:
            store.set_status(item.id, Status.PENDING)
        log.info("heal: re-armed id=%d for re-backup (%d claim(s) dropped)",
                 item.id, dropped)
        return HealOutcome.REARMED

    # Local bytes are gone (offloaded, or worse). Another verifying durable
    # copy keeps us safe; none at all is a loud, human-needed loss.
    if verified > 0:
        return HealOutcome.DEGRADED
    log.error("heal: id=%d (%s) has NO verifying durable copy and no local "
              "file — data at risk, manual recovery needed", item.id, item.path)
    return HealOutcome.LOST


@dataclass
class HealReport:
    intact:   int = 0
    rearmed:  int = 0
    degraded: int = 0
    lost:     int = 0
    skipped:  int = 0
    lost_ids: list[int] = field(default_factory=list)

    def record(self, outcome: HealOutcome, item) -> None:
        setattr(self, outcome.value, getattr(self, outcome.value) + 1)
        if outcome == HealOutcome.LOST:
            self.lost_ids.append(item.id)

    def __str__(self) -> str:
        return (f"heal: intact={self.intact} rearmed={self.rearmed} "
                f"degraded={self.degraded} lost={self.lost} "
                f"skipped={self.skipped}")


def heal_pass(store: ItemStore, registry: Registry, *,
              limit: int | None = None) -> HealReport:
    """Heal every BACKED_UP and OFFLOADED item (the two states that rest on a
    durable-copy claim). PENDING items have no claims worth judging; FAILED ones
    are re-entered via ingest's re-arm."""
    report = HealReport()
    n = 0
    for status in (Status.BACKED_UP, Status.OFFLOADED):
        for item in store.items_by_status(status):
            if limit is not None and n >= limit:
                break
            n += 1
            report.record(heal_item(store, registry, item), item)
    log.info("%s", report)
    return report
