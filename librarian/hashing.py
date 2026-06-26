"""
librarian.hashing
─────────────────
One definition of "the content hash of a file," shared by ingest (which stamps
`items.content_hash` on every row) and dedup (which collapses byte-identical
copies). Two callers, one algorithm — so "are these the same bytes?" can never
be answered two different ways.

Vendored from the suite's core.hashing (copied, not imported; see
librarian/DESIGN.md §0). Unchanged — the primitive is domain-agnostic.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Partial-hash window. 64 KB: big enough that media/document headers have
# diverged, small enough that a large prefilter stays cheap.
PARTIAL_HASH_BYTES = 64 * 1024
# Streaming chunk for the full hash.
FULL_HASH_CHUNK = 1024 * 1024


def partial_hash(path: Path, n_bytes: int = PARTIAL_HASH_BYTES) -> str | None:
    """SHA-256 of the first `n_bytes`. None on read failure (caller drops it
    from the candidate set rather than crashing a whole scan)."""
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            h.update(f.read(n_bytes))
    except OSError as e:
        log.warning("hashing: partial-read failed on %s: %s", path, e)
        return None
    return h.hexdigest()


def full_hash(path: Path) -> str | None:
    """Streaming full SHA-256. None on read failure."""
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            while chunk := f.read(FULL_HASH_CHUNK):
                h.update(chunk)
    except OSError as e:
        log.warning("hashing: full-read failed on %s: %s", path, e)
        return None
    return h.hexdigest()
