"""
librarian.backup
────────────────
The fan-out backup pass: take a PENDING item, store its bytes to every routed
+ available backend, record a `locations` row per copy, and flip it to
BACKED_UP once all available backends hold it.

INTEGRITY / RESUMABILITY:
  - A backend that's routed but not registered is logged and skipped — the item
    stays PENDING and is retried when that backend is configured, never lost.
  - Stores already recorded (a backend the item is already in) are skipped, so a
    re-run after a partial failure only retries the missing backends.
  - A DURABLE backend's copy is verified right after store; a store that doesn't
    verify is treated as a failure (no location recorded) — we never claim a
    durable copy we couldn't confirm.
  - On any failure the item is mark_failed (attempts++ → PENDING for retry, then
    FAILED at the cap). Successful copies are kept, so retries converge.

FAST TIER NOT GATED ON SLOW CLOUD: stores fan out CONCURRENTLY (one thread per
backend, IO-bound), so the Telegram copy lands as soon as it's done regardless of
a slow cloud upload. DB writes (recording locations) happen back on the main
thread, so the single sqlite connection is never touched from two threads.

Independent of the suite (no `import core`); see DESIGN §0.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from . import captioning
from .backends.base import BackendError, Locator, StorageBackend
from .backends.registry import Registry
from .models import Status
from .routing import RoutingPolicy
from .store import ItemStore, now_iso
from .tags import TagResolver

log = logging.getLogger(__name__)

DEFAULT_MAX_RETRIES = 4


class BackupOutcome(str, Enum):
    BACKED_UP  = "backed_up"     # every available backend now holds a copy
    RETRY      = "retry"         # a backend failed; still PENDING, retry next pass
    FAILED     = "failed"        # retries exhausted
    NO_BACKEND = "no_backend"    # nothing routed+available; left PENDING


def _caption_for(store: ItemStore, item) -> str | None:
    """Compose the send-time caption for one item from its folder taxonomy (and,
    for books, the ISBN caption already in `items.caption`). FAIL-SOFT: any
    problem returns None — a missing caption must never block delivery. Composed
    HERE, at send, so a later `.tags`/folder-move edit is reflected."""
    try:
        if not item.root:
            return None
        root = store.get_root(item.root)
        if not root or not root.get("path"):
            return None
        root_path = root["path"]
        base_tags = (root.get("tags") or "").split()
        caption = captioning.compose(
            item.path, root_path,
            resolver=TagResolver(root_path),
            base_tags=base_tags,
            book_caption=item.caption,
        )
        return caption or None
    except Exception as e:                      # composing must never crash a pass
        log.debug("backup: caption compose failed id=%s: %s", item.id, e)
        return None


def _safe_store(backend: StorageBackend, path: Path, content_hash: str,
                caption: str | None) -> "tuple[Locator | None, str | None]":
    """Run one backend.store off-thread, capturing any error as a string. A
    backend bug must never crash the whole pass. `caption` is honoured only by
    the fast-access tier (Telegram); durable backends ignore it."""
    try:
        return backend.store(path, content_hash, caption=caption), None
    except BackendError as e:
        return None, str(e)
    except Exception as e:                      # defensive
        return None, f"unexpected: {e}"


def backup_item(store: ItemStore, registry: Registry, policy: RoutingPolicy,
                item, *, max_retries: int = DEFAULT_MAX_RETRIES) -> BackupOutcome:
    """Store one item to its routed+available backends and update its status."""
    path = Path(item.path)
    routed = policy.backends_for(item.path)
    available = registry.available(routed)
    for missing in (b for b in routed if b not in available):
        log.warning("backup: id=%d routed to unconfigured backend %r — skipping",
                    item.id, missing)
    if not available:
        return BackupOutcome.NO_BACKEND

    already = {l.backend for l in store.locations_for(item.id)}
    todo = [n for n in available if n not in already]

    results: dict[str, tuple[Locator | None, str | None]] = {}
    # AVOID DUP UPLOAD: if these exact bytes already live on a backend (stored by
    # ANY row), reuse that locator instead of re-uploading — the durable backends
    # are content-addressed (the ref IS the hash) and a Telegram message points at
    # identical bytes, so the shared copy is interchangeable. A reused DURABLE copy
    # is still re-verified below (the integrity gate), so a corrupt share can't be
    # silently trusted.
    real_todo: list[str] = []
    for name in todo:
        ref = store.location_ref_for_hash(item.content_hash, name,
                                          exclude_item=item.id)
        if ref is not None:
            results[name] = (Locator(name, ref), None)
            log.info("backup: id=%d reuse existing %s copy (dup bytes, no upload)",
                     item.id, name)
        else:
            real_todo.append(name)

    if real_todo:
        caption = _caption_for(store, item)     # composed once, at send time
        with ThreadPoolExecutor(max_workers=len(real_todo)) as ex:
            futs = {ex.submit(_safe_store, registry.get(n), path,
                              item.content_hash, caption): n for n in real_todo}
            for fut in as_completed(futs):
                results[futs[fut]] = fut.result()

    failures: list[tuple[str, str]] = []
    for name in todo:
        loc, err = results.get(name, (None, "not attempted"))
        if loc is None:
            failures.append((name, err or "store failed"))
            continue
        verified_at = None
        if registry.is_durable(name):
            try:
                if registry.get(name).verify(loc, item.content_hash):
                    verified_at = now_iso()
                else:
                    failures.append((name, "stored but failed verify"))
                    continue                    # don't record an unconfirmed durable copy
            except BackendError as e:
                failures.append((name, f"verify error: {e}"))
                continue
        store.add_location(item.id, loc.backend, loc.ref, verified_at=verified_at)

    stored = {l.backend for l in store.locations_for(item.id)}
    if not failures and all(b in stored for b in available):
        store.set_status(item.id, Status.BACKED_UP)
        return BackupOutcome.BACKED_UP

    msg = "; ".join(f"{n}: {e}" for n, e in failures) or "incomplete"
    new = store.mark_failed(item.id, error=msg, max_retries=max_retries)
    return (BackupOutcome.FAILED if new == Status.FAILED.value
            else BackupOutcome.RETRY)


@dataclass
class BackupReport:
    backed_up:  int = 0
    retry:      int = 0
    failed:     int = 0
    no_backend: int = 0
    errors:     list[str] = field(default_factory=list)

    def record(self, outcome: BackupOutcome) -> None:
        setattr(self, outcome.value, getattr(self, outcome.value) + 1)

    def __str__(self) -> str:
        return (f"backup: backed_up={self.backed_up} retry={self.retry} "
                f"failed={self.failed} no_backend={self.no_backend}")


def backup_pass(store: ItemStore, registry: Registry, policy: RoutingPolicy,
                *, max_retries: int = DEFAULT_MAX_RETRIES,
                limit: int | None = None) -> BackupReport:
    """Back up every PENDING item. Serial across items (single-flight); the
    per-item backend fan-out is concurrent."""
    report = BackupReport()
    for item in store.items_by_status(Status.PENDING, limit=limit):
        report.record(backup_item(store, registry, policy, item,
                                  max_retries=max_retries))
    log.info("%s", report)
    return report
