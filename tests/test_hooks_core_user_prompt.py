"""UserPromptSubmit nudges by activity (v0.22.0 hooks-core: I4, I7)."""

from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path

import pytest
from hook_helpers import HOOKS_DIR

SID = "a1000000-0000-4000-8000-0000000000a1"
PARENT = "c3000000-0000-4000-8000-0000000000c3"
MB = 1_000_000


def _row(role: str, size: int, *, entrypoint: str | None = "cli", forked_from: str | None = None) -> str:
    row = {"type": role, "sessionId": SID, "message": {"role": role, "content": "x" * size}}
    if entrypoint:
        row["entrypoint"] = entrypoint
    if forked_from:
        row["forkedFrom"] = {"sessionId": forked_from, "messageUuid": "m"}
    return json.dumps(row) + "\n"


def _grow(t: Path, nbytes: int, **kw) -> None:
    """Append roughly ``nbytes`` of synthetic assistant activity."""
    with t.open("a") as fh:
        while nbytes > 0:
            chunk = min(nbytes, 250_000)
            fh.write(_row("assistant", chunk, **kw))
            nbytes -= chunk


def _prompt(hook_env, t: Path, text: str = "go on", **kw):
    result = hook_env.run("user-prompt-submit", {"session_id": SID, "transcript_path": str(t),
                                                 "cwd": str(hook_env.home), "prompt": text,
                                                 "hook_event_name": "UserPromptSubmit"}, **kw)
    assert result.returncode == 0, result.stderr
    return result


@pytest.fixture
def transcript(hook_env) -> Path:
    t = hook_env.home / "t" / f"{SID}.jsonl"
    t.parent.mkdir(parents=True)
    t.write_text(_row("user", 10))
    return t


def _state(env) -> dict:
    return json.loads((env.state_dir / "session-state" / f"{SID[:16]}.json").read_text())


def test_replay_of_a_nine_prompt_8_6_mb_session_is_nudged_before_the_end(hook_env, transcript):
    """I4 acceptance: ~60 agents, 8.6 MB, 9 prompts — the prompt tiers (40/80) never fired."""
    nudged_at = None
    for _ in range(9):
        result = _prompt(hook_env, transcript)
        if result.context and nudged_at is None:
            nudged_at = transcript.stat().st_size
        _grow(transcript, int(0.97 * MB))
    assert transcript.stat().st_size >= 8.6 * MB
    assert nudged_at is not None and nudged_at < 8.6 * MB
    assert nudged_at >= 3 * MB  # not on the first, small prompts
    assert _state(hook_env)["count"] == 9


def test_tiers_fire_once_each(hook_env, transcript):
    _grow(transcript, 3 * MB)
    first = _prompt(hook_env, transcript).context
    assert first.startswith("[memex] Substantial session.")
    assert _prompt(hook_env, transcript).context == ""
    _grow(transcript, 5 * MB)
    assert _prompt(hook_env, transcript).context.startswith("[memex] Long session")
    assert _prompt(hook_env, transcript).context == ""


def test_stamped_memo_means_no_nudge(hook_env, transcript):
    memo = hook_env.write_memo("p", "2026-10-28-a.md", f"session_id: {SID}")
    _grow(transcript, 3 * MB)
    assert _prompt(hook_env, transcript).context == ""
    assert _state(hook_env)["memo_path"] == str(memo)


def test_memo_saved_then_session_doubles_asks_to_extend(hook_env, transcript):
    _grow(transcript, 1 * MB)
    _prompt(hook_env, transcript)  # history: ~1 MB
    memo = hook_env.write_memo("p", "2026-10-28-a.md", f"session_id: {SID}")
    _grow(transcript, 2_100_000)
    assert _prompt(hook_env, transcript).context == ""  # tier 1 reached, memo found: no nudge
    base = _state(hook_env)["memo_bytes"]
    assert 3 * MB <= base < 4 * MB  # the first prompt AFTER the memo: never overstates growth
    _grow(transcript, base + 100_000)
    context = _prompt(hook_env, transcript).context
    assert context.startswith("[memex] This session's memo was saved ")
    assert f"(`{memo}`); the session has grown 2× since — extend it?" in context
    assert "--replace" in context
    _grow(transcript, 1 * MB)
    assert _prompt(hook_env, transcript).context == ""  # once per memo version
    # extending the memo moves its baseline
    future = time.time() + 5
    os.utime(memo, (future, future))
    _grow(transcript, 1 * MB)
    assert _prompt(hook_env, transcript).context == ""


