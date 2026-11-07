#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "filelock>=3.0",
#     "pyyaml>=6.0",
# ]
# ///
"""
Claude Memory Plugin - PreCompact Hook

Writes a pending-memo signal so the post-compaction session knows a memo is
needed, then asks the vault whether it is (a bounded scan; an unfinished
one keeps the signal):

- a memo stamped with this session's id exists (``memex.session_memos``) →
  the signal is cleared (an earlier compaction's too); the memo's path is
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
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from memex.paths import get_memex_path, get_pending_dir
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
from memex.session_memos import (
    confirm_memo,
    find_session_memo,
    record_session_memo,
    recorded_session_memo,
)

# Of the 10 s hooks.json timeout; the clock starts after interpreter start and
# imports, so leave headroom. A scan cut short keeps the signal.
SCAN_BUDGET_S = 6.0


def _vault() -> Path | None:
    try:
        return get_memex_path()
    except Exception:
        return None


def clear_own_signal(session_id: str) -> None:
    """An earlier compaction's signal, now covered by a stamped memo."""
    signal_file = get_pending_dir() / f"{session_id[:16]}.json"
    try:
        if json.loads(signal_file.read_text()).get("session_id") == session_id:
            signal_file.unlink()
            log_info(f"Cleared covered signal {signal_file.name}")
    except (OSError, ValueError, AttributeError):
        pass


def write_signal(session_id: str, transcript_path: str, project: str, cwd: str, facts: dict) -> None:
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


def main():
    started = time.monotonic()
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

    # Signal first, scan second: a cold vault scan can outlast the hook's
    # timeout (an iCloud-evicted memo blocks on its download), and a scan
    # killed before the signal was written lost the post-compact reminder.
    write_signal(session_id, transcript_path, project, cwd, facts)

    # The memo PostToolUse recorded when it was written answers without a
    # scan (and stays the one named, if the session has several).
    hint = recorded_session_memo(session_id)
    if hint and confirm_memo(Path(hint), session_id):
        clear_own_signal(session_id)
        log_info(f"Session {session_id[:8]}... has its recorded memo ({Path(hint).name}), signal cleared")
        sys.exit(0)

    vault = _vault()
    if vault is None:
        sys.exit(0)
    memo, complete = find_session_memo(vault, session_id, project=project,
                                       deadline=started + SCAN_BUDGET_S)
    if memo is not None:
        if not record_session_memo(session_id, memo):
            log_warning("Could not record memo path in session state")
        clear_own_signal(session_id)
        log_info(f"Session {session_id[:8]}... has a stamped memo ({memo.name}), signal cleared")
    elif not complete:
        log_info(f"Memo scan for {session_id[:8]}... incomplete (deadline or iCloud-evicted memos): signal kept")
    sys.exit(0)


if __name__ == "__main__":
    main()
