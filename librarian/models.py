"""
librarian.models
────────────────
The typed representation of a managed file and its backup lifecycle, plus the
backend-location record. One row per physical file (keyed by `path`); a file's
state lives in exactly one row.

LIFECYCLE (status column):

    pending ──backup(all routed backends stored)──▶ backed_up
       ▲                                                │
       │                                  offload(durable verified)
       │                                                ▼
       └──restore(fetch back)──────────────────────  offloaded
       (failed is the retry-exhausted terminal; reset re-arms)

  'pending'    discovered on disk; not yet backed up.
  'backed_up'  every routed backend holds a verified copy; file still on disk.
  'offloaded'  local file reclaimed; copies live only in backends (no placeholder).
  'failed'     backup retries exhausted (terminal until reset).

Design DNA from the suite's core.models (copied, not imported; see DESIGN §0).
Librarian drops the suite's social-media identity (platform/username/identifier)
— a managed file is identified by its path and content hash, nothing else.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import Enum


class Status(str, Enum):
    """str-Enum so values compare/serialize as plain SQLite text."""
    PENDING   = "pending"
    BACKED_UP = "backed_up"
    OFFLOADED = "offloaded"
    FAILED    = "failed"


# Not re-scanned for backup work; left only by an explicit transition.
TERMINAL = frozenset({Status.FAILED})


@dataclass(frozen=True)
class Item:
    """Immutable snapshot of one items-table row. Mirrors librarian.schema
    ITEMS_DDL column names (from_row maps by name, so column order is free)."""
    id:            int
    path:          str            # absolute file path — UNIQUE, the identity
    content_hash:  str | None     # full SHA-256; global dedup key
    root:          str | None     # registered root this file belongs to
    size_bytes:    int | None
    title:         str | None     # defaults to the filename stem
    caption:       str | None     # composed at send (folder tags, EXIF, …)
    upload_date:   str | None     # YYYYMMDD, EXIF/derived (Phase 2)
    group_key:     str | None     # album batch identity (Phase 2)
    status:        str
    discovered_at: str            # when the row was first written
    last_error:    str | None
    attempts:      int

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "Item":
        return cls(**{k: r[k] for k in r.keys()})


@dataclass(frozen=True)
class Location:
    """One stored copy of an item in one backend. (item_id, backend) is unique
    — a backend holds at most one copy of a given item."""
    item_id:     int
    backend:     str              # 'telegram' | 'gdrive' | 'box' | …
    locator:     str              # msg_id, drive file_id, rclone path …
    verified_at: str | None       # last verify() that confirmed the bytes

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "Location":
        return cls(**{k: r[k] for k in r.keys()})
