#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
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
    "cwd": "/path/to/project",
    "hook_event_name": "SessionEnd",
    "reason": "logout"  // "clear", "logout", "prompt_input_exit", "other"
}

Actions:
1. Detect project from cwd
2. Copy transcript .jsonl to projects/<project>/transcripts/
3. Convert to markdown with frontmatter
4. Update processing state
"""

import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from memex.paths import get_memex_path
from memex.scripts.utils import (
    read_hook_input,
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
from memex.scripts.transcript_to_md import convert_transcript_file


def is_trivial_transcript(path: Path) -> bool:
    """Check the archive threshold without loading the complete transcript."""
    count = 0
    with open(path, encoding="utf-8") as transcript:
        for line in transcript:
            if not line.strip():
                continue
            count += 1
            if count >= 6 or '"tool_use"' in line:
                return False
    return True


STALE_STAGING_SECONDS = 10 * 60


def _sweep_stale_staging(transcripts_dir: Path, now: float | None = None) -> int:
    """Remove `.archive-*` staging dirs an earlier hook left behind.

    TemporaryDirectory only cleans up on normal exit; the 30s hook timeout
    kills mid-conversion runs (SIGKILL), stranding a hidden dir holding a
    copied .jsonl and a partial .md.tmp. Harmless to indexing, but it
    accumulates in the vault. Anything older than STALE_STAGING_SECONDS
    cannot belong to a live hook.
    """
    now = time.time() if now is None else now
    removed = 0
    for stale in transcripts_dir.glob(".archive-*"):
        try:
            if stale.is_dir() and now - stale.stat().st_mtime > STALE_STAGING_SECONDS:
                shutil.rmtree(stale)
                removed += 1
        except OSError as exc:
            log_info(f"Could not remove stale staging dir {stale}: {exc}")
    return removed


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
        if is_trivial_transcript(transcript_path):
            # Less than ~3 turns and no tool usage = not worth archiving
            log_info(f"Session {session_id[:8]}... too trivial (<6 messages, no tools), skipping archive")
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

        # Discovery treats either visible artifact as an archived session.
        # Stage both files together so a failed conversion leaves no apparent
        # archive that would prevent a later import from recovering the session.
        _sweep_stale_staging(jsonl_dest.parent)
        with tempfile.TemporaryDirectory(prefix=".archive-", dir=jsonl_dest.parent) as staging:
            staged_jsonl = Path(staging) / jsonl_dest.name
            # Recursive index scans include hidden directories, so keep the
            # staging file off their *.md surface until publication too.
            staged_md = Path(staging) / f"{md_dest.name}.tmp"
            shutil.copy2(transcript_path, staged_jsonl)
            converted = convert_transcript_file(
                jsonl_path=staged_jsonl,
                output_path=staged_md,
                session_id=session_id,
                project=project,
                has_memo=has_memo,
            )
            if converted is None:
                raise RuntimeError("Transcript conversion produced no markdown; archive remains pending")
            staged_md.replace(md_dest)
            staged_jsonl.replace(jsonl_dest)
        log_info(f"Archived transcript to {jsonl_dest}")
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
