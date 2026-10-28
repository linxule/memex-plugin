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

def detect_project(cwd: str, *, _depth: int = 0) -> str:
    """
    Detect project name from working directory.

    Priority (rule names in brackets are ``memex.routing.RULES``):
    1. [pin] Explicit mapping in config (``project_mappings`` substring)
    1b. [scratchpad] Session scratchpad → the parent session's true cwd
        (recursive; nested fleets hop again, bounded at 3). Deliberately BEFORE
        the git rules: a worker's checkout inside a scratchpad belongs to the
        session that spawned it, not to whatever remote that checkout carries.
    1c. [temp-prefix] A cwd under a temp root (``/private/tmp``,
        ``/private/var/folders/*/*/T`` and their ``/tmp``, ``/var/folders``
        spellings) never mints a folder: a path component that starts with an
        existing vault project name plus a separator names the project
        (``paperb-v018-gemini-ci1pdz6a`` → ``paperb``), else a remote naming an
        existing project [git-remote], else ``_uncategorized`` [fallback].
    2. [git-remote] Git remote name (parsed from origin URL)
    3. [git-root] Git root folder name
       [remembered] 2 and 3 record ``repo → project`` on first resolution and
       keep returning it when the remote later names another project.
    4. [leaf] CWD folder name
    5. [fallback] '_uncategorized'
    """
    return _detect_project_explained(cwd, _depth)[0]


def detect_project_explained(cwd: str) -> tuple[str, str]:
    """``(project, rule)``: ``detect_project`` plus the rule that decided.

    The rule names are ``memex.routing.RULES`` (pin / scratchpad / temp-prefix
    / remembered / git-remote / git-root / leaf / fallback), so filing
    receipts can flag guesses. ``detect_project(cwd)`` is exactly
    ``detect_project_explained(cwd)[0]``: both run the same code.
    """
    return _detect_project_explained(cwd, 0)


# Rules whose answer is a guess: memex.routing.GUESS_RULES, restated because
# routing imports this module.
_GUESS_RULES = frozenset({"leaf", "fallback"})


def _detect_project_explained(cwd: str, _depth: int) -> tuple[str, str]:
    cwd_path = Path(cwd).resolve()
    config = get_config()

    # 1. Check explicit mapping
    mappings = config.get("project_mappings", {})
    for pattern, project in mappings.items():
        if pattern in str(cwd_path):
            return sanitize_project_name(project), "pin"

    # 1b. Session scratchpad: detect on the parent session's cwd instead of the
    #     worker's leaf folder. A validated parent's encoded name is strictly
    #     shorter than the child's, so the chain cannot cycle; the bound and the
    #     self-check are defence in depth, not load-bearing.
    is_scratchpad = bool(_SCRATCHPAD_CWD_RE.match(str(cwd_path)))
    if _depth < 3 and is_scratchpad:
        parent = scratchpad_parent_cwd(str(cwd_path))
        if parent and Path(parent).resolve() != cwd_path:
            project, parent_rule = _detect_project_explained(parent, _depth + 1)
            # Routed by the scratchpad rule; a guess on the owner's side stays
            # a guess so filing receipts still flag it.
            return project, (parent_rule if parent_rule in _GUESS_RULES else "scratchpad")
        if cwd_path.name == "scratchpad":
            # An orphaned scratchpad root (owner transcript pruned before a
            # late import) is nobody's project; a worker's leaf below it still
            # falls through to the usual rules. Scoped here rather than via
            # RESERVED_NAMES so a real repo named "scratchpad" is unaffected.
            return "_uncategorized", "fallback"

    # 1c. Temp roots. Scratchpad paths keep their own rule above (an
    #     unresolvable worker leaf still falls through to git/leaf, v0.20.2).
    temp_parts = None if is_scratchpad else _temp_root_parts(str(cwd_path))
    if temp_parts is not None:
        return _detect_temp_cwd(cwd_path, temp_parts)

    # 2. Try git remote
    git_root: Path | None = None
    git_project = get_git_project(cwd_path)
    if git_project:
        project, rule = sanitize_project_name(git_project), "git-remote"
    else:
        # 3. Try git root folder name
        git_root = get_git_root(cwd_path)
        project, rule = (sanitize_project_name(git_root.name), "git-root") if git_root else ("", "")
    if project == "_uncategorized":
        # A repo rooted at a reserved name (~/Documents, …/home.git).
        return project, "fallback"
    if project:
        if is_scratchpad:
            # A worker checkout under an unresolvable scratchpad is ephemeral:
            # never worth remembering.
            return project, rule
        return _apply_cwd_memory(cwd_path, git_root, project, rule)

    # 4. Use CWD folder name
    folder_name = cwd_path.name
    if folder_name and folder_name not in ("", "/"):
        project = sanitize_project_name(folder_name)
        # A reserved leaf (home, Downloads, tmp…) is the fallback in disguise.
        return project, ("fallback" if project == "_uncategorized" else "leaf")

    # 5. Fallback
    return "_uncategorized", "fallback"


