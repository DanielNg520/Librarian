"""
pytest bridge for the standalone test scripts.

Each tests/test_*.py is a self-contained script (run `python3 tests/test_X.py`),
but several take (db, work) parameters from their own main(). These fixtures
supply the same values under pytest so `pytest` and the scripts stay equivalent.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def db(tmp_path: Path) -> str:
    """Path to a fresh, per-test librarian.db."""
    return str(tmp_path / "librarian.db")


@pytest.fixture
def work(tmp_path: Path) -> Path:
    """Per-test scratch folder for files under ingest."""
    return tmp_path / "work"


# ── Phase 9 (test_icloud) injection fixtures ─────────────────────────────────
# The standalone script builds these itself in main(); under pytest we supply
# the SAME shapes so both runners stay equivalent. `patch_attr` is deliberately
# NOT named `monkeypatch` — it is a plain callable (obj, name, value), a
# different API from pytest's builtin fixture.

class _AttrPatcher:
    def __init__(self):
        self._undo = []

    def __call__(self, obj, name, val):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, val)

    def restore(self):
        for obj, name, val in reversed(self._undo):
            setattr(obj, name, val)


@pytest.fixture
def patch_attr():
    """Callable `(obj, name, value)` that patches an attribute and restores it
    after the test."""
    p = _AttrPatcher()
    yield p
    p.restore()


@pytest.fixture
def monkeypatch_state(patch_attr):
    """Callable `({Path: ICloudState})` that fakes `icloud.placeholder_state`
    for the given paths (everything else reports MATERIALIZED)."""
    from pathlib import Path as _P

    from librarian import icloud

    def apply(mapping):
        def fake(path, **kw):
            return mapping.get(_P(path), icloud.ICloudState.MATERIALIZED)
        patch_attr(icloud, "placeholder_state", fake)
    return apply
