"""
librarian.icloud
────────────────
iCloud-aware classification so the scanner decides download-vs-skip ON PURPOSE
instead of by accident. Two failure modes this fixes:

  1. An EVICTED file leaves a hidden `.<name>.icloud` placeholder stub. The scan's
     hidden-file filter skipped it silently → the file was NEVER backed up. We
     surface it instead.
  2. A DATALESS file keeps its real name and true `st_size`, but its bytes are
     evicted. Reading it (to hash at ingest) makes macOS transparently DOWNLOAD it
     — blocking, possibly metered, and it re-fills the very disk offload just
     freed. We detect this BEFORE the read and apply a policy.

STATES (`placeholder_state`):
  • MATERIALIZED  — bytes are local; safe to hash/read. Also the state of any
                    ordinary non-iCloud file, so classification is a no-op on a
                    normal filesystem (and on non-macOS).
  • DATALESS      — real name, contents evicted; a read would trigger a download.
  • EVICTED_STUB  — a `.<name>.icloud` placeholder file (the real file is gone).

Every OS probe is INJECTABLE (the `*_fn` params) so the logic is fully testable
off macOS. On macOS with no probe injected: dataless is detected by `st_blocks==0
&& st_size>0` (the reliable signal without pyobjc); download is driven by `brctl`.

Independent of the suite (no `import core`); see DESIGN §0.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from enum import Enum
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

STUB_SUFFIX = ".icloud"


class ICloudState(str, Enum):
    MATERIALIZED = "materialized"
    DATALESS     = "dataless"
    EVICTED_STUB = "evicted_stub"


def is_stub(path: str | Path) -> bool:
    """True for a `.<name>.icloud` eviction placeholder (hidden + `.icloud`)."""
    name = Path(path).name
    return name.startswith(".") and name.endswith(STUB_SUFFIX)


def original_name(stub: str | Path) -> str:
    """The real filename a stub stands in for: `.Foo.pdf.icloud` → `Foo.pdf`."""
    name = Path(stub).name
    if is_stub(name):
        return name[1:-len(STUB_SUFFIX)]
    return name


def original_path(stub: str | Path) -> Path:
    """The path the real file will occupy once the stub is materialized."""
    p = Path(stub)
    return p.parent / original_name(p)


def placeholder_state(
    path: str | Path,
    *,
    stat_fn:   Callable[[Path], os.stat_result] = os.stat,
) -> ICloudState:
    """Classify `path`. A stub is EVICTED_STUB; otherwise a file with allocated
    data blocks is MATERIALIZED and one with none (but a non-zero size) is
    DATALESS. Anything we can't stat is treated as MATERIALIZED (fail-open: better
    to attempt a normal ingest than to wrongly hide a real file)."""
    p = Path(path)
    if is_stub(p):
        return ICloudState.EVICTED_STUB
    try:
        st = stat_fn(p)
    except OSError:
        return ICloudState.MATERIALIZED
    blocks = getattr(st, "st_blocks", None)
    if blocks == 0 and st.st_size > 0:
        return ICloudState.DATALESS
    return ICloudState.MATERIALIZED


def _brctl_download(target: Path, *, timeout: float = 30.0) -> None:
    """Ask macOS to fetch a dataless/evicted file's bytes. Raises on failure."""
    subprocess.run(["brctl", "download", str(target)],
                   check=True, capture_output=True, timeout=timeout)


def materialize(
    path: str | Path,
    *,
    timeout:       float = 300.0,
    poll_interval: float = 1.0,
    runner:        Callable[[Path], None] | None = None,
    state_fn:      Callable[..., ICloudState] | None = None,
    sleep_fn:      Callable[[float], None] = time.sleep,
    clock_fn:      Callable[[], float] = time.monotonic,
) -> bool:
    """Download an evicted file's bytes and wait until they're local. Works for a
    stub (materializes the ORIGINAL path) or a dataless file (in place). Returns
    True once MATERIALIZED, False on timeout or a `brctl` failure. Never raises —
    a failed download must not crash a scan. All I/O is injectable for tests."""
    run   = runner or _brctl_download
    state = state_fn or placeholder_state
    target = original_path(path) if is_stub(path) else Path(path)
    try:
        run(target)
    except Exception as e:                      # brctl missing / non-zero / timeout
        log.warning("icloud: materialize(%s) failed to start: %s", target, e)
        return False
    deadline = clock_fn() + timeout
    while True:
        try:
            if target.exists() and state(target) == ICloudState.MATERIALIZED:
                log.info("icloud: materialized %s", target.name)
                return True
        except OSError:
            pass
        if clock_fn() >= deadline:
            log.warning("icloud: materialize(%s) timed out after %.0fs",
                        target, timeout)
            return False
        sleep_fn(poll_interval)


def is_evicted(state: ICloudState) -> bool:
    """True for a state whose bytes are NOT local (dataless or an evicted stub)."""
    return state in (ICloudState.DATALESS, ICloudState.EVICTED_STUB)
