"""Tests for the Obsidian CLI wrapper's deep health-check (v0.14.3).

A wedged Obsidian renderer keeps accepting connections — so `version` still
returns — while returning EMPTY for every real query. The legacy
``is_available()`` (version-only) reported True in that state, silently
starving callers that should have fallen back to the SQLite graph queries.
``is_available(deep=True)`` runs a cheap real query (``vault``) and treats an
empty result as unavailable. These tests pin that behaviour without needing a
running Obsidian instance.
"""

from __future__ import annotations

import sys
from pathlib import Path

# scripts/obsidian_cli.py is canonical vault tooling (no src/ shim), so add the
# scripts dir to the path explicitly rather than relying on conftest.
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from obsidian_cli import ObsidianCLI  # noqa: E402


def _cli(responses: dict[tuple[str, ...], str]) -> ObsidianCLI:
    """Build an ObsidianCLI with _run_raw stubbed to scripted responses."""
    cli = ObsidianCLI.__new__(ObsidianCLI)  # bypass __init__ (no binary needed)
    cli.vault = "test"
    cli.timeout = 1
    cli._binary = "/fake/obsidian"
    cli._run_raw = lambda args: responses.get(tuple(args), "")  # type: ignore[method-assign]
    return cli


def test_deep_health_check_detects_wedged_instance() -> None:
    # version succeeds but the real `vault` query returns empty → wedged.
    cli = _cli({("version",): "1.13.1 (installer 1.12.4)\n", ("vault",): ""})
    assert cli.is_available(deep=True) is False
    # The legacy version-only check still passes (documents the old blind spot).
    assert cli.is_available(deep=False) is True


def test_deep_health_check_passes_when_responsive() -> None:
    cli = _cli({("version",): "1.13.1\n", ("vault",): "name\tmemex\nfiles\t3500\n"})
    assert cli.is_available(deep=True) is True


def test_unavailable_when_version_empty() -> None:
    cli = _cli({("version",): "", ("vault",): "name\tmemex\n"})
    assert cli.is_available() is False


def test_unavailable_when_cli_not_enabled() -> None:
    cli = _cli({("version",): "Obsidian CLI is not enabled\n"})
    assert cli.is_available() is False


def test_no_binary_is_unavailable() -> None:
    cli = ObsidianCLI.__new__(ObsidianCLI)
    cli.vault = "test"
    cli.timeout = 1
    cli._binary = None
    assert cli.is_available() is False
