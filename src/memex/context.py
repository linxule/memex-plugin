from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from memex.scripts.utils import (
    cleanup_orphaned_sessions,
    get_pending_memos,
    log_info,
    parse_frontmatter,
    safe_project_path,
    truncate_to_tokens,
)


def build_standard_context(memex: Path, project: str | None, settings) -> str | None:
    """Build standard-level context: titles, counts, graph summary."""
    parts = []

    if project and project != "_uncategorized":
        parts.append(f"📁 **Project: {project}**")

        try:
            project_path = safe_project_path(project, memex)
            memos_dir = project_path / "memos"

            if memos_dir.exists():
                memo_files = sorted(
                    memos_dir.glob("*.md"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )[:3]

                if memo_files:
                    titles = []
                    for memo_file in memo_files:
                        frontmatter = parse_frontmatter(memo_file.read_text())
                        title = frontmatter.get("title", memo_file.stem)
                        titles.append(f"- {title}")
                    parts.append("📝 Recent memos:\n" + "\n".join(titles))

            open_count = count_open_threads(memex, project)
            if open_count > 0:
                parts.append(f"🎯 Open threads: {open_count}")

        except (ValueError, FileNotFoundError):
            pass

    graph_summary = get_graph_summary(memex)
    if graph_summary:
        parts.append(graph_summary)

    try:
        from obsidian_cli import ObsidianCLI

        cli = ObsidianCLI(vault="memex", timeout=2)
        if cli.is_available():
            recents = cli.recents()
            if recents:
                recent_list = [f"- {recent}" for recent in recents[:5]]
                parts.append("📖 Recently opened:\n" + "\n".join(recent_list))
    except Exception:
        pass

    pending = get_pending_memos()
    if pending:
        parts.append(f"⚠️ {len(pending)} memo(s) pending retry")

    parts.append("\nUse `/memex:search` for detailed lookup, `/memex:status` for full stats.")

    if parts:
        return "\n\n".join(parts)
    return None


def build_full_context(memex: Path, project: str | None, settings) -> str | None:
    """Build full-level context: everything (original behavior)."""
    context_parts = []

    if project:
        log_info(f"Detected project: {project}")

        project_context = load_project_context(memex, project)
        if project_context:
            context_parts.append(project_context)

        open_threads = extract_open_threads(memex, project)
        if open_threads:
            context_parts.append(open_threads)

        memos_context = load_recent_memos(memex, project, settings)
        if memos_context:
            context_parts.append(memos_context)

    global_context = load_global_memory(memex)
    if global_context:
        context_parts.append(global_context)

    pending_context = check_pending_memos()
    if pending_context:
        context_parts.append(pending_context)

    orphaned = cleanup_orphaned_sessions(max_age_hours=24)
    if orphaned:
        log_info(f"Cleaned up {len(orphaned)} orphaned sessions")

    if context_parts:
        full_context = "\n\n---\n\n".join(context_parts)
        max_tokens = getattr(settings, "max_context_tokens", 6000) if settings else 6000
        return truncate_to_tokens(full_context, max_tokens)

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
            reverse=True,
        )[:5]

        count = 0
        for memo_file in memo_files:
            try:
                count += len(re.findall(r"- \[ \] ", memo_file.read_text()))
            except Exception:
                continue

        return count
    except Exception:
        return 0


def get_graph_summary(memex: Path) -> str | None:
    """Get brief graph statistics."""
    index_path = memex / "_index.sqlite"
    if not index_path.exists():
        return None

    try:
        conn = sqlite3.connect(index_path)
        try:
            broken = conn.execute(
                "SELECT COUNT(*) FROM wikilinks WHERE is_broken = 1"
            ).fetchone()[0]
            open_tasks = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE completed = 0"
            ).fetchone()[0]

            parts = []
            if broken > 0:
                parts.append(f"{broken} broken links")
            if open_tasks > 0:
                parts.append(f"{open_tasks} open tasks")

            if parts:
                return "📊 Graph: " + ", ".join(parts)
        except sqlite3.OperationalError:
            pass
        finally:
            conn.close()
    except Exception:
        pass

    return None