# --- B1: temp-root cwds (v0.22.0) -------------------------------------------
# Fleet seats run from mkdtemp dirs whose names carry the launching project
# (``/T/paperb-v018-gemini-ci1pdz6a``, ``/T/alpha-direction-claude-cjpm6dd6``)
# or from a generic child of one (``/tmp/mcp-claude-v2-host.Jl689Y/repo``). The
# leaf rule minted one vault folder per seat (2026-10-28: 7 folders for one paper, 2
# for another, ``repo``). A temp path is never a project of its own — except a
# session-scratchpad worker whose owner is unresolvable, which keeps the
# v0.20.2 fall-through to git/leaf (rule 1b).
_TEMP_ROOT_RE = re.compile(r"^(?:/private)?(?:/tmp|/var/folders/[^/]+/[^/]+/T)(?=/|$)")
# Leaves that say nothing about the project: walk past them to the parent.
_GENERIC_TEMP_LEAVES = frozenset({
    "repo", "repos", "wt", "work", "worktree", "worktrees", "src", "checkout", "clone",
})
# Claude Code's per-user temp area (``claude-501``) is never a project name.
_CLAUDE_TMP_COMPONENT_RE = re.compile(r"^claude-\d+$")
_PROJECT_NAME_SEPARATORS = frozenset("-_.")
_TEMP_SCAN_LIMIT = 12


def _is_anonymous_temp_component(name: str) -> bool:
    return name in _GENERIC_TEMP_LEAVES or bool(_CLAUDE_TMP_COMPONENT_RE.match(name))


def _temp_root_parts(path: str) -> list[str] | None:
    """Components below a temp root (leaf last), or ``None`` if not under one."""
    m = _TEMP_ROOT_RE.match(path)
    if not m:
        return None
    rest = path[m.end():].strip("/")
    return rest.split("/") if rest else []


def _project_prefix_match(name: str, projects: frozenset[str] | tuple[str, ...]) -> str | None:
    """Longest project ``p`` with ``name == p`` or ``name`` = ``p`` + separator + …

    The separator is required so ``paper`` never captures ``paperb-…``.
    """
    best: str | None = None
    for p in projects:
        if name == p or (
            len(name) > len(p) and name.startswith(p) and name[len(p)] in _PROJECT_NAME_SEPARATORS
        ):
            if best is None or len(p) > len(best):
                best = p
    return best


