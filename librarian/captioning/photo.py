"""
librarian.captioning.photo
──────────────────────────
The folder taxonomy IS the photo's metadata. For a file under a registered root:

    Photos/selfie/bathroom selfie/outdoor/IMG_1234.jpg

we compose, deterministically and with no model:

    2024-08-14 18:32                       ← EXIF DateTimeOriginal (else mtime)
    selfie · bathroom selfie · outdoor     ← path segments below the root
    #selfie #bathroom_selfie #outdoor      ← each segment slugified = a layered tag

Tags come from THREE sources, unioned in layer order and de-duplicated:
  1. the registered root's base tags,
  2. each path segment's name (auto-slugified here),
  3. `.tags` sidecars (via librarian.tags.TagResolver — the Phase 0 mechanism).

The slug rule is shared with librarian.tags (lowercase, non-alnum→`_`, never `-`,
drop empty/all-digit), so a folder NAME and a `.tags` ENTRY tag the same way.

EXIF is read at INGEST and stored in items.upload_date (stable). The full
caption is composed at SEND time (so a later `.tags` edit is reflected). Both use
the pure functions here.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from .. import exif
from ..tags import TagResolver
from ._compose import (  # noqa: F401 — re-exported: the folder-caption spine
    description,
    folder_lines,
    merge_tags as _merge_tags,
    path_segments,
    segment_tags,
)

log = logging.getLogger(__name__)

PHOTO_EXTENSIONS = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".tiff", ".tif", ".bmp",
    ".heic", ".heif",
})


def is_photo(path) -> bool:
    return Path(path).suffix.lower() in PHOTO_EXTENSIONS


# ── timestamp ──────────────────────────────────────────────────────────────
def _from_exif(path) -> datetime | None:
    raw = exif.datetime_original(path)
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y:%m:%d %H:%M:%S")
    except ValueError:
        log.debug("photo: unparseable EXIF datetime %r on %s", raw, path)
        return None


def timestamp(path) -> tuple[str, str] | None:
    """Return (upload_date 'YYYYMMDD', display 'YYYY-MM-DD HH:MM') for a file.
    EXIF DateTimeOriginal for photos; otherwise (or on failure) file mtime.
    None only if even the mtime can't be read."""
    dt = _from_exif(path) if is_photo(path) else None
    if dt is None:
        try:
            dt = datetime.fromtimestamp(Path(path).stat().st_mtime)
        except OSError:
            return None
    return dt.strftime("%Y%m%d"), dt.strftime("%Y-%m-%d %H:%M")


# ── caption ────────────────────────────────────────────────────────────────
# path_segments / description / segment_tags / folder_lines live in _compose
# (imported above) — the taxonomy→caption rule is shared with book & generic.

def compose_caption(
    path,
    root_path,
    *,
    resolver: TagResolver | None = None,
    base_tags: "list[str] | tuple[str, ...]" = (),
) -> str:
    """Compose the full photo caption (date line, description line, tag line).
    The date is EXIF DateTimeOriginal (else mtime); the description + tag lines
    are the shared folder spine. Empty lines are omitted."""
    lines: list[str] = []

    ts = timestamp(path)
    if ts:
        lines.append(ts[1])

    lines += folder_lines(path, root_path, resolver=resolver, base_tags=base_tags)
    return "\n".join(lines)
