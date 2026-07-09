"""
librarian.captioning
─────────────────────
Deterministic, content-type-specific caption builders, composed at Librarian's
send-time seam. No models — captions come from metadata the user already
authored (folder taxonomy for photos; ISBN for books, Phase 6).

Photo captioning (Phase 2) lives in `photo.py`. Book captioning (Phase 6) lives
in `book.py` (the ISBN ladder) with pure ISBN logic in `isbn.py`.
"""

from __future__ import annotations

from . import book, isbn
from .book import (
    BOOK_EXTENSIONS,
    BookMetadata,
    compose_book_caption,
    is_book,
)
from .photo import (
    PHOTO_EXTENSIONS,
    compose_caption,
    description,
    is_photo,
    path_segments,
    segment_tags,
    timestamp,
)

__all__ = [
    "PHOTO_EXTENSIONS",
    "compose_caption",
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
