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

``discover_ignore`` in config.json (cwd substrings or fnmatch globs) keeps
deliberately skipped sessions out of every listing and import. Imports never
take sessions shorter than MIN_IMPORT_SECONDS, and route each session through
``memex.routing.resolve_session_project`` (relay channel first, then the cwd).
"""

import argparse
import fnmatch
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

from memex.scripts.utils import (
    cwd_from_session,
    get_config,
    get_memex_path,
    log_error,
    log_info,
    log_warning,
    project_names_for_claude_dir,
    warn_if_noncanonical,
)


CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"

# Minimum file size to consider a session worth processing (bytes)
MIN_SESSION_SIZE = 1024  # 1KB — filters out aborted/test sessions

# The import path never takes a session whose first and last event are closer
# than this (seconds). A 13 s session was imported under `--min-score 4`
# (2026-10-28): a score is a sum of signals, not proof of a conversation.
MIN_IMPORT_SECONDS = 30

# Routing reads a session's head up to its first few user turns (the relay
# channel lives in the first one), however many bookkeeping / summary records
# precede them; the cap only bounds a pathological file.
_ROUTE_USER_TURNS = 3
_ROUTE_MAX_RECORDS = 500

# Bytes read from the end of a session to find its last timestamp.
_TAIL_BYTES = 262144


# ============================================================================
# discover_ignore: deliberate skips (v0.22.0 A4)
# ============================================================================

def discover_ignore_patterns() -> list[str]:
    """Patterns from config ``discover_ignore`` (blank / non-string entries dropped)."""
    try:
        raw = get_config().get("discover_ignore") or []
    except Exception as e:
        log_warning(f"discover_ignore not applied (config unreadable): {e}")
        return []
    if not isinstance(raw, (list, tuple)):
        return []
    return [p.strip() for p in raw if isinstance(p, str) and p.strip()]


def _cwd_aliases(cwd: str) -> list[str]:
    """``cwd`` plus its macOS ``/private`` alias, so ``/var/folders/*`` matches both."""
    aliases = [cwd]
    if cwd.startswith("/private/"):
        aliases.append(cwd[len("/private"):])
    elif cwd.startswith(("/var/", "/tmp/", "/etc/")):
        aliases.append("/private" + cwd)
    return aliases


def match_discover_ignore(cwd: str | None, patterns: list[str] | None = None) -> str | None:
    """Return the first ``discover_ignore`` pattern that matches ``cwd``, else None.

    A pattern is a substring of the cwd, or, when it contains ``*``, ``?`` or
    ``[``, an fnmatch glob over the whole cwd (``/var/folders/*/T/tmp*``) --
    so a literal ``Projects/[draft]`` still matches as text. ``~`` is expanded.
    ``/private`` is optional on macOS temp paths.
    """
    if not cwd:
        return None
    if patterns is None:
        patterns = discover_ignore_patterns()
    aliases = _cwd_aliases(cwd)
    for pattern in patterns:
        needle = os.path.expanduser(pattern) if pattern.startswith("~") else pattern
        if any(needle in a for a in aliases):
            return pattern
        if any(ch in needle for ch in "*?[") and any(fnmatch.fnmatchcase(a, needle) for a in aliases):
            return pattern
    return None


def discover_projects() -> list[dict]:
    """Discover all Claude Code projects with session metadata.

    Scans ~/.claude/projects/ for directories containing JSONL session files.
    Returns project info with session counts and sizes.
    """
    if not PROJECTS_DIR.exists():
        return []

    patterns = discover_ignore_patterns()
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

        display_name, memex_name = project_names_for_claude_dir(project_dir)
        projects.append({
            "dir_name": project_dir.name,
            "display_name": display_name,
            "memex_name": memex_name,
            "session_count": len(sessions),
            "total_size_bytes": total_size,
            "newest_session": newest.isoformat() if newest else None,
            "oldest_session": oldest.isoformat() if oldest else None,
            "path": str(project_dir),
            "ignored": match_discover_ignore(cwd_from_session(project_dir), patterns) is not None,
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
    stats: dict | None = None,
) -> list[dict]:
    """Find sessions in ~/.claude/projects/ not yet in memex.

    Args:
        project_filter: Only check this project (by display_name or memex_name)
        since: Only look at sessions modified within this timedelta
        min_size: Minimum file size to consider (filters tiny/aborted sessions)
        stats: Optional out-dict; ``stats["ignored"]`` receives how many
            otherwise-listable sessions ``discover_ignore`` hid and
            ``stats["short"]`` how many were shorter than ``MIN_IMPORT_SECONDS``
            (the import path would never take them, so they are not listed).

    Returns:
        List of dicts with session info and suggested memex project.
    """
    memex = get_memex_path()
    known_ids, known_prefixes = get_memex_session_ids(memex)
    cutoff = datetime.now() - since if since else None

    # Sessions marked skipped_empty by a prior import (husks with no
    # conversation content) never produce a vault transcript, so they would
    # otherwise re-list as "unprocessed" on every run. Load once, not per id.
    # State unavailable (held/stale FileLock, corrupt file) must not turn a
    # read-only listing into a stall-then-crash — degrade to "nothing skipped".
    try:
        from memex.scripts.utils import load_state
        _processed = load_state().get("processed_sessions", {})
    except Exception:
        _processed = {}

    unprocessed = []
    ignore_patterns = discover_ignore_patterns()
    ignored_count = 0
    short_count = 0

    for project_dir in sorted(PROJECTS_DIR.iterdir()):
        if not project_dir.is_dir():
            continue

        # Deliberate skips are decided on the directory's true cwd, before any
        # canonical-name work (a git call per folder, and its drift warnings)
        # for a folder nobody wants: the June eval fleet is 180 of them.
        dir_cwd = cwd_from_session(project_dir)
        ignored_by = match_discover_ignore(dir_cwd, ignore_patterns)
        if ignored_by and not project_filter:
            display_name = memex_name = ""  # unused: ignored sessions are only counted
        else:
            display_name, memex_name = project_names_for_claude_dir(project_dir)
            if not ignored_by:
                warn_if_noncanonical(memex_name, dir_cwd, "session-import")

            # Filter by project if specified (an ignored folder that matches is
            # still counted as hidden, so "nothing to import" is not silent)
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
            if "skipped_empty_at" in _processed.get(session_id, {}):
                continue

            stat = session_file.stat()

            # Too small?
            if stat.st_size < min_size:
                continue

            # Too old?
            mtime = datetime.fromtimestamp(stat.st_mtime)
            if cutoff and mtime < cutoff:
                continue

            # Counted only now, so the number is "sessions this skip hid", not
            # "files in an ignored folder".
            if ignored_by:
                ignored_count += 1
                continue

            # A session this short is a husk (opened and closed, or still
            # starting): import refuses it, so don't list it either. Unknown
            # duration (no timestamps) is not "short".
            duration = session_duration_seconds(session_file)
            if duration is not None and duration < MIN_IMPORT_SECONDS:
                short_count += 1
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
                "cwd": dir_cwd,
            })

    if stats is not None:
        stats["ignored"] = ignored_count
        stats["short"] = short_count
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


def session_duration_seconds(path: Path) -> float | None:
    """Seconds between the first and last timestamped event; None if unknowable.

    Reads the head for the first timestamp and the tail for the last, so a
    250 MB session costs two small reads; only a tail with no timestamp at all
    (one enormous record) falls back to a full forward scan.
    """
    first_ts = last_ts = None
    try:
        with open(path, "rb") as f:
            for i, raw in enumerate(f):
                m = _RE_TIMESTAMP.search(raw.decode("utf-8", "replace"))
                if m:
                    first_ts = m.group(1)
                    break
                if i >= 200:
                    break
            offset = max(0, path.stat().st_size - _TAIL_BYTES)
            f.seek(offset)
            tail = f.read().decode("utf-8", "replace").splitlines()
            if offset > 0 and tail:
                tail = tail[1:]  # the window starts mid-record
            for raw in tail:
                m = _RE_TIMESTAMP.search(raw)
                if m:
                    last_ts = m.group(1)
            if last_ts is None:
                f.seek(0)
                for raw in f:
                    m = _RE_TIMESTAMP.search(raw.decode("utf-8", "replace"))
                    if m:
                        last_ts = m.group(1)
    except OSError:
        return None
    if not first_ts or not last_ts:
        return None
    try:
        t0 = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
        return max(0.0, (t1 - t0).total_seconds())
    except (ValueError, TypeError):
        return None


def _head_records(
    path: Path, user_turns: int = _ROUTE_USER_TURNS, limit: int = _ROUTE_MAX_RECORDS
) -> list[dict]:
    """JSON objects from the start of a session through its first ``user_turns`` user turns.

    Unparseable lines are skipped; ``limit`` caps the records read.
    """
    records: list[dict] = []
    seen_user = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    records.append(obj)
                    if obj.get("type") == "user":
                        seen_user += 1
                    if seen_user >= user_turns or len(records) >= limit:
                        break
    except OSError:
        pass
    return records


def route_session(session: dict, cache: dict | None = None):
    """Project route for one session: ``resolve_session_project(cwd, metadata)``.

    ``metadata`` comes from ``extract_session_metadata`` over the head of the
    transcript. Anything that goes wrong reading it degrades to the cwd rules
    (an empty metadata dict); a failure of the router itself returns None and
    the caller keeps the cwd-derived ``project_memex`` from discovery.
    """
    from memex.routing import resolve_session_project

    source = Path(session["source_path"])
    records = _head_records(source)
    cwd = session.get("cwd")
    if not cwd:
        cwd = next((r["cwd"] for r in records if isinstance(r.get("cwd"), str) and r["cwd"]), "")
    try:
        from memex.scripts.transcript_to_md import extract_session_metadata
        metadata = extract_session_metadata(records) if records else {}
    except Exception as e:
        log_warning(f"Could not read metadata of {session['session_id'][:8]}: {e}")
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    # A relay's channel sits in the queued wake message before the first user
    # turn, which extract_session_metadata does not read — the relay reader
    # (the one SessionEnd uses) does.
    try:
        from memex.scripts.transcript_to_md import read_relay_metadata
        metadata = {**metadata, **read_relay_metadata(source)}
    except Exception as e:
        log_warning(f"Could not read relay metadata of {session['session_id'][:8]}: {e}")
    channel = metadata.get("relay_channel")
    key = (cwd, channel if isinstance(channel, str) else None)
    if cache is not None and key in cache:
        return cache[key]
    try:
        route = resolve_session_project(cwd or "", metadata)
    except Exception as e:
        log_warning(f"Could not route {session['session_id'][:8]}: {e}")
        return None
    if cache is not None:
        cache[key] = route
    return route


def import_sessions(sessions: list[dict], dry_run: bool = True) -> list[dict]:
    """Import unprocessed sessions into memex via transcript_to_md.

    Each session is routed through ``memex.routing.resolve_session_project``
    (relay channel, then cwd rules); the result's ``project_memex`` is the
    routed project. Never imports a session whose cwd matches
    ``discover_ignore`` or that lasted under ``MIN_IMPORT_SECONDS``.

    Args:
        sessions: List from discover_unprocessed()
        dry_run: If True, only report what would be done

    Returns:
        List of results with status per session.
    """
    vault = get_memex_path()
    ignore_patterns = discover_ignore_patterns()
    route_cache: dict = {}
    # Imported lazily: the converter compiles a large regex table at import
    # time and `memex session discover` (the common path) never needs it.
    # Before v0.16.3 this was a flat `import transcript_to_md` off a
    # sys.path-inserted vault/scripts, which picked up a fallback block that
    # shadowed the module's real utils imports.
    from memex.scripts.transcript_to_md import convert_transcript_file

    results = []

    for session in sessions:
        source = Path(session["source_path"])
        session_id = session["session_id"]

        ignored_by = match_discover_ignore(session.get("cwd"), ignore_patterns)
        if ignored_by:
            results.append({
                **session,
                "status": "skipped",
                "reason": f"discover_ignore: {ignored_by}",
            })
            continue

        duration = session_duration_seconds(source)
        if duration is not None and duration < MIN_IMPORT_SECONDS:
            results.append({
                **session,
                "status": "skipped",
                "reason": f"short session ({duration:.0f}s < {MIN_IMPORT_SECONDS}s)",
                "duration_seconds": round(duration, 1),
            })
            continue

        route = route_session(session, route_cache)
        if route is not None:
            project = route.project
            if route.rule == "fallback" and session.get("project_memex"):
                # No cwd to route on: keep the name discovery recovered from
                # the dir slug rather than downgrading it to _uncategorized.
                project = session["project_memex"]
            session = {
                **session,
                "project_memex": project,
                "route_rule": route.rule,
                "unmapped_channel": route.unmapped_channel,
            }
        else:
            project = session["project_memex"]

        # Target path in memex
        target_dir = vault / "projects" / project / "transcripts"
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
                # The import path copies no sibling .jsonl, so the .md is the
                # sole vault artifact — gate it through the scrubber.
                scrub=True,
            )
            if result_path:
                results.append({
                    **session,
                    "status": "imported",
                    "target": str(result_path),
                })
                log_info(f"Imported {session_id[:8]} -> {project}")
            elif _is_empty_husk(source):
                # No user/assistant messages (e.g. file-history-snapshot-only
                # transcripts): nothing to convert, ever. Mark processed so
                # discover stops re-listing it as unprocessed forever
                # (2026-08-25 audit: 109 such husks re-listed on every run).
                from memex.scripts.utils import mark_session_phase
                mark_session_phase(session_id, "skipped_empty")
                results.append({
                    **session,
                    "status": "skipped_empty",
                    "reason": "no conversation content (husk)",
                })
                log_info(f"Skipped empty husk {session_id[:8]} ({project})")
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


def _is_empty_husk(jsonl_path: Path) -> bool:
    """True if the transcript contains no user/assistant messages at all.

    Some sessions write only bookkeeping lines (file-history-snapshot,
    queue-operation, last-prompt) and never a conversation turn — conversion
    correctly returns None for them, and they can never become importable.
    Cheap substring scan; only called on the already-rare conversion-failed
    path, never during discovery.
    """
    try:
        with open(jsonl_path) as f:
            for line in f:
                # Same regexes the triage scanner uses — a whitespace form the
                # scan misses would mean a PERMANENT false skipped_empty mark,
                # so the husk scanner must never be stricter than triage.
                if _RE_USER_MSG.search(line) or _RE_ASSISTANT_MSG.search(line):
                    return False
    except OSError:
        return False
    return True


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


def _build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description="Discover unprocessed Claude Code sessions",
        epilog=(
            "Examples:\n"
            "  memex session discover\n"
            "  memex session discover --triage --min-score=9\n"
            "  memex session import --apply\n"
            "  memex session discover --project=memex --since=7d"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def _run() -> None:
    parser = _build_parser()
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
    parser.add_argument("--exclude", action="append", default=[],
                        metavar="SESSION_ID",
                        help="Session id (or unique prefix) to skip on import; repeatable. "
                             "Use for currently-running sessions whose transcripts are still growing")
    parser.add_argument("--all-projects", action="store_true",
                        help="Show all Claude projects (not just unprocessed)")
    args = parser.parse_args()

    if not PROJECTS_DIR.exists():
        print(
            f"Error: Claude projects directory not found: {PROJECTS_DIR}\n"
            "Fix: Open Claude Code at least once or verify your ~/.claude/projects directory.",
            file=sys.stderr,
        )
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
                marker = "  (ignored: discover_ignore)" if p["ignored"] else ""
                print(f"  {p['display_name']:<30} {p['session_count']:>4} sessions  "
                      f"{format_size(p['total_size_bytes']):>8}{marker}")
        return

    # Mode: discover unprocessed
    discover_stats: dict = {}
    unprocessed = discover_unprocessed(
        project_filter=args.project,
        since=since,
        stats=discover_stats,
    )
    notes = []
    if discover_stats.get("ignored"):
        notes.append(f"{discover_stats['ignored']} session(s) hidden by discover_ignore in config.json")
    if discover_stats.get("short"):
        notes.append(f"{discover_stats['short']} session(s) under {MIN_IMPORT_SECONDS}s not listed: too short to import")
    hidden_note = f"({'; '.join(notes)})" if notes else ""

    # Mode: triage (score all sessions). When --import is also given, skip the
    # report and fall through to the import branch (which re-triages under
    # --min-score) — the old unconditional `return` here made the tool's own
    # recommended command (`--triage --min-score=9 --import --apply`) silently
    # display-and-exit without importing anything.
    if args.triage and not args.do_import:
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
            if hidden_note:
                print(hidden_note)
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
        if hidden_note:
            print(hidden_note)
        return

    if args.do_import:
        # --exclude runs FIRST: it exists for currently-running sessions whose
        # transcripts are still growing, and a just-started live session can
        # legitimately be >1KB of file-history-snapshot lines with no user
        # turn yet — the husk pass below would otherwise mark it
        # skipped_empty PERMANENTLY and discover would never see it again.
        if args.exclude:
            excluded = [s for s in unprocessed
                        if any(s["session_id"].startswith(e) for e in args.exclude)]
            excluded_ids = {s["session_id"] for s in excluded}
            unprocessed = [s for s in unprocessed if s["session_id"] not in excluded_ids]
            for s in excluded:
                # Not under --json: this went to stdout ahead of json.dumps and
                # made the document unparseable. Exclusions are visible there as
                # absent results.
                if not args.json:
                    print(f"  excluded: {s['session_id'][:8]}  {s['project_display']}")

        # Mark empty husks BEFORE any min-score filtering: a husk triages to
        # score 0, so under the tool's own recommended command
        # (`--triage --min-score=9 --import --apply`) it would be dropped
        # before import_sessions' marking path ever saw it and re-list forever
        # — the same recommended-command-silently-skips shape v0.16.3 fixed
        # for --triage --import. One-time cost per husk; marked ids are
        # skipped by discover_unprocessed on every later run.
        if args.apply:
            from memex.scripts.utils import mark_session_phase
            husks = []
            for s in unprocessed:
                if not _is_empty_husk(Path(s["source_path"])):
                    continue
                try:
                    # Guarded like the other two mark_session_phase call sites:
                    # a held state.json FileLock must not abort the entire
                    # import run — an unmarked husk just re-lists next time.
                    mark_session_phase(s["session_id"], "skipped_empty")
                except Exception as e:
                    log_warning(f"Could not mark husk {s['session_id'][:8]}: {e}")
                    continue
                husks.append(s)
                log_info(f"Skipped empty husk {s['session_id'][:8]} ({s['project_memex']})")
            if husks:
                husk_ids = {s["session_id"] for s in husks}
                unprocessed = [s for s in unprocessed if s["session_id"] not in husk_ids]
                if not args.json:
                    print(f"  skipped empty: {len(husks)} husk session(s) marked processed")

        # Triage when either flag asks for it. `--min-score` needs the scores to
        # filter on; `--triage` alone attaches them so the import report (and
        # --json) carries a score per session — before v0.16.3 `--triage` was
        # simply inert next to `--import`, silently doing nothing.
        if args.triage or args.min_score > 0:
            triage_all(unprocessed)
            if args.min_score > 0:
                unprocessed = [s for s in unprocessed if s.get("score", 999) >= args.min_score]

        dry_run = not args.apply
        results = import_sessions(unprocessed, dry_run=dry_run)

        if args.json:
            print(json.dumps(results, indent=2))
        else:
            imported = [r for r in results if r["status"] == "imported"]
            would = [r for r in results if r["status"] == "would_import"]
            failed = [r for r in results if r["status"] == "failed"]
            skipped_empty = [r for r in results if r["status"] == "skipped_empty"]

            if dry_run:
                print(f"Would import {len(would)} sessions (use --apply to execute):\n")
                for r in would:
                    # Score only present when --triage/--min-score ran.
                    score = f"  score {r['score']}" if r.get("score") is not None else ""
                    print(f"  {r['session_id'][:8]}  {r['project_display']:<25} "
                          f"{format_size(r['size_bytes']):>8}  -> {r['project_memex']}{score}")
            else:
                summary = f"Imported: {len(imported)}, Failed: {len(failed)}"
                if skipped_empty:
                    summary += f", Skipped empty: {len(skipped_empty)} (marked processed)"
                print(summary)
                for r in failed:
                    print(f"  FAILED: {r['session_id'][:8]} — {r.get('reason', '?')}")
            unmapped = {}
            for r in results:
                if r.get("unmapped_channel"):
                    unmapped[r["unmapped_channel"]] = unmapped.get(r["unmapped_channel"], 0) + 1
            for channel, n in sorted(unmapped.items()):
                print(f"  unmapped relay channel {channel} ({n} session(s)): "
                      f"add it to channel_mappings in config.json")
            if hidden_note:
                print(hidden_note)
        return

    # Mode: report
    if args.json:
        print(json.dumps(unprocessed, indent=2))
        return

    if not unprocessed:
        print("All sessions are already archived in memex.")
        if hidden_note:
            print(hidden_note)
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
    if hidden_note:
        print(hidden_note)


def main():
    try:
        _run()
    except SystemExit:
        raise
    except FileNotFoundError as exc:
        print(f"Error: {exc}\nFix: Verify the referenced session file exists and rerun the command.", file=sys.stderr)
        sys.exit(2)
    except sqlite3.OperationalError as exc:
        print(f"Error: {exc}\nFix: memex index rebuild --full", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
