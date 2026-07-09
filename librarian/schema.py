"""
librarian.schema
────────────────
Librarian's own DB — `librarian.db` (SQLite/WAL). Owns the DDL and the
connection factory. Fully separate from the suite's suite.db; the two never
share a file.

Tables
──────
  items      ONE row per managed file, cradle to grave. Identity: path UNIQUE.
             See librarian.models for the status lifecycle.
  locations  ONE row per stored copy of an item, per backend (item_id, backend).
             Generalizes "where do these bytes live, and are they verified?".
  roots      registered human-named folders → backup destination + base tags.
  metadata   generic key/value.

Vendored from the suite's core.schema (copied, not imported; see DESIGN §0):
the versioned forward-only migration runner, WAL pragmas, and locked-retry
helper are kept verbatim in spirit — they are the load-bearing correctness
machinery — while the DDL is Librarian's own.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .paths import db_path

# ── Immutable base schema (PRAGMA user_version 0). Never edited again; later
#    changes are appended to _MIGRATIONS and bump user_version. ───────────────
ITEMS_DDL = """
CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    path          TEXT    NOT NULL UNIQUE,             -- absolute file path = identity
    content_hash  TEXT,                                -- full SHA-256; global dedup key
    root          TEXT,                                -- registered root name
    size_bytes    INTEGER,
    title         TEXT,                                -- defaults to filename stem
    caption       TEXT,                                -- composed at send time
    upload_date   TEXT,                                -- YYYYMMDD, EXIF/derived
    group_key     TEXT,                                -- album batch identity
    status        TEXT    NOT NULL DEFAULT 'pending',  -- see librarian.models.Status
    discovered_at TEXT    NOT NULL,
    last_error    TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0
);

-- Backup-work scan: cheapest path to the next pending row.
CREATE INDEX IF NOT EXISTS idx_items_pending
    ON items (discovered_at) WHERE status='pending';

-- "Have these exact bytes appeared before?" lookup for ingest dedup.
CREATE INDEX IF NOT EXISTS idx_items_hash
    ON items (content_hash) WHERE content_hash IS NOT NULL;

CREATE TABLE IF NOT EXISTS locations (
    item_id     INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    backend     TEXT    NOT NULL,
    locator     TEXT    NOT NULL,
    verified_at TEXT,
    PRIMARY KEY (item_id, backend)
);

CREATE TABLE IF NOT EXISTS roots (
    name        TEXT PRIMARY KEY,        -- human name, e.g. 'Photos'
    path        TEXT NOT NULL UNIQUE,    -- absolute folder path
    destination TEXT,                    -- backup routing target (Phase 3)
    tags        TEXT,                    -- base hashtags (space-separated)
    added_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# ── v1: full-text search index (Phase 5, the retrieval `find`) ────────────────
# An external-content FTS5 table shadowing items over the human-searchable
# columns; `items` stays the single source of truth (content='items'), the FTS
# table holds only the inverted index. Three triggers keep it in lock-step with
# every INSERT/UPDATE/DELETE on items, and 'rebuild' back-fills any rows that
# predate the migration. Tokenized unicode61 + remove_diacritics so an accented
# caption matches an ASCII query. `path` is indexed too, so a folder name finds
# the file even before captions are composed.
FTS_DDL = [
    """CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
         title, caption, path, upload_date,
         content='items', content_rowid='id',
         tokenize="unicode61 remove_diacritics 2"
       )""",
    # Back-fill from any rows that already exist (no-op on a fresh DB).
    "INSERT INTO items_fts(items_fts) VALUES('rebuild')",
    """CREATE TRIGGER IF NOT EXISTS items_fts_ai AFTER INSERT ON items BEGIN
         INSERT INTO items_fts(rowid, title, caption, path, upload_date)
         VALUES (new.id, new.title, new.caption, new.path, new.upload_date);
       END""",
    """CREATE TRIGGER IF NOT EXISTS items_fts_ad AFTER DELETE ON items BEGIN
         INSERT INTO items_fts(items_fts, rowid, title, caption, path, upload_date)
         VALUES ('delete', old.id, old.title, old.caption, old.path, old.upload_date);
       END""",
    """CREATE TRIGGER IF NOT EXISTS items_fts_au AFTER UPDATE ON items BEGIN
         INSERT INTO items_fts(items_fts, rowid, title, caption, path, upload_date)
         VALUES ('delete', old.id, old.title, old.caption, old.path, old.upload_date);
         INSERT INTO items_fts(rowid, title, caption, path, upload_date)
         VALUES (new.id, new.title, new.caption, new.path, new.upload_date);
       END""",
]

# Forward-only, ordered migrations applied once each and recorded in
# PRAGMA user_version. Future additive changes append `(target_version,
# [statements])` here and bump SCHEMA_VERSION; NEVER edit ITEMS_DDL (CREATE IF
# NOT EXISTS no-ops on existing DBs, so an edited DDL would reach fresh installs
# but never existing ones).
SCHEMA_VERSION = 1
_MIGRATIONS: list[tuple[int, list[str]]] = [
    (1, FTS_DDL),
]


class SchemaVersionError(RuntimeError):
    """The DB was written by a NEWER Librarian than this binary understands.
    Fail loud rather than operate on a schema we don't know."""

    def __init__(self, found: int, known: int) -> None:
        self.found, self.known = found, known
        super().__init__(
            f"librarian.db schema is v{found} but this build only knows "
            f"v{known}. Upgrade Librarian — do not downgrade the DB."
        )


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Bring `conn` up to SCHEMA_VERSION under a serializing lock. Re-reading
    user_version inside BEGIN IMMEDIATE means a process that lost the race sees
    the bumped version and applies nothing."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        if current > SCHEMA_VERSION:
            conn.rollback()
            raise SchemaVersionError(current, SCHEMA_VERSION)
        for version, statements in _MIGRATIONS:
            if current >= version:
                continue
            for stmt in statements:
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version = {version}")  # trusted int
            current = version
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _retry_locked(fn, *, attempts: int = 5):
    for i in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or i == attempts - 1:
                raise
            time.sleep(0.2 * (2 ** i))


def connect(path: str | Path | None = None, *, init: bool = True) -> sqlite3.Connection:
    """Open (and by default initialize) librarian.db. WAL + busy_timeout so a
    worker, the bot, and the CLI can share the file concurrently."""
    p = Path(path).expanduser() if path is not None else db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    _retry_locked(lambda: conn.execute("PRAGMA journal_mode=WAL"))
    conn.execute("PRAGMA foreign_keys=ON")          # locations CASCADE relies on this
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-65536")
    if init:
        _retry_locked(lambda: conn.executescript(ITEMS_DDL))
        conn.commit()
        _retry_locked(lambda: _apply_migrations(conn))
    return conn