def test_mark_saved_without_stamp_still_silences(hook_env, transcript):
    state = hook_env.state_dir / "session-state"
    state.mkdir(parents=True)
    (state / f"{SID[:16]}.json").write_text(json.dumps({"count": 1, "chars": 1, "memo_saved": True, "tier_nudged": 0}))
    _grow(transcript, 9 * MB)
    assert _prompt(hook_env, transcript).context == ""


@pytest.mark.parametrize("how", ["env", "transcript"])
def test_seats_get_silence(hook_env, how):
    t = hook_env.home / "seat.jsonl"
    t.write_text(_row("user", 10, entrypoint="sdk-cli" if how == "transcript" else "cli"))
    _grow(t, 9 * MB)
    env = {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"} if how == "env" else None
    for _ in range(3):
        assert _prompt(hook_env, t, env=env).stdout == ""


def test_a_fork_is_not_nudged_for_its_parents_bytes(hook_env):
    t = hook_env.home / "fork.jsonl"
    t.write_text(_row("user", 10, forked_from=PARENT))
    _grow(t, 9 * MB, forked_from=PARENT)  # copied history
    assert _prompt(hook_env, t).context == ""
    assert _state(hook_env)["facts"]["fork_base"] == t.stat().st_size
    _grow(t, 3 * MB)  # the fork's own work
    assert _prompt(hook_env, t).context.startswith("[memex] Substantial session.")


def test_output_is_additional_context_json(hook_env, transcript):
    _grow(transcript, 3 * MB)
    data = json.loads(_prompt(hook_env, transcript).stdout)
    assert data["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert data["hookSpecificOutput"]["additionalContext"].startswith("[memex]")


# ── in-process: the vault is scanned only when a nudge is due ────────────

@pytest.fixture
def hook(monkeypatch):
    spec = importlib.util.spec_from_file_location("ups_hook", HOOKS_DIR / "user-prompt-submit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_vault_scan_only_when_about_to_nudge(hook, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(hook, "find_memo", lambda sid: calls.append(sid))
    t = tmp_path / "t.jsonl"
    t.write_text(_row("user", 10))
    state = hook.default_state()
    for _ in range(5):
        assert hook.decide(state, SID, str(t), "p") is None
    assert calls == []
    _grow(t, 3 * MB)
    assert hook.decide(state, SID, str(t), "p").startswith("[memex] Substantial")
    assert hook.decide(state, SID, str(t), "p") is None
    assert calls == [SID]  # one scan at the tier crossing, none after


def test_prompt_tiers_still_count(hook, monkeypatch, tmp_path):
    monkeypatch.setattr(hook, "find_memo", lambda sid: None)
    t = tmp_path / "t.jsonl"
    t.write_text(_row("user", 10))
    state = hook.default_state()
    texts = [hook.decide(state, SID, str(t), "y" * 120) for _ in range(40)]
    assert texts[:39] == [None] * 39 and texts[39].startswith("[memex] Substantial session.")


def test_bytes_at_rounds_up_to_the_next_prompt(hook):
    history = [[10.0, 100], [20.0, 200], [30.0, 300]]
    assert hook.bytes_at(history, 15.0, 999) == 200
    assert hook.bytes_at(history, 5.0, 999) == 100
    assert hook.bytes_at(history, 31.0, 999) == 999


def test_not_nudging_stays_fast_under_uv(hook_env, transcript):
    """Latency budget: < 3 s hard limit (target < 300 ms; measured in the report)."""
    _prompt(hook_env, transcript, real=True, timeout=120)  # warm uv's script environment
    start = time.perf_counter()
    _prompt(hook_env, transcript, real=True, timeout=120)
    assert time.perf_counter() - start < 3.0
