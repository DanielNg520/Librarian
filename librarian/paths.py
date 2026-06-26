"""
librarian.paths
───────────────
Where Librarian keeps its own database and config — a namespace fully separate
from the Media Archiver Suite (the suite uses ~/.config/archiver-suite/suite.db;
Librarian never touches it). Override the DB with $LIBRARIAN_DB for tests.

Independent of the suite (no `import core`); see librarian/DESIGN.md §0.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_DB_PATH = "~/.config/librarian/librarian.db"


def db_path() -> Path:
    return Path(os.environ.get("LIBRARIAN_DB", DEFAULT_DB_PATH)).expanduser()


def config_dir() -> Path:
    return Path("~/.config/librarian").expanduser()
