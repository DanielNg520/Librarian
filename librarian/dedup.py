"""
librarian.dedup
───────────────
Winner selection between byte-identical copies. When ingest finds that an
incoming file shares its content hash with a tracked file, exactly ONE physical
copy is kept; this module decides which.

Vendored from the suite's core.dedup `_pick_winner` (copied, not imported; see
DESIGN §0), SIMPLIFIED for Librarian's domain: there is no canonical
social-media filename convention and no per-file JSON sidecars here, so the
score reduces to "the already-tracked copy wins; ties break by earliest
discovery then absolute path." The path tiebreak is load-bearing — it makes the
survivor deterministic across re-scans so we never re-create a duplicate we just
removed.

Phase 8 adds the standalone `dedup_root` pass on top: a reconciliation over what
is physically on disk under a root, collapsing redundant LOCAL copies that
ingest-time collapse didn't catch (twins ingested under different rows, or
untracked files dropped in beside a tracked one). It is OPTIMIZED for Librarian:
every tracked row already carries a full `content_hash`, so grouping is
DB-driven and disk hashing runs ONLY on untracked stragglers — the suite's
pure-filesystem size→partial→full funnel is unnecessary work once the DB knows
the hashes. A cheap size prefilter still isolates candidates before any straggler
is hashed.

dedup_root is DISTINCT from offload: it removes a redundant *duplicate* local
copy while an identical one remains on disk — it never removes the last local
copy of some bytes, so it needs no durable-backup gate (that gate is offload's).
All unlinks still route through the one DeletionGuard, so a protected root or a
global pause shields them.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .deletion import DeletionGuard
from .hashing import full_hash
from .store import ItemStore

log = logging.getLogger(__name__)

# Sorts after every real ISO-8601 timestamp, so a row-less / timestamp-less
# path sinks to last in the winner ranking.
_LAST = "￿"

# Mirror stability's cheap filters — dedup runs post-ingest, no stat-probe needed.
_INCOMPLETE_SUFFIXES = frozenset({".part", ".tmp", ".crdownload", ".partial",
                                  ".ytdl", ".download"})


def _pick_winner(
    paths:   list[Path],
    db_meta: dict[Path, str | None],
) -> tuple[Path, list[Path]]:
    """Return (winner, losers). `db_meta[p]` is p's row discovery timestamp, or
    None if p has no row. Sort ascending by a key whose "better" fields are
    negated: has-a-row first, then earliest discovery, then absolute path."""
    def sort_key(p: Path) -> tuple:
        ts = db_meta.get(p)
        has_row = ts is not None
        return (0 if has_row else 1, ts if ts is not None else _LAST, str(p))

    ranked = sorted(paths, key=sort_key)
    return ranked[0], ranked[1:]


# ── standalone dedup pass (Phase 8) ───────────────────────────────────────────

@dataclass
class DedupReport:
    """Per-root result. In dry-run mode every counter is a PLANNED action — disk
    and DB are untouched, the report just predicts impact."""
    root:           str
    scanned:        int = 0    # on-disk files considered (passed cheap filters)
    dup_groups:     int = 0    # confirmed byte-identical groups (size ≥ 2)
    kept:           int = 0    # 1 survivor per group
    removed:        int = 0    # redundant copies unlinked (or planned)
    rows_removed:   int = 0    # loser rows deleted (winner's row+backups suffice)
    refused:        int = 0    # DeletionGuard shielded the copy → kept
    bytes_freed:    int = 0
    dry_run:        bool = True
    errors:         list[str] = field(default_factory=list)

    def __str__(self) -> str:
        mode = "DRY RUN" if self.dry_run else "LIVE"
        verb = "would_remove" if self.dry_run else "removed"
        mb = self.bytes_freed / (1024 * 1024)
        return (f"[{self.root}] dedup [{mode}]: scanned={self.scanned} "
                f"groups={self.dup_groups} {verb}={self.removed} ({mb:.1f} MB) "
                f"rows_removed={self.rows_removed} refused={self.refused} "
                f"errors={len(self.errors)}")


def _iter_files(base: Path):
    """Cheap recursive walk under `base`: skip hidden files (incl. `.tags`) and
    known still-writing suffixes. No stat-probe — dedup runs after ingest."""
    for p in base.rglob("*"):
        try:
            if not p.is_file() or p.name.startswith("."):
                continue
        except OSError:
            continue
        if any(s.lower() in _INCOMPLETE_SUFFIXES for s in p.suffixes):
            continue
        yield p


def dedup_root(store: ItemStore, guard: DeletionGuard, root: str,
               *, dry_run: bool = True) -> DedupReport:
    """Collapse byte-identical LOCAL copies under registered root `root`, keeping
    exactly one physical copy per content hash. DB-driven: tracked rows contribute
    their stored `content_hash` for free; only untracked on-disk files are hashed,
    and only when a same-SIZE candidate exists. Survivor is deterministic (tracked
    first, then earliest discovery, then path) so a re-run never re-creates a copy
    it just removed. Every unlink routes through `guard`.

    INVARIANT: a tracked row always outranks an untracked file in the winner
    order, so a file with a row can never LOSE to a row-less one — a backed-up row
    is never orphaned by dedup. A loser is therefore only ever "a tracked twin
    whose row we delete (the winner's identical row + backups suffice)" or "an
    untracked stray we unlink"; there is no adopt-the-orphan case (unlike the
    suite, whose loose files had no such row guarantee)."""
    root_row = store.get_root(root)
    report = DedupReport(root=root, dry_run=dry_run)
    if not root_row or not root_row.get("path"):
        report.errors.append(f"no such root: {root!r}")
        return report
    base = Path(root_row["path"])
    if not base.is_dir():
        report.errors.append(f"root path is not a directory: {base}")
        return report

    # Row index by absolute path: gives us the free content_hash + discovery ts.
    rows = {Path(it.path): it for it in store.items_for_root(root)}

    # 1. cheap SIZE prefilter over everything on disk.
    by_size: dict[int, list[Path]] = defaultdict(list)
    for p in _iter_files(base):
        report.scanned += 1
        try:
            by_size[p.stat().st_size].append(p)
        except OSError as e:
            log.warning("dedup: stat failed on %s: %s", p, e)

    # 2. within each size-collision, get a full hash per file: reuse the DB hash
    #    for a tracked row, hash the file for an untracked straggler.
    by_hash: dict[str, list[Path]] = defaultdict(list)
    for size, paths in by_size.items():
        if len(paths) < 2:
            continue
        for p in paths:
            it = rows.get(p)
            digest = it.content_hash if (it and it.content_hash) else full_hash(p)
            if digest:
                by_hash[digest].append(p)

    # 3. reconcile each confirmed duplicate group.
    for digest, paths in by_hash.items():
        if len(paths) < 2:
            continue
        report.dup_groups += 1
        report.kept += 1
        meta = {p: (rows[p].discovered_at if p in rows else None) for p in paths}
        winner, losers = _pick_winner(paths, meta)
        log.info("dedup [%s] keep %s (%d dup(s), %s)",
                 root, winner.name, len(losers), digest[:12])

        for loser in losers:
            try:
                size = loser.stat().st_size
            except OSError:
                size = 0
            loser_row = rows.get(loser)

            if dry_run:
                report.removed += 1
                report.bytes_freed += size
                if loser_row is not None:      # its row would be deleted
                    report.rows_removed += 1
                continue

            if not guard.delete(loser, reason=f"dedup dup of {winner.name}"):
                report.refused += 1
                continue                       # shielded → leave file AND row alone

            report.removed += 1
            report.bytes_freed += size

            # DB reconciliation — the file is gone; keep state coherent. A tracked
            # loser's row is redundant (the winner is tracked too, by the invariant
            # above), so drop it; an untracked stray has nothing to reconcile.
            if loser_row is not None:
                store.delete(loser_row.id)     # winner's row + backups suffice
                report.rows_removed += 1

    log.info("%s", report)
    return report


def dedup_pass(store: ItemStore, guard: DeletionGuard,
               *, dry_run: bool = True) -> list[DedupReport]:
    """Run `dedup_root` over every registered root. Serial (single-flight)."""
    return [dedup_root(store, guard, r["name"], dry_run=dry_run)
            for r in store.list_roots()]
