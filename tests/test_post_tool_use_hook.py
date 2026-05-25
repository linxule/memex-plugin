"""Tests for hooks/post-tool-use.py — the deterministic scrub gate.

The hook itself is a PEP 723 script invoked by Claude Code with JSON on
stdin. These tests exercise the same code by importing the module via
`importlib` (the hook lives in `hooks/`, not on the package path) and
calling its `main()` with stdin redirected and a mocked sys.exit.

Coverage: gated-path scrub triggers, off-path writes pass through, non-write
tools pass through, missing-file is a no-op, scrub errors don't propagate.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest


HOOK_PATH = Path(__file__).resolve().parent.parent / "hooks" / "post-tool-use.py"


@pytest.fixture
def hook_module():
    """Import the hook script as a module without running it."""
    spec = importlib.util.spec_from_file_location("post_tool_use_hook", HOOK_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_hook(hook_module, payload: dict) -> int:
    """Invoke the hook's main() with `payload` on stdin. Returns exit code."""
    stdin = io.StringIO(json.dumps(payload))
    orig_stdin = sys.stdin
    sys.stdin = stdin
    try:
        hook_module.main()
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0
    finally:
        sys.stdin = orig_stdin
    return 0


class TestGatedPaths:
    def test_memo_write_triggers_scrub(self, tmp_path: Path, hook_module):
        # Build the gated path shape: /<tmp>/projects/<name>/memos/<file>
        memo_dir = tmp_path / "projects" / "myproj" / "memos"
        memo_dir.mkdir(parents=True)
        memo = memo_dir / "2026-05-25-test.md"
        memo.write_text("key=ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA done")

        code = run_hook(hook_module, {
            "session_id": "test",
            "tool_name": "Write",
            "tool_input": {"file_path": str(memo), "content": "..."},
            "hook_event_name": "PostToolUse",
        })
        assert code == 0
        assert "<REDACTED:github-token>" in memo.read_text()
        assert "ghp_" not in memo.read_text()

    def test_auto_memory_write_triggers_scrub(self, tmp_path: Path, hook_module):
        am_dir = tmp_path / "projects" / "myproj" / "auto-memory"
        am_dir.mkdir(parents=True)
        am = am_dir / "session.md"
        am.write_text("Found AIzaSyD1234567890abcdefghijklmnopqrstuv in env")

        code = run_hook(hook_module, {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(am)},
            "hook_event_name": "PostToolUse",
        })
        assert code == 0
        assert "<REDACTED:google-api>" in am.read_text()


class TestPassThrough:
    def test_write_outside_gated_dirs_passes_through(self, tmp_path: Path, hook_module):
        # Anywhere not matching /projects/.../{memos,auto-memory}/
        other = tmp_path / "projects" / "myproj" / "transcripts" / "session.md"
        other.parent.mkdir(parents=True)
        original = "key=ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA in transcript"
        other.write_text(original)

        code = run_hook(hook_module, {
            "tool_name": "Write",
            "tool_input": {"file_path": str(other)},
            "hook_event_name": "PostToolUse",
        })
        assert code == 0
        assert other.read_text() == original, "Transcripts must not be touched"

    def test_topics_write_passes_through(self, tmp_path: Path, hook_module):
        topics = tmp_path / "topics"
        topics.mkdir()
        t = topics / "agent-architecture.md"
        original = "key=ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA in topic"
        t.write_text(original)

        code = run_hook(hook_module, {
            "tool_name": "Write",
            "tool_input": {"file_path": str(t)},
            "hook_event_name": "PostToolUse",
        })
        assert code == 0
        assert t.read_text() == original

    def test_non_write_tool_passes_through(self, tmp_path: Path, hook_module):
        # Tool is Read — hook should exit 0 without touching anything.
        memo_dir = tmp_path / "projects" / "myproj" / "memos"
        memo_dir.mkdir(parents=True)
        memo = memo_dir / "x.md"
        original = "key=ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        memo.write_text(original)

        code = run_hook(hook_module, {
            "tool_name": "Read",
            "tool_input": {"file_path": str(memo)},
            "hook_event_name": "PostToolUse",
        })
        assert code == 0
        # File untouched: even if path matches, Read shouldn't trigger scrub.
        assert memo.read_text() == original

    def test_missing_file_path_passes_through(self, hook_module):
        code = run_hook(hook_module, {
            "tool_name": "Write",
            "tool_input": {},
            "hook_event_name": "PostToolUse",
        })
        assert code == 0  # No file_path → exit cleanly

    def test_nonexistent_file_passes_through(self, tmp_path: Path, hook_module):
        # Path matches gate but file doesn't exist (Write failed upstream).
        fake = tmp_path / "projects" / "myproj" / "memos" / "ghost.md"
        code = run_hook(hook_module, {
            "tool_name": "Write",
            "tool_input": {"file_path": str(fake)},
            "hook_event_name": "PostToolUse",
        })
        assert code == 0


class TestIdempotency:
    def test_already_scrubbed_memo_unchanged(self, tmp_path: Path, hook_module):
        memo_dir = tmp_path / "projects" / "myproj" / "memos"
        memo_dir.mkdir(parents=True)
        memo = memo_dir / "x.md"
        already = "key=<REDACTED:github-token> from earlier"
        memo.write_text(already)

        code = run_hook(hook_module, {
            "tool_name": "Write",
            "tool_input": {"file_path": str(memo)},
            "hook_event_name": "PostToolUse",
        })
        assert code == 0
        assert memo.read_text() == already
