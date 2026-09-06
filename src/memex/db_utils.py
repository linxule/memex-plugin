"""Shared sqlite connection helpers for the memex index.

Centralizes the WAL + busy_timeout connection pattern and sqlite-vec
extension loading so every module that touches `_index.sqlite` behaves
consistently under concurrent access.

Background (2026-04-21): writers like `memex index rebuild` and
`memex backfill obs --stdin` used to collide under sqlite's default
DELETE journal mode — "database is locked" errors. Standardizing on
WAL + busy_timeout=10s fixes that. Read paths don't strictly need it
but it doesn't hurt and keeps every module honest.
"""
from __future__ import annotations

import fcntl
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path


def connect_index(index_path: Path) -> sqlite3.Connection:
    """Open a sqlite connection with WAL + busy_timeout applied.

    Use this everywhere we touch `_index.sqlite`. WAL is a persistent
    database property — the first caller sets it, subsequent calls are
    no-ops. busy_timeout applies per-connection and expires per-call
    so every caller needs to set it locally.
    """
    conn = sqlite3.connect(index_path, timeout=10.0)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 10000")
    except BaseException:
        # Ownership transfers to the caller only after setup succeeds.
        conn.close()
        raise
    return conn


def load_vec_extension(conn: sqlite3.Connection) -> bool:
    """Load sqlite-vec into `conn`. Returns True on success, False if the
    extension is unavailable. Silent on failure — callers inspect the
    return value and decide whether to warn.

    vec_* virtual tables require the extension to be loaded in the
    connection that queries them — loading once globally is not enough.

    enable_load_extension(True) is a dangerous state (allows arbitrary
    dylib loads); we always clear it with try/finally, even when the
    load itself throws. Without this, a failed sqlite_vec.load would
    leave the connection open to further extension loads for its lifetime.
    """
    try:
        import sqlite_vec
    except ImportError:
        return False
    try:
        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            # Always clear the privileged state, even on load failure.
            conn.enable_load_extension(False)
        return True
    except Exception:  # noqa: BLE001 — we intentionally absorb load errors
        return False


# Default bounded wait before a contended lock gives up with exit 3. Shared
# writers (a memo subagent storing observations) hold the lock for seconds;
# an incremental rebuild that fails instantly on that overlap turns a routine
# skill step into a spurious error. Rebuilds hold it for minutes, so waiting
# longer than this rarely helps and would stall hooks past their timeouts.
LOCK_WAIT_SECONDS = 30.0
_LOCK_POLL_SECONDS = 0.25


@contextmanager
def _index_lock(mode: int, contention_message: str, timeout: float | None = None):
    # Resolved at call time so tests (and operators) can override the module
    # attribute without re-binding every caller's default.
    if timeout is None:
        timeout = LOCK_WAIT_SECONDS
    lock_dir = Path.home() / ".memex" / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    # Keep the file in place: unlinking it lets new callers lock a different
    # inode while existing callers still hold the original lock.
    lock_path = lock_dir / "full-rebuild.lock"
    with lock_path.open("a") as lock_file:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                fcntl.flock(lock_file.fileno(), mode | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    print(contention_message, file=sys.stderr)
                    sys.exit(3)
                time.sleep(_LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def rebuild_lock(timeout: float | None = None):
    """Serialize index rebuilds and exclude direct observation writers.

    Both full and incremental rebuild entry points hold LOCK_EX for their
    complete operation, so an atomic index replacement cannot discard a
    concurrent rebuild or observation write. Waits up to `timeout` seconds
    for a shared writer to finish, then exits with code 3 if another
    rebuild or writer still holds the lockfile.
    """
    with _index_lock(
        fcntl.LOCK_EX,
        "Error: another index rebuild or observation write is currently running. "
        "Retry after it completes.",
        timeout=timeout,
    ):
        yield


@contextmanager
def writer_lock(timeout: float | None = None):
    """Acquire LOCK_SH on the full-rebuild lockfile.

    Any code path that writes observations into `_index.sqlite` directly
    (i.e., not via `index_document` inside a rebuild that already holds
    LOCK_EX) must hold LOCK_SH for the duration of the write so that a
    concurrent `memex index rebuild --full` cannot ATTACH-snapshot a
    moving target and silently lose post-snapshot rows on atomic swap.

    Exits with code 3 (matches the rebuild CLI's exit code) when a
    rebuild already holds LOCK_EX so chained scripts / hooks see a
    consistent signal. Multiple writers can hold LOCK_SH simultaneously
    — they only contend with the exclusive rebuild lock, not each other.

    Callers:
    - `memex backfill obs --stdin` (extract.py::main)
    - `memex.dreamer` (store_observations inside _run_dreamer_sync)
    - `memex index embed-missing` (inserts vec rows keyed by reusable chunk ids)
    - Any future direct `store_observations` writer
    """
    with _index_lock(
        fcntl.LOCK_SH,
        "Error: an index rebuild is currently running. "
        "Retry after the rebuild completes.",
        timeout=timeout,
    ):
        yield
