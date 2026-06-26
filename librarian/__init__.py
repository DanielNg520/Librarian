"""
librarian
─────────
Independent systemwide file manager. Backs up the user's own folders to multiple
durable backends (routed by filetype), keeps Telegram as a fast-access tier,
reclaims local disk once a durable copy is verified, and retrieves on demand.

INDEPENDENCE CONTRACT: this package does NOT import the Media Archiver Suite
(`core`, `archiver`, `recorder`, `dispatcher`). The suite is a source of
*copyable patterns* only; reused code is vendored here and adapted. See
`librarian/DESIGN.md` §0.
"""

from __future__ import annotations

__version__ = "0.0.1"
