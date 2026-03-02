#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "filelock>=3.0",
#     "tiktoken>=0.5",
# ]
# ///
"""
Claude Memory Plugin - SessionEnd Hook

Archives the session transcript to the project folder when a session ends.

Input (stdin):
{
    "session_id": "abc123",
    "transcript_path": "/path/to/transcript.jsonl",
    "cwd": "/path/to/myproject",
    "hook_event_name": "SessionEnd",
    "reason": "logout"  // "clear", "logout", "prompt_input_exit", "other"
}

Actions:
1. Detect project from cwd
2. Copy transcript .jsonl to projects/<project>/transcripts/
3. Convert to markdown with frontmatter
4. Update processing state
"""

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

# Add scripts directory to path for imports
scripts_dir = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(scripts_dir))

from utils import (
    read_hook_input,
    get_memex_path,
    detect_project,
    ensure_project_structure,
    mark_session_phase,
    is_session_processed,
    get_session_memo_saved,
    log_info,
    log_error,
    log_warning,
    safe_write,
)
from transcript_to_md import convert_transcript_file


def main():
    # Read hook input
    input_data = read_hook_input()

    session_id = input_data.get("session_id", "")
    transcript_path = input_data.get("transcript_path", "")
    cwd = input_data.get("cwd", "")
    reason = input_data.get("reason", "other")

    log_info(f"SessionEnd hook triggered: session={session_id[:8]}..., reason={reason}")

    if not session_id or not transcript_path:
        log_error("Missing session_id or transcript_path")
        sys.exit(0)  # Non-blocking, just log

    # Check if already processed
    if is_session_processed(session_id, "transcript_archived"):
        log_info(f"Session {session_id[:8]}... already archived, skipping")
        sys.exit(0)

    transcript_path = Path(transcript_path)
    if not transcript_path.exists():
        log_warning(f"Transcript not found: {transcript_path}")
        sys.exit(0)

    # Minimum viability check: skip trivial sessions (test prompts, "hi", aborted)
    try:
        with open(transcript_path, 'r') as f:
            lines = [l for l in f if l.strip()]
        msg_count = len(lines)
        has_tools = any('"tool_use"' in l for l in lines)
        if msg_count < 6 and not has_tools:
            # Less than ~3 turns and no tool usage = not worth archiving
            log_info(f"Session {session_id[:8]}... too trivial ({msg_count} messages, no tools), skipping archive")
            sys.exit(0)
    except Exception:
        pass  # If we can't check, archive anyway

    try:
        # Get memex path
        memex = get_memex_path()

        # Detect project
        project = detect_project(cwd) if cwd else "_uncategorized"
        log_info(f"Detected project: {project}")

        # Check if memo was already saved for this session
        has_memo = get_session_memo_saved(session_id)
        if has_memo:
            log_info(f"Session {session_id[:8]}... has memo saved")

        # Ensure project structure exists
        project_path = ensure_project_structure(project, memex)

        # Create transcript filenames
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base_name = f"{timestamp}-{session_id[:8]}"

        jsonl_dest = project_path / "transcripts" / f"{base_name}.jsonl"
        md_dest = project_path / "transcripts" / f"{base_name}.md"

        # Copy JSONL file
        shutil.copy2(transcript_path, jsonl_dest)
        log_info(f"Archived transcript to {jsonl_dest}")

        # Convert to markdown
        convert_transcript_file(
            jsonl_path=jsonl_dest,
            output_path=md_dest,
            session_id=session_id,
            project=project,
            has_memo=has_memo,
        )
        log_info(f"Created markdown transcript at {md_dest}")

        # Update project metadata if needed
        project_meta = project_path / "_project.md"
        if not project_meta.exists():
            create_project_meta(project_meta, project, cwd)

        # Mark as processed
        mark_session_phase(session_id, "transcript_archived")
        log_info(f"Session {session_id[:8]}... archived successfully")

    except Exception as e:
        log_error(f"SessionEnd hook error: {e}")
        # Non-blocking - just log the error
        sys.exit(0)

    sys.exit(0)


def create_project_meta(path: Path, project: str, cwd: str):
    """Create initial project metadata file."""
    content = f"""---
type: project
name: {project}
created: {datetime.now().strftime("%Y-%m-%d")}
---

# {project}

## Overview

Project workspace: `{cwd}`

## Session History

<!-- Transcripts and memos will accumulate in this project folder -->
"""
    safe_write(path, content)
    log_info(f"Created project metadata: {path}")


if __name__ == "__main__":
    main()
