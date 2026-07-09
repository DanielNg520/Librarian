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
