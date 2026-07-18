"""
librarian.roots
───────────────
A "root" is a human-named folder the user hands to Librarian to manage
(e.g. `Photos` → ~/Pictures, `Books` → ~/Documents/Books). The name is friendly;
the path is where the files live; `destination` (a backup routing target) and
base `tags` are attached for later phases.

This module owns root registration + the scan that turns a root's files into
pending item rows via `ingest.register_file`. Unlike the suite's chat_id folders,
a root name is NOT a routing authority — it's just a label; routing is explicit
config (Phase 3).

Independent of the suite (no `import core`); see DESIGN §0.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from . import captioning, icloud, ingest
from .ingest import IngestOutcome
from .store import ItemStore

log = logging.getLogger(__name__)

# A root NAME is a label, not a path: keep it simple and filesystem/cli-safe.
_NAME_BANNED = set("/\\:")

# How a scan treats iCloud-evicted files (dataless files + `.icloud` stubs):
#   report_only — surface them (counted in cloud_only), never download; DEFAULT.
#   materialize — download the bytes, then ingest normally.
#   skip        — ignore them silently (not ingested, not counted).
ICLOUD_POLICIES = ("report_only", "materialize", "skip")


class RootError(ValueError):
    """A root name/path that can't be registered (with a human reason)."""


def register(store: ItemStore, name: str, folder: Path | str,
             *, destination: str | None = None,
             tags: str | None = None) -> dict:
    """Register `folder` under the label `name`. The folder must exist and the
    path is stored ABSOLUTE so later scans and the tags-root boundary agree.
    Returns the stored root record. Raises RootError on a bad name/path or a
    duplicate name."""
    name = (name or "").strip()
    if not name or any(c in _NAME_BANNED for c in name):
        raise RootError(f"invalid root name {name!r} (no / \\ : and not empty)")
    p = Path(folder).expanduser()
    if not p.is_absolute():
        p = p.resolve()
    if not p.is_dir():
        raise RootError(f"root path is not a directory: {p}")
    if not store.add_root(name, str(p), destination=destination, tags=tags):
        raise RootError(f"root {name!r} already exists")
    log.info("roots: registered %r → %s", name, p)
    return store.get_root(name)


def remove(store: ItemStore, name: str) -> bool:
    """Unregister a root (the files on disk and their item rows are untouched)."""
    return store.remove_root(name) > 0


def list_roots(store: ItemStore) -> list[dict]:
    return store.list_roots()


@dataclass
class ScanReport:
    root:       str
    scanned:    int = 0         # files considered (passed the cheap filters)
    inserted:   int = 0         # new pending rows (incl. re-armed)
    dropped:    int = 0         # byte-duplicates collapsed away
    known:      int = 0         # already tracked
    skipped:    int = 0         # unstable / unreadable this pass
    cloud_only: int = 0         # iCloud-evicted, surfaced but not ingested

    def __str__(self) -> str:
        return (f"[{self.root}] scanned={self.scanned} inserted={self.inserted} "
                f"dropped={self.dropped} known={self.known} skipped={self.skipped} "
                f"cloud_only={self.cloud_only}")


def _resolve_cloud(p: Path, state, policy: str,
                   report: ScanReport) -> Path | None:
    """Apply the iCloud `policy` to an evicted file `p` (state DATALESS or
    EVICTED_STUB). Returns the local path to ingest (materialize succeeded), or
    None when the file is left in the cloud (report_only / skip / failed
    download). Only `materialize` ever reads the bytes."""
    if policy == "skip":
        return None                                 # ignore silently, don't count
    if policy == "materialize":
        if icloud.materialize(p):
            target = (icloud.original_path(p)
                      if state == icloud.ICloudState.EVICTED_STUB else p)
            log.info("roots: materialized cloud file %s", target.name)
            return target
        report.cloud_only += 1                       # tried, failed → still cloud
        return None
    # report_only (default): surface it, never download.
    report.cloud_only += 1
    log.info("roots: cloud-only (not backed up) %s", icloud.original_name(p))
    return None


def scan(store: ItemStore, name: str, *,
         icloud_policy: str = "report_only") -> ScanReport:
    """Walk a registered root and ingest every stable file under it. Idempotent:
    a second scan finds everything already known. Hidden files (incl. `.tags`
    sidecars) and in-flight downloads are skipped by ingest's stability gate.

    iCloud: evicted files (dataless files + `.icloud` stubs) are classified
    BEFORE the read that would otherwise trigger an accidental download, and
    handled per `icloud_policy` (see ICLOUD_POLICIES). Default `report_only`
    surfaces them in `cloud_only` and changes nothing on disk."""
    if icloud_policy not in ICLOUD_POLICIES:
        raise RootError(f"invalid icloud_policy {icloud_policy!r} "
                        f"(one of {ICLOUD_POLICIES})")
    root = store.get_root(name)
    if root is None:
        raise RootError(f"no such root: {name!r}")
    base = Path(root["path"])
    report = ScanReport(root=name)

    for p in sorted(base.rglob("*")):
        stub = icloud.is_stub(p)
        if not stub:
            try:
                if not p.is_file() or p.name.startswith("."):
                    continue
            except OSError:
                continue
        # Classify iCloud state before any content read (stubs included).
        state = icloud.placeholder_state(p)
        if icloud.is_evicted(state):
            target = _resolve_cloud(p, state, icloud_policy, report)
            if target is None:
                continue                             # left in the cloud this pass
            p = target                               # materialized → ingest for real
        report.scanned += 1
        # Stamp a stable capture date at ingest: EXIF for photos, mtime else.
        ts = captioning.timestamp(p)
        res = ingest.register_file(store, p, root=name,
                                   upload_date=ts[0] if ts else None)
        if res.outcome in (IngestOutcome.INSERTED, IngestOutcome.REARMED,
                           IngestOutcome.DEDUP_ADOPTED):
            report.inserted += 1
        elif res.outcome == IngestOutcome.DEDUP_DROPPED:
            report.dropped += 1
        elif res.outcome == IngestOutcome.ALREADY_KNOWN:
            report.known += 1
        else:  # UNSTABLE / HASH_FAILED
            report.skipped += 1
    log.info("roots: %s", report)
    return report
