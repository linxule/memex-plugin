"""Harness adapters: sessions from tools other than Claude Code (v0.22.0, plan D1).

memex read only ``~/.claude/projects``. Since ~2026-10-13 most of the owner's
work ran in Codex Desktop and Kimi Code, and none of it was captured (61 memos
were reconstructed by hand on 2026-10-28). An adapter turns one harness's
session files into a ``NormalizedSession``; ``render_transcript_md`` turns that
into the same transcript markdown SessionEnd writes, with ``harness:`` in the
frontmatter, so search, ``has_memo`` and condensation treat every harness alike.

Adapters live in this package (``codex_desktop``, ``kimi_code``), one module
each, and must never ship real session content as test fixtures.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

# The `harness:` frontmatter enum (plan E1). `claude-desktop-relay` is a
# Claude Code session started by a claude.ai coordinator on the Mac.
HARNESSES = ("claude-code", "claude-desktop-relay", "codex-desktop", "kimi-code")


@dataclass(frozen=True)
class SessionRef:
    """One session as discovered on disk: its FULL id and every file it spans
    (a Codex thread resumes across several rollout files; 8-hex prefixes collide)."""

    harness: str
    session_id: str
    files: tuple[Path, ...]


@dataclass
class Turn:
    role: Literal["user", "assistant"]
    text: str
    timestamp: str | None = None  # ISO 8601


@dataclass
class NormalizedSession:
    harness: str
    session_id: str
    cwd: str
    turns: list[Turn]
    source_files: list[Path]
    started_at: str | None = None  # ISO 8601
    ended_at: str | None = None
    models: list[str] = field(default_factory=list)
    subagent_count: int = 0  # child threads folded into this one, not rendered
    parent_session_id: str | None = None
    # Harness-specific facts worth keeping (e.g. Codex `originator`). Values must
    # be YAML-scalar-safe strings or numbers; they are NOT written to frontmatter.
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def user_turns(self) -> int:
        return sum(1 for t in self.turns if t.role == "user")

    @property
    def date(self) -> str | None:
        stamp = self.started_at or self.ended_at
        return stamp[:10] if stamp else None


class HarnessAdapter(Protocol):
    name: str  # one of HARNESSES

    def discover(self, *, root: Path | None = None) -> Iterable[SessionRef]:
        """Every top-level session under ``root`` (the harness default when None).
        Subagent/child sessions are not yielded; they count toward the parent."""
        ...

    def load(self, ref: SessionRef) -> NormalizedSession | None:
        """Parse one session; ``None`` when it holds no user/assistant text."""
        ...


def _duration_minutes(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    try:
        a = datetime.fromisoformat(start.replace("Z", "+00:00"))
        b = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, int((b - a).total_seconds() // 60))


def _yaml_scalar(value: str) -> str:
    """Double-quoted YAML scalar (escapes backslash and quote)."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_transcript_md(session: NormalizedSession, *, project: str, has_memo: bool) -> str:
    """Transcript markdown in the shape SessionEnd produces, plus ``harness:``.

    Frontmatter keys match ``transcript_to_md`` where they exist (type,
    session_id, date, turns, has_memo, start_time, end_time, duration_minutes,
    models, project) and add ``harness``, ``source_cwd`` and, when non-zero,
    ``subagents``. The body is turns only: these harnesses' tool I/O is not
    rendered and reasoning is often encrypted.
    """
    if session.harness not in HARNESSES:
        raise ValueError(f"unknown harness {session.harness!r}; expected one of {HARNESSES}")
    lines = ["---", "type: transcript", f"harness: {session.harness}", f"session_id: {session.session_id}"]
    if session.date:
        lines.append(f"date: {session.date}")
    lines.append(f"turns: {session.user_turns}")
    lines.append(f"has_memo: {'true' if has_memo else 'false'}")
    if session.started_at:
        lines.append(f"start_time: {session.started_at}")
    if session.ended_at:
        lines.append(f"end_time: {session.ended_at}")
    minutes = _duration_minutes(session.started_at, session.ended_at)
    if minutes is not None:
        lines.append(f"duration_minutes: {minutes}")
    if session.models:
        lines.append("models: [" + ", ".join(_yaml_scalar(m) for m in session.models) + "]")
    if session.subagent_count:
        lines.append(f"subagents: {session.subagent_count}")
    if session.parent_session_id:
        lines.append(f"parent_session_id: {session.parent_session_id}")
    lines.append(f"source_cwd: {_yaml_scalar(session.cwd)}")
    lines.append(f"project: {project}")
    lines.append("---")
    lines.append("")
    lines.append(f"# Session: {session.session_id[:8]}... ({session.harness})")
    lines.append("")
    n = 0
    for turn in session.turns:
        if turn.role == "user":
            n += 1
            lines += ["---", "", f"## Turn {n}", "", "### User", ""]
        else:
            lines += ["### Assistant", ""]
        lines += [turn.text.rstrip(), ""]
    return "\n".join(lines).rstrip() + "\n"
