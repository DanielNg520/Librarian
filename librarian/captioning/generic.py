"""
librarian.captioning.generic
─────────────────────────────
The fallback captioner for any file that is neither a photo (EXIF taxonomy) nor
a book (ISBN ladder) — a video, an archive, an arbitrary document. The folder
taxonomy is still the metadata: a generic caption is the same shape as a photo's,
minus EXIF (there is none to read), so NOTHING ever ships caption-less.

    2024-08-14 18:32                  ← file mtime (no EXIF for a generic file)
    talks · 2024                      ← folder description (path segments)
    #talks #2024                      ← root base tags + segments + .tags sidecars

Pure: delegates the date to `_compose.display_date` and the two folder lines to
`_compose.folder_lines`, so a generic file tags identically to a photo in the
same folder.
"""

from __future__ import annotations

from ..tags import TagResolver
from ._compose import display_date, folder_lines


def compose_generic_caption(
    path,
    root_path,
    *,
    resolver:  TagResolver | None = None,
    base_tags: "list[str] | tuple[str, ...]" = (),
) -> str:
    """Compose a folder-taxonomy caption for a non-photo, non-book file: mtime
    date line + the shared description/hashtag folder lines. Empties omitted."""
    lines: list[str] = []

    ts = display_date(path)
    if ts:
        lines.append(ts)

    lines += folder_lines(path, root_path, resolver=resolver, base_tags=base_tags)
    return "\n".join(lines)
