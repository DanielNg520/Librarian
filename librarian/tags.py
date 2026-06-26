r"""
librarian.tags
──────────────
Folder-authored hashtags → appended to the caption Telegram shows. The additive
sibling of a banned-word sanitizer (one STRIPS configured words, this ADDS
configured hashtags), applied at Librarian's send-time caption seam.

Design DNA (copied, not imported) from the suite's `core.sanitize`
(ReloadingSanitizer's mtime-reload idea) and `dispatcher._append_filetype_tag`
(append-a-hashtag-to-a-caption idea). Pure: no DB, no network — fully testable
in isolation, drops into the send path later.

SOURCE — a `.tags` sidecar in any folder under a registered root:
  - one or more WHITESPACE-SEPARATED tags per line (so `#beach #sunset` works),
  - a leading `#` is optional,
  - blank lines and lines starting with `//` are ignored (notes/comments).

INHERITANCE ("layers") — a file inherits the UNION of `.tags` from its own folder
and every ANCESTOR up to (and including) the registered ROOT, OUTERMOST FIRST.
Tags on a parent apply to everything beneath it; nearer folders ADD, they never
override. Order-stable, de-duplicated.

SLUG RULE (Telegram-correct) — a Telegram hashtag is `[A-Za-z0-9_]` and TERMINATES
at the first `-`. So we: lowercase, replace every run of non-alphanumeric with a
single `_` (`"bathroom selfie"` → `"bathroom_selfie"`), NEVER emit `-`, trim edge
`_`, and drop anything empty or all-digits (Telegram doesn't treat `#123` as a
tag). Non-ASCII letters are folded out (conservative; revisit if needed).

APPLY — append a final `#a #b #c` line to the caption, skipping any tag the
caption ALREADY carries (word-boundary, case-insensitive) so it's never doubled.
No tags, or no/relative root → caption returned UNCHANGED (the feature costs
nothing when unconfigured).

SAFETY — the inheritance walk STOPS at the registered root and never reads a
`.tags` outside it; a file not under the root yields no tags. This guarantees we
can never pull hashtags from an unexpected place on disk.

CACHING — a `.tags` file is re-read only when its mtime moves (one stat per
folder per lookup, negligible beside the network send it precedes), mirroring the
suite's ReloadingSanitizer so a hand-edit takes effect without a restart.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

TAGS_FILENAME = ".tags"
_COMMENT_PREFIX = "//"
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify_tag(token: str | None) -> str | None:
    """Turn one raw token into a Telegram-safe hashtag body (no leading `#`), or
    None if nothing usable remains. Lowercase → non-alnum runs to `_` → trim `_`
    → reject empty/all-digit."""
    if not token:
        return None
    s = token.strip().lstrip("#").lower()
    s = _NON_ALNUM.sub("_", s).strip("_")
    if not s or s.isdigit():
        return None
    return s


def parse_tags_file(path: "str | Path") -> list[str]:
    """Read a `.tags` sidecar → ordered, de-duplicated slugs. One or more
    whitespace-separated tags per line; blank lines and `//` comments skipped.
    A missing/unreadable file → [] (feature simply off for that folder)."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        line = line.strip()
        if not line or line.startswith(_COMMENT_PREFIX):
            continue
        for token in line.split():
            tag = slugify_tag(token)
            if tag and tag not in seen:
                seen.add(tag)
                out.append(tag)
    return out


class TagResolver:
    """Resolves the inherited `.tags` for a file and appends them to a caption.

    Built once with a ROOT boundary (the registered folder root). The walk never
    climbs above `root`. A None/empty/relative root makes the resolver a no-op
    (falsy), so an unconfigured Librarian adds nothing and reads nothing.

    Single-threaded use assumed (one send loop), matching the suite's drain.
    """

    def __init__(self, root: "str | Path | None") -> None:
        self._root: Path | None = None
        if root:
            p = Path(root).expanduser()
            if p.is_absolute():
                # resolve() so the under-root check uses canonical paths.
                self._root = p.resolve()
            else:
                log.warning("tags: ignoring non-absolute root %r (resolver is a "
                            "no-op)", str(root))
        # dir -> (mtime_or_None, tuple_of_tags)
        self._cache: dict[Path, tuple[float | None, tuple[str, ...]]] = {}

    def __bool__(self) -> bool:
        return self._root is not None

    def _dir_tags(self, directory: Path) -> list[str]:
        """Tags declared by THIS folder's `.tags`, mtime-cached."""
        f = directory / TAGS_FILENAME
        try:
            m: float | None = f.stat().st_mtime
        except OSError:
            m = None
        cached = self._cache.get(directory)
        if cached is None or cached[0] != m:
            tags = tuple(parse_tags_file(f)) if m is not None else ()
            self._cache[directory] = (m, tags)
            return list(tags)
        return list(cached[1])

    def tags_for(self, file_path: "str | Path") -> list[str]:
        """Inherited tags for `file_path`: union of `.tags` from the file's folder
        up to the root, OUTERMOST FIRST, de-duplicated. [] when no root, or when
        the file is not under the root."""
        if self._root is None:
            return []
        p = Path(file_path).expanduser().resolve()
        try:
            p.relative_to(self._root)
        except ValueError:
            return []                      # outside the root → never read its tags
        # Ancestor folders from the file's directory up to (and incl.) the root.
        dirs: list[Path] = []
        d = p.parent
        while True:
            dirs.append(d)
            if d == self._root or d == d.parent:
                break
            d = d.parent
        dirs.reverse()                     # root first → innermost last (layers)
        out: list[str] = []
        seen: set[str] = set()
        for directory in dirs:
            for tag in self._dir_tags(directory):
                if tag not in seen:
                    seen.add(tag)
                    out.append(tag)
        return out

    def apply(self, caption: str | None, file_path: "str | Path") -> str | None:
        """Append the file's inherited hashtags to `caption` as a trailing line,
        skipping any already present. Returns `caption` unchanged when there are
        no tags to add; returns the tag line alone when `caption` is empty."""
        tags = self.tags_for(file_path)
        if not tags:
            return caption
        present = {w.lower() for w in (caption or "").split()}
        fresh = [t for t in tags if f"#{t}" not in present]
        if not fresh:
            return caption
        line = " ".join(f"#{t}" for t in fresh)
        return f"{caption}\n{line}" if caption else line
