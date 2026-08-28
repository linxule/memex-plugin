from __future__ import annotations

import json
import os
from pathlib import Path

_DEFAULT_STATE_DIR = "~/.memex"
_DEFAULT_CONFIG_PATH = "~/.memex/config.json"


def _read_config_value(key: str, default: str | None = None) -> str | None:
    """Read a single value from config.json without pydantic dependency."""
    config_path = Path(_DEFAULT_CONFIG_PATH).expanduser()
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
            return config.get(key, default)
        except (json.JSONDecodeError, OSError):
            pass
    return default


def _try_get_settings():
    """Try to load full Settings; return None if pydantic-settings unavailable."""
    try:
        from memex.config import get_settings
        return get_settings()
    except ImportError:
        return None


def get_memex_path(settings=None) -> Path:
    _settings_provided = settings is not None

    if not _settings_provided:
        settings = _try_get_settings()

    if settings is not None and settings.memex_path:
        return Path(settings.memex_path).expanduser().resolve()

    # Only fall back to raw config when no settings object was available
    if not _settings_provided and settings is None:
        memex_path = _read_config_value("memex_path")
        if memex_path:
            return Path(memex_path).expanduser().resolve()

    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root:
        return Path(plugin_root).expanduser().resolve()

    raise ValueError("memex_path not configured and CLAUDE_PLUGIN_ROOT not set")


def get_state_dir(settings=None) -> Path:
    _settings_provided = settings is not None

    if not _settings_provided:
        settings = _try_get_settings()

    if settings is not None:
        state_dir = Path(settings.state_dir).expanduser().resolve()
    else:
        raw = _read_config_value("state_dir", _DEFAULT_STATE_DIR)
        state_dir = Path(raw).expanduser().resolve()

    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def get_lock_dir() -> Path:
    lock_dir = get_state_dir() / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir


def get_pending_dir() -> Path:
    pending_dir = get_state_dir() / "pending-memos"
    pending_dir.mkdir(parents=True, exist_ok=True)
    return pending_dir


_INDEX_FILENAME = "_index.sqlite"


def _index_override(settings=None) -> str | None:
    env = os.environ.get("MEMEX_INDEX_PATH")
    if env:
        return env
    if settings is None:
        settings = _try_get_settings()
    if settings is not None:
        return settings.index_path
    return _read_config_value("index_path")


def get_index_path(memex: Path | None = None, settings=None) -> Path:
    """Resolve where a vault's search index lives.

    For the *configured* vault (what `memex_path` points at) the index is kept
    OUT of the vault by default. Vaults tend to live in iCloud/Dropbox, and a
    multi-GB WAL-mode sqlite file inside a synced folder means every write
    re-uploads the whole file and leaves `_index 2.sqlite-wal`-style conflict
    copies behind (hit Aug 2026: 5.6GB index in iCloud Documents). Precedence:

      1. `index_path` in config.json / `MEMEX_INDEX_PATH` env
      2. `<state_dir>/_index.sqlite` when it already exists
      3. `<vault>/_index.sqlite` when it already exists (legacy, pre-v0.17)
      4. `<state_dir>/_index.sqlite` (default for fresh installs)

    Any OTHER vault (tests, ad-hoc `--vault`) keeps its index in-vault. The
    override and state-dir rules apply only to the configured vault, so a tmp
    vault in a test can never resolve to the user's live index.
    """
    try:
        configured = get_memex_path(settings)
    except ValueError:
        configured = None
    if memex is None:
        if configured is None:
            raise ValueError("memex_path not configured and CLAUDE_PLUGIN_ROOT not set")
        memex = configured
    memex = Path(memex).expanduser()
    in_vault = memex / _INDEX_FILENAME

    if configured is None or memex.resolve() != configured:
        return in_vault

    override = _index_override(settings)
    if override:
        # The only exit that can name a directory nobody has created yet;
        # every other branch goes through get_state_dir(), which mkdirs.
        pinned = Path(override).expanduser().resolve()
        pinned.parent.mkdir(parents=True, exist_ok=True)
        return pinned

    state_index = get_state_dir(settings) / _INDEX_FILENAME
    if state_index.exists():
        return state_index
    if in_vault.exists():
        return in_vault
    return state_index
