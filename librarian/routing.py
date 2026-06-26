"""
librarian.routing
──────────────────
Filetype → backend routing. A file's media bucket (photo / video / document /
audio / other) selects the ORDERED list of backends it's backed up to. This is
where "videos to Google Drive + Telegram, books to Box + Telegram" is expressed.

Config: `[backup.routing]` in librarian's config.toml (read with stdlib tomllib
— no TOML dependency), e.g.

    [backup.routing]
    default  = ["gdrive", "telegram"]
    photo    = ["box", "telegram"]
    video    = ["gdrive", "telegram"]
    document = ["gdrive", "telegram"]

A missing file / section → DEFAULT_ROUTING. Backend NAMES here must match the
registry (librarian.backends.registry); routing decides *which*, the registry
provides *how*.

Independent of the suite (no `import core`); see DESIGN §0.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path

from .captioning import PHOTO_EXTENSIONS
from .paths import config_path

log = logging.getLogger(__name__)

_VIDEO = frozenset({".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".ts",
                    ".flv", ".wmv"})
_DOCUMENT = frozenset({".pdf", ".epub", ".mobi", ".azw3", ".djvu", ".doc",
                       ".docx", ".txt", ".rtf", ".odt"})
_AUDIO = frozenset({".mp3", ".flac", ".m4a", ".wav", ".ogg", ".aac", ".opus"})

# Telegram = fast-access tier; a durable cloud copy is the integrity-bearing one.
DEFAULT_ROUTING: dict[str, list[str]] = {
    "default": ["gdrive", "telegram"],
}


def bucket(path: str | Path) -> str:
    """Classify a file by extension: photo / video / document / audio / other."""
    ext = Path(path).suffix.lower()
    if ext in PHOTO_EXTENSIONS:
        return "photo"
    if ext in _VIDEO:
        return "video"
    if ext in _DOCUMENT:
        return "document"
    if ext in _AUDIO:
        return "audio"
    return "other"


class RoutingPolicy:
    """Maps a file's bucket → ordered backend names. Falls back to `default`."""

    def __init__(self, table: dict[str, list[str]] | None = None) -> None:
        self._table = dict(DEFAULT_ROUTING)
        if table:
            self._table.update({k: list(v) for k, v in table.items()})
        self._table.setdefault("default", list(DEFAULT_ROUTING["default"]))

    def backends_for(self, path: str | Path) -> list[str]:
        return list(self._table.get(bucket(path), self._table["default"]))

    @property
    def default(self) -> list[str]:
        return list(self._table["default"])

    @classmethod
    def load(cls, path: str | Path | None = None) -> "RoutingPolicy":
        """Load `[backup.routing]` from config.toml; missing/invalid → defaults."""
        p = Path(path).expanduser() if path is not None else config_path()
        if not p.exists():
            return cls()
        try:
            with p.open("rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError) as e:
            log.warning("routing: could not read %s (%s); using defaults", p, e)
            return cls()
        table = (data.get("backup", {}) or {}).get("routing", {}) or {}
        # Coerce to {str: [str]}, ignore malformed entries.
        clean = {k: list(v) for k, v in table.items()
                 if isinstance(v, (list, tuple))}
        return cls(clean)
