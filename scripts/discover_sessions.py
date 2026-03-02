# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "filelock>=3.0",
#     "tiktoken>=0.5",
# ]
# ///
"""
Discover unprocessed Claude Code sessions.

Scans ~/.claude/projects/ for JSONL session files and cross-references
with memex transcripts to find sessions that haven't been archived.

Adapted from peteromallet/dataclaw discover_projects() pattern.

Usage:
    uv run scripts/discover_sessions.py                    # summary by project
    uv run scripts/discover_sessions.py --verbose          # list each unprocessed session
    uv run scripts/discover_sessions.py --triage           # score sessions by value
    uv run scripts/discover_sessions.py --triage --min-score=10  # only high-value
    uv run scripts/discover_sessions.py --project=memex    # filter to one project
    uv run scripts/discover_sessions.py --since=7d         # only recent sessions
    uv run scripts/discover_sessions.py --import           # import unprocessed into memex
    uv run scripts/discover_sessions.py --json             # machine-readable output
"""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Import from utils (when used as module)
try:
    from .utils import (
        claude_dir_to_project_name, sanitize_project_name,
        get_memex_path, log_info, log_warning, log_error,
    )
except ImportError:
    # Standalone mode
    sys.path.insert(0, str(Path(__file__).parent))
    from utils import (
        claude_dir_to_project_name, sanitize_project_name,
        get_memex_path, log_info, log_warning, log_error,
    )


CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"

# Minimum file size to consider a session worth processing (bytes)
MIN_SESSION_SIZE = 1024  # 1KB — filters out aborted/test sessions


def discover_projects() -> list[dict]:
    """Discover all Claude Code projects with session metadata.

    Scans ~/.claude/projects/ for directories containing JSONL session files.
    Returns project info with session counts and sizes.
    """
    if not PROJECTS_DIR.exists():
        return []

    projects = []
    for project_dir in sorted(PROJECTS_DIR.iterdir()):
        if not project_dir.is_dir():
            continue
        sessions = list(project_dir.glob("*.jsonl"))
        if not sessions:
            continue

        # Get session stats
        total_size = 0
        newest = None
        oldest = None
        for f in sessions:
            stat = f.stat()
            total_size += stat.st_size
            mtime = datetime.fromtimestamp(stat.st_mtime)
            if newest is None or mtime > newest:
                newest = mtime
            if oldest is None or mtime < oldest:
                oldest = mtime

        projects.append({
            "dir_name": project_dir.name,
            "display_name": claude_dir_to_project_name(project_dir.name),
            "memex_name": sanitize_project_name(
                claude_dir_to_project_name(project_dir.name)
            ),
            "session_count": len(sessions),
            "total_size_bytes": total_size,
            "newest_session": newest.isoformat() if newest else None,
            "oldest_session": oldest.isoformat() if oldest else None,
            "path": str(project_dir),
        })

    return projects


def get_memex_session_ids(memex: Path) -> tuple[set[str], set[str]]:
    """Get all session IDs already archived in memex (from transcripts).

    Memex renames transcripts with date prefixes: YYYYMMDD-HHMMSS-<uuid8>
    while Claude stores them as full UUIDs. We extract the UUID prefix
    (first 8 chars) for matching against full UUIDs.
    """
    session_ids = set()       # Full stems for exact match
    uuid_prefixes = set()     # 8-char UUID prefixes for fuzzy match

    for pattern in ("projects/*/transcripts/*.md", "projects/*/transcripts/*.jsonl"):
        for f in memex.glob(pattern):
            stem = f.stem
            session_ids.add(stem)
            # Extract UUID portion from date-prefixed names
            if re.match(r'^\d{8}-\d{6}-', stem):
                uuid_prefix = stem[16:]  # after YYYYMMDD-HHMMSS-
                if uuid_prefix:
                    uuid_prefixes.add(uuid_prefix)
            else:
                # Might be a raw UUID — take first 8 chars
                uuid_prefixes.add(stem[:8])

    return session_ids, uuid_prefixes


