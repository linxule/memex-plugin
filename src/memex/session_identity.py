"""Which agent session is running this command (``memex session whoami``, v0.22.0).

A memo is matched to its session by ``session_id:`` (plus ``harness:``), so every
agent that writes its own memo needs its own id, and environment variables alone
can't be trusted for that: a session inherits its parent's. A Kimi Code session
started from a Claude Code shell carries ``CLAUDE_CODE_SESSION_ID``; a Claude
seat started by Codex carries ``CODEX_THREAD_ID`` (both seen on 2026-10-28). So
the harness is the NEAREST one in this process's ancestry, and its id comes from
that harness's own source:

- Claude Code: ``CLAUDE_CODE_SESSION_ID`` (each Claude process sets its own).
- Codex: ``CODEX_THREAD_ID`` (each Codex thread sets its own; verified with
  ``codex exec``, 2026-10-28).
- Kimi Code: no variable exists; the session whose lock file
  (``~/.kimi-code/locks/sessions/session_<uuid>.lock``) names the calling
  ``kimi-code`` process's pid — exact, even with several sessions in one cwd —
  else the newest session whose work dir is this cwd. Bare UUID, as the Kimi
  adapter uses it.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

CLAUDE = "claude-code"
CODEX = "codex-desktop"
KIMI = "kimi-code"

_MAX_HOPS = 40


@dataclass(frozen=True)
class Identity:
    harness: str
    session_id: str
    source: str  # how it was found, for --json and error messages


def _ps(pid: int) -> tuple[int, str] | None:
    """(parent pid, command line) of ``pid``, or None."""
    try:
        out = subprocess.run(
            ["ps", "-o", "ppid=", "-o", "args=", "-p", str(pid)],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not out:
        return None
    ppid, _, args = out.partition(" ")
    try:
        return int(ppid), args.strip()
    except ValueError:
        return None


def classify(args: str) -> str | None:
    """The harness a process command line belongs to, or None."""
    low = args.lower()
    first = low.split(" ", 1)[0]
    base = first.rsplit("/", 1)[-1]
    # Kimi sets its process title to plain "kimi-code" (seen live, 2.3.3)
    if "kimi-code/dist/" in low or base in ("kimi", "kimi-code"):
        return KIMI
    if base == "codex" or "codex.app/" in low or "/codex app-server" in low or "@openai/codex" in low:
        return CODEX
    if base == "claude" or "/claude/versions/" in first:
        return CLAUDE
    return None


def nearest_harness_process(
    pid: int | None = None,
    ps: Callable[[int], tuple[int, str] | None] = _ps,
) -> tuple[str, int] | None:
    """(harness, pid of its process): the first harness walking up from ``pid``."""
    current = os.getpid() if pid is None else pid
    for _ in range(_MAX_HOPS):
        row = ps(current)
        if row is None:
            return None
        ppid, args = row
        harness = classify(args)
        if harness:
            return harness, current
        if ppid <= 1 or ppid == current:
            return None
        current = ppid
    return None


def nearest_harness(
    pid: int | None = None,
    ps: Callable[[int], tuple[int, str] | None] = _ps,
) -> str | None:
    """Walk up from ``pid`` (default: this process) to the first harness process."""
    found = nearest_harness_process(pid, ps)
    return found[0] if found else None


def _process_start_epoch(pid: int) -> float | None:
    """Wall-clock start of ``pid`` (seconds), from ``ps -o etime=``; None if unknown."""
    try:
        out = subprocess.run(["ps", "-o", "etime=", "-p", str(pid)], capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not out:
        return None
    days, _, clock = out.rpartition("-")
    parts = [int(x) for x in clock.split(":") if x.isdigit()]
    if not parts:
        return None
    while len(parts) < 3:
        parts.insert(0, 0)
    elapsed = (int(days) if days.isdigit() else 0) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]
    return time.time() - elapsed


def kimi_session_from_lock(
    kimi_pid: int,
    kimi_home: Path | None = None,
    *,
    hostname: str | None = None,
    process_start: float | None = None,
) -> tuple[str | None, bool]:
    """(bare UUID of the Kimi session whose lock names ``kimi_pid``, locks_present).

    A running Kimi Code session holds ``~/.kimi-code/locks/sessions/session_<uuid>.lock``
    (JSON: ``pid``, ``hostname``, ``startedAt`` ms; seen live, 2.3.3), so this is exact
    even when several Kimi sessions share one working directory. A lock left by a
    crashed session can carry a pid the OS has since reused, so a lock only counts
    when its host is this machine and it was taken no earlier than the process
    started; among several, the newest wins (gauntlet round 2). ``locks_present``
    tells the caller this Kimi keeps locks at all — then a miss means "unknown",
    never "guess from the cwd"."""
    locks = (kimi_home or Path.home() / ".kimi-code") / "locks" / "sessions"
    if not locks.is_dir():
        return None, False
    host = hostname if hostname is not None else socket.gethostname()
    started = process_start if process_start is not None else _process_start_epoch(kimi_pid)
    best: tuple[float, str] | None = None
    try:
        entries = list(locks.glob("session_*.lock"))
    except OSError:
        return None, True
    for lock in entries:
        try:
            data = json.loads(lock.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("pid") != kimi_pid:
            continue
        lock_host = data.get("hostname")
        # compare the bare host: a Mac's suffix (.home / .local) can change with
        # the network during a long session, which must not orphan a live lock
        if isinstance(lock_host, str) and lock_host.split(".")[0].lower() != host.split(".")[0].lower():
            continue
        taken = data.get("startedAt")
        taken_s = taken / 1000.0 if isinstance(taken, (int, float)) else None
        if started is not None and taken_s is not None and taken_s < started - 5:
            continue  # older than this process: a dead session's lock on a reused pid
        rank = taken_s if taken_s is not None else 0.0
        if best is None or rank > best[0]:
            sid = lock.name[: -len(".lock")]
            best = (rank, sid[len("session_"):] if sid.startswith("session_") else sid)
    return (best[1] if best else None), True

def _same_or_inside(cwd: Path, work_dir: str) -> bool:
    try:
        wd = Path(work_dir).expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    return cwd == wd or wd in cwd.parents


def kimi_session_for(cwd: str, kimi_home: Path | None = None) -> str | None:
    """Bare UUID of the most recently active Kimi session working in ``cwd``."""
    home = kimi_home or Path.home() / ".kimi-code"
    sessions = home / "sessions"
    work_dirs: dict[str, str] = {}
    try:
        with (home / "session_index.jsonl").open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                sid = row.get("sessionId") if isinstance(row, dict) else None
                if not isinstance(sid, str):
                    continue
                if row.get("deleted") is True:
                    work_dirs.pop(sid, None)
                elif isinstance(row.get("workDir"), str):
                    work_dirs[sid] = row["workDir"]
    except OSError:
        return None
    try:
        here = Path(cwd).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    best: tuple[float, str] | None = None
    for sid, wd in work_dirs.items():
        if not _same_or_inside(here, wd):
            continue
        for wire in sessions.glob(f"*/{sid}/agents/main/wire.jsonl"):
            try:
                mtime = wire.stat().st_mtime
            except OSError:
                continue
            if best is None or mtime > best[0]:
                best = (mtime, sid)
    if best is None:
        return None
    sid = best[1]
    return sid[len("session_"):] if sid.startswith("session_") else sid


def whoami(
    *,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    harness: str | None = None,
    kimi_home: Path | None = None,
) -> Identity | None:
    """This command's session, or None when no harness session can be named."""
    env = os.environ if env is None else env
    cwd = cwd or os.getcwd()
    process = None if harness else nearest_harness_process()
    found = harness or (process[0] if process else None)
    if found == CLAUDE and env.get("CLAUDE_CODE_SESSION_ID"):
        return Identity(CLAUDE, env["CLAUDE_CODE_SESSION_ID"], "CLAUDE_CODE_SESSION_ID")
    if found == CODEX and env.get("CODEX_THREAD_ID"):
        return Identity(CODEX, env["CODEX_THREAD_ID"], "CODEX_THREAD_ID")
    if found == KIMI:
        if process is not None:
            sid, locks_present = kimi_session_from_lock(process[1], kimi_home)
            if sid:
                return Identity(KIMI, sid, "Kimi session lock held by the calling kimi-code process")
            if locks_present:
                # This Kimi keeps session locks, yet none names us: unknown. A
                # guess from the cwd could name another session (gauntlet round 2).
                return None
        sid = kimi_session_for(cwd, kimi_home)
        return Identity(KIMI, sid, "newest Kimi session for this cwd (Kimi keeps no session locks)") if sid else None
    if found is None:
        # No harness in the ancestry (a plain terminal, an unrecognised launcher):
        # fall back to the variables, innermost-first is unknowable, so only
        # when exactly one harness has left one.
        present = [(h, k) for h, k in ((CLAUDE, "CLAUDE_CODE_SESSION_ID"), (CODEX, "CODEX_THREAD_ID")) if env.get(k)]
        if len(present) == 1:
            h, k = present[0]
            return Identity(h, env[k], f"{k} (no harness process found)")
    return None
