"""Every hook starts the way Claude Code starts it (v0.22.0 wave 0).

Production runs each hook with ``uv run --script``: only the PEP 723
dependencies in the script's header exist. The rest of the suite runs hooks
with the test interpreter, which has every project dependency, so a hook that
imports e.g. ``yaml`` (via ``memex.session_memos``) without listing ``pyyaml``
would pass there and die at session start in real use — and a dead hook is
silent. This file is the guard: each hook, real interpreter, several payloads,
exit 0 and no traceback.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hook_helpers import HOOKS_DIR

SID = "00000000-0000-4000-8000-00000000c0de"


def _transcript(tmp: Path) -> Path:
    t = tmp / f"{SID}.jsonl"
    rows = [
        {"type": "user", "sessionId": SID, "cwd": str(tmp), "entrypoint": "cli",
         "timestamp": "2026-10-28T10:00:00Z", "message": {"role": "user", "content": "hello"}},
        {"type": "assistant", "sessionId": SID, "timestamp": "2026-10-28T10:00:05Z",
         "message": {"role": "assistant", "model": "claude-opus-5-5", "content": [{"type": "text", "text": "hi"}]}},
    ]
    t.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return t


def _big_transcript(tmp: Path) -> Path:
    t = tmp / "big" / f"{SID}.jsonl"
    t.parent.mkdir(exist_ok=True)
    t.write_bytes(_transcript(tmp).read_bytes() + b" " * 3_100_000 + b"\n")
    return t


def _payloads(env) -> dict[str, list[dict]]:
    t = str(_transcript(env.home))
    cwd = str(env.home)
    base = {"session_id": SID, "transcript_path": t, "cwd": cwd}
    vault_cwd = str(env.vault)
    return {
        "session-start": [
            {**base, "hook_event_name": "SessionStart", "source": s} for s in ("startup", "resume", "compact", "clear", "fork")
        ] + [{**base, "cwd": vault_cwd, "hook_event_name": "SessionStart", "source": "startup"}],
        "user-prompt-submit": [{**base, "hook_event_name": "UserPromptSubmit", "prompt": "x"},
                               # past the first byte tier: the nudge path and its lazy imports run
                               {**base, "transcript_path": str(_big_transcript(env.home)),
                                "hook_event_name": "UserPromptSubmit", "prompt": "x"}],
        "pre-compact": [{**base, "hook_event_name": "PreCompact", "trigger": "auto"}],
        "post-compact": [{**base, "hook_event_name": "PostCompact", "trigger": "auto",
                          "compact_summary": "Synthetic summary: the user and Claude set up a test fixture."}],
        "session-end": [{**base, "hook_event_name": "SessionEnd", "reason": "other"}],
        "post-tool-use": [{**base, "hook_event_name": "PostToolUse", "tool_name": "Write",
                           "tool_input": {"file_path": str(env.vault / "projects/p/memos/m.md")}, "tool_response": {}}],
    }


def _hooks() -> list[str]:
    return sorted(p.stem for p in HOOKS_DIR.glob("*.py"))


def test_every_hook_script_has_payloads(hook_env):
    """A new hook (e.g. post-compact.py) must be added to _payloads, or it is unguarded."""
    covered = set(_payloads(hook_env))
    assert set(_hooks()) <= covered, f"add payloads for: {sorted(set(_hooks()) - covered)}"


@pytest.mark.parametrize("hook", _hooks())
def test_hook_runs_with_only_its_inline_dependencies(hook_env, hook):
    for payload in _payloads(hook_env).get(hook, []):
        result = hook_env.run(hook, payload, real=True, timeout=120)
        where = f"{hook} {payload.get('source') or payload.get('hook_event_name')}"
        assert result.returncode == 0, f"{where}: exit {result.returncode}\n{result.stderr[-2000:]}"
        for marker in ("Traceback", "ModuleNotFoundError", "ImportError"):
            assert marker not in result.stderr, f"{where}: {marker}\n{result.stderr[-2000:]}"


def test_post_tool_use_records_the_memo_with_only_its_inline_dependencies(hook_env):
    """The recording step swallows errors (it is a shortcut), so a missing
    dependency would only log: check that the hint file is really written."""
    memo = hook_env.write_memo("p", "2026-11-07-m.md", f"session_id: {SID}")
    payload = {"session_id": SID, "hook_event_name": "PostToolUse", "tool_name": "Write",
               "tool_input": {"file_path": str(memo)}, "tool_response": {}}
    result = hook_env.run("post-tool-use", payload, real=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    hint = hook_env.state_dir / "session-state" / f"{SID[:16]}.memo"
    assert json.loads(hint.read_text()) == {"session_id": SID, "memo_path": str(memo)}, result.stderr[-2000:]
