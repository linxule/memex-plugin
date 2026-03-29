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


def get_index_path() -> Path:
    return get_memex_path() / "_index.sqlite"
