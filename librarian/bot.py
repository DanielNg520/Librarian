"""
librarian.bot
─────────────
The retrieval seam — how a human gets bytes back out of Librarian after the
offload pass has reclaimed local disk. Three verbs, all driven off librarian.db
and the same StorageBackend registry the backup/offload passes use:

    find     FTS5 search over title/caption/path/upload_date → matching items.
    serve    forward the file's stored TELEGRAM message inline (fast tier, no
             download — Telegram already holds a copy for exactly this).
    restore  pull the bytes back down from the best backend into ~/Downloads,
             RE-VERIFY content_hash against the freshly-written file, and flip
             an OFFLOADED row back to BACKED_UP.

RESTORE'S INTEGRITY CHECK mirrors offload's in reverse: offload only deletes once
a durable copy verifies; restore only reports success once the bytes it just
wrote hash back to the item's content_hash. A backend that hands back the wrong
or corrupt bytes is skipped, and the next backend is tried — the caller never
gets a silently-wrong file. Because we hash the actual downloaded file, this
holds even for Telegram (whose own verify is presence-only).

STRUCTURE: the search/restore/serve LOGIC lives here as plain functions, testable
with fake backends and no network. `LibrarianBot` is the thin Telethon wiring
(own MTProto session, single-flight talker) that maps chat commands onto them;
telethon is imported lazily so this module imports anywhere and only a CONSTRUCTED
bot needs the library. Mirrors backends/telegram.py's lazy-import discipline.

Independent of the suite (no `import core`); see DESIGN §0.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .backends.base import BackendError, Locator
from .backends.registry import Registry
from .hashing import full_hash
from .models import Item, Status
from .store import ItemStore

log = logging.getLogger(__name__)


# ── find ──────────────────────────────────────────────────────────────────────
def find(store: ItemStore, query: str, *, limit: int = 20) -> list[Item]:
    """FTS5 `find`. Thin pass-through to store.search so the bot and the CLI share
    one query path; kept here so all three retrieval verbs live together."""
    return store.search(query, limit=limit)


# ── serve (inline Telegram forward) ──────────────────────────────────────────
def telegram_location(store: ItemStore, registry: Registry, item: Item):
    """The item's location on a registered, NON-durable (presence-only, i.e.
    Telegram-style) backend — the copy `serve` forwards inline without a download.
    None if no such copy is registered."""
    for loc in store.locations_for(item.id):
        if registry.has(loc.backend) and not registry.is_durable(loc.backend):
            return loc
    return None


# ── restore ───────────────────────────────────────────────────────────────────
class RestoreOutcome(str, Enum):
    RESTORED      = "restored"        # bytes fetched + content_hash re-verified
    NO_LOCATION   = "no_location"     # no available backend holds it → nothing to do
    FETCH_FAILED  = "fetch_failed"    # every backend errored on fetch
    VERIFY_FAILED = "verify_failed"   # fetched, but no copy hashed back correctly


@dataclass
class RestoreResult:
    outcome: RestoreOutcome
    path:    Path | None = None       # where the file landed (on RESTORED)
    backend: str | None = None        # which backend served it
    error:   str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == RestoreOutcome.RESTORED


def default_download_dir() -> Path:
    return Path.home() / "Downloads"


def _restore_order(store: ItemStore, registry: Registry, item: Item):
    """Backends to try, DURABLE (hash-verifying) first: they give the strongest
    integrity story and never spend the fast tier's quota. Presence-only backends
    (Telegram) follow as a fallback — still hash-checked once downloaded."""
    locs = [l for l in store.locations_for(item.id) if registry.has(l.backend)]
    durable = [l for l in locs if registry.is_durable(l.backend)]
    other   = [l for l in locs if not registry.is_durable(l.backend)]
    return durable + other


def restore(store: ItemStore, registry: Registry, item: Item,
            dest_dir: str | Path | None = None, *,
            verify: bool = True) -> RestoreResult:
    """Fetch `item`'s bytes back to `dest_dir` (default ~/Downloads), re-verify the
    content_hash of the freshly-written file, and — on success — flip an OFFLOADED
    row to BACKED_UP. Tries durable backends first, then presence-only ones; a copy
    that fails the hash check is discarded and the next backend tried."""
    dest_dir = Path(dest_dir).expanduser() if dest_dir else default_download_dir()
    dest = dest_dir / Path(item.path).name

    order = _restore_order(store, registry, item)
    if not order:
        return RestoreResult(RestoreOutcome.NO_LOCATION)

    fetched_any = False
    last_error: str | None = None
    for loc in order:
        backend = registry.get(loc.backend)
        try:
            out = Path(backend.fetch(Locator(loc.backend, loc.locator), dest))
        except BackendError as e:
            last_error = str(e)
            log.warning("restore: fetch failed id=%d on %s: %s",
                        item.id, loc.backend, e)
            continue
        fetched_any = True

        if verify and item.content_hash:
            got = full_hash(out)
            if got != item.content_hash:
                last_error = f"hash mismatch from {loc.backend}"
                log.warning("restore: %s (id=%d, got=%s)", last_error, item.id, got)
                try:
                    out.unlink()                     # don't leave a corrupt file behind
                except OSError:
                    pass
                continue

        # Re-verified (or verification waived): the bytes are back on disk.
        if item.status == Status.OFFLOADED.value:
            store.set_status(item.id, Status.BACKED_UP)
        log.info("restore: id=%d served from %s → %s", item.id, loc.backend, out)
        return RestoreResult(RestoreOutcome.RESTORED, path=out, backend=loc.backend)

    outcome = (RestoreOutcome.VERIFY_FAILED if fetched_any
               else RestoreOutcome.FETCH_FAILED)
    return RestoreResult(outcome, error=last_error)


# ── the Telegram bot (lazy telethon wiring) ───────────────────────────────────
_HELP = (
    "Librarian bot\n"
    "  /find <query>   — search your archive\n"
    "  /serve <id>     — resend a file inline\n"
    "  /restore <id>   — download a file back to ~/Downloads"
)


def _format_hits(items: list[Item]) -> str:
    if not items:
        return "No matches."
    lines = []
    for it in items:
        name = Path(it.path).name
        when = it.upload_date or ""
        lines.append(f"[{it.id}] {name}  {when}  ({it.status})".rstrip())
    return "\n".join(lines)


class LibrarianBot:
    """Librarian's own single-flight Telegram talker (its own MTProto session,
    never the suite's dispatcher). Maps /find, /serve, /restore onto the functions
    above. telethon is imported lazily so importing this module costs nothing; only
    a constructed bot needs the library.

    `client` is a connected telethon TelegramClient; `store_db` opens Librarian's
    DB per-handler (SQLite WAL lets the worker and bot share the file). `peer` is
    the destination the archive lives in — the same peer the Telegram backend
    stores to — so `serve` can forward from it.
    """

    def __init__(self, client, registry: Registry, peer, *,
                 store_db: str | None = None,
                 download_dir: str | Path | None = None) -> None:
        try:
            import telethon  # noqa: F401 — presence check; client is passed in
            from telethon import events
        except ImportError as e:  # pragma: no cover - environment guard
            raise RuntimeError(
                "telethon is not installed — `pip install telethon` to run the "
                "Librarian bot.") from e
        self._client = client
        self._events = events
        self._registry = registry
        self._peer = peer
        self._db = store_db
        self._download_dir = download_dir
        self._register_handlers()

    def _store(self) -> ItemStore:
        return ItemStore.open(self._db)

    def _register_handlers(self) -> None:
        events = self._events
        self._client.add_event_handler(
            self._on_find, events.NewMessage(pattern=r"^/find(?:@\w+)?\s+(.+)"))
        self._client.add_event_handler(
            self._on_serve, events.NewMessage(pattern=r"^/serve(?:@\w+)?\s+(\d+)"))
        self._client.add_event_handler(
            self._on_restore, events.NewMessage(pattern=r"^/restore(?:@\w+)?\s+(\d+)"))
        self._client.add_event_handler(
            self._on_help, events.NewMessage(pattern=r"^/(start|help)\b"))

    # Each handler opens its own short-lived store so a long-lived bot never pins
    # a stale connection, and closes it in a finally.
    async def _on_help(self, event) -> None:
        await event.respond(_HELP)

    async def _on_find(self, event) -> None:
        query = event.pattern_match.group(1).strip()
        store = self._store()
        try:
            hits = find(store, query)
        finally:
            store.close()
        await event.respond(_format_hits(hits))

    async def _on_serve(self, event) -> None:
        item_id = int(event.pattern_match.group(1))
        store = self._store()
        try:
            item = store.get(item_id)
            loc = telegram_location(store, self._registry, item) if item else None
        finally:
            store.close()
        if item is None:
            await event.respond(f"No item [{item_id}].")
            return
        if loc is None:
            await event.respond(
                f"[{item_id}] has no Telegram copy — try /restore instead.")
            return
        try:
            msg = await self._client.get_messages(self._peer, ids=int(loc.locator))
            if msg is None:
                raise BackendError("stored message is gone")
            await self._client.send_message(await event.get_input_chat(), msg)
        except Exception as e:  # telethon raises a broad set
            await event.respond(f"Could not serve [{item_id}]: {e}")

    async def _on_restore(self, event) -> None:
        item_id = int(event.pattern_match.group(1))
        store = self._store()
        try:
            item = store.get(item_id)
            if item is None:
                await event.respond(f"No item [{item_id}].")
                return
            # restore() may hit the network; run it off the event loop thread so
            # the single-flight client stays responsive.
            import asyncio
            result = await asyncio.to_thread(
                restore, store, self._registry, item, self._download_dir)
        finally:
            store.close()
        if result.ok:
            await event.respond(
                f"Restored [{item_id}] from {result.backend} → {result.path}")
        else:
            await event.respond(
                f"Restore of [{item_id}] failed ({result.outcome.value}): "
                f"{result.error or ''}".rstrip())

    def run(self) -> None:  # pragma: no cover - live loop
        """Block serving until disconnected (own event loop)."""
        log.info("librarian bot: running")
        self._client.run_until_disconnected()
