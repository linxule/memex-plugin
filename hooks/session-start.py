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

Loads project context and recent memos when a new session starts.

Input (stdin):
{
    "session_id": "abc123",
    "transcript_path": "/path/to/transcript.jsonl",
    "cwd": "/path/to/myproject",
    "hook_event_name": "SessionStart",
    "source": "startup",  // "startup", "resume", "clear", "compact"
    "model": "claude-sonnet-4-20250514"
}

Actions:
1. Check source - skip/minimize for "resume"
2. Detect project
3. Load project overview (if exists)
4. Load recent memos (up to 3)
5. Alert about pending memos
6. Clean up orphaned sessions
7. Output context for injection
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

# Add scripts directory to path for imports
scripts_dir = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(scripts_dir))

from utils import (
    read_hook_input,
    get_memex_path,
    get_config,
    detect_project,
    safe_project_path,
    get_pending_memos,
    cleanup_orphaned_sessions,
    truncate_to_tokens,
    parse_frontmatter,
    log_info,
    log_warning,
    output_context,
)


def main():
    # Read hook input
    input_data = read_hook_input()

    session_id = input_data.get("session_id", "")
    cwd = input_data.get("cwd", "")
    source = input_data.get("source", "startup")

    log_info(f"SessionStart hook triggered: session={session_id[:8] if session_id else 'unknown'}..., source={source}")

    # Handle resume - skip full context injection
    if source == "resume":
        log_info("Resuming session, minimal context injection")
        handle_resume()
        sys.exit(0)

    # Handle post-compact - memo should have been generated
    if source == "compact":
        log_info("Post-compaction, checking for memo")
        handle_post_compact(session_id)
        sys.exit(0)

    # Get config and verbosity level
    config = get_config()
    verbosity = config.get("session_context", {}).get("verbosity", "standard")

    # Full context injection for new sessions
    try:
        # Get memex path
        try:
            memex = get_memex_path()
        except ValueError as e:
            log_warning(f"Memex not configured: {e}")
            sys.exit(0)

        # Detect project
        project = detect_project(cwd) if cwd else None

        # Handle verbosity levels
        if verbosity == "minimal":
            # Minimal: Just a hint that memex exists
            context = "Memex available. Use `/memex:search` for past decisions, `/memex:status` for overview."
            output_context(context)
            log_info(f"Minimal context injection ({len(context)} chars)")
            sys.exit(0)

        elif verbosity == "standard":
            # Standard: Project + memo titles + counts + graph summary
            context = build_standard_context(memex, project, config)
            if context:
                output_context(context)
                log_info(f"Standard context injection ({len(context)} chars)")
            sys.exit(0)

        else:  # "full"
            # Full: Everything (original behavior)
            context = build_full_context(memex, project, config)
            if context:
                output_context(context)
                log_info(f"Full context injection ({len(context)} chars)")
            sys.exit(0)

    except Exception as e:
        log_warning(f"SessionStart hook error: {e}")
        # Non-blocking - session continues without context

    sys.exit(0)


