"""
librarian.backends.rclone
──────────────────────────
One wrapper over the `rclone` CLI → every cloud rclone speaks (Google Drive,
Box, Dropbox, S3, …). Adding a provider is an `rclone config` remote + a routing
line, never new code here. Hash-verifying (`rclone hashsum`), so it counts as a
DURABLE backend for the offload gate.

A `remote` is an rclone target like `gdrive:` or `box:Backups`, OR a plain local
path (rclone treats a colon-less argument as a local filesystem path — handy for
testing the wrapper with no cloud config). Objects are stored content-addressed
under `<remote>/<base>/<hash[:2]>/<hash>`.

Startup guard: the `rclone` binary must be on PATH, else BackendUnavailable —
fail loud rather than silently skip a backup.

Independent of the suite (no `import core`); see DESIGN §0.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from .base import BackendError, BackendUnavailable, Locator

log = logging.getLogger(__name__)

_TIMEOUT_S = 3600          # large files / slow links; the backup pass is async


class RcloneBackend:
    durable = True

    def __init__(self, remote: str, *, base: str = "librarian",
                 name: str = "rclone", rclone_bin: str | None = None) -> None:
        self.name = name
        self._remote = remote.rstrip("/")
        self._base = base.strip("/")
        self._bin = rclone_bin or shutil.which("rclone")
        if not self._bin:
            raise BackendUnavailable(
                "rclone not found on PATH — install it (e.g. `brew install "
                "rclone`) and configure a remote, or remove this backend from "
                "your routing.")

    # rclone joins a remote ('gdrive:') and a path without a separating slash;
    # a local path ('/tmp/x') joins with one. Normalize both.
    def _ref_for(self, content_hash: str) -> str:
        sub = f"{self._base}/{content_hash[:2]}/{content_hash}"
        if self._remote.endswith(":"):
            return f"{self._remote}{sub}"
        return f"{self._remote}/{sub}"

    def _run(self, *args: str, timeout: int = _TIMEOUT_S) -> subprocess.CompletedProcess:
        try:
            return subprocess.run([self._bin, *args], capture_output=True,
                                  text=True, timeout=timeout, check=True)
        except subprocess.CalledProcessError as e:
            raise BackendError(
                f"rclone {args[0]} failed (rc={e.returncode}): "
                f"{(e.stderr or '').strip()}") from e
        except subprocess.TimeoutExpired as e:
            raise BackendError(f"rclone {args[0]} timed out") from e

    def store(self, path: Path, content_hash: str, *,
              caption: str | None = None) -> Locator:
        # Durable cloud copy = bytes only; `caption` (fast-tier text) is ignored.
        ref = self._ref_for(content_hash)
        # copyto = copy a single file to an explicit destination object.
        self._run("copyto", str(Path(path)), ref)
        return Locator(self.name, ref)

    def fetch(self, locator: Locator, dest: Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._run("copyto", locator.ref, str(dest))
        return dest

    def verify(self, locator: Locator, content_hash: str) -> bool:
        if not self.exists(locator):
            return False
        cp = self._run("hashsum", "SHA-256", locator.ref, timeout=_TIMEOUT_S)
        # Output: "<hexhash>  <name>" (possibly multiple lines); match our hash.
        for line in cp.stdout.splitlines():
            tok = line.strip().split()
            if tok and tok[0].lower() == content_hash.lower():
                return True
        return False

    def exists(self, locator: Locator) -> bool:
        # lsf lists the leaf name if the object exists; empty/err → absent.
        try:
            cp = self._run("lsf", locator.ref, timeout=120)
        except BackendError:
            return False
        return bool(cp.stdout.strip())
