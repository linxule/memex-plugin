"""Memex - Personal knowledge base for Claude Code sessions."""
from pathlib import Path
import tomllib


def _read_version() -> str:
    """Single source of truth: pyproject.toml at the vault/cache root.

    memex uses `package = false`, so importlib.metadata is unreliable.
    Reading pyproject.toml directly works both from the working vault and
    from the plugin cache (both preserve the src/memex/ layout under root).

    Collapses the version-bump surface from 4 files to 3: bump pyproject.toml,
    .claude-plugin/plugin.json, .claude-plugin/marketplace.json. This file
    auto-tracks.
    """
    pyproject = Path(__file__).resolve().parent.parent.parent / "pyproject.toml"
    with open(pyproject, "rb") as f:
        return tomllib.load(f)["project"]["version"]


__version__ = _read_version()
