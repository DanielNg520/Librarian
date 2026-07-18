"""
librarian.captioning
─────────────────────
Deterministic, content-type-specific caption builders, composed at Librarian's
send-time seam. No models — captions come from metadata the user already
authored (folder taxonomy for photos; ISBN for books, Phase 6).

Photo captioning (Phase 2) lives in `photo.py`. Book captioning (Phase 6) lives
in `book.py` (the ISBN ladder) with pure ISBN logic in `isbn.py`. Any other file
type falls back to the folder-taxonomy caption in `generic.py` (Phase 7). The
`_compose.py` spine (folder description + hashtags) is shared by all three so the
taxonomy→caption rule can never drift between types.

`compose()` is the ONE dispatcher the send seam calls: it routes a file to the
right builder so the caller never type-switches.
"""

from __future__ import annotations

from . import book, isbn
from ._compose import TagResolver
from .book import (
    BOOK_EXTENSIONS,
    BookMetadata,
    compose_book_caption,
    is_book,
)
from .generic import compose_generic_caption
from .photo import (
    PHOTO_EXTENSIONS,
    compose_caption,
    description,
    is_photo,
    path_segments,
    segment_tags,
    timestamp,
)


def compose(
    path,
    root_path,
    *,
    resolver:     TagResolver | None = None,
    base_tags:    "list[str] | tuple[str, ...]" = (),
    book_caption: str | None = None,
) -> str:
    """Compose the send-time caption for one file, dispatched by type:

      • photo → EXIF-dated folder caption (`compose_caption`).
      • book  → the enriched caption already written to `items.caption`
                (`book_caption`, from the Phase 6 ISBN pass) if present;
                otherwise the generic folder caption, so an un-enriched book
                still ships something rather than nothing.
      • other → generic mtime-dated folder caption.

    All three share the folder description + hashtag spine, so a file tags the
    same way regardless of type. Pure — the caller supplies `root_path`,
    `base_tags`, a `.tags` `resolver`, and (for books) the stored caption."""
    if is_photo(path):
        return compose_caption(path, root_path, resolver=resolver,
                               base_tags=base_tags)
    if is_book(path) and book_caption:
        return book_caption
    return compose_generic_caption(path, root_path, resolver=resolver,
                                   base_tags=base_tags)


__all__ = [
    "PHOTO_EXTENSIONS",
    "compose",
    "compose_caption",
    "compose_generic_caption",
    "description",
    "is_photo",
    "path_segments",
    "segment_tags",
    "timestamp",
    # Phase 6 — books
    "book",
    "isbn",
    "BOOK_EXTENSIONS",
    "BookMetadata",
    "compose_book_caption",
    "is_book",
]
