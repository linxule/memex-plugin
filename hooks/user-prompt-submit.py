#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""
Claude Memory Plugin - UserPromptSubmit Hook

Tracks session activity and nudges Claude to save a memo
when substantial work has accumulated.

Input (stdin):
{
    "session_id": "abc123",
    "prompt": "user's message",
    "cwd": "/path/to/myproject"
}

Output (stdout): injected into Claude's context when nudge triggers.
"""

import json
import sys
from pathlib import Path


def main():
    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    session_id = input_data.get("session_id", "")
    if not session_id:
        sys.exit(0)

    # State file per session
    state_dir = Path.home() / ".memex" / "session-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / f"{session_id[:16]}.json"

    # Load or init state
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
        except (json.JSONDecodeError, ValueError):
            state = {"count": 0, "memo_saved": False, "nudged": False}
    else:
        state = {"count": 0, "memo_saved": False, "nudged": False}

    state["count"] += 1

    # Save state
    state_file.write_text(json.dumps(state))

    # Nudge conditions:
    # - At least 20 messages exchanged
    # - No memo saved yet this session
    # - Haven't nudged recently (wait another 15 messages before re-nudging)
    threshold = 20
    re_nudge_interval = 15

    should_nudge = (
        not state["memo_saved"]
        and state["count"] >= threshold
        and not state["nudged"]
    )

    # Re-nudge if still no memo after another interval
    if not should_nudge and not state["memo_saved"] and state.get("nudged"):
        last_nudge_at = state.get("nudged_at", 0)
        if state["count"] - last_nudge_at >= re_nudge_interval:
            should_nudge = True

    if should_nudge:
        print(
            "[memex] Substantial session activity detected. "
            "Consider `/memex:save` to capture decisions and learnings "
            "before context compaction."
        )
        state["nudged"] = True
        state["nudged_at"] = state["count"]
        state_file.write_text(json.dumps(state))

    sys.exit(0)


if __name__ == "__main__":
    main()
