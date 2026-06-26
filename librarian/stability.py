"""
librarian.stability
────────────────────
"Is this file safe to process, or still being written?"

A scan that runs while a file is mid-copy (a download in progress, an unfinished
`cp`) must never register it — a row makes it claimable, and a backup pass would
ship garbage. The classic rsync/Syncthing guard: stat twice with a small gap; if
size or mtime moved, it's not done. Plus name/extension blocklists for known
"still-writing" markers and a quiescent-age fast path.

Vendored from the suite's core.stability (copied, not imported; see
librarian/DESIGN.md §0). Adapted only in framing — Librarian ingests any file
type, not just media, so there is no media-extension gate here; the size floor,
hidden-file skip (which also skips `.tags` sidecars), and incomplete-suffix skip
do the filtering.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

# Suffixes indicating a file is still being written by some tool.
_INCOMPLETE_SUFFIXES = frozenset({".part", ".tmp", ".crdownload", ".partial",
                                  ".ytdl", ".download"})

# Minimum reasonable file size in bytes. Smaller is almost certainly a
# placeholder or an error stub.
MIN_FILE_BYTES = 100
# Probe gap between the two stat() calls.
PROBE_INTERVAL_S = 1.5
# Files untouched for longer than this are assumed stable without a probe.
QUIESCENT_AGE_S = 5.0


def _is_hidden(path: Path) -> bool:
    return path.name.startswith(".")


def is_stable(path: Path) -> bool:
    """True iff `path` is a file safe to register. Cheapest checks first:
    1. a non-hidden, non-incomplete-suffix file; 2. meets the size floor;
    3. EITHER older than QUIESCENT_AGE_S (instant pass) OR a stat-sleep-stat
    probe shows size/mtime unchanged."""
    if _is_hidden(path):
        return False
    if any(s.lower() in _INCOMPLETE_SUFFIXES for s in path.suffixes):
        return False

    try:
        s1 = path.stat()
    except OSError as e:
        log.debug("stability: stat failed for %s: %s", path, e)
        return False
    if not path.is_file():
        return False
    if s1.st_size < MIN_FILE_BYTES:
        log.debug("stability: %s too small (%d bytes)", path.name, s1.st_size)
        return False

    if time.time() - s1.st_mtime > QUIESCENT_AGE_S:
        return True

    time.sleep(PROBE_INTERVAL_S)
    try:
        s2 = path.stat()
    except OSError as e:
        log.debug("stability: second stat failed for %s: %s", path, e)
        return False
    if s2.st_size != s1.st_size or s2.st_mtime != s1.st_mtime:
        log.info("stability: %s still being written; skipping this pass",
                 path.name)
        return False
    return True
