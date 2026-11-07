"""Which memo belongs to a session: the one truth check (v0.22.0, plan item I1).

A memo is a session's memo when its frontmatter stamps the session's id, as
``session_id: <uuid>`` or as one entry of ``session_ids: [<uuid>, ...]`` (memos
reconstructed from several threads, plan item E1). Hooks, ``backfill memos``
and ``reconcile-orphans`` all ask this module instead of ``state.json``: the
state file is written only by ``memex mark-saved``, so a memo saved without it
looked missing (2026-10-28: a session with a stamped memo was told "a memo was
not saved before compaction").

Positive evidence only, the v0.20.0 rule: a memo whose frontmatter can't be
parsed is never evidence, so a quirk can hide a stamp but never forge one.

Hooks that import this module must list ``pyyaml`` in their PEP 723
dependencies: the strict frontmatter parser lives in ``reconcile_orphans``.
"""

from __future__ import annotations

import json
import os
import stat
import time
from collections.abc import Sequence
from pathlib import Path

from memex.paths import get_state_dir
from memex.scripts.reconcile_orphans import _UUID_RE, _frontmatter

# A stamp must sit in the frontmatter, and frontmatter over 64 KB is untrusted
# anyway, so a read this long always contains any stamp that counts.
_PREFILTER_CHARS = 2 * 64 * 1024

# macOS marks a file whose bytes iCloud has evicted ``dataless``; opening one
# blocks until fileproviderd downloads it (a cold whole-vault scan took 71 s,
# 2026-11-07). ``stat`` itself never downloads.
_SF_DATALESS = getattr(stat, "SF_DATALESS", 0x40000000)


def is_dataless(path: Path) -> bool:
    try:
        return bool(getattr(os.stat(path), "st_flags", 0) & _SF_DATALESS)
    except OSError:
        return False


