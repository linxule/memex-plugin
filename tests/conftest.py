from __future__ import annotations

import pytest

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _no_lock_wait(monkeypatch):
    """Contention tests assert exit 3; don't sit through the operator-facing wait."""
    from memex import db_utils

    monkeypatch.setattr(db_utils, "LOCK_WAIT_SECONDS", 0.0)