def discover_unprocessed(
    project_filter: str | None = None,
    since: timedelta | None = None,
    min_size: int = MIN_SESSION_SIZE,
) -> list[dict]:
    """Find sessions in ~/.claude/projects/ not yet in memex.

    Args:
        project_filter: Only check this project (by display_name or memex_name)
        since: Only look at sessions modified within this timedelta
        min_size: Minimum file size to consider (filters tiny/aborted sessions)

    Returns:
        List of dicts with session info and suggested memex project.
    """
    memex = get_memex_path()
    known_ids, known_prefixes = get_memex_session_ids(memex)
    cutoff = datetime.now() - since if since else None

    unprocessed = []

    for project_dir in sorted(PROJECTS_DIR.iterdir()):
        if not project_dir.is_dir():
            continue

        display_name = claude_dir_to_project_name(project_dir.name)
        memex_name = sanitize_project_name(display_name)

        # Filter by project if specified
        if project_filter:
            pf = project_filter.lower()
            if pf not in (display_name.lower(), memex_name.lower()):
                continue

        for session_file in sorted(project_dir.glob("*.jsonl")):
            session_id = session_file.stem

            # Already processed? Check exact match and UUID prefix match
            if session_id in known_ids:
                continue
            if session_id[:8] in known_prefixes:
                continue

            stat = session_file.stat()

            # Too small?
            if stat.st_size < min_size:
                continue

            # Too old?
            mtime = datetime.fromtimestamp(stat.st_mtime)
            if cutoff and mtime < cutoff:
                continue

            # Quick peek: count lines for viability
            line_count = 0
            try:
                with open(session_file) as f:
                    for line in f:
                        if line.strip():
                            line_count += 1
                        if line_count > 10:
                            break  # Enough to know it's substantial
            except OSError:
                continue

            unprocessed.append({
                "session_id": session_id,
                "project_display": display_name,
                "project_memex": memex_name,
                "source_path": str(session_file),
                "size_bytes": stat.st_size,
                "modified": mtime.isoformat(),
                "line_count_sample": line_count,
            })

    return unprocessed


# ============================================================================
# Triage: fast viability scoring without full parse
# ============================================================================

# String patterns for fast line-by-line scanning (no JSON parse needed)
_RE_TOOL_USE = re.compile(r'"type"\s*:\s*"tool_use"')
_RE_TOOL_WRITE = re.compile(r'"name"\s*:\s*"(?:Write|Edit)"')
_RE_TOOL_BASH = re.compile(r'"name"\s*:\s*"Bash"')
_RE_TOOL_READ = re.compile(r'"name"\s*:\s*"Read"')
_RE_TOOL_TASK = re.compile(r'"name"\s*:\s*"Task"')
_RE_USER_MSG = re.compile(r'"type"\s*:\s*"user"')
_RE_ASSISTANT_MSG = re.compile(r'"type"\s*:\s*"assistant"')
_RE_GIT_COMMIT = re.compile(r'\[[\w\-/]+ [a-f0-9]{7,}\]')
_RE_MODEL_OPUS = re.compile(r'"model"\s*:\s*"claude-opus')
_RE_MODEL_SONNET = re.compile(r'"model"\s*:\s*"claude-sonnet')
_RE_SUBAGENT = re.compile(r'"agentId"\s*:')
_RE_ERROR = re.compile(r'"is_error"\s*:\s*true')
_RE_COMPACT = re.compile(r'"isCompactSummary"\s*:\s*true')
_RE_TIMESTAMP = re.compile(r'"timestamp"\s*:\s*"([^"]+)"')

# Score thresholds
SCORE_SKIP = 3       # 0-3: noise/aborted
SCORE_LOW = 8        # 4-8: quick tasks
SCORE_MODERATE = 15  # 9-15: worth importing
                     # 16+: high value