def normalize_session_id(value: object) -> str | None:
    """A full UUID, lowercased, or ``None``. Prefixes never match: 8-hex
    prefixes collide across Codex threads. Kimi Code names sessions
    ``session_<uuid>`` (its skills receive that as ``${KIMI_SESSION_ID}``);
    the Kimi adapter's id is the bare UUID, so the ``session_`` prefix is
    dropped here and both spellings stamp the same session."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    if v.lower().startswith("session_"):
        v = v[len("session_"):]
    if _UUID_RE.fullmatch(v):
        return v.lower()
    return None


def memo_stamps(frontmatter: object) -> set[str]:
    """Every session id a parsed frontmatter mapping stamps (may be empty)."""
    if not isinstance(frontmatter, dict):
        return set()
    stamps: set[str] = set()
    single = normalize_session_id(frontmatter.get("session_id"))
    if single:
        stamps.add(single)
    many = frontmatter.get("session_ids")
    if isinstance(many, list):
        stamps.update(sid for sid in map(normalize_session_id, many) if sid)
    return stamps


def _project_of(vault: Path, memo: Path) -> str:
    return memo.relative_to(vault).parts[1]


def _all_memos(vault: Path) -> list[Path]:
    # memos/<sub>/ too (e.g. memos/archive/): an archived memo is still that
    # session's memo (27 memos live in subfolders, 2026-10-28).
    return sorted(vault.glob("projects/*/memos/**/*.md"))


def _memo_files(vault: Path, project: str | None, within: Sequence[str] | None = None) -> list[Path]:
    if within is not None:
        return [f for name in dict.fromkeys(p for p in within if p)
                for f in sorted((vault / "projects" / name / "memos").glob("**/*.md"))]
    everything = _all_memos(vault)
    if not project:
        return everything
    first = [f for f in everything if _project_of(vault, f) == project]
    return first + [f for f in everything if _project_of(vault, f) != project]


def memo_for_session(vault: Path, session_id: str, *, project: str | None = None) -> Path | None:
    """The first memo stamped with ``session_id``, or ``None``.

    ``project`` only orders the scan (that folder first); a memo filed under
    another project still counts. Each file is checked for the id as plain
    text before its frontmatter is parsed, which keeps a whole-vault miss
    cheap enough for a hook.
    """
    sid = normalize_session_id(session_id)
    return stamped_memos(vault, {sid}, project=project)[0].get(sid) if sid else None


def find_session_memo(
    vault: Path, session_id: str, *, project: str | None = None, deadline: float
) -> tuple[Path | None, bool]:
    """``(memo, complete)`` for a caller with a time limit (a hook).

    Stops at ``deadline`` (a ``time.monotonic()`` value) and never opens an
    iCloud-evicted memo. Either leaves ``complete`` False: ``(None, False)``
    means "not known", never "no memo" — a hook must not act on it as a miss.
    """
    sid = normalize_session_id(session_id)
    if sid is None:
        return None, True  # not a UUID: nothing can stamp it
    found, complete = stamped_memos(vault, {sid}, project=project, deadline=deadline)
    return found.get(sid), complete


def stamped_memos(
    vault: Path, session_ids: set[str | None], *, project: str | None = None,
    within: Sequence[str] | None = None, deadline: float | None = None,
) -> tuple[dict[str, Path], bool]:
    """``({session id: first memo stamping it}, complete)`` in one vault pass.

    Each memo's head is checked for the ids as plain text first, and only a
    hit is parsed, so N ids cost one scan, not N. ``project`` orders
    the pass (that folder first); ``within`` restricts it to those projects'
    memos, in that order. With a ``deadline`` the pass stops there and skips
    iCloud-evicted memos; either makes ``complete`` False, and an id it did
    not reach is unknown, not a miss.
    """
    wanted = {sid for sid in map(normalize_session_id, session_ids) if sid}
    found: dict[str, Path] = {}
    complete = True
    for memo in _memo_files(vault, project, within) if wanted else ():
        if deadline is not None:
            if time.monotonic() > deadline:
                return found, False
            if is_dataless(memo):
                complete = False
                continue
        try:
            with open(memo, encoding="utf-8", errors="ignore") as fh:
                head = fh.read(_PREFILTER_CHARS)
        except OSError:
            continue
        lowered = head.lower()
        hits = {sid for sid in wanted - found.keys() if sid in lowered}
        if not hits:
            continue
        for sid in hits & memo_stamps(_frontmatter(memo)):
            found[sid] = memo
        if found.keys() >= wanted:
            return found, True
    return found, complete


def _hint_file(session_id: str) -> Path:
    return get_state_dir() / "session-state" / f"{session_id[:16]}.memo"


def record_session_memo(session_id: str, memo: Path) -> bool:
    """Remember the session's memo, so hooks find it without a vault scan.

    Written by PostToolUse when the memo is saved and by PreCompact when its
    scan finds one; read by UserPromptSubmit and the post-compact SessionStart,
    which re-verify the stamp. Its own file, not the nudge state
    ``<sid16>.json``: UserPromptSubmit rewrites that on every prompt, so a
    read-modify-write here could be lost to a racing prompt, and ``memex
    mark-saved`` globs ``*.json`` there. Structural content (one path)."""
    hint = _hint_file(session_id)
    try:
        hint.parent.mkdir(parents=True, exist_ok=True)
        tmp = hint.parent / (hint.name + ".tmp")
        tmp.write_text(json.dumps({"session_id": session_id, "memo_path": str(memo)}))
        os.replace(tmp, hint)
    except OSError:
        return False
    return True


def recorded_session_memo(session_id: str) -> str | None:
    """The memo path recorded for this session (not re-verified), or ``None``."""
    try:
        data = json.loads(_hint_file(session_id).read_text())
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and data.get("session_id") == session_id:
        raw = data.get("memo_path")
        if isinstance(raw, str) and raw:
            return raw
    return None


def confirm_memo(path: Path, session_id: str) -> bool:
    """``path`` is a memo stamped with ``session_id`` — checked without opening
    an iCloud-evicted file (that waits on its download), which reads False."""
    sid = normalize_session_id(session_id)
    try:
        return (sid is not None and path.is_file() and not is_dataless(path)
                and sid in memo_stamps(_frontmatter(path)))
    except OSError:
        return False


def memo_session_map(vault: Path) -> dict[str, list[str]]:
    """session id → vault-relative paths of every memo stamping it.

    One session can own several memos (a long Codex thread reconstructed as one
    memo per day), so values are lists, sorted.
    """
    out: dict[str, list[str]] = {}
    for memo in _all_memos(vault):
        for sid in memo_stamps(_frontmatter(memo)):
            out.setdefault(sid, []).append(str(memo.relative_to(vault)))
    return out
