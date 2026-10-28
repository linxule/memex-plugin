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

from pathlib import Path

from memex.scripts.reconcile_orphans import _UUID_RE, _frontmatter

# A stamp must sit in the frontmatter, and frontmatter over 64 KB is untrusted
# anyway, so a read this long always contains any stamp that counts.
_PREFILTER_CHARS = 2 * 64 * 1024


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


def _memo_files(vault: Path, project: str | None) -> list[Path]:
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
    if sid is None:
        return None
    for memo in _memo_files(vault, project):
        try:
            with open(memo, encoding="utf-8", errors="ignore") as fh:
                head = fh.read(_PREFILTER_CHARS)
        except OSError:
            continue
        if sid not in head.lower():
            continue
        if sid in memo_stamps(_frontmatter(memo)):
            return memo
    return None


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
