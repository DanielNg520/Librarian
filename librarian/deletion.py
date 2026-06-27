"""
librarian.deletion
───────────────────
The ONE chokepoint every local-file deletion funnels through. Today the only
caller is the offload pass, but routing all unlinks through here means a future
protection policy (shield a root from offload, a global pause switch) is added in
ONE place, and call sites stay declarative ("delete this, because <reason>").

Vendored in spirit from the suite's core.deletion.DeletionGuard (copied, not
imported; see DESIGN §0), reduced to Librarian's needs: a single optional
`protect` predicate instead of the suite's PolicyStore-backed ProtectionPolicy.
Never raises — a refused or failed delete returns False so the caller leaves the
item where it is rather than corrupting state.

SAFETY NOTE: the guard does NOT know whether a durable backup exists — that gate
lives in the offload pass, which must verify a durable copy IMMEDIATELY before
calling delete(). The guard is the mechanism; the offload pass is the policy.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


class DeletionGuard:
    def __init__(self, *, protect: Callable[[Path], bool] | None = None) -> None:
        """`protect(path) -> True` shields a path from deletion (refused, logged).
        Default: nothing protected."""
        self._protect = protect

    def delete(self, path: str | Path, *, reason: str) -> bool:
        """Unlink `path`. Returns True iff the file is gone afterwards. A
        protected path is refused (False); a missing file counts as success
        (idempotent — a re-run after a crash mid-delete still converges)."""
        p = Path(path)
        if self._protect is not None and self._protect(p):
            log.warning("deletion: REFUSED %s (%s) — protected", p, reason)
            return False
        try:
            p.unlink(missing_ok=True)
            log.info("deletion: removed %s (%s)", p.name, reason)
            return True
        except OSError as e:
            log.error("deletion: failed to remove %s: %s", p, e)
            return False
