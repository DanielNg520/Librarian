"""
librarian.bootstrap
────────────────────
Config → wired, ready-to-run objects. The ONE place `config.toml` becomes a
`Registry` + `RoutingPolicy` + `DeletionGuard`, so the CLI, the daemon, and any
script all assemble the machine identically (Builder over the existing DI seams
— nothing here adds behavior, it only wires).

    [backends.disk]
    type = "local"                  # durable, hash-verifying
    path = "/Volumes/NAS/librarian"

    [backends.gdrive]
    type = "rclone"                 # durable, via `rclone hashsum`
    remote = "gdrive:"
    base   = "librarian"

    [backends.telegram]
    type = "telegram"               # fast-access tier (presence-only)
    api_id = 12345
    api_hash = "…"
    session = "~/.config/librarian/telegram.session"
    destination = "me"              # peer: 'me', @channel, or a chat id

FAIL-SOFT PER BACKEND (mirrors the backup pass's own discipline): a backend
whose prerequisite is missing (no rclone binary, no telethon, bad section) is
logged and SKIPPED — routing keeps naming it, items routed to it simply stay
PENDING until it's configured, and everything else runs. A typo'd `type` is a
loud skip, never a crash.

Independent of the suite (no `import core`); see DESIGN §0.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path

from .backends.base import BackendError
from .backends.local import LocalBackend
from .backends.registry import Registry
from .backends.rclone import RcloneBackend
from .deletion import DeletionGuard, ProtectionPolicy
from .paths import config_path
from .routing import RoutingPolicy
from .store import ItemStore

log = logging.getLogger(__name__)


def _load_toml(path: str | Path | None) -> dict:
    p = Path(path).expanduser() if path is not None else config_path()
    if not p.exists():
        return {}
    try:
        with p.open("rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        log.warning("bootstrap: could not read %s (%s); empty config", p, e)
        return {}


def _build_telegram(name: str, sect: dict):
    """Construct the Telegram backend from config: start its OWN Telethon client
    (Librarian is its own single-flight talker). Any missing piece → None."""
    from .backends.telegram import TelegramBackend   # lazy: telethon optional
    import telethon

    session = str(Path(sect["session"]).expanduser())
    client = telethon.TelegramClient(session, int(sect["api_id"]),
                                     str(sect["api_hash"]))
    client.loop.run_until_complete(client.connect())
    if not client.loop.run_until_complete(client.is_user_authorized()):
        raise BackendError(
            f"telegram session {session!r} is not authorized — run "
            f"`librarian telegram-login` once to sign in")
    dest = sect.get("destination", "me")
    entity = client.loop.run_until_complete(client.get_entity(dest))
    return TelegramBackend(client, entity, name=name,
                           max_bytes=int(sect.get("max_bytes", 2_000_000_000)))


def registry_from_config(path: str | Path | None = None) -> Registry:
    """Build the Registry from `[backends.*]`. Each backend constructs fail-soft:
    a failure disables THAT backend (loud log), never the process."""
    data = _load_toml(path)
    reg = Registry()
    for name, sect in (data.get("backends") or {}).items():
        if not isinstance(sect, dict):
            log.warning("bootstrap: [backends.%s] is not a table — skipped", name)
            continue
        kind = str(sect.get("type", "")).lower()
        try:
            if kind == "local":
                reg.register(LocalBackend(sect["path"], name=name))
            elif kind == "rclone":
                reg.register(RcloneBackend(sect["remote"],
                                           base=sect.get("base", "librarian"),
                                           name=name))
            elif kind == "telegram":
                reg.register(_build_telegram(name, sect))
            else:
                log.warning("bootstrap: [backends.%s] unknown type %r — skipped "
                            "(known: local, rclone, telegram)", name, kind)
        except KeyError as e:
            log.warning("bootstrap: [backends.%s] missing key %s — skipped",
                        name, e)
        except ImportError as e:
            log.warning("bootstrap: [backends.%s] dependency missing (%s) — "
                        "skipped; items routed to it stay pending", name, e)
        except Exception as e:                    # BackendUnavailable, network, …
            log.warning("bootstrap: [backends.%s] failed to construct (%s) — "
                        "skipped; items routed to it stay pending", name, e)
    if not reg.names():
        log.warning("bootstrap: NO backends configured — scans will discover "
                    "but nothing can back up (add [backends.*] to config.toml)")
    return reg


def assemble(store: ItemStore, *, config: str | Path | None = None
             ) -> "tuple[Registry, RoutingPolicy, DeletionGuard]":
    """Everything `worker.full_cycle` needs, from ONE config read."""
    registry = registry_from_config(config)
    policy = RoutingPolicy.load(config)
    guard = DeletionGuard(policy=ProtectionPolicy.load(store=store, path=config))
    return registry, policy, guard
