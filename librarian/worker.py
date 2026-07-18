"""
librarian.worker
────────────────
Orchestrates one maintenance cycle: (optionally) HEAL stale backup claims, back
up everything PENDING, THEN (optionally) offload to reclaim disk. Order matters
— heal first so an item whose durable copy went bad is re-shipped in the SAME
cycle, and back up before you delete.

Two entry points, both plain functions (trivially testable, safe by hand):

  • `run_once`   — the original heal → backup → offload core, unchanged.
  • `full_cycle` — the FACADE over every pass Librarian has, in dependency
    order, each stage fail-soft so one bad stage never starves the rest:

        heal → scan → enrich → dedup → backup → offload

    WHY THIS ORDER (each arrow is a real data dependency):
      heal first     — a claim dropped now re-arms the item, and the SAME
                       cycle's backup pass re-ships it.
      scan next      — discovery writes the PENDING rows backup will drain.
      enrich BEFORE  — book captions are composed at send from items.caption;
        backup         enriching first means the very first upload already
                       carries the ISBN caption (not the generic fallback).
      dedup BEFORE   — collapse redundant local copies so backup never even
        backup         considers shipping a duplicate row's bytes.
      offload LAST   — only after fan-out + live verify can a delete be safe.

A long-running daemon (scheduling, heartbeat) wraps these; that wrapper lands
with the service phase. Independent of the suite (no `import core`); see
DESIGN §0.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from . import roots
from .backup import BackupReport, backup_pass, DEFAULT_MAX_RETRIES
from .backends.registry import Registry
from .dedup import DedupReport, dedup_pass
from .deletion import DeletionGuard
from .enrich import EnrichReport, enrich_pass
from .heal import HealReport, heal_pass
from .offload import OffloadReport, offload_pass
from .roots import ScanReport
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


@dataclass
class CycleReport:
    """Everything one `full_cycle` did, stage by stage. A stage that didn't run
    (disabled) is None/[]; a stage that CRASHED is also None/[] with the error
    recorded in `errors` — the cycle itself never raises."""
    heal:    HealReport | None      = None
    scans:   list[ScanReport]       = field(default_factory=list)
    enrich:  EnrichReport | None    = None
    dedup:   list[DedupReport]      = field(default_factory=list)
    backup:  BackupReport | None    = None
    offload: OffloadReport | None   = None
    errors:  list[str]              = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def __str__(self) -> str:
        parts = []
        if self.heal:    parts.append(str(self.heal))
        for s in self.scans:  parts.append(str(s))
        if self.enrich:  parts.append(str(self.enrich))
        for d in self.dedup:  parts.append(str(d))
        if self.backup:  parts.append(str(self.backup))
        if self.offload: parts.append(str(self.offload))
        if self.errors:  parts.append(f"errors={self.errors}")
        return "cycle: " + (" | ".join(parts) or "nothing ran")


def _stage(report: CycleReport, name: str, fn):
    """Run one stage fail-soft: its result is returned, its crash is recorded.
    One misbehaving stage must never starve the stages after it (in particular,
    a scan/enrich hiccup must not stop the backup pass from shipping)."""
    try:
        return fn()
    except Exception as e:                       # defensive by design
        log.exception("worker: %s stage failed", name)
        report.errors.append(f"{name}: {e}")
        return None


def full_cycle(
    store: ItemStore,
    registry: Registry,
    policy: RoutingPolicy,
    guard: DeletionGuard,
    *,
    heal: bool = True,
    scan: bool = True,
    enrich: bool = True,
    dedup: bool = False,
    offload: bool = False,
    icloud_policy: str = "report_only",
    enrich_online: bool = True,
    enrich_ocr: bool = True,
    max_retries: int = DEFAULT_MAX_RETRIES,
    older_than_days: float = 0.0,
    target_free_bytes: int | None = None,
    free_path: "str | None" = None,
    dry_run: bool = False,
) -> CycleReport:
    """One FULL maintenance cycle over every registered root — the facade a
    daemon (or a cron line) calls. Stages run in dependency order (see module
    docstring), each individually fail-soft; the returned CycleReport carries a
    per-stage report plus any stage errors. Defaults are the safe automation
    set: heal + scan + enrich + backup on, dedup/offload (the deleting stages)
    opt-in, iCloud report_only."""
    report = CycleReport()

    if heal:
        report.heal = _stage(report, "heal", lambda: heal_pass(store, registry))

    if scan:
        for r in store.list_roots():
            res = _stage(report, f"scan[{r['name']}]",
                         lambda n=r["name"]: roots.scan(
                             store, n, icloud_policy=icloud_policy))
            if res is not None:
                report.scans.append(res)

    if enrich:
        report.enrich = _stage(report, "enrich",
                               lambda: enrich_pass(store, online=enrich_online,
                                                   ocr=enrich_ocr))

    if dedup:
        res = _stage(report, "dedup",
                     lambda: dedup_pass(store, guard, dry_run=dry_run))
        if res is not None:
            report.dedup = res

    report.backup = _stage(report, "backup",
                           lambda: backup_pass(store, registry, policy,
                                               max_retries=max_retries))

    if offload:
        report.offload = _stage(
            report, "offload",
            lambda: offload_pass(store, registry, guard,
                                 older_than_days=older_than_days,
                                 target_free_bytes=target_free_bytes,
                                 free_path=free_path, dry_run=dry_run))

    log.info("%s", report)
    return report
