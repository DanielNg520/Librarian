"""
librarian.captioning
─────────────────────
Deterministic, content-type-specific caption builders, composed at Librarian's
send-time seam. No models — captions come from metadata the user already
authored (folder taxonomy for photos; ISBN for books, Phase 6).

Photo captioning (Phase 2) lives in `photo.py`. Book captioning arrives in
Phase 6 as `book.py`.
"""

from __future__ import annotations

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
]
