"""
librarian.deletion
───────────────────
The ONE chokepoint every local-file deletion funnels through. Today the only
caller is the offload pass, but routing all unlinks through here means a future
protection policy (shield a root from offload, a global pause switch) is added in
ONE place, and call sites stay declarative ("delete this, because <reason>").

Vendored in spirit from the suite's core.deletion.DeletionGuard (copied, not
imported; see DESIGN §0). Phase 8 grows Librarian's own `ProtectionPolicy` —
config-driven (`[protect]` in config.toml: a global `pause` switch + protected
root names / path prefixes), read the same reload-on-load way `routing.py` reads
config, with NO separate PolicyStore process. It is a plain callable so it drops
straight into the guard's `protect` seam.

Never raises — a refused or failed delete returns False so the caller leaves the
item where it is rather than corrupting state.

SAFETY NOTE: the guard does NOT know whether a durable backup exists — that gate
lives in the offload pass, which must verify a durable copy IMMEDIATELY before
calling delete(). The guard is the mechanism; offload/dedup are the policy.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Callable

from .paths import config_path

log = logging.getLogger(__name__)


def _is_under(path: Path, prefix: Path) -> bool:
    """True iff `path` is `prefix` or lives beneath it (both already resolved)."""
    try:
        path.relative_to(prefix)
        return True
    except ValueError:
        return False


class ProtectionPolicy:
    """A config-driven shield answering "may this path be deleted?". A global
    `pause` blocks EVERY delete; otherwise a path is protected iff it lives under
    any protected prefix (a registered root's folder, or an explicit path). Plain
    callable: `policy(path) -> bool`, so it slots into `DeletionGuard(protect=…)`.

    Empty/default → nothing protected (a bare Librarian deletes normally)."""

    def __init__(self, *, pause: bool = False,
                 protected_prefixes: "list[str | Path] | tuple" = ()) -> None:
        self.pause = pause
        self._prefixes: list[Path] = []
        for pre in protected_prefixes:
            p = Path(pre).expanduser()
            if p.is_absolute():
                self._prefixes.append(p.resolve())
            else:
                log.warning("protect: ignoring non-absolute prefix %r", str(pre))

    def is_protected(self, path: str | Path) -> bool:
        if self.pause:
            return True
        p = Path(path).expanduser().resolve()
        return any(_is_under(p, pre) for pre in self._prefixes)

    __call__ = is_protected

    @classmethod
    def load(cls, store=None, path: str | Path | None = None) -> "ProtectionPolicy":
        """Build from `[protect]` in config.toml (missing/invalid → nothing
        protected). Keys: `pause` (bool), `paths` (list of absolute path
        prefixes), `roots` (list of registered root NAMES → resolved to their
        folders via `store`, if given). Mirrors `RoutingPolicy.load`."""
        p = Path(path).expanduser() if path is not None else config_path()
        if not p.exists():
            return cls()
        try:
            with p.open("rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError) as e:
            log.warning("protect: could not read %s (%s); nothing protected", p, e)
            return cls()
        sect = (data.get("protect", {}) or {})
        pause = bool(sect.get("pause", False))
        prefixes: list[str | Path] = [
            x for x in (sect.get("paths") or []) if isinstance(x, str)]
        if store is not None:
            for name in (sect.get("roots") or []):
                root = store.get_root(name) if isinstance(name, str) else None
                if root and root.get("path"):
                    prefixes.append(root["path"])
                elif isinstance(name, str):
                    log.warning("protect: unknown root %r in [protect].roots", name)
        return cls(pause=pause, protected_prefixes=prefixes)


class DeletionGuard:
    def __init__(self, *, protect: Callable[[Path], bool] | None = None,
                 policy: ProtectionPolicy | None = None) -> None:
        """`protect(path) -> True` (or a `ProtectionPolicy` passed as `policy`)
        shields a path from deletion (refused, logged). Passing both is fine; a
        path protected by EITHER is refused. Default: nothing protected."""
        self._protect = protect
        self._policy = policy

    def _shielded(self, p: Path) -> str | None:
        """Return a human reason the path is shielded, or None if deletable."""
        if self._policy is not None:
            if self._policy.pause:
                return "deletions paused"
            if self._policy.is_protected(p):
                return "protected root/path"
        if self._protect is not None and self._protect(p):
            return "protected"
        return None

    def delete(self, path: str | Path, *, reason: str) -> bool:
        """Unlink `path`. Returns True iff the file is gone afterwards. A
        protected path is refused (False); a missing file counts as success
        (idempotent — a re-run after a crash mid-delete still converges)."""
        p = Path(path)
        shield = self._shielded(p)
        if shield is not None:
            log.warning("deletion: REFUSED %s (%s) — %s", p, reason, shield)
            return False
        try:
            p.unlink(missing_ok=True)
            log.info("deletion: removed %s (%s)", p.name, reason)
            return True
        except OSError as e:
            log.error("deletion: failed to remove %s: %s", p, e)
            return False
