#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "filelock>=3.0",
#     "pyyaml>=6.0",
# ]
# ///
"""
Claude Memory Plugin - PreCompact Hook (v3, v0.22.0)

Writes a pending-memo signal so the post-compaction session knows a memo is
needed — unless the vault already answers the question:

- a memo stamped with this session's id exists (``memex.session_memos``) →
  no signal (an earlier compaction's signal is cleared); the memo's path is
  recorded in ``session-state/<sid16>.memo`` instead, where UserPromptSubmit
  and the post-compact SessionStart find it without a scan;
- the session is a fleet seat (``CLAUDE_CODE_ENTRYPOINT=sdk-cli``; without that
  variable, the transcript's first ``entrypoint`` is sdk-cli) → no signal: the
  session that launched the seat owns the work.

The memo itself is written inside the Claude session (Layer 1 or 2), never via
external API calls. PreCompact output is discarded by Claude Code, so this hook
only writes files.

Input (stdin):
{
    "session_id": "abc123",
    "transcript_path": "/path/to/transcript.jsonl",
    "cwd": "/path/to/project",
    "hook_event_name": "PreCompact",
    "trigger": "manual" | "auto"
}
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from memex.paths import get_memex_path, get_pending_dir, get_state_dir
from memex.scripts.reconcile_orphans import (
    is_helper_session,
    session_entrypoint,
    transcript_facts,
)
from memex.scripts.utils import (
    detect_project,
    is_session_processed,
    log_info,
    log_warning,
    read_hook_input,
)
from memex.session_memos import memo_for_session


def _vault() -> Path | None:
    try:
        return get_memex_path()
    except Exception:
        return None


def record_memo(session_id: str, memo: Path) -> None:
    """Remember the session's memo for UserPromptSubmit and SessionStart.

    Its own file, not the nudge state ``<sid16>.json``: UserPromptSubmit
    rewrites that file on every prompt, so a read-modify-write here could be
    lost to a racing prompt, and ``memex mark-saved`` globs ``*.json`` there.
    Structural content (one path), no prose."""
    hint = get_state_dir() / "session-state" / f"{session_id[:16]}.memo"
    try:
        hint.parent.mkdir(parents=True, exist_ok=True)
        tmp = hint.parent / (hint.name + ".tmp")
        tmp.write_text(json.dumps({"session_id": session_id, "memo_path": str(memo)}))
        os.replace(tmp, hint)
    except OSError as e:
        log_warning(f"Could not record memo path in session state: {e}")


def clear_own_signal(session_id: str) -> None:
    """An earlier compaction's signal, now covered by a stamped memo."""
    signal_file = get_pending_dir() / f"{session_id[:16]}.json"
    try:
        if json.loads(signal_file.read_text()).get("session_id") == session_id:
            signal_file.unlink()
            log_info(f"Cleared covered signal {signal_file.name}")
    except (OSError, ValueError, AttributeError):
        pass


def main():
    input_data = read_hook_input()

    session_id = input_data.get("session_id", "")
    transcript_path = input_data.get("transcript_path", "")
    cwd = input_data.get("cwd", "")

    log_info(f"PreCompact hook triggered: session={session_id[:8] if session_id else 'unknown'}...")

    if not session_id or not transcript_path:
        log_warning("Missing session_id or transcript_path")
        sys.exit(0)

    facts = transcript_facts(transcript_path)
    if is_helper_session(facts):
        log_info(f"Helper session {session_id[:8]}... (sdk-cli): no pending-memo signal")
        sys.exit(0)

    # `memex mark-saved` ran: a memo exists even if it carries no stamp
    if is_session_processed(session_id, "memo_generated"):
        log_info(f"Memo already saved for session {session_id[:8]}..., no signal needed")
        sys.exit(0)

    project = detect_project(cwd) if cwd else "_uncategorized"

    vault = _vault()
    memo = memo_for_session(vault, session_id, project=project) if vault else None
    if memo is not None:
        record_memo(session_id, memo)
        clear_own_signal(session_id)
        log_info(f"Session {session_id[:8]}... has a stamped memo ({memo.name}), no signal needed")
        sys.exit(0)

    # Write signal file for post-compaction pickup
    signal_dir = get_pending_dir()
    signal_dir.mkdir(parents=True, exist_ok=True)

    signal_file = signal_dir / f"{session_id[:16]}.json"
    signal = {
        "session_id": session_id,
        "transcript_path": str(transcript_path),
        "project": project,
        "cwd": cwd,
        "timestamp": datetime.now().isoformat(),
    }
    entrypoint = session_entrypoint(facts)
    if entrypoint:
        # what the helper rule judged by; later readers trust this over the transcript
        signal["entrypoint"] = entrypoint
    if facts["forked_from"]:
        # a fork carries its parent's records: the parent's memo owns those
        signal["forked_from"] = facts["forked_from"]

    try:
        # Atomic write: temp file + os.replace (atomic on POSIX) so a hook
        # killed mid-write can't leave a truncated signal that the post-compact
        # reader would skip as corrupt JSON, orphaning the pending memo.
        tmp_file = signal_file.parent / (signal_file.name + ".tmp")
        tmp_file.write_text(json.dumps(signal, indent=2))
        os.replace(tmp_file, signal_file)
        log_info(f"Signal file written: {signal_file.name}")
    except Exception as e:
        log_warning(f"Failed to write signal file: {e}")

    sys.exit(0)


if __name__ == "__main__":
    main()
