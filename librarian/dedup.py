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

The full three-stage size→partial→full funnel (for a standalone "dedupe this
whole folder" pass) is deferred; ingest-time collapse covers the common case.
"""

from __future__ import annotations

from pathlib import Path

# Sorts after every real ISO-8601 timestamp, so a row-less / timestamp-less
# path sinks to last in the winner ranking.
_LAST = "￿"


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