def _detect_temp_cwd(cwd_path: Path, parts: list[str]) -> tuple[str, str]:
    """Route a temp-root cwd to an existing project, else ``_uncategorized``.

    In order: (1) the leaf, walking up through generic names (``repo``,
    ``wt``…) only, i.e. the seat dir's own name; (2) a git remote naming an
    existing project (a clone in a ``tmpXXXX`` dir); (3) the nearest
    ancestor that matches (a subdir of a named seat dir). Never a new folder:
    an unknown temp path is ``_uncategorized``.
    """
    projects = _vault_project_names() or frozenset()
    if not (projects and parts):
        return "_uncategorized", "fallback"
    scan = parts[-_TEMP_SCAN_LIMIT:]
    i = len(scan) - 1
    while i >= 0 and _is_anonymous_temp_component(scan[i]):
        i -= 1
    if i >= 0:
        match = _project_prefix_match(scan[i], projects)
        if match:
            return match, "temp-prefix"
    remote = get_git_project(cwd_path)
    if remote and sanitize_project_name(remote) in projects:
        return sanitize_project_name(remote), "git-remote"
    for name in reversed(scan[:max(i, 0)]):
        if not _is_anonymous_temp_component(name):
            match = _project_prefix_match(name, projects)
            if match:
                return match, "temp-prefix"
    return "_uncategorized", "fallback"


def _vault_project_names() -> frozenset[str] | None:
    """Folder names under the configured vault's ``projects/``; ``None`` if unknown.

    Only names detection itself could produce (sanitize fixed points: no
    ``_uncategorized``, reserved, dotted or hidden names). Cached per process,
    keyed by the directory's mtime so a folder added or removed mid-run is
    seen; a missing or unconfigured vault is ``None``, never an error.
    """
    try:
        from memex.paths import get_memex_path as resolve_memex_path

        projects_dir = resolve_memex_path() / "projects"
        mtime_ns = projects_dir.stat().st_mtime_ns
    except (ValueError, OSError):
        return None
    return _project_names_in(str(projects_dir), mtime_ns)


@functools.lru_cache(maxsize=8)
def _project_names_in(projects_dir: str, mtime_ns: int) -> frozenset[str] | None:
    try:
        entries = list(os.scandir(projects_dir))
    except OSError:
        return None
    names = set()
    for entry in entries:
        try:
            is_dir = entry.is_dir()
        except OSError:
            continue
        if is_dir and entry.name != "_uncategorized" and sanitize_project_name(entry.name) == entry.name:
            names.add(entry.name)
    return frozenset(names)


# --- B2: remembered repo → project (v0.22.0) -------------------------------
# A cwd's remote can change under it: on 2026-10-22 a repo SPLIT repointed a
# checkout's origin to a new ``<name>-internal`` repo and every later session
# minted that folder. The first git-rule resolution of a repo is recorded
# (keyed by git root, so subdirs agree) and wins over a later, different git
# answer. Pins still win over memory. Memory yields to the git rule again when
# the remembered folder has left the vault (moved/renamed on purpose) or a
# different checkout now lives at the path (its ``.git`` is another inode).
CWD_MEMORY_FILENAME = "cwd-projects.json"
_CWD_MEMORY_MAX = 2000
# Eviction is least-recently-seen; ``last_seen`` is refreshed at most this often
# so a hit almost never writes.
_CWD_MEMORY_TOUCH_DAYS = 7


def _cwd_memory_path() -> Path | None:
    try:
        from memex.paths import get_state_dir as resolve_state_dir

        return resolve_state_dir() / CWD_MEMORY_FILENAME
    except (ValueError, OSError):
        return None


