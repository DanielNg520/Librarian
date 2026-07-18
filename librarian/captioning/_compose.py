"""
librarian.captioning._compose
──────────────────────────────
The ONE caption spine shared by every content type. A Librarian caption is a
stack of lines, empties omitted:

    <lead lines>              ← type-specific (EXIF date / mtime / book title…)
    selfie · outdoor          ← folder DESCRIPTION: path segments below the root
    #selfie #outdoor          ← folder HASHTAGS: root base tags + segments + .tags

`photo.py`, `book.py`, and `generic.py` each supply only their lead lines and
delegate the two FOLDER lines (description + hashtags) here, so the taxonomy →
caption rule lives in exactly one place and the slug/merge behaviour can never
drift between types.

Pure: no DB, no network. The folder-tag union is layer-ordered and de-duplicated
(root base tags, then path segments, then inherited `.tags` sidecars), matching
the Phase 0 tag mechanism.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..tags import TagResolver, slugify_tag

_SEP = " · "


def display_date(path) -> str | None:
    """Type-neutral capture date: file mtime as 'YYYY-MM-DD HH:MM'. None only if
    the mtime can't be read. (Photos override this with EXIF in photo.py.)"""
    try:
        dt = datetime.fromtimestamp(Path(path).stat().st_mtime)
    except OSError:
        return None
    return dt.strftime("%Y-%m-%d %H:%M")


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


def merge_tags(*groups) -> list[str]:
    """Union of tag groups, first occurrence wins, order-stable, de-duplicated."""
    out, seen = [], set()
    for group in groups:
        for t in group:
            if t and t not in seen:
                seen.add(t)
                out.append(t)
    return out


def folder_lines(
    path,
    root_path,
    *,
    resolver:  TagResolver | None = None,
    base_tags: "list[str] | tuple[str, ...]" = (),
) -> list[str]:
    """The two folder-derived caption lines — [description?, hashtag-line?] — with
    empties omitted. `base_tags` are the root's raw base hashtags; `resolver`
    supplies inherited `.tags` (Phase 0). All tag sources union in layer order,
    de-duplicated. Shared by photo / book / generic composers."""
    lines: list[str] = []

    desc = description(path, root_path)
    if desc:
        lines.append(desc)

    tags = merge_tags(
        (slugify_tag(b) for b in base_tags),
        segment_tags(path, root_path),
        resolver.tags_for(path) if resolver else (),
    )
    if tags:
        lines.append(" ".join(f"#{t}" for t in tags))

    return lines
