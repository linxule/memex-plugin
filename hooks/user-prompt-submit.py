#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
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
    "cwd": "/path/to/project"
}

Output (stdout): injected into Claude's context when nudge triggers.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from memex.paths import get_state_dir


def default_state():
    return {"count": 0, "chars": 0, "memo_saved": False, "tier_nudged": 0}


# Two-tier nudge for 1M context window.
# Message count is the real trigger — user prompt chars are a tiny fraction
# of total context (tool results + assistant responses + injected skills dominate).
# Chars threshold is an AND gate to avoid nudging low-substance sessions.
TIERS = [
    # (min_chars, min_messages, message)
    (4_000, 40,
     "[memex] Substantial session. "
     "Consider `/memex:save` when you reach a natural pause."),
    (8_000, 80,
     "[memex] Long session — worth preserving. "
     "`/memex:save` before context compaction."),
]


def main():
    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    session_id = input_data.get("session_id", "")
    if not session_id:
        sys.exit(0)

    # State file per session
    state_dir = get_state_dir() / "session-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / f"{session_id[:16]}.json"

    # Load or init state
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
        except (json.JSONDecodeError, ValueError):
            state = default_state()
    else:
        state = default_state()

    prompt = input_data.get("prompt", "")
    state["count"] += 1
    state["chars"] = state.get("chars", 0) + len(prompt)

    # Determine which tier we've reached
    current_tier = 0
    for i, (char_threshold, msg_threshold, _) in enumerate(TIERS, 1):
        if state["chars"] >= char_threshold and state["count"] >= msg_threshold:
            current_tier = i

    # Nudge if we've crossed a new tier and no memo saved
    tier_nudged = state.get("tier_nudged", 0)
    if not state["memo_saved"] and current_tier > tier_nudged:
        _, _, message = TIERS[current_tier - 1]
        print(message)
        state["tier_nudged"] = current_tier

    # Atomic write: temp + os.replace so a killed hook can't leave a truncated
    # state file. Non-critical (reader self-heals via default_state on JSON
    # error), but cheap to do right and avoids spurious nudge resets.
    try:
        tmp_file = state_file.parent / (state_file.name + ".tmp")
        tmp_file.write_text(json.dumps(state))
        os.replace(tmp_file, state_file)
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