def _load_cwd_memory(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    cwds = data.get("cwds") if isinstance(data, dict) else None
    return cwds if isinstance(cwds, dict) else {}


def _remembered_project(entry: object) -> str | None:
    project = entry.get("project") if isinstance(entry, dict) else None
    return project if isinstance(project, str) and project.strip() else None


def _git_root_on_disk(path: Path) -> Path | None:
    """Nearest ancestor holding ``.git`` (a dir, or a file for worktrees and
    submodules): git's own discovery walk without a subprocess."""
    for candidate in (path, *path.parents):
        if os.path.lexists(candidate / ".git"):
            return candidate
    return None


def _repo_identity(root: Path | None) -> str | None:
    """Inode of ``<root>/.git``: survives a repointed origin, not a re-clone."""
    if root is None:
        return None
    try:
        return str(os.stat(root / ".git").st_ino)
    except OSError:
        return None


def _apply_cwd_memory(cwd_path: Path, git_root: Path | None, project: str, rule: str) -> tuple[str, str]:
    """Record a first git-rule answer; prefer the recorded one when they differ."""
    path = _cwd_memory_path()
    if path is None:
        return project, rule
    root = _git_root_on_disk(cwd_path) or git_root or get_git_root(cwd_path)
    key = str(root.resolve()) if root else str(cwd_path)
    repo_id = _repo_identity(root)
    entry = _load_cwd_memory(path).get(key)
    remembered = _remembered_project(entry)
    stored_id = entry.get("repo_id") if isinstance(entry, dict) else None
    if remembered is None or (repo_id and isinstance(stored_id, str) and stored_id != repo_id):
        # First resolution, or a different checkout now lives at this path.
        _record_cwd_project(path, key, project, rule, repo_id, replace=remembered is not None)
        return project, rule
    remembered = sanitize_project_name(remembered)
    if remembered != project:
        known = _vault_project_names()
        if known is not None and remembered not in known:
            # The remembered folder left the vault (moved/renamed on purpose).
            _record_cwd_project(path, key, project, rule, repo_id, replace=True)
            return project, rule
        project, rule = remembered, "remembered"
    if _cwd_memory_is_stale(entry):
        _touch_cwd_memory(path, key)
    return project, rule


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _record_cwd_project(
    path: Path, key: str, project: str, rule: str, repo_id: str | None, *, replace: bool
) -> None:
    """``key → project``; without ``replace`` an existing entry wins (first
    resolution, also against a concurrent writer)."""
    now = _now_iso()

    def update(cwds: dict) -> bool:
        current = cwds.get(key)
        current_project = _remembered_project(current)
        if current_project is not None:
            if not replace:
                return False
            if current_project == project and current.get("repo_id") == repo_id:
                return False
        entry = {"project": project, "rule": rule, "first_seen": now, "last_seen": now}
        if repo_id:
            entry["repo_id"] = repo_id
        cwds[key] = entry
        return True

    _update_cwd_memory(path, update)


def _cwd_memory_is_stale(entry: object) -> bool:
    last = entry.get("last_seen") if isinstance(entry, dict) else None
    try:
        return datetime.now() - datetime.fromisoformat(last) > timedelta(days=_CWD_MEMORY_TOUCH_DAYS)
    except (TypeError, ValueError):
        return True


def _touch_cwd_memory(path: Path, key: str) -> None:
    now = _now_iso()

    def update(cwds: dict) -> bool:
        current = cwds.get(key)
        if not isinstance(current, dict):
            return False
        current["last_seen"] = now
        return True

    _update_cwd_memory(path, update)


def _update_cwd_memory(path: Path, update) -> None:
    """Load, apply ``update(cwds) -> changed``, write atomically (temp +
    ``os.replace``) under a short lock. Best effort: a busy lock or an I/O
    error leaves the file as it was. Bounded to ``_CWD_MEMORY_MAX`` entries."""
    from filelock import Timeout

    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with FileLock(str(path) + ".lock", timeout=1):
            cwds = _load_cwd_memory(path)
            if not update(cwds):
                return
            if len(cwds) > _CWD_MEMORY_MAX:
                cwds = _bounded_cwd_memory(cwds)
            tmp.write_text(json.dumps({"version": 1, "cwds": cwds}, indent=1, sort_keys=True), encoding="utf-8")
            os.replace(tmp, path)
    except (Timeout, OSError):
        pass
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _bounded_cwd_memory(cwds: dict) -> dict:
    """The ``_CWD_MEMORY_MAX`` most recently seen entries; deleted checkouts
    (old worktrees) stop being seen and age out first."""

    def seen(item: tuple[str, object]) -> str:
        value = item[1]
        if isinstance(value, dict):
            for field in ("last_seen", "first_seen"):
                stamp = value.get(field)
                if isinstance(stamp, str):
                    return stamp
        return ""

    return dict(sorted(cwds.items(), key=seen, reverse=True)[:_CWD_MEMORY_MAX])


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
        cwd = _cwd_from_transcript(f, expected)
        if cwd:
            return cwd
    return None


def _cwd_from_transcript(transcript: Path, expected: str) -> str | None:
    """First ``cwd`` in a session JSONL that encodes to ``expected``; else None.

    Bounded to the first 200 lines; a read failure is ``None`` (the caller
    decides whether to try another file).
    """
    try:
        with transcript.open(encoding="utf-8", errors="ignore") as fh:
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
        return None
    return None


# Session scratchpads. Claude Code gives every session a scratchpad at
# ``/private/tmp/claude-<uid>/<encoded-parent-cwd>/<session-uuid>/scratchpad``
# and fleet workers (``entrypoint: sdk-cli``) are routinely launched from a
# subfolder of it, so the cwd leaf (``banks``, ``audit``, ``probe/cc_work``)
# minted one project folder per worker (2026-09-30: 9 folders in one week,
# after 12 ``/private/tmp/ncs-*`` workers on 09-23 needed pins). The encoded
# segment IS the parent's ``~/.claude/projects/`` dir name, so the parent
# session's own transcript yields the true cwd — no need to invert the lossy
# ``/``→``-`` encoding, and the value is validated by ``cwd_from_session``.
_UUID_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# The temp root is not pinned to /tmp: Claude Code carries a CLAUDE_CODE_TMPDIR
# override, and the `claude-<uid>/<enc>/<uuid>/scratchpad` tail is specific
# enough on its own.
_SCRATCHPAD_CWD_RE = re.compile(
    r"^/(?:[^/]+/)*claude-\d+/(?P<enc>-[^/]+)/(?P<uuid>" + _UUID_RE + r")/scratchpad(?:/|$)"
)
# The same layout after Claude's `/`→`-` project-dir encoding (a scratchpad
# session's OWN ~/.claude/projects dir), e.g.
# `-private-tmp-claude-501--Users-x-Documents-Apps-llm-world-<uuid>-scratchpad-wt-studio-1`.
_SCRATCHPAD_DIR_RE = re.compile(
    r"^-(?:.*-)?claude-\d+-(?P<enc>-.+?)-(?P<uuid>" + _UUID_RE + r")-scratchpad(?:-.*)?$"
)


def scratchpad_parent_cwd(cwd: str, claude_projects: Path | None = None) -> str | None:
    """True cwd of the session that owns a scratchpad path, else ``None``.

    The owner session's uuid is in the path, so its own transcript
    (``<enc>/<uuid>.jsonl``) is read first — exact and constant-cost; the
    newest-first scan of the parent dir is the fallback (a worker can outlive
    the owner's transcript, or a subagent transcript may carry the cwd).
    ``claude_projects`` overrides ``~/.claude/projects`` (tests). A parent
    whose transcripts are gone, or whose recorded cwd doesn't encode to the
    dir name, yields ``None`` and the caller falls through to its usual rules.
    """
    m = _SCRATCHPAD_CWD_RE.match(cwd)
    if not m:
        return None
    root = claude_projects if claude_projects is not None else Path.home() / ".claude" / "projects"
    parent_dir = root / m.group("enc")
    own = _cwd_from_transcript(parent_dir / f"{m.group('uuid')}.jsonl", parent_dir.name)
    return own or cwd_from_session(parent_dir)


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
    # A transcript-less scratchpad-session dir (pruned transcripts, memory
    # only) still names its owner in the encoding — route through the owner
    # rather than slugging the whole encoded temp path into a fragment folder.
    m = _SCRATCHPAD_DIR_RE.match(project_dir.name)
    if m and m.group("enc") != project_dir.name:
        return project_names_for_claude_dir(project_dir.parent / m.group("enc"))
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
