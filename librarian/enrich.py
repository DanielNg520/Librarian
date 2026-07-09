"""
librarian.enrich
────────────────
The async, fail-soft book-enrichment PASS — the out-of-band consumer of the
captioning.book ladder. It is deliberately SEPARATE from ingest and the backup
pass: ISBN lookup and OCR are slow and network-bound, so identifying a book must
never gate discovery, backup, or delivery. Run it on its own cadence (or once, by
hand); a failure on any single book is swallowed and the row is left as-is for a
later attempt.

For each document-bucket item that hasn't been captioned yet, it runs the ladder,
writes items.title (canonical title) and items.caption (title / author · year ·
publisher / ISBN / folder description + layered tags), and moves on. Folder tags
layer on exactly as for photos, via the item's registered root.

Independent of the suite (no `import core`); see DESIGN §0.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import routing
from .captioning import book
from .models import Item
from .store import ItemStore
from .tags import TagResolver

log = logging.getLogger(__name__)


@dataclass
class EnrichReport:
    scanned:   int = 0        # document-bucket items considered
    enriched:  int = 0        # title/caption written
    identified: int = 0       # resolved via an ISBN lookup (not just a fallback)
    skipped:   int = 0        # not a book / already captioned
    failed:    int = 0        # ladder raised (swallowed) — left for a retry

    def __str__(self) -> str:
        return (f"enrich: scanned={self.scanned} enriched={self.enriched} "
                f"identified={self.identified} skipped={self.skipped} "
                f"failed={self.failed}")


def enrich_item(store: ItemStore, item: Item, *,
                online: bool = True, ocr: bool = True,
                fetcher: Callable[[str], str | None] | None = None,
                enricher: Callable[..., book.BookMetadata] | None = None
                ) -> book.BookMetadata | None:
    """Identify one book and write its title + caption. Returns the metadata
    written (truthy), or None when nothing was written. Fail-soft: any exception
    is logged and swallowed (returns None). `enricher` is injectable for
    testing; default is captioning.book.enrich."""
    path = Path(item.path)
    if not book.is_book(path):
        return None
    run = enricher or book.enrich
    try:
        meta = run(path, online=online, ocr=ocr, fetcher=fetcher)
    except Exception as e:                        # ladder must never crash the pass
        log.warning("enrich: ladder failed for id=%d (%s): %s", item.id, path, e)
        return None

    root = store.get_root(item.root) if item.root else None
    root_path = root["path"] if root else path.parent
    base_tags = (root["tags"].split() if root and root.get("tags") else ())
    resolver = TagResolver(root["path"]) if root else None

    caption = book.compose_book_caption(
        meta, path, root_path, resolver=resolver, base_tags=base_tags)
    store.set_caption(item.id, title=meta.title, caption=caption or None)
    log.info("enrich: id=%d → %r (%s)", item.id, meta.title, meta.source)
    return meta


def enrich_pass(store: ItemStore, *,
                online: bool = True, ocr: bool = True,
                limit: int | None = None,
                recaption: bool = False,
                fetcher: Callable[[str], str | None] | None = None,
                enricher: Callable[..., book.BookMetadata] | None = None
                ) -> EnrichReport:
    """Enrich every document-bucket book that lacks a caption (or all of them when
    `recaption` is set). Reads items directly so it can run independently of the
    backup/offload cadence. Fully fail-soft."""
    report = EnrichReport()
    sql = "SELECT * FROM items ORDER BY discovered_at"
    rows = store.conn.execute(sql).fetchall()
    for r in rows:
        item = Item.from_row(r)
        if routing.bucket(item.path) != "document" or not book.is_book(item.path):
            continue
        report.scanned += 1
        if item.caption and not recaption:
            report.skipped += 1
            continue
        try:
            meta = enrich_item(store, item, online=online, ocr=ocr,
                               fetcher=fetcher, enricher=enricher)
        except Exception as e:                    # defensive: never abort the pass
            log.warning("enrich: unexpected error id=%d: %s", item.id, e)
            report.failed += 1
            continue
        if meta is None:
            report.failed += 1
            continue
        report.enriched += 1
        # An ISBN-sourced record means we actually identified the book online.
        if meta.source == "isbn":
            report.identified += 1
        if limit is not None and report.enriched >= limit:
            break
    log.info("%s", report)
    return report
