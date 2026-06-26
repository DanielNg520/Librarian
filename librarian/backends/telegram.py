"""
librarian.backends.telegram
────────────────────────────
Telegram as the FAST-ACCESS backend — its own Telethon session (Librarian is its
own single-flight Telegram talker; it never touches the suite's dispatcher). The
inverse of an uploader: it can both `store` (send_file) and `fetch`
(download_media) by message id.

PRESENCE-ONLY VERIFY: Telegram cannot return an object hash without downloading,
so `verify` == `exists` (the message is still there). This is deliberate and is
why offload requires a durable, hash-verifying backend (Local/rclone) — Telegram
presence never authorizes reclaiming local disk. See base.py / DESIGN §4.1.

SYNC OVER ASYNC: Telethon is async; the backup pass is a simple sync loop, so
this backend drives the client's own event loop via run_until_complete. (The
async bot in Phase 5 will use the client directly, off the same session.)

SPLITTING: files above `max_bytes` are rejected for now (BackendError) — the
≈2 GiB single-message ceiling. Part-splitting (and group_key reassembly on fetch)
is a documented TODO for a later phase; cloud backends have no such limit, so a
big file still gets a durable copy.

Lazy `telethon` import in __init__ → BackendUnavailable if the lib is missing, so
the module imports anywhere and only a CONSTRUCTED Telegram backend needs it.

Independent of the suite (no `import core`); see DESIGN §0.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .base import BackendError, BackendUnavailable, Locator

log = logging.getLogger(__name__)

# Conservative single-message ceiling (non-premium ~2 GiB). Splitting TODO.
DEFAULT_MAX_BYTES = 2_000_000_000


class TelegramBackend:
    durable = False                      # presence-only verify → never gates offload

    def __init__(self, client, destination, *, name: str = "telegram",
                 max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        try:
            import telethon  # noqa: F401  — presence check; client is passed in
        except ImportError as e:
            raise BackendUnavailable(
                "telethon is not installed — `pip install telethon` to use the "
                "Telegram backend, or remove it from your routing.") from e
        if client is None or destination is None:
            raise BackendError("TelegramBackend needs a connected client and a "
                               "destination peer")
        self.name = name
        self._client = client
        self._dest = destination
        self._max = max_bytes

    def _run(self, coro):
        """Drive the client's event loop to completion (sync facade)."""
        return self._client.loop.run_until_complete(coro)

    def store(self, path: Path, content_hash: str) -> Locator:
        path = Path(path)
        try:
            size = path.stat().st_size
        except OSError as e:
            raise BackendError(f"telegram store: cannot stat {path}: {e}") from e
        if size > self._max:
            raise BackendError(
                f"telegram store: {path.name} is {size} bytes, over the "
                f"{self._max}-byte single-message limit (splitting not yet "
                f"implemented; a durable cloud backend still holds it)")
        try:
            msg = self._run(self._client.send_file(self._dest, str(path)))
        except Exception as e:                       # telethon raises a broad set
            raise BackendError(f"telegram store failed for {path}: {e}") from e
        return Locator(self.name, str(msg.id))

    def fetch(self, locator: Locator, dest: Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            msg = self._run(self._client.get_messages(self._dest,
                                                      ids=int(locator.ref)))
            if msg is None:
                raise BackendError(f"telegram fetch: message {locator.ref} gone")
            out = self._run(self._client.download_media(msg, file=str(dest)))
        except BackendError:
            raise
        except Exception as e:
            raise BackendError(f"telegram fetch failed for {locator}: {e}") from e
        return Path(out) if out else dest

    def exists(self, locator: Locator) -> bool:
        try:
            msg = self._run(self._client.get_messages(self._dest,
                                                      ids=int(locator.ref)))
        except Exception as e:
            log.debug("telegram exists check failed for %s: %s", locator, e)
            return False
        return msg is not None

    # Presence-only — see module docstring.
    def verify(self, locator: Locator, content_hash: str) -> bool:
        return self.exists(locator)
