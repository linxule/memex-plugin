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


# v0.22.0: end-to-end hook runs against a throwaway home (tests/hook_helpers.py)
from hook_helpers import hook_env  # noqa: E402,F401


@pytest.fixture(autouse=True)
def _hook_log_to_tmp(tmp_path, monkeypatch):
    """memex's logger writes to ~/.memex/logs/plugin-<date>.log, resolved from the
    REAL home whatever vault a test uses, so the suite used to append test
    warnings to the owner's hook log (38 lines in one wave-1 run, 2026-10-28).
    Point the cached logger at a per-test file instead."""
    import logging

    from memex.scripts import utils

    logger = logging.getLogger(f"memex-test-{id(tmp_path)}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = logging.FileHandler(tmp_path / "memex-test.log")
    logger.addHandler(handler)
    monkeypatch.setattr(utils, "_logger", logger)
    yield
    logger.removeHandler(handler)
    handler.close()
