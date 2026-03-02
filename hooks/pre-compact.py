#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "filelock>=3.0",
# ]
# ///
"""
Claude Memory Plugin - PreCompact Hook (v2)

Writes a signal file so the post-compaction session knows a memo is needed.
The actual memo generation happens inside the Claude session (Layer 1 or 2),
not via external API calls.

Input (stdin):
{
    "session_id": "abc123",
    "transcript_path": "/path/to/transcript.jsonl",
    "cwd": "/path/to/myproject",
    "hook_event_name": "PreCompact"
}

Actions:
1. Write signal file to ~/.memex/pending-memos/
2. Return immediately (< 100ms)
"""

import json
import sys
from datetime import datetime
from pathlib import Path

# Add scripts directory to path for imports
scripts_dir = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(scripts_dir))

from utils import (
    read_hook_input,
    detect_project,
    is_session_processed,
    log_info,
    log_warning,
)


def main():
    input_data = read_hook_input()

    session_id = input_data.get("session_id", "")
    transcript_path = input_data.get("transcript_path", "")
    cwd = input_data.get("cwd", "")

    log_info(f"PreCompact hook triggered: session={session_id[:8] if session_id else 'unknown'}...")

    if not session_id or not transcript_path:
        log_warning("Missing session_id or transcript_path")
        sys.exit(0)

    # Skip if memo was already saved this session (Layer 1 caught it)
    if is_session_processed(session_id, "memo_generated"):
        log_info(f"Memo already saved for session {session_id[:8]}..., no signal needed")
        sys.exit(0)

    # Detect project
    project = detect_project(cwd) if cwd else "_uncategorized"

    # Write signal file for post-compaction pickup
    signal_dir = Path.home() / ".memex" / "pending-memos"
    signal_dir.mkdir(parents=True, exist_ok=True)

    signal_file = signal_dir / f"{session_id[:16]}.json"
    signal = {
        "session_id": session_id,
        "transcript_path": str(transcript_path),
        "project": project,
        "cwd": cwd,
        "timestamp": datetime.now().isoformat(),
    }

    try:
        signal_file.write_text(json.dumps(signal, indent=2))
        log_info(f"Signal file written: {signal_file.name}")
    except Exception as e:
        log_warning(f"Failed to write signal file: {e}")

    sys.exit(0)


if __name__ == "__main__":
    main()
