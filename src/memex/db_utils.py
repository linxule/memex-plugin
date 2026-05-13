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
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 10000")
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


@contextmanager
def writer_lock():
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
    - Any future direct `store_observations` writer
    """
    lock_dir = Path.home() / ".memex" / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "full-rebuild.lock"
    f = lock_path.open("w")
    try:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            print(
                "Error: `memex index rebuild --full` is currently running. "
                "Refusing to write observations into an index that is about "
                "to be atomically swapped (would be silently lost on swap). "
                "Retry after the rebuild completes.",
                file=sys.stderr,
            )
            sys.exit(3)
        yield
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()
