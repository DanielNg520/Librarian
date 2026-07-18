"""
librarian.backends.local
─────────────────────────
A content-addressed backend that stores bytes under a local directory tree —
the "external disk / NAS" tier, and the reference implementation every other
backend is measured against. Fully hash-verifying, so it counts as a DURABLE
backend for the offload gate.

Layout: <root>/<hash[:2]>/<hash> — content-addressed, so identical bytes land
once and `store` is naturally idempotent.

Independent of the suite (no `import core`); see DESIGN §0.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from ..hashing import full_hash
from .base import BackendError, Locator

log = logging.getLogger(__name__)


class LocalBackend:
    durable = True                       # hashes its objects → gates offload

    def __init__(self, root: str | Path, name: str = "local") -> None:
        self.name = name
        self._root = Path(root).expanduser()
        self._root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, content_hash: str) -> Path:
        return self._root / content_hash[:2] / content_hash

    def store(self, path: Path, content_hash: str, *,
              caption: str | None = None) -> Locator:
        # `caption` is a fast-access-tier concern; a durable content-addressed
        # store keeps only the bytes. Accepted for a uniform seam, ignored here.
        path = Path(path)
        dest = self._path_for(content_hash)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():                    # content-addressed → idempotent
                tmp = dest.with_suffix(".part")
                shutil.copy2(path, tmp)
                tmp.replace(dest)                    # atomic publish
        except OSError as e:
            raise BackendError(f"local store failed for {path}: {e}") from e
        return Locator(self.name, str(dest))

    def fetch(self, locator: Locator, dest: Path) -> Path:
        src = Path(locator.ref)
        dest = Path(dest)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        except OSError as e:
            raise BackendError(f"local fetch failed for {locator}: {e}") from e
        return dest

    def verify(self, locator: Locator, content_hash: str) -> bool:
        p = Path(locator.ref)
        if not p.exists():
            return False
        return full_hash(p) == content_hash          # byte-integrity, not just presence

    def exists(self, locator: Locator) -> bool:
        return Path(locator.ref).exists()
