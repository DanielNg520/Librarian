"""
librarian.backends.registry
────────────────────────────
Name → backend instance, built once per process. Routing (librarian.routing)
decides WHICH backends a file goes to by name; the registry resolves a name to
the concrete StorageBackend that knows HOW.

Phase 3 exposes dependency-injection construction (`Registry({...})`) so the
backup pass and tests wire whatever backends they want. A `from_config` factory
that instantiates real Local/Rclone/Telegram backends from creds arrives with
the backup pass (Phase 4).

Independent of the suite (no `import core`); see DESIGN §0.
"""

from __future__ import annotations

from .base import BackendError, StorageBackend


class Registry:
    def __init__(self, backends: dict[str, StorageBackend] | None = None) -> None:
        self._backends: dict[str, StorageBackend] = dict(backends or {})

    def register(self, backend: StorageBackend) -> None:
        self._backends[backend.name] = backend

    def get(self, name: str) -> StorageBackend:
        try:
            return self._backends[name]
        except KeyError:
            raise BackendError(
                f"no backend registered as {name!r} "
                f"(have: {sorted(self._backends)})") from None

    def has(self, name: str) -> bool:
        return name in self._backends

    def names(self) -> list[str]:
        return sorted(self._backends)

    def available(self, names: "list[str] | tuple[str, ...]") -> list[str]:
        """Filter `names` to those actually registered, preserving order. Lets
        the backup pass skip a routed-but-unconfigured backend rather than crash
        (it stays pending for that backend, retried when configured)."""
        return [n for n in names if n in self._backends]

    def is_durable(self, name: str) -> bool:
        """True iff the backend hash-verifies its objects (gates offload). A
        backend marks this via a `durable` attribute; default False."""
        return bool(getattr(self._backends.get(name), "durable", False))
