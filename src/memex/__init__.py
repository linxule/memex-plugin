"""Memex - Personal knowledge base for Claude Code sessions."""
from pathlib import Path
import tomllib


def _read_version() -> str:
    """Read live source metadata in checkouts, distribution metadata in wheels."""
    source_dir = Path(__file__).resolve().parent.parent
    pyproject = source_dir.parent / "pyproject.toml"
    # Vaults and plugin caches preserve src/memex; prefer the live version
    # there even if an editable install's distribution metadata is stale.
    if source_dir.name == "src" and pyproject.is_file():
        with pyproject.open("rb") as f:
            return tomllib.load(f)["project"]["version"]

    # Installed wheels do not contain the source tree's pyproject.toml.
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("memex")
    except PackageNotFoundError:
        # Vendored or copied without metadata: importing must still succeed.
        if pyproject.is_file():
            with pyproject.open("rb") as f:
                return tomllib.load(f)["project"]["version"]
        return "0.0.0+unknown"


__version__ = _read_version()
