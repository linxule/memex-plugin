"""Memex - Personal knowledge base for Claude Code sessions."""
from pathlib import Path
import tomllib

UNKNOWN_VERSION = "0.0.0+unknown"


def _pyproject_version(pyproject: Path) -> str | None:
    """Version from ``pyproject`` only when it really describes memex.

    A vendored copy can sit under a host application's ``src/``; its
    pyproject names the host (and may use dynamic versioning with no
    ``version`` key). Neither must be reported as memex's version.
    """
    try:
        with pyproject.open("rb") as f:
            project = tomllib.load(f).get("project", {})
    except (OSError, tomllib.TOMLDecodeError):
        return None
    if project.get("name") != "memex":
        return None
    version = project.get("version")
    return version if isinstance(version, str) and version else None


def _read_version() -> str:
    """Read live source metadata in checkouts, distribution metadata in wheels."""
    source_dir = Path(__file__).resolve().parent.parent
    pyproject = source_dir.parent / "pyproject.toml"
    # Vaults and plugin caches preserve src/memex; prefer the live version
    # there even if an editable install's distribution metadata is stale.
    if source_dir.name == "src" and pyproject.is_file():
        version = _pyproject_version(pyproject)
        if version:
            return version

    # Installed wheels do not contain the source tree's pyproject.toml.
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("memex")
    except PackageNotFoundError:
        # Vendored or copied without metadata: importing must still succeed.
        return _pyproject_version(pyproject) or UNKNOWN_VERSION


__version__ = _read_version()
