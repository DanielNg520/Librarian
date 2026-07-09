"""
librarian.store
───────────────
The typed gateway to librarian.db. Every read/write of items, locations, and
roots goes through here so the SQL lives in one place.

Vendored from the suite's core.store (copied, not imported; see DESIGN §0),
reduced to Librarian's needs: no claim_batch/album machinery yet (arrives with
the send path in Phase 3), no social-media identity. Adds the locations and
roots accessors the suite never had.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import schema
from .models import Item, Location, Status

log = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fts_match(query: str) -> str:
    """Turn a raw user query into a safe FTS5 MATCH expression.

    We never pass user text to MATCH verbatim — bare `-`, `*`, `"`, `AND/OR/NEAR`
    and unbalanced quotes are FTS5 operators that would either error or mean
    something the user didn't intend. Each whitespace token is emitted as a quoted
    prefix phrase (`"tok"*`), internal quotes doubled; tokens are ANDed (implicit).
    A query with no alphanumeric content yields "" → the caller returns no rows.
    """
    tokens: list[str] = []
    for raw in query.split():
        cleaned = raw.strip()
        if not any(ch.isalnum() for ch in cleaned):
            continue
        tokens.append('"' + cleaned.replace('"', '""') + '"*')
    return " ".join(tokens)


class ItemStore:
    def __init__(self, conn: sqlite3.Connection | None = None,
                 db_path: str | None = None) -> None:
        self.conn = conn if conn is not None else schema.connect(db_path)

    @classmethod
    def open(cls, db_path: str | None = None) -> "ItemStore":
        return cls(schema.connect(db_path))

    def close(self) -> None:
        try:
            self.conn.execute("PRAGMA optimize")
        except sqlite3.Error:
            pass
        self.conn.close()

    def __enter__(self) -> "ItemStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _commit(self) -> None:
        self.conn.commit()

    # ── items: write ─────────────────────────────────────────────────────────
    def add_item(
        self, *,
        path:         str,
        content_hash: str | None = None,
        root:         str | None = None,
        size_bytes:   int | None = None,
        title:        str | None = None,
        caption:      str | None = None,
        upload_date:  str | None = None,
        group_key:    str | None = None,
    ) -> bool:
        """Register a discovered file as a pending item. INSERT OR IGNORE on the
        UNIQUE path, so a re-scan won't duplicate. Returns True iff a row was
        inserted."""
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO items
                 (path, content_hash, root, size_bytes, title, caption,
                  upload_date, group_key, status, discovered_at, attempts)
               VALUES (?,?,?,?,?,?,?,?, 'pending', ?, 0)""",
            (path, content_hash, root, size_bytes, title, caption,
             upload_date, group_key, now_iso()),
        )
        self._commit()
        return cur.rowcount > 0

    def relink_file(self, item_id: int, new_path: str) -> None:
        """Re-point a row at a new physical file (dedup adopt). Preserves status
        and backup history."""
        self.conn.execute("UPDATE items SET path = ? WHERE id = ?",
                           (new_path, item_id))
        self._commit()

    def rearm_failed(self, item_id: int) -> bool:
        """failed → pending (re-introduced content whose bytes never shipped).
        Returns True iff a failed row actually flipped."""
        cur = self.conn.execute(
            "UPDATE items SET status='pending', last_error=NULL, attempts=0 "
            "WHERE id = ? AND status = ?",
            (item_id, Status.FAILED.value),
        )
        self._commit()
        return cur.rowcount > 0

    def set_caption(self, item_id: int, *, title: str | None = None,
                    caption: str | None = None) -> None:
        """Write back a composed title/caption (book enrichment, Phase 6). Only
        the provided fields are updated; the items_fts trigger re-indexes them.
        The caption is NOT hashed, so this never disturbs dedup."""
        sets, params = [], []
        if title is not None:
            sets.append("title = ?")
            params.append(title)
        if caption is not None:
            sets.append("caption = ?")
            params.append(caption)
        if not sets:
            return
        params.append(item_id)
        self.conn.execute(f"UPDATE items SET {', '.join(sets)} WHERE id = ?", params)
        self._commit()

    def set_status(self, item_id: int, status: Status | str) -> None:
        self.conn.execute("UPDATE items SET status = ? WHERE id = ?",
                          (getattr(status, "value", status), item_id))
        self._commit()

    def delete(self, item_id: int) -> int:
        cur = self.conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
        self._commit()
        return cur.rowcount

    # ── items: read ──────────────────────────────────────────────────────────
    def has_path(self, path: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM items WHERE path = ? LIMIT 1", (path,)
        ).fetchone() is not None

    def id_of(self, path: str) -> int | None:
        r = self.conn.execute(
            "SELECT id FROM items WHERE path = ?", (path,)).fetchone()
        return r["id"] if r else None

    def get(self, item_id: int) -> Item | None:
        r = self.conn.execute(
            "SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return Item.from_row(r) if r else None

    def status_of(self, path: str) -> str | None:
        r = self.conn.execute(
            "SELECT status FROM items WHERE path = ?", (path,)).fetchone()
        return r["status"] if r else None

    def find_by_content_hash(self, content_hash: str) -> Item | None:
        """Any existing row with these exact bytes. A non-failed (deliverable)
        twin is preferred, so a 'failed' result means it's the only one — which
        ingest treats as a re-arm signal."""
        r = self.conn.execute(
            "SELECT * FROM items WHERE content_hash = ? "
            "ORDER BY (status = 'failed'), id LIMIT 1",
            (content_hash,),
        ).fetchone()
        return Item.from_row(r) if r else None

    def items_by_status(self, status: Status | str, *,
                        limit: int | None = None) -> list[Item]:
        """All items in `status`, oldest first. The backup pass reads PENDING;
        the offload pass reads BACKED_UP."""
        sql = "SELECT * FROM items WHERE status = ? ORDER BY discovered_at"
        params: list = [getattr(status, "value", status)]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [Item.from_row(r) for r in rows]

    def mark_failed(self, item_id: int, *, error: str, max_retries: int) -> str:
        """Record a backup failure: bump attempts; stay PENDING for a retry until
        attempts reach max_retries, then FAILED (terminal until reset). Returns
        the resulting status value. Mirrors the suite's retry semantics."""
        r = self.conn.execute(
            "SELECT attempts FROM items WHERE id = ?", (item_id,)).fetchone()
        attempts = (r["attempts"] if r else 0) + 1
        status = (Status.FAILED.value if attempts >= max_retries
                  else Status.PENDING.value)
        self.conn.execute(
            "UPDATE items SET attempts = ?, status = ?, last_error = ? WHERE id = ?",
            (attempts, status, error, item_id))
        self._commit()
        return status

    # ── search (FTS5, Phase 5) ─────────────────────────────────────────────────
    def search(self, query: str, *, limit: int = 20) -> list[Item]:
        """Full-text `find` over title/caption/path/upload_date via the items_fts
        index, best matches first (FTS5 `rank`). Returns [] for an empty/degenerate
        query. Fail-soft: any FTS error (e.g. a build without FTS5) yields [] rather
        than raising into the bot loop."""
        match = _fts_match(query)
        if not match:
            return []
        try:
            rows = self.conn.execute(
                "SELECT items.* FROM items "
                "JOIN items_fts ON items.id = items_fts.rowid "
                "WHERE items_fts MATCH ? ORDER BY rank LIMIT ?",
                (match, limit),
            ).fetchall()
        except sqlite3.Error as e:
            log.warning("store.search failed for %r: %s", query, e)
            return []
        return [Item.from_row(r) for r in rows]

    def count_by_status(self, status: Status | str | None = None) -> int:
        if status is None:
            r = self.conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()
        else:
            r = self.conn.execute(
                "SELECT COUNT(*) AS n FROM items WHERE status = ?",
                (getattr(status, "value", status),)).fetchone()
        return r["n"]

    # ── locations ────────────────────────────────────────────────────────────
    def add_location(self, item_id: int, backend: str, locator: str,
                     *, verified_at: str | None = None) -> None:
        """Record (or update) where one backend holds this item's bytes."""
        self.conn.execute(
            "INSERT INTO locations (item_id, backend, locator, verified_at) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(item_id, backend) DO UPDATE SET "
            "  locator=excluded.locator, verified_at=excluded.verified_at",
            (item_id, backend, locator, verified_at),
        )
        self._commit()

    def locations_for(self, item_id: int) -> list[Location]:
        rows = self.conn.execute(
            "SELECT * FROM locations WHERE item_id = ? ORDER BY backend",
            (item_id,)).fetchall()
        return [Location.from_row(r) for r in rows]

    # ── roots ────────────────────────────────────────────────────────────────
    def add_root(self, name: str, path: str, *, destination: str | None = None,
                 tags: str | None = None) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO roots (name, path, destination, tags, added_at) "
            "VALUES (?,?,?,?,?)",
            (name, path, destination, tags, now_iso()),
        )
        self._commit()
        return cur.rowcount > 0

    def get_root(self, name: str) -> dict | None:
        r = self.conn.execute(
            "SELECT * FROM roots WHERE name = ?", (name,)).fetchone()
        return dict(r) if r else None

    def list_roots(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM roots ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    def remove_root(self, name: str) -> int:
        cur = self.conn.execute("DELETE FROM roots WHERE name = ?", (name,))
        self._commit()
        return cur.rowcount
