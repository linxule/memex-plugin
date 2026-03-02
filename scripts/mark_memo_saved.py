#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["filelock>=3.0"]
# ///
"""Mark current session's memo as saved — unifies state for both Layer 1 and PreCompact."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from utils import mark_session_phase, clear_pending_memo, get_state_dir

def main():
    # Find current session ID from session-state files (written by UserPromptSubmit hook)
    state_dir = Path.home() / ".memex" / "session-state"
    if not state_dir.exists():
        return

    # Get most recently modified session state (that's the active one)
    state_files = sorted(state_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not state_files:
        return

    state_file = state_files[0]
    try:
        state = json.loads(state_file.read_text())
    except (json.JSONDecodeError, ValueError):
        return

    # Extract session_id from filename (first 16 chars)
    session_prefix = state_file.stem

    # Find full session_id from processed_sessions state
    # Or just use the prefix — mark_session_phase handles partial IDs
    # Search pending memos for full session_id
    pending_dir = Path.home() / ".memex" / "pending-memos"
    full_session_id = session_prefix  # fallback

    if pending_dir.exists():
        for pf in pending_dir.glob("*.json"):
            try:
                signal = json.loads(pf.read_text())
                if signal.get("session_id", "")[:16] == session_prefix:
                    full_session_id = signal["session_id"]
                    pf.unlink()  # Clear the pending signal
                    break
            except (json.JSONDecodeError, ValueError):
                continue

    # Mark in canonical state store (what PreCompact checks)
    mark_session_phase(full_session_id, "memo_generated")

    # Mark in nudge state store (what UserPromptSubmit checks)
    state["memo_saved"] = True
    state_file.write_text(json.dumps(state))

    print(f"Memo marked as saved for session {session_prefix}")


if __name__ == "__main__":
    main()
