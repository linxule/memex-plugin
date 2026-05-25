"""Verify that `sync_auto_memory.sync_file` pre-scrubs secrets before disk.

The PostToolUse hook only fires on Claude Code's Write|Edit|MultiEdit tool
invocations. `memex sync --apply` writes via `Path.write_text`, which bypasses
the hook. Without coverage at the sync site, a Claude auto-memory file
containing a leaked key would round-trip into the vault un-redacted and become
discoverable via search.

Tests build provider-shaped fixtures at runtime via concatenation so the test
source bytes don't trip GitHub push protection (same pattern as test_scrub.py).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memex.scripts.sync_auto_memory import sync_file


def _anthropic_fixture() -> str:
    """Build a fake-but-shape-valid Anthropic key without putting the full
    pattern in source bytes."""
    return "sk-ant" + "-api03-" + "A" * 56


def _slack_fixture() -> str:
    return "xox" + "b-" + "0" * 10 + "-" + "0" * 10 + "-" + "A" * 24


def _make_plan_item(tmp_path: Path, source_content: str) -> dict:
    src = tmp_path / "claude-memory" / "session-A" / "memory" / "MEMORY.md"
    src.parent.mkdir(parents=True)
    src.write_text(source_content)
    return {
        "source_path": str(src),
        "filename": "MEMORY.md",
        "project_memex": "test-proj",
        "project_display": "test-proj",
        "modified_date": "2026-05-25",
        "source_hash": "deadbeef",
        "is_memory_md": True,
        "title": "MEMORY",
        "line_count": source_content.count("\n") + 1,
        "action": "new",
        "vault_path": "projects/test-proj/auto-memory/MEMORY.md",
    }


def test_sync_file_scrubs_anthropic_key(tmp_path: Path) -> None:
    secret = _anthropic_fixture()
    body = f"# Memory\n\nKey value:\nANTHROPIC_API_KEY={secret}\n"
    item = _make_plan_item(tmp_path, body)

    result = sync_file(item, memex=tmp_path / "vault", dry_run=False)

    assert result["status"] == "created"
    assert result.get("scrubbed") == 1, "expected one scrub hit"

    written = (tmp_path / "vault" / item["vault_path"]).read_text()
    assert secret not in written, "raw secret must not survive sync"
    assert "<REDACTED:anthropic>" in written


def test_sync_file_scrubs_multiple_secrets(tmp_path: Path) -> None:
    anthropic = _anthropic_fixture()
    slack = _slack_fixture()
    body = (
        f"# Memory\n\n"
        f"Anthropic: {anthropic}\n"
        f"Slack: {slack}\n"
    )
    item = _make_plan_item(tmp_path, body)

    result = sync_file(item, memex=tmp_path / "vault", dry_run=False)

    assert result.get("scrubbed") == 2
    written = (tmp_path / "vault" / item["vault_path"]).read_text()
    assert anthropic not in written
    assert slack not in written
    assert "<REDACTED:anthropic>" in written
    assert "<REDACTED:slack>" in written


def test_sync_file_clean_content_has_no_scrubbed_field(tmp_path: Path) -> None:
    body = "# Memory\n\nNo secrets here, just notes about pytest.\n"
    item = _make_plan_item(tmp_path, body)

    result = sync_file(item, memex=tmp_path / "vault", dry_run=False)

    assert result["status"] == "created"
    assert "scrubbed" not in result, "scrubbed field should be absent when 0"
    written = (tmp_path / "vault" / item["vault_path"]).read_text()
    assert "REDACTED" not in written


def test_sync_file_dry_run_does_not_scrub_or_write(tmp_path: Path) -> None:
    secret = _anthropic_fixture()
    item = _make_plan_item(tmp_path, f"key={secret}\n")

    result = sync_file(item, memex=tmp_path / "vault", dry_run=True)

    assert result["status"] == "would_new"
    assert "scrubbed" not in result
    assert not (tmp_path / "vault" / item["vault_path"]).exists()
