# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "filelock>=3.0",
#     "tiktoken>=0.5",
# ]
# ///
"""
Claude Memory Plugin - Shared Utilities

Provides: config, logging, project detection, file locking, path sanitization,
token counting, and state management.
"""

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
    "memex_path": None,  # Set to plugin root by default
    "model": "claude-sonnet-4-20250514",
    "max_context_tokens": 6000,
    "session_start": {
        "load_recent_memos": 3,
        "load_project_overview": True,
        "load_related_concepts": True,
    },
    "memo_generation": {
        "min_messages": 5,
        "min_human_tokens": 500,
    },
    "state_dir": "~/.memex",
}


def get_config() -> dict:
    """Load configuration, merging defaults with any custom config."""
    config = DEFAULT_CONFIG.copy()

    # Plugin root is the memex vault
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root:
        config["memex_path"] = plugin_root

    # Check for custom config file
    config_path = Path(config.get("state_dir", "~/.memex")).expanduser() / "config.json"
    if config_path.exists():
        try:
            custom = json.loads(config_path.read_text())
            config = deep_merge(config, custom)
        except json.JSONDecodeError:
            log_warning(f"Invalid config at {config_path}, using defaults")

    return config


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
    'documents', 'users', 'var', 'etc', 'usr', 'bin'
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
    # Limit length
    name = name[:50]
    # Handle reserved/generic names
    if name.lower() in RESERVED_NAMES:
        return '_uncategorized'
    return name or '_uncategorized'


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
    """Check if a session phase was already processed."""
    state = load_state()
    session = state.get("processed_sessions", {}).get(session_id, {})
    return f"{phase}_at" in session


def get_session_memo_saved(session_id: str) -> bool:
    """Check if memo was saved for this session (canonical state or session-state file)."""
    # Check canonical state first (most reliable — set by mark_memo_saved.py)
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
            except (json.JSONDecodeError, ValueError):
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
        except json.JSONDecodeError:
            log_warning(f"Invalid pending memo file: {f}")

    return pending


def clear_pending_memo(session_id: str):
    """Remove a pending memo marker after successful generation."""
    pending_file = get_pending_dir() / f"{session_id}.json"
    if pending_file.exists():
        pending_file.unlink()


# ============================================================================
# Embedding Queue Management
# ============================================================================

def get_embedding_queue_path() -> Path:
    """Get path to the embedding job queue file."""
    return get_state_dir() / "pending_embeddings.jsonl"


def enqueue_embedding_job(rel_path: str):
    """
    Add a document to the embedding queue for later processing.

    Memos are immediately FTS-indexed; embeddings are queued for batch processing
    in /memex:maintain to avoid blocking the hook timeout.
    """
    queue_path = get_embedding_queue_path()
    lock = FileLock(str(queue_path) + ".lock", timeout=5)

    with lock:
        with open(queue_path, "a") as f:
            job = {
                "path": rel_path,
                "enqueued_at": datetime.now().isoformat()
            }
            f.write(json.dumps(job) + "\n")

    log_info(f"Enqueued embedding job for {rel_path}")


def dequeue_embedding_jobs(max_jobs: int | None = None) -> list[dict]:
    """
    Retrieve and clear pending embedding jobs from the queue.

    Args:
        max_jobs: Maximum number of jobs to return. None = all jobs.

    Returns:
        List of job dicts with 'path' and 'enqueued_at' fields.
    """
    queue_path = get_embedding_queue_path()
    lock = FileLock(str(queue_path) + ".lock", timeout=5)

    if not queue_path.exists():
        return []

    with lock:
        jobs = []
        remaining = []

        try:
            with open(queue_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        job = json.loads(line)
                        if max_jobs is None or len(jobs) < max_jobs:
                            jobs.append(job)
                        else:
                            remaining.append(job)
                    except json.JSONDecodeError:
                        log_warning(f"Invalid job in embedding queue: {line[:50]}")
        except FileNotFoundError:
            return []

        # Write back remaining jobs (or clear file)
        if remaining:
            with open(queue_path, "w") as f:
                for job in remaining:
                    f.write(json.dumps(job) + "\n")
        else:
            # Clear the file
            queue_path.write_text("")

    return jobs


def get_embedding_queue_count() -> int:
    """Get number of pending embedding jobs without removing them."""
    queue_path = get_embedding_queue_path()

    if not queue_path.exists():
        return 0

    lock = FileLock(str(queue_path) + ".lock", timeout=5)

    with lock:
        count = 0
        try:
            with open(queue_path, "r") as f:
                for line in f:
                    if line.strip():
                        count += 1
        except FileNotFoundError:
            return 0

    return count


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
    1. ~/.memex/config.json -> memex_path (user override)
    2. CLAUDE_PLUGIN_ROOT env var (set by plugin system)
    3. Script location fallback (if fallback_to_script=True)
    """
    config = get_config()
    memex_path = config.get("memex_path")

    if not memex_path:
        if fallback_to_script:
            # Fallback: assume scripts are in memex/scripts/
            return Path(__file__).parent.parent.resolve()
        raise ValueError("memex_path not configured and CLAUDE_PLUGIN_ROOT not set")

    path = Path(memex_path).expanduser().resolve()

    if not path.exists():
        if fallback_to_script:
            # Fallback: assume scripts are in memex/scripts/
            return Path(__file__).parent.parent.resolve()
        raise ValueError(f"Memex path does not exist: {path}")

    return path


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
