"""
Claude Memory Plugin - Shared Utilities

Provides: config, logging, project detection, file locking, path sanitization,
token counting, and state management.
"""

import functools
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


# Cross-platform file locking
from filelock import FileLock

# Token counting (lazy import - not all scripts need this)
tiktoken = None

# ============================================================================
# Configuration
# ============================================================================

DEFAULT_CONFIG = {
    "memex_path": None,
    "memo_generation": {
        "min_messages": 5,
        "min_human_tokens": 500,
    },
    "state_dir": "~/.memex",
}


def get_config() -> dict:
    """Load configuration, merging defaults with any custom config."""
    try:
        from memex.config import get_settings
        return get_settings().model_dump()
    except ImportError:
        # Fallback for hook context where pydantic-settings isn't available
        import json
        config_path = Path("~/.memex/config.json").expanduser()
        if config_path.exists():
            try:
                return json.loads(config_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {}


def deep_merge(base: dict, override: dict) -> dict:
    """Deep merge override into base dict."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


# ============================================================================
# Logging
# ============================================================================

_logger = None


def get_logger() -> logging.Logger:
    """Get or create logger with file output."""
    global _logger
    if _logger is not None:
        return _logger

    _logger = logging.getLogger("memex")
    _logger.setLevel(logging.DEBUG)

    # Ensure log directory exists
    log_dir = Path("~/.memex/logs").expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)

    # File handler with rotation-friendly naming
    log_file = log_dir / f"plugin-{datetime.now():%Y-%m-%d}.log"
    handler = logging.FileHandler(log_file)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    _logger.addHandler(handler)

    return _logger


def log_info(msg: str):
    get_logger().info(msg)


def log_warning(msg: str):
    get_logger().warning(msg)


def log_error(msg: str):
    get_logger().error(msg)


def log_debug(msg: str):
    get_logger().debug(msg)


# ============================================================================
# Project Detection
# ============================================================================

def detect_project(cwd: str) -> str:
    """
    Detect project name from working directory.

    Priority:
    1. Explicit mapping in config
    2. Git remote name (parsed from origin URL)
    3. Git root folder name
    4. CWD folder name
    5. Fallback: '_uncategorized'
    """
    cwd_path = Path(cwd).resolve()
    config = get_config()

    # 1. Check explicit mapping
    mappings = config.get("project_mappings", {})
    for pattern, project in mappings.items():
        if pattern in str(cwd_path):
            return sanitize_project_name(project)

    # 2. Try git remote
    git_project = get_git_project(cwd_path)
    if git_project:
        return sanitize_project_name(git_project)

    # 3. Try git root folder name
    git_root = get_git_root(cwd_path)
    if git_root:
        return sanitize_project_name(git_root.name)

    # 4. Use CWD folder name
    folder_name = cwd_path.name
    if folder_name and folder_name not in ("", "/"):
        return sanitize_project_name(folder_name)

    # 5. Fallback
    return "_uncategorized"


def get_git_root(path: Path) -> Path | None:
    """Get git repository root directory."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def get_git_project(path: Path) -> str | None:
    """Extract project name from git remote URL."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            return parse_git_remote(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def parse_git_remote(remote_url: str) -> str | None:
    """Parse project name from git remote URL."""
    patterns = [
        r'git@[^:]+:(?:[^/]+)/([^/]+?)(?:\.git)?$',  # SSH: git@github.com:user/repo.git
        r'https?://[^/]+/(?:[^/]+)/([^/]+?)(?:\.git)?$',  # HTTPS: https://github.com/user/repo.git
    ]
    for pattern in patterns:
        match = re.match(pattern, remote_url)
        if match:
            return match.group(1)
    return None


# ============================================================================
# Path Sanitization
# ============================================================================

RESERVED_NAMES = frozenset({
    '', '_', 'tmp', 'temp', 'downloads', 'desktop', 'home',
    'documents', 'users', 'var', 'etc', 'usr', 'bin',
    # 'uncategorized' makes sanitize idempotent on its own fallback value:
    # sanitize('_uncategorized') strips the underscore, and without this entry
    # the result escaped un-prefixed — the write path (safe_project_path /
    # get_unique_project_name re-sanitizing an already-detected name) then
    # created projects/uncategorized/ alongside projects/_uncategorized/.
    'uncategorized'
})


def claude_dir_to_project_name(dir_name: str) -> str:
    """Convert a Claude Code project directory name to a human-readable project name.

    Claude stores sessions in ~/.claude/projects/<encoded-path>/ where the
    directory name is the absolute path with slashes replaced by hyphens.

    Examples:
        '-Users-alice-Documents-myapp' -> 'myapp'
        '-home-bob-project' -> 'project'
        '-Users-alice-Desktop' -> '~Desktop'

    Adapted from peteromallet/dataclaw parser.py.
    """
    path = dir_name.replace("-", "/")
    path = path.lstrip("/")
    parts = path.split("/")
    common_dirs = {"Documents", "Downloads", "Desktop"}

    if len(parts) >= 2 and parts[0] == "Users":
        if len(parts) >= 4 and parts[2] in common_dirs:
            meaningful = parts[3:]
        elif len(parts) >= 3 and parts[2] not in common_dirs:
            meaningful = parts[2:]
        else:
            meaningful = []
    elif len(parts) >= 2 and parts[0] == "home":
        meaningful = parts[2:] if len(parts) > 2 else []
    else:
        meaningful = parts

    if meaningful:
        # Reconstruct from original segments to preserve multi-word names
        segments = dir_name.lstrip("-").split("-")
        prefix_parts = len(parts) - len(meaningful)
        return "-".join(segments[prefix_parts:]) or dir_name
    else:
        if len(parts) >= 2 and parts[0] in ("Users", "home"):
            if len(parts) == 2:
                return "~home"
            if len(parts) == 3 and parts[2] in common_dirs:
                return f"~{parts[2]}"
        return dir_name.strip("-") or "unknown"


def sanitize_project_name(name: str) -> str:
    """
    Sanitize project name for safe filesystem use.

    - Removes dangerous characters
    - Collapses multiple underscores
    - Limits length
    - Handles reserved names
    """
    # Remove dangerous characters, keep alphanumeric, hyphen, underscore
    name = re.sub(r'[^\w\-]', '_', name)
    # Collapse multiple underscores
    name = re.sub(r'_+', '_', name)
    # Strip leading/trailing underscores
    name = name.strip('_')
    # Limit length, then re-strip: truncation can land on an underscore, and a
    # trailing '_' the next call would strip makes sanitize non-idempotent —
    # the write path re-sanitizes an already-detected name, so 'a…a_' and 'a…a'
    # become two folders for one project.
    name = name[:50].strip('_')
    # Handle reserved/generic names
    if name.lower() in RESERVED_NAMES:
        return '_uncategorized'
    return name or '_uncategorized'


# --- Canonical project identity for Claude project dirs ---------------------
# Single source of truth, shared by sync_auto_memory + discover_sessions. The
# Claude project-dir name is a lossy ``/``→``-`` encoding of the cwd; the slug
# parser (claude_dir_to_project_name) can't invert hyphenated names and ignores
# project_mappings/git remote, which produced cwd-fragment folders like
# ``Apps-arena`` and split projects across folders. Prefer the true ``cwd`` from
# a session transcript fed to detect_project(); fall back to the slug only when
# no cwd is recoverable. (v0.15.5 fixed sync; v0.15.6 centralized + fixed import.)

def _cwd_encodes_to(cwd: str, expected: str) -> bool:
    """Does ``cwd`` encode to the Claude project-dir name ``expected``?

    BOTH branches below are load-bearing — do not "simplify" either away.
    Claude Code has shipped two different encoders and a single machine's
    ``~/.claude/projects/`` holds dirs from both (audited 2026-08-05, 118 dirs):

    - most dirs map every non-alphanumeric character to ``-``, so
      ``/Users/x/.slock/agents`` → ``-Users-x--slock-agents`` (the dot became a
      second dash). Only the regex branch matches these.
    - some dirs keep the dot verbatim, e.g.
      ``/Users/x/Documents/Apps/mcp/alcor-tmux/.alcor-v2`` →
      ``-Users-x-Documents-Apps-mcp-alcor-tmux-.alcor-v2``. Only the
      slash-only branch matches these; the regex branch rejects them.

    The slash-only branch therefore is NOT dead code, despite looking like a
    strict subset of the regex branch — that equivalence holds only if
    ``expected`` contains nothing outside ``[A-Za-z0-9-]``, which real dirs
    violate. Two independent reviewers reached the wrong conclusion here.

    The pre-fix check ran the slash-only branch alone, so it rejected any cwd
    whose dot the encoder had replaced; those sessions fell to the lossy slug
    fallback and minted fragment folders (found 2026-08-05: .slock agents,
    .claude-worktrees sessions). ``tests/test_cwd_encoding.py`` pins one real
    dir of each shape.
    """
    stripped = cwd.rstrip("/")
    if stripped.replace("/", "-") == expected:
        return True
    return re.sub(r"[^A-Za-z0-9-]", "-", stripped) == expected


@functools.lru_cache(maxsize=None)
def cwd_from_session(project_dir: Path) -> str | None:
    """Read the true working directory from a session transcript.

    Session JSONLs carry a per-event ``cwd``. The recovered value is validated
    against the dir name's ``/``→``-`` encoding so a stray tool-recorded path
    can't mis-map the project. Scans newest session first, bounded line count;
    a per-file ``stat``/read failure degrades that one file only. Cached per
    process (callers hit the same dir from both ``project_names_for_claude_dir``
    and the tripwire); ``.cache_clear()`` for tests.
    """
    expected = project_dir.name

    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    try:
        sessions = sorted(project_dir.glob("*.jsonl"), key=_mtime, reverse=True)
    except OSError:
        return None
    for f in sessions:
        try:
            with f.open(encoding="utf-8", errors="ignore") as fh:
                for i, line in enumerate(fh):
                    if i >= 200:
                        break
                    if '"cwd"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if isinstance(obj, dict):
                        cwd = obj.get("cwd")
                        if isinstance(cwd, str) and cwd and _cwd_encodes_to(cwd, expected):
                            return cwd
        except OSError:
            continue
    return None


@functools.lru_cache(maxsize=None)
def project_names_for_claude_dir(project_dir: Path) -> tuple[str, str]:
    """Return ``(display_name, memex_name)`` for a Claude project dir, canonically.

    Uses the true session ``cwd`` → ``detect_project`` (project_mappings → git
    remote → folder name); falls back to the slug parser only when no cwd is
    recoverable (deleted project, memory-only dir). Cached per process —
    ``detect_project`` shells out to git and callers hit the same dirs repeatedly;
    canonical identity is stable for a run. ``.cache_clear()`` for tests.
    """
    cwd = cwd_from_session(project_dir)
    if cwd:
        name = detect_project(cwd)
        return name, name
    display = claude_dir_to_project_name(project_dir.name)
    return display, sanitize_project_name(display)


# Generic parent-dir tokens that, leading a project name, signal a cwd-fragment
# folder (the path wasn't collapsed to its leaf) — e.g. "Apps-arena",
# "GitHub-loom". Kept machine-agnostic (no user/vault-specific tokens) since this
# ships in the plugin; the audit is advisory and routes matches through human
# confirmation, so a rare false positive (a real leaf literally named
# "Library-foo") is acceptable.
_CWD_FRAGMENT_PREFIXES = (
    "Apps-", "Documents-", "Desktop-", "Downloads-",
    "Users-", "home-", "GitHub-", "Library-", "CloudStorage-",
)


def looks_like_cwd_fragment(name: str) -> bool:
    """Heuristic: does a project name look like an un-collapsed cwd path fragment?

    Canonical names are leaf folders ('arena', 'mcp-workspace'); fragments retain
    a parent-dir prefix because detection fell back to the lossy slug parser.
    Used by the sync tripwire and ``memex check --folders``.
    """
    return any(name.startswith(p) for p in _CWD_FRAGMENT_PREFIXES)


def warn_if_noncanonical(planned_name: str, cwd: str | None, source: str) -> bool:
    """Tripwire: WARN when a folder-creating path is about to use a non-canonical
    project name, so cwd-fragment drift is visible at creation rather than only in
    a later audit. Non-blocking (returns True if it warned); ``memex check
    --folders`` is the corrective surface.
    """
    if cwd:
        canonical = detect_project(cwd)
        if planned_name != canonical:
            log_warning(
                f"[{source}] project '{planned_name}' != canonical '{canonical}' "
                f"for cwd {cwd} — possible fragment drift; see `memex check --folders`"
            )
            return True
        return False
    if looks_like_cwd_fragment(planned_name):
        log_warning(
            f"[{source}] project '{planned_name}' looks like a cwd-fragment folder "
            f"(no session cwd to verify); see `memex check --folders`"
        )
        return True
    return False


def safe_project_path(project: str, memex: Path) -> Path:
    """
    Get safe project path, preventing traversal attacks.

    Raises ValueError if path traversal detected.
    """
    sanitized = sanitize_project_name(project)
    full_path = (memex / "projects" / sanitized).resolve()

    # Verify path is still within memex using proper containment check
    memex_resolved = memex.resolve()
    try:
        full_path.relative_to(memex_resolved)
    except ValueError:
        raise ValueError(f"Path traversal detected: {project}")

    return full_path


def get_unique_project_name(project: str, cwd: str, memex: Path) -> str:
    """
    Get unique project name, handling collisions.

    If a project folder already exists for a different cwd,
    adds a hash suffix to differentiate.
    """
    base_name = sanitize_project_name(project)
    project_path = memex / "projects" / base_name

    if not project_path.exists():
        return base_name

    # Check if existing project is for the same cwd
    project_meta = project_path / "_project.md"
    if project_meta.exists():
        content = project_meta.read_text()
        if cwd in content:
            return base_name  # Same project

    # Collision - add hash suffix
    path_hash = hashlib.md5(cwd.encode()).hexdigest()[:6]
    return f"{base_name}-{path_hash}"


# ============================================================================
# File Locking (Cross-Platform)
# ============================================================================

def safe_write(path: Path, content: str, timeout: int = 10):
    """
    Atomic write with cross-platform file locking.

    Uses temp file + rename for atomicity.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    lock = FileLock(str(path) + ".lock", timeout=timeout)
    with lock:
        temp = path.with_suffix('.tmp')
        temp.write_text(content)
        temp.replace(path)  # Atomic on POSIX, near-atomic on Windows


def safe_read(path: Path, timeout: int = 10) -> str | None:
    """Read file with locking, returns None if file doesn't exist."""
    path = Path(path)
    if not path.exists():
        return None

    lock = FileLock(str(path) + ".lock", timeout=timeout)
    with lock:
        return path.read_text()


# ============================================================================
# Token Counting
# ============================================================================

# Use cl100k_base as approximation for Claude
_tokenizer = None


def get_tokenizer():
    """Get or create tokenizer (lazy loading)."""
    global _tokenizer, tiktoken
    if _tokenizer is None:
        import tiktoken as _tiktoken
        tiktoken = _tiktoken
        _tokenizer = tiktoken.get_encoding("cl100k_base")
    return _tokenizer


def count_tokens(text: str) -> int:
    """Count tokens in text (approximation for Claude)."""
    return len(get_tokenizer().encode(text))


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate text to fit within token limit."""
    enc = get_tokenizer()
    tokens = enc.encode(text)
    if len(tokens) <= max_tokens:
        return text
    truncated = enc.decode(tokens[:max_tokens])
    return truncated + "\n\n[Truncated for context limit]"


# ============================================================================
# State Management
# ============================================================================

def get_state_dir() -> Path:
    """Get state directory, creating if needed."""
    state_dir = Path("~/.memex").expanduser()
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def get_lock_dir() -> Path:
    """Get directory for lock files, creating if needed."""
    lock_dir = get_state_dir() / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir


def get_state_file() -> Path:
    """Get state.json path."""
    return get_state_dir() / "state.json"


def _load_state_unlocked() -> dict:
    """Load processing state without locking (caller must hold lock)."""
    state_file = get_state_file()
    if not state_file.exists():
        return {"schema_version": 1, "processed_sessions": {}}

    try:
        content = state_file.read_text()
        return json.loads(content)
    except (json.JSONDecodeError, OSError):
        log_warning("Corrupt or unreadable state.json, resetting")
        return {"schema_version": 1, "processed_sessions": {}}


def _save_state_unlocked(state: dict):
    """Save processing state without locking (caller must hold lock)."""
    state_file = get_state_file()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    temp = state_file.with_suffix('.tmp')
    temp.write_text(json.dumps(state, indent=2))
    temp.replace(state_file)


def load_state() -> dict:
    """Load processing state with locking."""
    state_file = get_state_file()
    lock = FileLock(str(state_file) + ".lock", timeout=30)
    with lock:
        return _load_state_unlocked()


def save_state(state: dict):
    """Save processing state with locking."""
    state_file = get_state_file()
    lock = FileLock(str(state_file) + ".lock", timeout=30)
    with lock:
        _save_state_unlocked(state)


def mark_session_phase(session_id: str, phase: str) -> bool:
    """
    Mark session phase as done. Returns False if already done.

    Phases: 'transcript_archived', 'memo_generated', 'completed'
    """
    lock = FileLock(str(get_state_file()) + ".lock", timeout=30)

    with lock:
        state = _load_state_unlocked()
        sessions = state.setdefault("processed_sessions", {})
        session = sessions.setdefault(session_id, {})

        phase_key = f"{phase}_at"
        if phase_key in session:
            return False  # Already processed

        session[phase_key] = datetime.now().isoformat()
        _save_state_unlocked(state)
        return True


def is_session_processed(session_id: str, phase: str) -> bool:
    """Check if a session phase was already processed.

    Also checks the 16-char prefix key: before v0.16.4, `memex mark-saved`
    recorded phases under the session-state file's stem (the first 16 chars
    of the session id) whenever it couldn't recover the full id, so 300+
    legacy entries live under prefix keys. Without this fallback, PreCompact's
    full-id lookup misses them and re-signals already-saved sessions as
    stale orphans.
    """
    state = load_state()
    sessions = state.get("processed_sessions", {})
    phase_key = f"{phase}_at"
    if phase_key in sessions.get(session_id, {}):
        return True
    # Scoped to memo_generated: that is the only phase legacy prefix keys
    # ever carried (verified against live state 2026-08-26 — zero prefix keys
    # hold transcript_archived_at/completed_at). Leaving it unscoped would let
    # a prefix key silently suppress OTHER phases (e.g. transcript archiving).
    if (
        phase == "memo_generated"
        and len(session_id) > 16
        and phase_key in sessions.get(session_id[:16], {})
    ):
        return True
    return False


def get_session_memo_saved(session_id: str) -> bool:
    """Check if memo was saved for this session (canonical state or session-state file)."""
    # Check canonical state first (most reliable — set by `memex mark-saved`)
    if is_session_processed(session_id, "memo_generated"):
        return True

    # Check session-state files (UserPromptSubmit nudge state)
    state_dir = Path.home() / ".memex" / "session-state"
    if state_dir.exists():
        prefix = session_id[:16]
        state_file = state_dir / f"{prefix}.json"
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text())
                return state.get("memo_saved", False)
            except (json.JSONDecodeError, OSError, ValueError):
                pass

    return False


# ============================================================================
# Pending Memo Management
# ============================================================================

def get_pending_dir() -> Path:
    """Get pending memos directory."""
    pending_dir = get_state_dir() / "pending-memos"
    pending_dir.mkdir(parents=True, exist_ok=True)
    return pending_dir


def mark_pending_memo(session_id: str, error: str, transcript_path: str = "", project: str = ""):
    """Mark a memo as pending (failed to generate)."""
    pending_file = get_pending_dir() / f"{session_id}.json"

    # Load existing or create new
    if pending_file.exists():
        data = json.loads(pending_file.read_text())
        data["attempts"] = data.get("attempts", 0) + 1
        data["last_error"] = error
        data["last_attempt_at"] = datetime.now().isoformat()
    else:
        data = {
            "session_id": session_id,
            "transcript_path": transcript_path,
            "project": project,
            "failed_at": datetime.now().isoformat(),
            "attempts": 1,
            "last_error": error,
        }

    safe_write(pending_file, json.dumps(data, indent=2))
    log_warning(f"Marked pending memo for session {session_id}: {error}")


def get_pending_memos() -> list[dict]:
    """Get list of pending memo requests."""
    pending_dir = get_pending_dir()
    if not pending_dir.exists():
        return []

    pending = []
    for f in pending_dir.glob("*.json"):
        try:
            pending.append(json.loads(f.read_text()))
        except (json.JSONDecodeError, OSError):
            log_warning(f"Invalid or unreadable pending memo file: {f}")

    return pending


def clear_pending_memo(session_id: str):
    """Remove a pending memo marker after successful generation."""
    pending_file = get_pending_dir() / f"{session_id}.json"
    if pending_file.exists():
        pending_file.unlink()


# Note: The `enqueue_embedding_job` / `dequeue_embedding_jobs` /
# `get_embedding_queue_count` helpers were deleted in v0.11.0 along with
# their `~/.memex/pending_embeddings.jsonl` queue file. They had zero call
# sites and duplicated the purpose of `memex index embed-missing`, which
# uses vec-table LEFT JOIN as the single source of truth for "what still
# needs embedding." See `.claude/rules/search-and-embeddings.md`.


# ============================================================================
# Orphaned Session Cleanup
# ============================================================================

def cleanup_orphaned_sessions(max_age_hours: int = 24) -> list[str]:
    """
    Clean up sessions that never completed.

    Returns list of orphaned session IDs found.
    """
    state = load_state()
    cutoff = datetime.now() - timedelta(hours=max_age_hours)

    orphaned = []
    sessions = state.get("processed_sessions", {})

    for session_id, data in sessions.items():
        # Session started archiving but never completed
        if "transcript_archived_at" in data and "completed_at" not in data:
            try:
                started = datetime.fromisoformat(data["transcript_archived_at"])
                if started < cutoff:
                    orphaned.append(session_id)
            except ValueError:
                pass

    if orphaned:
        log_warning(f"Found {len(orphaned)} orphaned sessions")

    return orphaned


# ============================================================================
# Frontmatter Utilities
# ============================================================================

def parse_frontmatter(content: str) -> dict:
    """Extract YAML frontmatter from markdown content."""
    if not content.startswith("---"):
        return {}

    try:
        end = content.index("---", 3)
        yaml_content = content[3:end].strip()

        # Simple YAML parsing (no external dependency)
        result = {}
        for line in yaml_content.split("\n"):
            if ":" in line:
                key, _, value = line.partition(":")
                key = key.strip()
                value = value.strip().strip('"').strip("'")

                # Handle lists (simple case)
                if value.startswith("[") and value.endswith("]"):
                    value = [v.strip().strip('"').strip("'") for v in value[1:-1].split(",")]

                result[key] = value

        return result
    except ValueError:
        return {}


def format_frontmatter(data: dict) -> str:
    """Format dict as YAML frontmatter."""
    lines = ["---"]
    for key, value in data.items():
        if isinstance(value, list):
            lines.append(f"{key}: [{', '.join(str(v) for v in value)}]")
        elif isinstance(value, bool):
            lines.append(f"{key}: {str(value).lower()}")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines)


# ============================================================================
# Memex Path Utilities
# ============================================================================

def get_memex_path(fallback_to_script: bool = True) -> Path:
    """Get memex vault path from config.

    Resolution order:
    1. Configured memex_path (user override)
    2. CLAUDE_PLUGIN_ROOT env var (set by plugin system)
    3. Script location fallback (if fallback_to_script=True)
    """
    from memex.paths import get_memex_path as resolve_memex_path

    try:
        return resolve_memex_path()
    except ValueError:
        if fallback_to_script:
            return Path(__file__).parent.parent.resolve()
        raise


def ensure_project_structure(project: str, memex: Path) -> Path:
    """Ensure project directory structure exists, return project path."""
    project_path = safe_project_path(project, memex)

    # Create subdirectories
    (project_path / "memos").mkdir(parents=True, exist_ok=True)
    (project_path / "transcripts").mkdir(parents=True, exist_ok=True)

    return project_path


# ============================================================================
# Hook I/O Helpers
# ============================================================================

def read_hook_input() -> dict:
    """Read and parse hook input from stdin."""
    try:
        return json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError) as e:
        log_error(f"Failed to parse hook input: {e}")
        return {}  # Return empty dict, let caller handle gracefully


def output_context(context: str):
    """Output context for SessionStart hook."""
    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context
        }
    }
    print(json.dumps(output))


def output_plain(message: str):
    """Output plain text (simpler alternative for SessionStart)."""
    print(message)
