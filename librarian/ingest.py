"""
librarian.ingest
────────────────
The ONE primitive every discovered file funnels through to become a tracked,
backup-able item row.

TEMPLATE METHOD — register_file runs a fixed skeleton:

    stabilize → hash → dedup-collapse → insert

  1. stabilize FIRST. A half-written file must never get a row — a row makes it
     a backup candidate, and a backup pass would ship garbage.
  2. hash before insert so EVERY row carries content_hash. The whole dedup /
     verify story rests on this stamp being universal.
  3. dedup-collapse BEFORE inserting a second row. If these exact bytes already
     have a row, keep exactly one physical copy (dedup winner rules) and never
     create a duplicate row.
  4. insert — writing the row IS the enqueue.

Vendored from the suite's core.ingest (copied, not imported; see DESIGN §0).
Adapted: no social-media identity resolver — a managed file's identity is its
path, its title defaults to the filename stem, and upload_date is filled later
by the photo/book captioners (Phase 2/6). UNIQUE(path) is the racing-duplicate
backstop the suite got from UNIQUE(platform, identifier).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from . import stability
from .dedup import _pick_winner
from .hashing import full_hash
from .models import Status
from .store import ItemStore

log = logging.getLogger(__name__)


class IngestOutcome(str, Enum):
    INSERTED      = "inserted"        # new content → new pending row
    REARMED       = "rearmed"         # bytes' only twin had FAILED; row re-armed
    DEDUP_DROPPED = "dedup_dropped"   # bytes already known; incoming file deleted
    DEDUP_ADOPTED = "dedup_adopted"   # incoming won; row re-pointed, old deleted
    ALREADY_KNOWN = "already_known"   # this exact path already has a row
    UNSTABLE      = "unstable"        # still being written; skipped this pass
    HASH_FAILED   = "hash_failed"     # unreadable; skipped


@dataclass(frozen=True)
class IngestResult:
    outcome:      IngestOutcome
    item_id:      int | None = None
    content_hash: str | None = None

    @property
    def inserted(self) -> bool:
        """True when this call made content newly backup-able."""
        return self.outcome in (IngestOutcome.INSERTED, IngestOutcome.REARMED)


def register_file(
    store: ItemStore,
    path:  Path | str,
    *,
    root:        str | None = None,
    caption:     str | None = None,
    group_key:   str | None = None,
    upload_date: str | None = None,
) -> IngestResult:
    """Register one finished file as a pending item. Never raises for an
    expected condition (unstable / unreadable / duplicate) — it reports the
    outcome so a bulk scan keeps going."""
    path = Path(path)

    # 1. stabilize — refuse a file that's still being written.
    if not stability.is_stable(path):
        return IngestResult(IngestOutcome.UNSTABLE)

    # Cheap short-circuit: this exact path is already tracked.
    if store.has_path(str(path)):
        return IngestResult(IngestOutcome.ALREADY_KNOWN)

    # 2. hash — the global dedup key, stamped on every row.
    digest = full_hash(path)
    if digest is None:
        return IngestResult(IngestOutcome.HASH_FAILED)

    # 3. dedup-collapse — if these exact bytes already have a row, keep one copy.
    twin = store.find_by_content_hash(digest)
    if twin is not None:
        return _collapse(store, path, twin, digest)

    # 4. insert — writing the row IS the enqueue.
    try:
        size = path.stat().st_size
    except OSError:
        size = None
    inserted = store.add_item(
        path         = str(path),
        content_hash = digest,
        root         = root,
        size_bytes   = size,
        title        = path.stem,
        caption      = caption,
        upload_date  = upload_date,
        group_key    = group_key,
    )
    if not inserted:
        # Lost a race on UNIQUE(path) between our checks and the insert.
        return IngestResult(IngestOutcome.ALREADY_KNOWN, content_hash=digest)

    return IngestResult(IngestOutcome.INSERTED,
                        item_id=store.id_of(str(path)),
                        content_hash=digest)


def _collapse(store: ItemStore, incoming: Path, twin, digest: str) -> IngestResult:
    """Resolve a byte-identical collision between `incoming` and the tracked
    file behind `twin`. Keep exactly ONE physical copy (dedup winner rules);
    never create a second row. If the twin had permanently FAILED, its bytes
    were never backed up, so re-arm it (failed → pending) — reported REARMED."""
    failed   = (twin.status == Status.FAILED.value)
    existing = Path(twin.path)

    def _finish(adopted: bool) -> IngestResult:
        if failed and store.rearm_failed(twin.id):
            log.info("ingest: re-arm failed twin id=%d from %s "
                     "(bytes never backed up)", twin.id, incoming.name)
            return IngestResult(IngestOutcome.REARMED, item_id=twin.id,
                                content_hash=digest)
        outcome = (IngestOutcome.DEDUP_ADOPTED if adopted
                   else IngestOutcome.DEDUP_DROPPED)
        return IngestResult(outcome, item_id=(twin.id if adopted else None),
                            content_hash=digest)

    # Twin's file vanished → the incoming copy simply takes its place.
    if not existing.exists():
        store.relink_file(twin.id, str(incoming))
        log.info("ingest: dedup adopt (twin file gone) %s → row id=%d",
                 incoming.name, twin.id)
        return _finish(adopted=True)

    winner, _losers = _pick_winner(
        [incoming, existing],
        {incoming: None, existing: twin.discovered_at},
    )

    if winner == existing:
        # Existing tracked copy wins → incoming is the redundant duplicate.
        _unlink(incoming)
        log.info("ingest: dedup drop %s (dup of row id=%d)",
                 incoming.name, twin.id)
        return _finish(adopted=False)

    # Incoming wins → adopt: re-point the row, retire the old copy.
    store.relink_file(twin.id, str(incoming))
    _unlink(existing)
    log.info("ingest: dedup adopt %s → row id=%d (retired %s)",
             incoming.name, twin.id, existing.name)
    return _finish(adopted=True)


def _unlink(path: Path) -> None:
    """Remove a redundant duplicate. A failed unlink is logged, not raised — a
    stranded duplicate is far less bad than a crashed scan."""
    try:
        path.unlink()
    except OSError as e:
        log.warning("ingest: could not remove duplicate %s: %s", path, e)