def build_standard_context(memex: Path, project: str | None, config: dict) -> str | None:
    """Build standard-level context: titles, counts, graph summary."""
    parts = []

    if project and project != "_uncategorized":
        parts.append(f"📁 **Project: {project}**")

        # Get memo titles (not full content)
        try:
            project_path = safe_project_path(project, memex)
            memos_dir = project_path / "memos"

            if memos_dir.exists():
                memo_files = sorted(
                    memos_dir.glob("*.md"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True
                )[:3]

                if memo_files:
                    titles = []
                    for mf in memo_files:
                        fm = parse_frontmatter(mf.read_text())
                        title = fm.get("title", mf.stem)
                        titles.append(f"- {title}")
                    parts.append("📝 Recent memos:\n" + "\n".join(titles))

            # Count open threads
            open_count = count_open_threads(memex, project)
            if open_count > 0:
                parts.append(f"🎯 Open threads: {open_count}")

        except (ValueError, FileNotFoundError):
            pass

    # Graph summary (if index exists)
    graph_summary = get_graph_summary(memex)
    if graph_summary:
        parts.append(graph_summary)

    # Check pending memos
    pending = get_pending_memos()
    if pending:
        parts.append(f"⚠️ {len(pending)} memo(s) pending retry")

    # Add hint for more detail
    parts.append("\nUse `/memex:search` for detailed lookup, `/memex:status` for full stats.")

    if parts:
        return "\n\n".join(parts)
    return None


def build_full_context(memex: Path, project: str | None, config: dict) -> str | None:
    """Build full-level context: everything (original behavior)."""
    context_parts = []

    if project:
        log_info(f"Detected project: {project}")

        # Load project context
        project_context = load_project_context(memex, project)
        if project_context:
            context_parts.append(project_context)

        # IMPORTANT: Open threads first - these are actionable
        open_threads = extract_open_threads(memex, project)
        if open_threads:
            context_parts.append(open_threads)

        # Load recent memos (key decisions)
        memos_context = load_recent_memos(memex, project)
        if memos_context:
            context_parts.append(memos_context)

    # Load global memory if exists
    global_context = load_global_memory(memex)
    if global_context:
        context_parts.append(global_context)

    # Check for pending memos
    pending_context = check_pending_memos()
    if pending_context:
        context_parts.append(pending_context)

    # Cleanup orphaned sessions (background maintenance)
    orphaned = cleanup_orphaned_sessions(max_age_hours=24)
    if orphaned:
        log_warning(f"Cleaned up {len(orphaned)} orphaned sessions")

    # Output combined context
    if context_parts:
        full_context = "\n\n---\n\n".join(context_parts)

        # Respect token limits
        max_tokens = config.get("max_context_tokens", 6000)
        full_context = truncate_to_tokens(full_context, max_tokens)
        return full_context

    return None


def count_open_threads(memex: Path, project: str) -> int:
    """Count open threads across recent memos."""
    try:
        project_path = safe_project_path(project, memex)
        memos_dir = project_path / "memos"

        if not memos_dir.exists():
            return 0

        memo_files = sorted(
            memos_dir.glob("*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )[:5]

        count = 0
        for memo_file in memo_files:
            try:
                content = memo_file.read_text()
                # Count unchecked items
                count += len(re.findall(r'- \[ \] ', content))
            except Exception:
                continue

        return count
    except Exception:
        return 0


def get_graph_summary(memex: Path) -> str | None:
    """Get brief graph statistics."""
    import sqlite3

    index_path = memex / "_index.sqlite"
    if not index_path.exists():
        return None

    try:
        conn = sqlite3.connect(index_path)
        try:
            # Get quick stats
            broken = conn.execute("SELECT COUNT(*) FROM wikilinks WHERE is_broken = 1").fetchone()[0]
            open_tasks = conn.execute("SELECT COUNT(*) FROM tasks WHERE completed = 0").fetchone()[0]

            parts = []
            if broken > 0:
                parts.append(f"{broken} broken links")
            if open_tasks > 0:
                parts.append(f"{open_tasks} open tasks")

            if parts:
                return "📊 Graph: " + ", ".join(parts)

        except sqlite3.OperationalError:
            pass  # Tables don't exist yet
        finally:
            conn.close()
    except Exception:
        pass

    return None


def handle_resume():
    """Handle session resume - minimal context."""
    pending = get_pending_memos()
    if pending:
        print(f"📝 Note: {len(pending)} memo(s) pending regeneration. Use /memex:retry to process.")
    sys.exit(0)


def handle_post_compact(session_id: str):
    """Handle post-compaction - check for pending memo, inject instructions."""
    parts = ["📚 Session compacted."]

    # Check for pending memo signal from PreCompact hook
    signal_dir = Path.home() / ".memex" / "pending-memos"
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
        parts.append(
            f"\n⚠️ **Memo needed**: A memo was not saved before compaction. "
            f"Transcript at: `{transcript_path}`\n"
            f"Project: {project}\n\n"
            f"Please spawn a background subagent to generate the memo:\n"
            f"```\n"
            f"Task(subagent_type='general-purpose', run_in_background=true,\n"
            f"     model='haiku',\n"
            f"     prompt='Generate a session memo from the transcript at {transcript_path}. "
            f"Read the memo prompt at {memex_path}/prompts/memo-default.md for format guidance. "
            f"Search for related memos using: uv run {memex_path}/scripts/search.py \"<keywords>\" --mode=hybrid --format=text. "
            f"Save the memo to {memex_path}/projects/{project}/memos/')\n"
            f"```"
        )
    else:
        parts.append(
            "\nUse `/memex:search <keywords>` to recall prior decisions, "
            "`/memex:status` for overview."
        )

    context = "\n".join(parts)
    output_context(context)
    log_info(f"Post-compact injection ({len(context)} chars)")
    sys.exit(0)


def load_project_context(memex: Path, project: str) -> str | None:
    """Load project overview if exists."""
    try:
        project_path = safe_project_path(project, memex)
        project_meta = project_path / "_project.md"

        if project_meta.exists():
            content = project_meta.read_text()
            # Extract just the overview section, not full history
            lines = content.split("\n")
            overview_lines = []
            in_overview = False

            for line in lines:
                if line.startswith("## Overview"):
                    in_overview = True
                    overview_lines.append(line)
                elif line.startswith("## ") and in_overview:
                    break
                elif in_overview:
                    overview_lines.append(line)

            if overview_lines:
                return f"📁 **Project: {project}**\n\n" + "\n".join(overview_lines)

    except (ValueError, FileNotFoundError):
        pass

    return None


def load_recent_memos(memex: Path, project: str) -> str | None:
    """Load recent memos for the project."""
    config = get_config()
    max_memos = config.get("session_start", {}).get("load_recent_memos", 3)

    try:
        project_path = safe_project_path(project, memex)
        memos_dir = project_path / "memos"

        if not memos_dir.exists():
            return None

        # Find recent memo files
        memo_files = sorted(
            memos_dir.glob("*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )[:max_memos]

        if not memo_files:
            return None

        memo_summaries = []
        for memo_file in memo_files:
            summary = summarize_memo(memo_file)
            if summary:
                memo_summaries.append(summary)

        if memo_summaries:
            return "📝 **Recent Session Memos:**\n\n" + "\n\n".join(memo_summaries)

    except (ValueError, FileNotFoundError):
        pass

    return None


def summarize_memo(memo_path: Path) -> str | None:
    """Extract summary from memo file."""
    try:
        content = memo_path.read_text()
        frontmatter = parse_frontmatter(content)

        title = frontmatter.get("title", memo_path.stem)
        date = frontmatter.get("date", "")

        # Extract body
        body_start = content.find("---", 3)
        body = content[body_start + 3:].strip() if body_start > 0 else content

        parts = [f"**{title}**"]
        if date:
            parts.append(f"({date})")

        summary = " ".join(parts)

        # Extract key decisions (more useful than first paragraph)
        decisions = extract_section(body, ["Key Decisions", "Decisions", "Key Points"])
        if decisions:
            summary += f"\n{decisions}"

        return summary

    except Exception:
        return None


def extract_section(body: str, section_names: list[str]) -> str | None:
    """Extract a named section from memo body."""
    for name in section_names:
        # Look for ## Section Name or ### Section Name (2+ hashes)
        # Note: {{2,}} escapes the curly braces in f-string
        pattern = rf'#{{2,}}\s*{re.escape(name)}\s*\n(.*?)(?=\n#{{2,}}|\Z)'
        match = re.search(pattern, body, re.IGNORECASE | re.DOTALL)
        if match:
            section = match.group(1).strip()
            # Truncate if too long
            if len(section) > 400:
                section = section[:400] + "..."
            return section

    return None


def extract_open_threads(memex: Path, project: str) -> str | None:
    """Extract all open threads from recent memos - these are actionable."""
    try:
        project_path = safe_project_path(project, memex)
        memos_dir = project_path / "memos"

        if not memos_dir.exists():
            return None

        # Get recent memos
        memo_files = sorted(
            memos_dir.glob("*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )[:5]  # Check more memos for open threads

        all_threads = []

        for memo_file in memo_files:
            try:
                content = memo_file.read_text()
                frontmatter = parse_frontmatter(content)
                title = frontmatter.get("title", memo_file.stem)

                # Extract body
                body_start = content.find("---", 3)
                body = content[body_start + 3:].strip() if body_start > 0 else content

                # Find open threads section
                threads = extract_section(body, ["Open Threads", "Open Items", "TODO", "Next Steps"])
                if threads:
                    # Parse individual items (lines starting with - [ ])
                    unchecked = re.findall(r'- \[ \] (.+)', threads)
                    if unchecked:
                        all_threads.append((title, unchecked))

            except Exception:
                continue

        if not all_threads:
            return None

        # Format open threads prominently
        lines = ["🎯 **Open threads from previous sessions:**\n"]
        for title, items in all_threads[:3]:  # Limit to 3 memos
            lines.append(f"*From {title}:*")
            for item in items[:4]:  # Limit items per memo
                lines.append(f"- [ ] {item}")
            lines.append("")

        return "\n".join(lines)

    except Exception:
        return None


def load_global_memory(memex: Path) -> str | None:
    """Load vault awareness guide from MEMORY.md if exists."""
    memory_file = memex / "MEMORY.md"

    if not memory_file.exists():
        return None

    content = memory_file.read_text().strip()

    # Skip if just template/placeholder
    if len(content) < 100:
        return None

    # Skip frontmatter, get body
    if content.startswith("---"):
        body_start = content.find("---", 3)
        if body_start > 0:
            content = content[body_start + 3:].strip()

    # Truncate if too long
    if len(content) > 1000:
        content = content[:1000] + "\n\n[...see MEMORY.md for more]"

    if content:
        return f"📚 **Vault Guide:**\n\n{content}"

    return None


def check_pending_memos() -> str | None:
    """Check for pending memos and return alert if any."""
    pending = get_pending_memos()

    if not pending:
        return None

    alert = f"⚠️ **{len(pending)} memo(s) failed to generate:**\n"
    for p in pending[:3]:  # Show up to 3
        session = p.get("session_id", "unknown")[:8]
        error = p.get("last_error", "unknown error")
        alert += f"- Session {session}... ({error})\n"

    if len(pending) > 3:
        alert += f"- ...and {len(pending) - 3} more\n"

    alert += "\nRun `/memex:retry` to regenerate."

    return alert


if __name__ == "__main__":
    main()