def triage_session(session_path: Path) -> dict:
    """Fast viability scoring of a JSONL session file.

    Scans lines with regex (no JSON parse) for speed, except for
    the first user message which gets parsed for a preview.

    Returns dict with signal counts, score, grade, and first_message preview.
    """
    signals = {
        "user_messages": 0,
        "assistant_messages": 0,
        "tool_uses": 0,
        "writes_edits": 0,
        "bash_commands": 0,
        "reads": 0,
        "subagent_spawns": 0,
        "git_commits": 0,
        "errors": 0,
        "compactions": 0,
        "model": None,
        "first_message": None,
        "duration_minutes": 0,
    }

    first_ts = None
    last_ts = None
    first_user_line = None

    try:
        with open(session_path) as f:
            for line in f:
                if not line.strip():
                    continue

                # Timestamp tracking
                ts_match = _RE_TIMESTAMP.search(line)
                if ts_match:
                    ts_str = ts_match.group(1)
                    if first_ts is None:
                        first_ts = ts_str
                    last_ts = ts_str

                # Message counts
                if _RE_USER_MSG.search(line):
                    signals["user_messages"] += 1
                    if first_user_line is None:
                        first_user_line = line
                elif _RE_ASSISTANT_MSG.search(line):
                    signals["assistant_messages"] += 1

                # Tool usage
                if _RE_TOOL_USE.search(line):
                    signals["tool_uses"] += 1
                if _RE_TOOL_WRITE.search(line):
                    signals["writes_edits"] += 1
                if _RE_TOOL_BASH.search(line):
                    signals["bash_commands"] += 1
                if _RE_TOOL_READ.search(line):
                    signals["reads"] += 1
                if _RE_TOOL_TASK.search(line):
                    signals["subagent_spawns"] += 1

                # Git commits
                if _RE_GIT_COMMIT.search(line):
                    signals["git_commits"] += 1

                # Errors
                if _RE_ERROR.search(line):
                    signals["errors"] += 1

                # Model detection (first match wins)
                if signals["model"] is None:
                    if _RE_MODEL_OPUS.search(line):
                        signals["model"] = "opus"
                    elif _RE_MODEL_SONNET.search(line):
                        signals["model"] = "sonnet"

                # Compaction
                if _RE_COMPACT.search(line):
                    signals["compactions"] += 1

                # Subagent (deduplicated by agentId presence)
                if _RE_SUBAGENT.search(line):
                    pass  # counted via Task tool above

    except OSError:
        return {**signals, "score": 0, "grade": "error", "first_message": None}

    # Parse first user message for preview
    if first_user_line:
        try:
            entry = json.loads(first_user_line)
            content = entry.get("message", entry.get("content", ""))
            if isinstance(content, dict):
                content = content.get("content", "")
            if isinstance(content, list):
                texts = [b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text"]
                content = " ".join(texts)
            if isinstance(content, str):
                # Strip system tags for cleaner preview
                content = re.sub(r'<system-reminder>.*?</system-reminder>', '', content, flags=re.DOTALL)
                content = content.strip()
                signals["first_message"] = content[:150].replace("\n", " ")
        except (json.JSONDecodeError, TypeError):
            pass

    # Duration from timestamps
    if first_ts and last_ts:
        try:
            t0 = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
            signals["duration_minutes"] = max(0, int((t1 - t0).total_seconds() / 60))
        except (ValueError, TypeError):
            pass

    # Compute score
    score = _compute_score(signals)
    signals["score"] = score
    signals["grade"] = _score_to_grade(score)

    return signals


def _compute_score(s: dict) -> int:
    """Compute viability score from signal counts.

    Scoring philosophy:
    - File mutations (Write/Edit) are the strongest signal of real work
    - Git commits prove tangible output
    - Subagent spawns indicate complex orchestration
    - Turn count reflects conversation depth
    - Duration shows sustained engagement
    - Opus model correlates with deeper architectural work
    - Errors are slightly negative (but debugging has value too)
    """
    score = 0

    # File mutations: strongest signal (capped)
    score += min(s["writes_edits"] * 3, 15)

    # Git commits: tangible output
    score += min(s["git_commits"] * 5, 15)

    # Bash commands: real work being done
    score += min(s["bash_commands"], 5)

    # Conversation depth: turns matter
    score += min(s["user_messages"], 10)

    # Subagent spawns: complex tasks
    score += min(s["subagent_spawns"] * 3, 9)

    # Compactions: session was long enough to compact
    score += s["compactions"] * 4

    # Duration bonuses
    dur = s["duration_minutes"]
    if dur >= 60:
        score += 8
    elif dur >= 30:
        score += 5
    elif dur >= 10:
        score += 3
    elif dur >= 5:
        score += 1

    # Model bonus: opus sessions tend to be deeper
    if s["model"] == "opus":
        score += 2

    # First message intent: longer opening = more thoughtful request
    if s["first_message"] and len(s["first_message"]) > 100:
        score += 2

    # Error penalty (mild — debugging is still valuable)
    score -= min(s["errors"], 3)

    return max(0, score)


def _score_to_grade(score: int) -> str:
    """Convert numeric score to human-readable grade."""
    if score >= SCORE_MODERATE + 1:
        return "high"
    elif score >= SCORE_LOW + 1:
        return "moderate"
    elif score >= SCORE_SKIP + 1:
        return "low"
    else:
        return "skip"


GRADE_SYMBOLS = {"high": "+", "moderate": "~", "low": ".", "skip": " "}


def triage_all(sessions: list[dict]) -> list[dict]:
    """Run triage on a list of unprocessed sessions.

    Adds triage signals and score to each session dict.
    """
    for session in sessions:
        triage = triage_session(Path(session["source_path"]))
        session["triage"] = triage
        session["score"] = triage["score"]
        session["grade"] = triage["grade"]
    return sessions


def import_sessions(sessions: list[dict], dry_run: bool = True) -> list[dict]:
    """Import unprocessed sessions into memex via transcript_to_md.

    Args:
        sessions: List from discover_unprocessed()
        dry_run: If True, only report what would be done

    Returns:
        List of results with status per session.
    """
    try:
        from .transcript_to_md import convert_transcript_file
    except ImportError:
        from transcript_to_md import convert_transcript_file

    memex = get_memex_path()
    results = []

    for session in sessions:
        source = Path(session["source_path"])
        project = session["project_memex"]
        session_id = session["session_id"]

        # Target path in memex
        target_dir = memex / "projects" / project / "transcripts"
        target_path = target_dir / f"{session_id}.md"

        if target_path.exists():
            results.append({
                **session,
                "status": "skipped",
                "reason": "already exists",
            })
            continue

        if dry_run:
            results.append({
                **session,
                "status": "would_import",
                "target": str(target_path),
            })
            continue

        # Actually import
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            result_path = convert_transcript_file(
                source,
                output_path=target_path,
                session_id=session_id,
                project=project,
            )
            if result_path:
                results.append({
                    **session,
                    "status": "imported",
                    "target": str(result_path),
                })
                log_info(f"Imported {session_id[:8]} -> {project}")
            else:
                results.append({
                    **session,
                    "status": "failed",
                    "reason": "conversion returned None",
                })
        except Exception as e:
            results.append({
                **session,
                "status": "failed",
                "reason": str(e),
            })
            log_error(f"Failed to import {session_id[:8]}: {e}")

    return results


def parse_duration(s: str) -> timedelta | None:
    """Parse duration string like '7d', '2w', '3m' into timedelta."""
    match = re.match(r'^(\d+)([dwm])$', s.lower())
    if not match:
        return None
    n, unit = int(match.group(1)), match.group(2)
    if unit == 'd':
        return timedelta(days=n)
    elif unit == 'w':
        return timedelta(weeks=n)
    elif unit == 'm':
        return timedelta(days=n * 30)
    return None


def format_size(size_bytes: int) -> str:
    """Format byte count as human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f}MB"


def main():
    parser = argparse.ArgumentParser(
        description="Discover unprocessed Claude Code sessions"
    )
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="List each unprocessed session")
    parser.add_argument("--project", "-p", type=str,
                        help="Filter to a specific project")
    parser.add_argument("--since", type=str,
                        help="Only recent sessions (e.g., 7d, 2w, 3m)")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON")
    parser.add_argument("--import", dest="do_import", action="store_true",
                        help="Import unprocessed sessions into memex")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Show what would be imported (default)")
    parser.add_argument("--apply", action="store_true",
                        help="Actually import (overrides --dry-run)")
    parser.add_argument("--triage", "-t", action="store_true",
                        help="Score sessions by viability before import")
    parser.add_argument("--min-score", type=int, default=0,
                        help="Minimum triage score to include (use with --triage)")
    parser.add_argument("--all-projects", action="store_true",
                        help="Show all Claude projects (not just unprocessed)")
    args = parser.parse_args()

    if not PROJECTS_DIR.exists():
        print(f"Claude projects directory not found: {PROJECTS_DIR}")
        sys.exit(1)

    since = parse_duration(args.since) if args.since else None

    # Mode: list all projects
    if args.all_projects:
        projects = discover_projects()
        if args.json:
            print(json.dumps(projects, indent=2))
        else:
            print(f"Claude Code projects ({len(projects)}):\n")
            for p in sorted(projects, key=lambda x: -x["session_count"]):
                print(f"  {p['display_name']:<30} {p['session_count']:>4} sessions  "
                      f"{format_size(p['total_size_bytes']):>8}")
        return

    # Mode: discover unprocessed
    unprocessed = discover_unprocessed(
        project_filter=args.project,
        since=since,
    )

    # Mode: triage (score all sessions)
    if args.triage:
        triage_all(unprocessed)

        # Filter by minimum score
        if args.min_score > 0:
            unprocessed = [s for s in unprocessed if s["score"] >= args.min_score]

        # Sort by score descending
        unprocessed.sort(key=lambda x: -x["score"])

        if args.json:
            print(json.dumps(unprocessed, indent=2))
            return

        if not unprocessed:
            if args.min_score > 0:
                print(f"No sessions scored >= {args.min_score}.")
            else:
                print("All sessions are already archived in memex.")
            return

        # Grade distribution
        grades = {"high": [], "moderate": [], "low": [], "skip": []}
        for s in unprocessed:
            grades[s["grade"]].append(s)

        print(f"Triage results ({len(unprocessed)} sessions):\n")
        print(f"  + high (16+):     {len(grades['high']):>3}  — definitely import")
        print(f"  ~ moderate (9-15):{len(grades['moderate']):>3}  — worth importing")
        print(f"  . low (4-8):      {len(grades['low']):>3}  — quick tasks, optional")
        print(f"    skip (0-3):     {len(grades['skip']):>3}  — noise/aborted")
        print()

        # Show sessions by grade
        for grade_name in ("high", "moderate", "low", "skip"):
            sessions = grades[grade_name]
            if not sessions:
                continue
            sym = GRADE_SYMBOLS[grade_name]
            print(f"{'=' * 60}")
            print(f" {sym} {grade_name.upper()} ({len(sessions)} sessions)")
            print(f"{'=' * 60}")
            for s in sessions:
                t = s["triage"]
                dur = f"{t['duration_minutes']}m" if t["duration_minutes"] else "?"
                model = t["model"] or "?"
                tools_summary = []
                if t["writes_edits"]:
                    tools_summary.append(f"{t['writes_edits']}W")
                if t["bash_commands"]:
                    tools_summary.append(f"{t['bash_commands']}B")
                if t["subagent_spawns"]:
                    tools_summary.append(f"{t['subagent_spawns']}A")
                if t["git_commits"]:
                    tools_summary.append(f"{t['git_commits']}C")
                tools_str = "/".join(tools_summary) if tools_summary else "-"

                print(f"  {s['score']:>3}  {s['session_id'][:8]}  {s['modified'][:10]}  "
                      f"{format_size(s['size_bytes']):>7}  {dur:>5}  {model:<7} "
                      f"{tools_str:<12} {s['project_display']}")
                if args.verbose and t["first_message"]:
                    preview = t["first_message"][:100]
                    print(f"       \"{preview}\"")
            print()

        worth_it = grades["high"] + grades["moderate"]
        if worth_it:
            print(f"Recommend importing {len(worth_it)} sessions (high + moderate).")
            print(f"Run with --triage --min-score=9 --import --apply")
        return

    if args.do_import:
        # If min-score is set, triage first to filter
        if args.min_score > 0:
            triage_all(unprocessed)
            unprocessed = [s for s in unprocessed if s.get("score", 999) >= args.min_score]

        dry_run = not args.apply
        results = import_sessions(unprocessed, dry_run=dry_run)

        if args.json:
            print(json.dumps(results, indent=2))
        else:
            imported = [r for r in results if r["status"] == "imported"]
            would = [r for r in results if r["status"] == "would_import"]
            failed = [r for r in results if r["status"] == "failed"]

            if dry_run:
                print(f"Would import {len(would)} sessions (use --apply to execute):\n")
                for r in would:
                    print(f"  {r['session_id'][:8]}  {r['project_display']:<25} "
                          f"{format_size(r['size_bytes']):>8}  -> {r['project_memex']}")
            else:
                print(f"Imported: {len(imported)}, Failed: {len(failed)}")
                for r in failed:
                    print(f"  FAILED: {r['session_id'][:8]} — {r.get('reason', '?')}")
        return

    # Mode: report
    if args.json:
        print(json.dumps(unprocessed, indent=2))
        return

    if not unprocessed:
        print("All sessions are already archived in memex.")
        return

    # Group by project
    by_project: dict[str, list] = {}
    for s in unprocessed:
        by_project.setdefault(s["project_display"], []).append(s)

    total_size = sum(s["size_bytes"] for s in unprocessed)
    print(f"Unprocessed sessions: {len(unprocessed)} ({format_size(total_size)})\n")

    for project, sessions in sorted(by_project.items(), key=lambda x: -len(x[1])):
        proj_size = sum(s["size_bytes"] for s in sessions)
        memex_name = sessions[0]["project_memex"]
        print(f"  {project:<30} {len(sessions):>3} sessions  "
              f"{format_size(proj_size):>8}  -> {memex_name}")

        if args.verbose:
            for s in sorted(sessions, key=lambda x: x["modified"], reverse=True):
                print(f"    {s['session_id'][:8]}  {s['modified'][:10]}  "
                      f"{format_size(s['size_bytes']):>8}")

    print(f"\nRun with --import --apply to archive these into memex.")


if __name__ == "__main__":
    main()
