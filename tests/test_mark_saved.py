"""Verify `memex mark-saved` selects the right session.

The pre-fix heuristic (newest state file by mtime, no project filter) caused
cross-project contamination: running `memex mark-saved` from project A could
mark a session in project B whose state file was touched a moment later. The
fix prefers `CLAUDE_CODE_SESSION_ID` env var (set by Claude Code 2.1+) and
falls back to mtime only when the env var is absent.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner


def _state_dir(home: Path) -> Path:
    d = home / ".memex" / "session-state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_state(home: Path, session_id: str, count: int = 10) -> Path:
    """Write a session state file. session_id must be at least 16 chars
    because the CLI keys by the first 16 chars (matching the hook)."""
    assert len(session_id) >= 16
    f = _state_dir(home) / f"{session_id[:16]}.json"
    f.write_text(json.dumps({"count": count, "chars": 100, "memo_saved": False, "tier_nudged": 0}))
    return f


def _read_state(home: Path, session_id_16: str) -> dict:
    f = _state_dir(home) / f"{session_id_16}.json"
    return json.loads(f.read_text())


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect HOME so mark-saved touches tmp_path/.memex instead of the real one."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return tmp_path


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


def _invoke_mark_saved(cli_runner: CliRunner, isolated_home: Path):
    """Re-import the CLI module so it picks up the fresh HOME env var."""
    # The CLI uses Path.home() at call time (not import time), so re-import
    # isn't strictly required, but it keeps the test resilient against future
    # changes that cache the path.
    from memex.cli import app
    return cli_runner.invoke(app, ["mark-saved"])


def test_env_var_selects_correct_session(isolated_home, cli_runner, monkeypatch):
    """When CLAUDE_CODE_SESSION_ID is set, mark-saved must pick THAT session
    regardless of which state file is newest by mtime."""
    older_id = "aaaaaaaa-1111-4111-8111-111111111111"
    newer_id = "bbbbbbbb-2222-4222-8222-222222222222"

    older_file = _write_state(isolated_home, older_id)
    # Make older_file actually older
    old_time = time.time() - 100
    os.utime(older_file, (old_time, old_time))

    newer_file = _write_state(isolated_home, newer_id)
    # newer_file gets current mtime — newest by definition

    # Env var says "use the OLDER session"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", older_id)

    result = _invoke_mark_saved(cli_runner, isolated_home)
    assert result.exit_code == 0, f"unexpected exit: {result.stdout}{result.stderr}"

    # Older session should be marked saved
    assert _read_state(isolated_home, older_id[:16])["memo_saved"] is True
    # Newer session should NOT be touched
    assert _read_state(isolated_home, newer_id[:16])["memo_saved"] is False


def test_fallback_to_newest_when_env_var_absent(isolated_home, cli_runner, monkeypatch):
    """Without CLAUDE_CODE_SESSION_ID, mark-saved falls back to newest-by-mtime
    (the legacy behavior) — kept for older Claude Code versions / ad-hoc CLI use."""
    older_id = "aaaaaaaa-1111-4111-8111-111111111111"
    newer_id = "bbbbbbbb-2222-4222-8222-222222222222"

    older_file = _write_state(isolated_home, older_id)
    old_time = time.time() - 100
    os.utime(older_file, (old_time, old_time))

    newer_file = _write_state(isolated_home, newer_id)

    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    result = _invoke_mark_saved(cli_runner, isolated_home)
    assert result.exit_code == 0

    # Newer session should be marked saved (legacy mtime fallback)
    assert _read_state(isolated_home, newer_id[:16])["memo_saved"] is True
    assert _read_state(isolated_home, older_id[:16])["memo_saved"] is False


def test_env_var_with_no_matching_state_file_warns_and_falls_back(
    isolated_home, cli_runner, monkeypatch
):
    """If the env var points at a session with no state file (e.g. config
    drift), warn on stderr and fall back to the newest-by-mtime state file.
    Better to nudge user about the drift than silently fix the wrong session."""
    real_id = "aaaaaaaa-1111-4111-8111-111111111111"
    unknown_id = "ffffffff-9999-4999-8999-999999999999"

    _write_state(isolated_home, real_id)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", unknown_id)

    result = _invoke_mark_saved(cli_runner, isolated_home)
    assert result.exit_code == 0
    assert "WARN" in (result.stderr or "")
    assert "had no state file" in (result.stderr or "")
    # The real (only) state file should be marked saved as the fallback
    assert _read_state(isolated_home, real_id[:16])["memo_saved"] is True


def test_clears_matching_pending_signal(isolated_home, cli_runner, monkeypatch):
    """When a pending-memo signal exists matching the env-var-selected session,
    mark-saved should also remove the signal file."""
    session_id = "aaaaaaaa-1111-4111-8111-111111111111"
    _write_state(isolated_home, session_id)

    pending_dir = isolated_home / ".memex" / "pending-memos"
    pending_dir.mkdir(parents=True, exist_ok=True)
    signal_file = pending_dir / f"{session_id[:16]}.json"
    signal_file.write_text(json.dumps({
        "session_id": session_id,
        "transcript_path": "/fake/path.jsonl",
        "project": "tests",
        "cwd": str(isolated_home),
        "timestamp": "2026-05-25T15:30:00.000000",
    }))

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session_id)

    result = _invoke_mark_saved(cli_runner, isolated_home)
    assert result.exit_code == 0
    assert not signal_file.exists(), "matching signal must be removed"


# ── v0.16.4: full-id canonical-state keys ───────────────────────────────
#
# Before v0.16.4, mark-saved recorded memo_generated under the 16-char
# state-file prefix whenever no pending signal existed (i.e. the normal
# /memex:save-before-compact flow). PreCompact looks up the FULL session id,
# so the check missed and a stale orphan signal was written after every
# compact — observed live 2026-08-23 (mac-migration-kit, 2-minute race), with
# 305 prefix keys accumulated in state.json by 2026-08-25.


def _canonical_sessions(home: Path) -> dict:
    state_file = home / ".memex" / "state.json"
    if not state_file.exists():
        return {}
    return json.loads(state_file.read_text()).get("processed_sessions", {})


def test_env_var_marks_full_session_id_without_pending_signal(
    isolated_home, cli_runner, monkeypatch
):
    """The exact stale-signal scenario: /memex:save runs BEFORE compact, so no
    pending signal exists. The canonical key must be the full id from the env
    var, not the 16-char prefix."""
    session_id = "cccccccc-3333-4333-8333-333333333333"
    _write_state(isolated_home, session_id)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session_id)

    result = _invoke_mark_saved(cli_runner, isolated_home)
    assert result.exit_code == 0

    sessions = _canonical_sessions(isolated_home)
    assert "memo_generated_at" in sessions.get(session_id, {}), (
        "must key by FULL session id so PreCompact's full-id lookup hits"
    )
    assert session_id[:16] not in sessions, "no prefix key should be written"


def test_precompact_check_sees_mark_saved_state(isolated_home, cli_runner, monkeypatch):
    """End-to-end guard for the stale-signal bug: after mark-saved, the exact
    check PreCompact performs must return True for the full session id."""
    session_id = "dddddddd-4444-4444-8444-444444444444"
    _write_state(isolated_home, session_id)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session_id)

    result = _invoke_mark_saved(cli_runner, isolated_home)
    assert result.exit_code == 0

    from memex.scripts.utils import is_session_processed
    assert is_session_processed(session_id, "memo_generated"), (
        "PreCompact would have written a stale orphan signal"
    )


def test_is_session_processed_falls_back_to_legacy_prefix_key(
    isolated_home, monkeypatch
):
    """300+ pre-v0.16.4 entries live under 16-char prefix keys; the reader
    must still find them so those sessions aren't re-signaled as orphans."""
    session_id = "eeeeeeee-5555-4555-8555-555555555555"
    state_file = isolated_home / ".memex" / "state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({
        "processed_sessions": {
            session_id[:16]: {"memo_generated_at": "2026-08-23T15:17:40"},
        }
    }))

    from memex.scripts.utils import is_session_processed
    assert is_session_processed(session_id, "memo_generated")
    # A different full id sharing no prefix must NOT match.
    assert not is_session_processed(
        "ffffffff-6666-4666-8666-666666666666", "memo_generated"
    )


def test_pending_signal_still_recovers_full_id_when_env_absent(
    isolated_home, cli_runner, monkeypatch
):
    """Fallback path (older Claude Code / ad-hoc CLI): with no env var, the
    pending signal's recorded full id is still used for the canonical key."""
    session_id = "bbbbbbbb-2222-4222-8222-222222222222"
    _write_state(isolated_home, session_id)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    pending_dir = isolated_home / ".memex" / "pending-memos"
    pending_dir.mkdir(parents=True, exist_ok=True)
    (pending_dir / f"{session_id[:16]}.json").write_text(json.dumps({
        "session_id": session_id,
        "transcript_path": "/fake/path.jsonl",
        "project": "tests",
        "cwd": str(isolated_home),
        "timestamp": "2026-08-25T12:00:00.000000",
    }))

    result = _invoke_mark_saved(cli_runner, isolated_home)
    assert result.exit_code == 0
    sessions = _canonical_sessions(isolated_home)
    assert "memo_generated_at" in sessions.get(session_id, {})
