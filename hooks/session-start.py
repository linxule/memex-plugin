#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "filelock>=3.0",
#     "tiktoken>=0.5",
# ]
# ///
"""
Claude Memory Plugin - SessionStart Hook

Checks for pending memos on startup, handles resume and post-compact flows.

Input (stdin):
{
    "session_id": "abc123",
    "transcript_path": "/path/to/transcript.jsonl",
    "cwd": "/path/to/project",
    "hook_event_name": "SessionStart",
    "source": "startup",  // "startup", "resume", "clear", "compact"
    "model": "claude-sonnet-4-20250514"
}

Actions:
1. Check source - skip/minimize for "resume"
2. For "compact" - check for pending memo signal, inject generation instructions
3. For "startup" - only inject if pending memos exist
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from memex.paths import get_memex_path, get_pending_dir
from memex.scripts.utils import (
    read_hook_input,
    get_pending_memos,
    log_info,
    log_warning,
    output_context,
)


def _resolve(path_str: str) -> Path | None:
    """Best-effort Path.resolve(); returns None on empty/invalid input."""
    if not path_str:
        return None
    try:
        return Path(path_str).expanduser().resolve()
    except (OSError, RuntimeError):
        return None


def filter_pending_for_cwd(pending: list[dict], cwd: str) -> tuple[list[dict], bool]:
    """
    Filter pending memos to those relevant to the current working directory.

    Returns (visible_memos, is_vault_view):
    - If cwd exactly matches a pending memo's cwd: return matching memos, False
    - Else if cwd is inside the memex vault: return ALL pending memos, True
      (vault is the meta/triage location for orphan memos)
    - Otherwise: return [], False
    """
    current = _resolve(cwd)
    if current is None:
        return ([], False)

    matching = []
    for memo in pending:
        memo_cwd = _resolve(memo.get("cwd", ""))
        if memo_cwd is not None and memo_cwd == current:
            matching.append(memo)

    if matching:
        return (matching, False)

    # No project match — fall back to vault-only triage view
    try:
        memex_path = get_memex_path()
    except Exception:
        return ([], False)

    if current == memex_path or memex_path in current.parents:
        return (pending, True)

    return ([], False)


def main():
    input_data = read_hook_input()

    session_id = input_data.get("session_id", "")
    source = input_data.get("source", "startup")
    cwd = input_data.get("cwd", "")

    log_info(f"SessionStart hook triggered: session={session_id[:8] if session_id else 'unknown'}..., source={source}")

    if source == "resume":
        log_info("Resuming session, minimal context injection")
        handle_resume(cwd)
        sys.exit(0)

    if source == "compact":
        log_info("Post-compaction, checking for memo")
        handle_post_compact(session_id)
        sys.exit(0)

    # Standard startup: only inject if pending memos exist for this project
    pending = get_pending_memos()
    visible, is_vault_view = filter_pending_for_cwd(pending, cwd)
    if visible:
        if is_vault_view:
            output_context(
                f"⚠️ {len(visible)} orphan memo(s) pending retry (no matching project cwd). "
                f"Ask Claude to retry them — spawn one background model='sonnet' subagent per memo (memo generation is a sonnet-tier task; don't burn a heavier main model on it). Or check: ls ~/.memex/pending-memos/"
            )
            log_info(f"Startup injection (vault triage): {len(visible)} orphan pending memos")
        else:
            output_context(
                f"⚠️ {len(visible)} memo(s) pending retry for this project. "
                f"Ask Claude to retry them — spawn one background model='sonnet' subagent per memo (memo generation is a sonnet-tier task; don't burn a heavier main model on it). Or check: ls ~/.memex/pending-memos/"
            )
            log_info(f"Startup injection: {len(visible)} pending memos matching cwd")
    elif pending:
        log_info(f"Suppressed: {len(pending)} pending memo(s) exist but none match cwd={cwd}")

    sys.exit(0)


def handle_resume(cwd: str):
    """Handle session resume - minimal context."""
    pending = get_pending_memos()
    visible, is_vault_view = filter_pending_for_cwd(pending, cwd)
    if visible:
        if is_vault_view:
            output_context(
                f"📝 Note: {len(visible)} orphan memo(s) pending regeneration. "
                f"Ask Claude to retry them — spawn one background model='sonnet' subagent per memo (memo generation is a sonnet-tier task; don't burn a heavier main model on it). Or check: ls ~/.memex/pending-memos/"
            )
        else:
            output_context(
                f"📝 Note: {len(visible)} memo(s) pending regeneration for this project. "
                f"Ask Claude to retry them — spawn one background model='sonnet' subagent per memo (memo generation is a sonnet-tier task; don't burn a heavier main model on it). Or check: ls ~/.memex/pending-memos/"
            )
    sys.exit(0)


def handle_post_compact(session_id: str):
    """Handle post-compaction - check for pending memo, inject instructions."""
    parts = ["📚 Session compacted."]

    signal_dir = get_pending_dir()
    pending_signal = None

    if signal_dir.exists():
        for signal_file in signal_dir.glob("*.json"):
            try:
                signal = json.loads(signal_file.read_text())
                if signal.get("session_id", "")[:16] == session_id[:16]:
                    pending_signal = signal
                    break
            except (json.JSONDecodeError, ValueError):
                continue

    if pending_signal:
        transcript_path = pending_signal.get("transcript_path", "")
        project = pending_signal.get("project", "unknown")
        memex_path = get_memex_path()
        # transcript_path and project come from a JSON signal file written by
        # PreCompact — neither is sanitized at write time, so escape any
        # single-quote that would break the Python literal below. Without this,
        # a path like /Users/x's-laptop/... would silently corrupt the Task()
        # call the user copy-pastes.
        safe_transcript = transcript_path.replace("\\", "\\\\").replace("'", "\\'")
        safe_project = project.replace("\\", "\\\\").replace("'", "\\'")
        parts.append(
            f"\n⚠️ **Memo needed**: A memo was not saved before compaction. "
            f"Transcript at: `{transcript_path}`\n"
            f"Project: {project}\n\n"
            f"Please spawn a background subagent to generate the memo and extract observations:\n"
            f"```\n"
            f"Task(subagent_type='general-purpose', run_in_background=true,\n"
            f"     model='sonnet',\n"
            f"     prompt='Generate a session memo from the transcript at {safe_transcript}. "
            f"Read the memo prompt at {memex_path}/skills/memo-writing/memo-default.md for format guidance. "
            f"Search for related memos using: memex search \"<keywords>\" --mode=hybrid --format=text. "
            f"Save the memo to {memex_path}/projects/{safe_project}/memos/<YYYY-MM-DD>-<slug>.md. "
            f"Do NOT transcribe API keys, tokens, or any string matching sk-*, ghp_*, AIza*, AKIA*, xox*, or PEM private-key blocks into the memo body — describe what was leaked (provider + leak vector) without reproducing the secret. "
            f"After writing the memo, run: memex scrub \"<memo-path>\" --apply  "
            f"(double-quote the path — defense-in-depth: redacts any surface-pattern secrets that slipped through; idempotent and ~free on a single memo). "
            f"Then extract 5-15 atomic observations as JSON (see {memex_path}/commands/save.md step 5 for format and rules) "
            f"and pipe them to: memex backfill obs --stdin --doc-path \"projects/{safe_project}/memos/<filename>.md\". "
            f"Each observation must be independently understandable; skip trivial facts. "
            f"Use obs_type \"explicit\" for directly stated facts, \"deductive\" for facts that follow from combining stated information.')\n"
            f"```"
        )
    else:
        parts.append(
            "\nUse `memex search <keywords>` to recall prior decisions, "
            "`/memex:status` for overview."
        )

    context = "\n".join(parts)
    output_context(context)
    log_info(f"Post-compact injection ({len(context)} chars)")
    sys.exit(0)


if __name__ == "__main__":
    main()