def load_project_context(memex: Path, project: str) -> str | None:
    """Load project overview if it exists."""
    try:
        project_path = safe_project_path(project, memex)
        project_meta = project_path / "_project.md"

        if project_meta.exists():
            lines = project_meta.read_text().split("\n")
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


def load_recent_memos(memex: Path, project: str, settings) -> str | None:
    """Load recent memos for the project."""
    max_memos = (
        getattr(getattr(settings, "session_start", None), "load_recent_memos", 3)
        if settings
        else 3
    )

    try:
        project_path = safe_project_path(project, memex)
        memos_dir = project_path / "memos"

        if not memos_dir.exists():
            return None

        memo_files = sorted(
            memos_dir.glob("*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
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
    """Extract a compact summary from a memo file."""
    try:
        content = memo_path.read_text()
        frontmatter = parse_frontmatter(content)

        title = frontmatter.get("title", memo_path.stem)
        date = frontmatter.get("date", "")

        body_start = content.find("---", 3)
        body = content[body_start + 3 :].strip() if body_start > 0 else content

        parts = [f"**{title}**"]
        if date:
            parts.append(f"({date})")

        summary = " ".join(parts)
        decisions = extract_section(body, ["Key Decisions", "Decisions", "Key Points"])
        if decisions:
            summary += f"\n{decisions}"

        return summary
    except Exception:
        return None


def extract_section(body: str, section_names: list[str]) -> str | None:
    """Extract a named section from memo body."""
    for name in section_names:
        pattern = rf"#{{2,}}\s*{re.escape(name)}\s*\n(.*?)(?=\n#{{2,}}|\Z)"
        match = re.search(pattern, body, re.IGNORECASE | re.DOTALL)
        if match:
            section = match.group(1).strip()
            if len(section) > 400:
                section = section[:400] + "..."
            return section
    return None


def extract_open_threads(memex: Path, project: str) -> str | None:
    """Extract actionable open threads from recent memos."""
    try:
        project_path = safe_project_path(project, memex)
        memos_dir = project_path / "memos"

        if not memos_dir.exists():
            return None

        memo_files = sorted(
            memos_dir.glob("*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:5]

        all_threads = []
        for memo_file in memo_files:
            try:
                content = memo_file.read_text()
                frontmatter = parse_frontmatter(content)
                title = frontmatter.get("title", memo_file.stem)

                body_start = content.find("---", 3)
                body = content[body_start + 3 :].strip() if body_start > 0 else content

                threads = extract_section(
                    body,
                    ["Open Threads", "Open Items", "TODO", "Next Steps"],
                )
                if threads:
                    unchecked = re.findall(r"- \[ \] (.+)", threads)
                    if unchecked:
                        all_threads.append((title, unchecked))
            except Exception:
                continue

        if not all_threads:
            return None

        lines = ["🎯 **Open threads from previous sessions:**\n"]
        for title, items in all_threads[:3]:
            lines.append(f"*From {title}:*")
            for item in items[:4]:
                lines.append(f"- [ ] {item}")
            lines.append("")

        return "\n".join(lines)
    except Exception:
        return None


def load_global_memory(memex: Path) -> str | None:
    """Load vault guidance from MEMORY.md if present."""
    memory_file = memex / "MEMORY.md"
    if not memory_file.exists():
        return None

    content = memory_file.read_text().strip()
    if len(content) < 100:
        return None

    if content.startswith("---"):
        body_start = content.find("---", 3)
        if body_start > 0:
            content = content[body_start + 3 :].strip()

    if len(content) > 1000:
        content = content[:1000] + "\n\n[...see MEMORY.md for more]"

    if content:
        return f"📚 **Vault Guide:**\n\n{content}"
    return None


def check_pending_memos() -> str | None:
    """Return a pending memo alert if any failed generations exist."""
    pending = get_pending_memos()
    if not pending:
        return None

    alert = f"⚠️ **{len(pending)} memo(s) failed to generate:**\n"
    for item in pending[:3]:
        session = item.get("session_id", "unknown")[:8]
        error = item.get("last_error", "unknown error")
        alert += f"- Session {session}... ({error})\n"

    if len(pending) > 3:
        alert += f"- ...and {len(pending) - 3} more\n"

    alert += "\nRun `/memex:retry` to regenerate."
    return alert
