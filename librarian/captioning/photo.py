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
from ..tags import TagResolver, slugify_tag

log = logging.getLogger(__name__)

PHOTO_EXTENSIONS = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".tiff", ".tif", ".bmp",
    ".heic", ".heif",
})

_SEP = " · "


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


# ── path → description / tags ──────────────────────────────────────────────
def path_segments(path, root_path) -> list[str]:
    """The folder segments between the root and the file (excludes the
    filename). [] when `path` is not under `root_path`."""
    try:
        rel = Path(path).resolve().relative_to(Path(root_path).resolve())
    except ValueError:
        return []
    return list(rel.parts[:-1])


def description(path, root_path) -> str:
    """Human description = path segments joined, e.g. 'selfie · outdoor'."""
    return _SEP.join(path_segments(path, root_path))


def segment_tags(path, root_path) -> list[str]:
    """Each path segment slugified to a tag, in layer order, de-duplicated."""
    out, seen = [], set()
    for seg in path_segments(path, root_path):
        t = slugify_tag(seg)
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _merge_tags(*groups) -> list[str]:
    out, seen = [], set()
    for group in groups:
        for t in group:
            if t and t not in seen:
                seen.add(t)
                out.append(t)
    return out


def compose_caption(
    path,
    root_path,
    *,
    resolver: TagResolver | None = None,
    base_tags: "list[str] | tuple[str, ...]" = (),
) -> str:
    """Compose the full photo caption (date line, description line, tag line).
    Empty lines are omitted. `base_tags` are the root's raw base hashtags;
    `resolver` supplies `.tags` sidecar tags (Phase 0). All tag sources are
    unioned in layer order and de-duplicated."""
    lines: list[str] = []

    ts = timestamp(path)
    if ts:
        lines.append(ts[1])

    desc = description(path, root_path)
    if desc:
        lines.append(desc)

    tags = _merge_tags(
        (slugify_tag(b) for b in base_tags),
        segment_tags(path, root_path),
        resolver.tags_for(path) if resolver else (),
    )
    if tags:
        lines.append(" ".join(f"#{t}" for t in tags))

    return "\n".join(lines)
