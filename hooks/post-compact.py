#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "filelock>=3.0",
# ]
# ///
"""
Claude Memory Plugin - PostCompact Hook

Keeps the summary Claude Code generated when it compacted the conversation.

PostCompact can neither inject context nor show a message (Claude Code
discards ``systemMessage``/``continue`` for it), so this hook has exactly one
job: write ``compact_summary`` to the state dir. A model-written digest of the
session exists at that moment and nowhere else; the Layer-2 memo recovery
recipe (skills/memo-writing/recovery.md) reads it before the raw transcript.

Input (stdin):
{
    "session_id": "abc123",
    "transcript_path": "/path/to/transcript.jsonl",
    "cwd": "/path/to/project",
    "hook_event_name": "PostCompact",
    "trigger": "auto",                 // "manual" | "auto"
    "compact_summary": "..."           // the conversation summary
}

Actions:
1. Skip headless seats (CLAUDE_CODE_ENTRYPOINT=sdk-cli) and empty summaries
2. Write <state_dir>/compact-summaries/<session_id>/<n>.md (n = 1, 2, ... per
   compaction of the session) with small frontmatter. Never the vault.
3. Exit 0 whatever happens — a hook must not fail the session
"""

import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from memex.paths import get_state_dir
from memex.scrub import safe_write_text
from memex.scripts.utils import (
    read_hook_input,
    log_info,
    log_warning,
)

SUMMARIES_DIRNAME = "compact-summaries"

# The session id becomes a directory name: accept only what Claude Code
# generates (UUIDs) and look-alikes, never a path component.
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


# json.dumps leaves these raw; a YAML parser reads them as line breaks inside a quoted scalar
_YAML_LINE_BREAKS = {"\u0085": "\\u0085", "\u2028": "\\u2028", "\u2029": "\\u2029"}


def _yaml_scalar(value: str) -> str:
    """A double-quoted YAML scalar (JSON strings are one, bar the characters above)."""
    out = json.dumps(value, ensure_ascii=False)
    for raw, escaped in _YAML_LINE_BREAKS.items():
        out = out.replace(raw, escaped)
    return out


def _frontmatter(session_id: str, trigger: str, cwd: str) -> str:
    fields = {
        "session_id": session_id,
        "trigger": trigger,
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "cwd": cwd,
    }
    lines = [f"{key}: {_yaml_scalar(value)}" for key, value in fields.items()]
    return "---\n" + "\n".join(lines) + "\n---\n\n"


def _publish(staged: Path, session_dir: Path) -> Path:
    """Move the finished ``staged`` file to the next free ``<n>.md`` and return that path.

    A hard link is atomic and refuses to replace an existing name, so two
    compactions of one session that race (idle compaction + a manual /compact)
    get distinct numbers instead of one overwriting the other, and a reader
    never sees a ``<n>.md`` that is empty or half written.
    """
    highest = 0
    for entry in session_dir.iterdir():
        if entry.suffix == ".md" and entry.stem.isdecimal():
            highest = max(highest, int(entry.stem))
    n = highest + 1
    while True:
        path = session_dir / f"{n}.md"
        try:
            os.link(staged, path)
            return path
        except FileExistsError:
            n += 1
        except OSError:  # a filesystem without hard links: fall back to the plain rename
            os.replace(staged, path)
            return path


def save_summary(input_data: dict) -> Path | None:
    """Write the compaction summary; return its path, or None when nothing was written."""
    session_id = str(input_data.get("session_id") or "")
    summary = input_data.get("compact_summary")
    if not isinstance(summary, str) or not summary.strip():
        log_info(f"PostCompact: no compact_summary for session {session_id[:8] or 'unknown'}...")
        return None
    if not _SAFE_SESSION_ID.match(session_id):
        log_warning("PostCompact: missing or malformed session_id, summary not kept")
        return None

    session_dir = get_state_dir() / SUMMARIES_DIRNAME / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    trigger = str(input_data.get("trigger") or "")
    cwd = str(input_data.get("cwd") or "")
    # mkstemp: unique, owner-only (a summary is session content), dot-prefixed so
    # a listing of the directory shows finished summaries only
    fd, staged_name = tempfile.mkstemp(dir=session_dir, prefix=".incoming-", suffix=".tmp")
    os.close(fd)
    staged = Path(staged_name)
    try:
        safe_write_text(staged, _frontmatter(session_id, trigger, cwd) + summary.strip() + "\n")
        return _publish(staged, session_dir)
    finally:
        staged.unlink(missing_ok=True)


def main():
    try:
        input_data = read_hook_input()  # drain stdin even when the payload is about to be ignored
        if os.environ.get("CLAUDE_CODE_ENTRYPOINT") == "sdk-cli":
            pass  # helper seats leave no trace (plan I7)
        elif isinstance(input_data, dict):
            path = save_summary(input_data)
            if path is not None:
                log_info(f"PostCompact: kept summary {path.parent.name[:8]}.../{path.name}")
    except Exception as e:
        log_warning(f"PostCompact hook failed: {e}")
    sys.exit(0)


if __name__ == "__main__":
    main()
