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




# The owner's live state dir, captured before any test can patch HOME.
_LIVE_STATE_DIR = (Path.home() / ".memex").resolve()


@pytest.fixture(autouse=True)
def _no_live_state_dir(tmp_path, monkeypatch):
    """memex keeps locks, state.json and pending-memo signals in the configured
    state dir, which is the owner's ~/.memex for any test that patches neither
    HOME nor MEMEX_STATE_DIR. Such tests used to take the LIVE full-rebuild
    lock (failing with exit 3 whenever a real session or the hourly cloud sync
    held it, and able to stall a real `backfill obs`) and to lock the live
    state.json: 25 tests on 2026-10-28, measured by recording every flock.
    A state dir that resolves to the live one is swapped for a per-test one;
    tests that chose their own HOME or state dir keep it. Covers call-time
    lookups (locks, utils' state.json, pending-memos) and aliases modules bound
    at import. It redirects rather than fails: tests that rely on the default
    resolution don't care where state lives, only that it is not the owner's."""
    from memex import paths

    resolve = paths.get_state_dir

    def guarded(settings=None):
        state_dir = resolve(settings)
        if state_dir.resolve() == _LIVE_STATE_DIR:
            state_dir = tmp_path / "state-home"
            state_dir.mkdir(exist_ok=True)
        return state_dir

    monkeypatch.setattr(paths, "get_state_dir", guarded)
    # Modules that bound the resolver at import (`from memex.paths import
    # get_state_dir`, e.g. backfill_has_memo) would bypass the patch above.
    for module in list(sys.modules.values()):
        if getattr(module, "__dict__", {}).get("get_state_dir") is resolve:
            monkeypatch.setattr(module, "get_state_dir", guarded)

    # The index lock is fixed at ~/.memex/locks (paths.get_index_lock_dir), not
    # in the state dir: guard it the same way. Subprocess tests must set HOME.
    resolve_lock = paths.get_index_lock_dir

    def guarded_lock():
        # Judge the path before resolving: resolve_lock() mkdirs its folder.
        if (Path.home() / ".memex" / "locks").resolve() == _LIVE_STATE_DIR / "locks":
            lock_dir = tmp_path / "state-home" / "locks"
            lock_dir.mkdir(parents=True, exist_ok=True)
            return lock_dir
        return resolve_lock()

    monkeypatch.setattr(paths, "get_index_lock_dir", guarded_lock)

    # state.json is fixed at ~/.memex/state.json too (utils.get_state_file).
    from memex.scripts import utils as script_utils

    resolve_state_file = script_utils.get_state_file

    def guarded_state_file():
        if (Path.home() / ".memex").resolve() == _LIVE_STATE_DIR:
            state_home = tmp_path / "state-home"
            state_home.mkdir(exist_ok=True)
            return state_home / "state.json"
        return resolve_state_file()

    for module in list(sys.modules.values()):
        if getattr(module, "__dict__", {}).get("get_state_file") is resolve_state_file:
            monkeypatch.setattr(module, "get_state_file", guarded_state_file)


@pytest.fixture(autouse=True)
def _fresh_settings():
    """get_settings() caches by config path only, so a test that sets an env
    override (MEMEX_STATE_DIR) after an earlier test loaded settings got the
    stale cached state dir, while a child process it spawned honoured the env:
    test_rebuild_lock_waits_for_shared_writer_to_finish passed alone and
    failed in the suite. Each test starts and ends with an empty cache."""
    from memex.config import reset_settings

    reset_settings()
    yield
    reset_settings()
