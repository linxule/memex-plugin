#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "filelock>=3.0",
#     "tiktoken>=0.5",
#     "pyyaml>=6.0",
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
1. Route the session to a project: a claude.ai relay worker by its channel
   (config `channel_mappings`), everything else by cwd
   (`memex.routing.resolve_session_project`)
2. Copy transcript .jsonl to projects/<project>/transcripts/
3. Convert to markdown with frontmatter (relay workers get `harness`,
   `relay_channel`, `relay_coordinator`)
4. Update processing state
5. Substantial relay worker with no stamped memo → Layer-2 pending-memo
   signal, the same shape PreCompact writes (plan C4): relays rarely compact.
   The memo scan itself runs before step 2, bounded to the relay's folders.
6. Append a filing receipt to <state_dir>/last-archives.jsonl (plan I6)

Claude Code gives SessionEnd 1.5 s in total, whatever `timeout` hooks.json
sets, and discards its systemMessage: everything here stays cheap and silent.
"""

import time

# As early as possible: SessionEnd's 1.5 s budget includes the imports below.
_HOOK_STARTED = time.monotonic()

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from memex.paths import get_memex_path, get_pending_dir, get_state_dir
from memex.routing import append_filing_receipt, filing_receipt, resolve_session_project
from memex.scripts.reconcile_orphans import _frontmatter as strict_frontmatter
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
from memex.scripts.transcript_to_md import RELAY_HARNESS, convert_transcript_file, read_relay_metadata
from memex.session_memos import memo_stamps, normalize_session_id


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

# A relay worker is "substantial" (worth a memo) at this many tool calls. The
# 28 relays on disk 2026-10-28 fall into three clusters: 7–15 tool calls
# (spawn, one errand, report back; ≤ 20 min — the coordinator's chat already
# holds the outcome), 23–37, and 68–1,317 (24 min to 18 h of real work on the
# Mac that claude.ai never sees). 30 signals the top cluster and the three
# busiest of the middle one: 13 of 28. Bytes are useless for relays: the
# attachment preamble alone makes even a 0-minute relay 560 KB, and duration
# counts the idle gaps between coordinator wakes.
RELAY_SUBSTANTIAL_TOOL_USES = 30

# The relay memo scan may run until this many seconds after the hook module
# started loading. It runs BEFORE the conversion (which needs the rest of the
# budget), so a slow conversion of a big relay can't starve it. Measured: 5–10
# ms for the vault's largest project folders. A scan cut short still signals,
# because SessionStart and `reconcile-orphans` re-check against the whole vault.
MEMO_SCAN_BUDGET_S = 0.5
_MEMO_HEAD_CHARS = 128 * 1024


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

        # Route: a relay worker's channel first, then the cwd rules. Only the
        # file head is read for the relay tags (SessionEnd's 1.5 s budget).
        relay = read_relay_metadata(transcript_path)
        route = resolve_session_project(cwd, relay)
        project = route.project
        log_info(f"Detected project: {project} (rule: {route.rule})")
        if route.unmapped_channel:
            log_info(f"Relay channel {route.unmapped_channel} has no channel_mappings entry; filed by cwd")

        # Check if memo was already saved for this session
        has_memo = get_session_memo_saved(session_id)
        if has_memo:
            log_info(f"Session {session_id[:8]}... has memo saved")

        # C4: look for a relay's stamped memo before the conversion, so a slow
        # conversion can't eat the scan's share of the budget.
        relay_memo_scan = None
        if relay and not has_memo:
            relay_memo_scan = find_relay_memo(
                session_id, cwd, route, memex, _HOOK_STARTED + MEMO_SCAN_BUDGET_S
            )

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
        session_meta: dict = {}
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
                metadata_out=session_meta,
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

    # Best effort after the archive is safe: neither step may undo or block it.
    try:
        signal_relay_memo(
            session_id=session_id,
            transcript_path=transcript_path,
            cwd=cwd,
            project=project,
            session_meta=session_meta,
            memo_scan=relay_memo_scan,
        )
    except Exception as exc:
        log_warning(f"Relay memo signal not written: {exc}")
    record_filing_receipt(route, session_id, md_dest, memex, cwd)

    sys.exit(0)


def record_filing_receipt(route, session_id: str, md_path: Path, vault: Path, cwd: str) -> None:
    """One line in <state_dir>/last-archives.jsonl: where the session went and why."""
    try:
        archived = md_path.resolve().relative_to(vault.resolve()).as_posix()
    except ValueError:
        archived = str(md_path)
    try:
        receipt = filing_receipt(route, session_id=session_id, archived=archived, cwd=cwd)
        if append_filing_receipt(get_state_dir(), receipt) is None:
            log_warning("Filing receipt dropped: receipts file busy")
    except Exception as exc:
        log_warning(f"Filing receipt not written: {exc}")


def relay_is_substantial(session_meta: dict) -> bool:
    """A relay worker that did enough work to deserve a memo; never an sdk-cli seat."""
    if session_meta.get("harness") != RELAY_HARNESS:
        return False
    entrypoint = session_meta.get("entrypoint")
    entrypoints = entrypoint if isinstance(entrypoint, list) else [entrypoint]
    if "sdk-cli" in entrypoints or os.environ.get("CLAUDE_CODE_ENTRYPOINT") == "sdk-cli":
        return False
    try:
        tool_uses = int(session_meta.get("tool_uses") or 0)
    except (TypeError, ValueError):
        return False
    return tool_uses >= RELAY_SUBSTANTIAL_TOOL_USES


def stamped_memo_in_projects(
    vault: Path, session_id: str, projects: list[str], deadline: float
) -> tuple[Path | None, bool]:
    """``(memo, complete)``: the first memo in these projects' ``memos/`` stamped
    with ``session_id`` (``memex.session_memos`` stamps, strict parser).

    Bounded on purpose: ``memo_for_session`` falls through to the whole vault
    on a miss (0.06–0.55 s), which SessionEnd cannot afford. ``complete`` is
    False when ``deadline`` cut the scan short.
    """
    sid = normalize_session_id(session_id)
    if sid is None:
        return None, True  # not a UUID: nothing can stamp it
    for project in dict.fromkeys(p for p in projects if p):
        for memo in sorted((vault / "projects" / project / "memos").glob("*.md")):
            if time.monotonic() > deadline:
                return None, False
            try:
                with open(memo, encoding="utf-8", errors="ignore") as fh:
                    head = fh.read(_MEMO_HEAD_CHARS)
            except OSError:
                continue
            if sid in head.lower() and sid in memo_stamps(strict_frontmatter(memo)):
                return memo, True
    return None, True


def find_relay_memo(
    session_id: str, cwd: str, route, vault: Path, deadline: float
) -> tuple[Path | None, bool]:
    """``(memo, complete)`` for a relay worker: its channel/cwd project, plus
    the cwd's own project when the channel decided — an in-session
    /memex:save files by cwd (the catch-all), not by channel. Never raises:
    an error reads as an incomplete scan, which still signals."""
    try:
        projects = [route.project]
        if route.rule == "channel" and cwd and time.monotonic() < deadline:
            projects.append(detect_project(cwd))
        return stamped_memo_in_projects(vault, session_id, projects, deadline)
    except Exception as exc:
        log_warning(f"Relay memo scan failed: {exc}")
        return None, False


def signal_relay_memo(
    *,
    session_id: str,
    transcript_path: Path,
    cwd: str,
    project: str,
    session_meta: dict,
    memo_scan: tuple[Path | None, bool] | None,
) -> Path | None:
    """Write a pending-memo signal for a substantial relay worker without a memo.

    ``memo_scan`` is ``find_relay_memo``'s answer, or ``None`` when no scan
    ran (not a relay, or state already records a saved memo): no signal then.
    """
    if memo_scan is None or not relay_is_substantial(session_meta):
        return None
    memo, complete = memo_scan
    if memo is not None:
        log_info(f"Relay session {session_id[:8]}... already has memo {memo.name}; no signal")
        return None
    if not complete:
        log_info("Memo scan hit the SessionEnd deadline; signalling anyway (SessionStart re-checks)")
    # Same name and shape as PreCompact's signal, so every reader handles it.
    signal_file = get_pending_dir() / f"{session_id[:16]}.json"
    if signal_file.exists():
        try:
            owner = json.loads(signal_file.read_text()).get("session_id")
        except (OSError, ValueError, AttributeError):
            owner = None
        if owner != session_id:
            log_warning(f"Pending signal {signal_file.name} belongs to another session; not overwritten")
        return None  # PreCompact (or an earlier run) already asked for this memo
    signal = {
        "session_id": session_id,
        "transcript_path": str(transcript_path),
        "project": project,
        "cwd": cwd,
        "timestamp": datetime.now().isoformat(),
        "source": "session-end",
    }
    # Structural state file (category a): atomic temp + os.replace, as PreCompact.
    tmp_file = signal_file.with_name(signal_file.name + ".tmp")
    try:
        tmp_file.write_text(json.dumps(signal, indent=2))
        os.replace(tmp_file, signal_file)
    except OSError:
        tmp_file.unlink(missing_ok=True)
        raise
    log_info(
        f"Relay memo signal written: {signal_file.name} "
        f"({session_meta.get('tool_uses')} tool uses, project {project})"
    )
    return signal_file


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
