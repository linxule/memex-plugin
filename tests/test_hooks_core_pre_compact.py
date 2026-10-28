"""PreCompact asks the vault before it signals (v0.22.0 hooks-core: I1, I7)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

SID = "a1000000-0000-4000-8000-0000000000a1"
OTHER = "b2000000-0000-4000-8000-0000000000b2"
PARENT = "c3000000-0000-4000-8000-0000000000c3"


def _transcript(where: Path, sid: str = SID, *, entrypoint: str | None = "cli",
                forked_from: str | None = None) -> Path:
    where.mkdir(parents=True, exist_ok=True)
    t = where / f"{sid}.jsonl"
    rows = [{"type": "queue-operation", "sessionId": sid}]
    for role in ("user", "assistant"):
        row = {"type": role, "sessionId": sid, "message": {"role": role, "content": "synthetic"}}
        if entrypoint:
            row["entrypoint"] = entrypoint
        if forked_from:
            row["forkedFrom"] = {"sessionId": forked_from, "messageUuid": "m-1"}
        rows.append(row)
    t.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return t


@pytest.fixture
def cwd(hook_env):
    c = hook_env.home / "work" / "radar"
    c.mkdir(parents=True)
    hook_env.write_config(project_mappings={str(c.resolve()): "radar"})
    return c


def _compact(env, cwd: Path, transcript: Path, *, env_vars: dict | None = None, real: bool = False):
    payload = {"session_id": SID, "transcript_path": str(transcript), "cwd": str(cwd),
               "hook_event_name": "PreCompact", "trigger": "auto"}
    result = env.run("pre-compact", payload, env=env_vars, real=real, timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""  # PreCompact output is discarded by Claude Code
    return env.state_dir / "pending-memos" / f"{SID[:16]}.json"


def test_no_memo_writes_the_signal(hook_env, cwd):
    hook_env.write_memo("radar", "2026-10-28-other.md", f"session_id: {OTHER}")  # not ours
    t = _transcript(hook_env.home / "t")
    signal = _compact(hook_env, cwd, t)
    data = json.loads(signal.read_text())
    assert data["session_id"] == SID and data["project"] == "radar"
    assert data["transcript_path"] == str(t) and "forked_from" not in data


@pytest.mark.parametrize("stamp", [f"session_id: {SID}", f"session_ids:\n  - {OTHER}\n  - {SID}"])
def test_stamped_memo_means_no_signal(hook_env, cwd, stamp):
    memo = hook_env.write_memo("elsewhere", "2026-10-28-done.md", f"type: memo\n{stamp}")
    signal = _compact(hook_env, cwd, _transcript(hook_env.home / "t"))
    assert not signal.exists()
    hint = json.loads((hook_env.state_dir / "session-state" / f"{SID[:16]}.memo").read_text())
    assert hint == {"session_id": SID, "memo_path": str(memo)}  # recorded instead, for the other hooks


def test_record_leaves_the_nudge_state_alone(hook_env, cwd):
    """UserPromptSubmit rewrites <sid16>.json every prompt; PreCompact never races it."""
    state_file = hook_env.state_dir / "session-state" / f"{SID[:16]}.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(json.dumps({"count": 7, "tier_nudged": 1}))
    hook_env.write_memo("radar", "2026-10-28-done.md", f"session_id: {SID}")
    _compact(hook_env, cwd, _transcript(hook_env.home / "t"))
    assert json.loads(state_file.read_text()) == {"count": 7, "tier_nudged": 1}
    assert sorted(p.name for p in state_file.parent.glob("*.json")) == [state_file.name]  # mark-saved globs *.json


def test_second_compaction_clears_the_first_ones_signal_once_a_memo_exists(hook_env, cwd):
    t = _transcript(hook_env.home / "t")
    signal = _compact(hook_env, cwd, t)
    assert signal.exists()
    hook_env.write_memo("radar", "2026-10-28-done.md", f"session_id: {SID}")
    _compact(hook_env, cwd, t)
    assert not signal.exists()


def test_signal_records_the_entrypoint_it_was_judged_by(hook_env, cwd):
    signal = _compact(hook_env, cwd, _transcript(hook_env.home / "t", entrypoint="claude-desktop"))
    assert json.loads(signal.read_text())["entrypoint"] == "claude-desktop"


def test_env_entrypoint_outranks_the_transcript(hook_env, cwd):
    """A headless session resumed interactively: its env says cli, its first record sdk-cli."""
    t = _transcript(hook_env.home / "t", entrypoint="sdk-cli")
    signal = _compact(hook_env, cwd, t, env_vars={"CLAUDE_CODE_ENTRYPOINT": "cli"})
    assert json.loads(signal.read_text())["entrypoint"] == "cli"


@pytest.mark.parametrize("how", ["env", "transcript"])
def test_a_compacting_seat_leaves_no_signal(hook_env, cwd, how):
    t = _transcript(hook_env.home / "t", entrypoint="sdk-cli" if how == "transcript" else "cli")
    env_vars = {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"} if how == "env" else None
    assert not _compact(hook_env, cwd, t, env_vars=env_vars).exists()
    # the same session without the seat marker does get one
    assert _compact(hook_env, cwd, _transcript(hook_env.home / "u")).exists()


def test_a_compacting_seat_leaves_no_signal_under_uv(hook_env, cwd):
    """I7 acceptance, run the way Claude Code runs the hook (inline deps only)."""
    t = _transcript(hook_env.home / "t", entrypoint="sdk-cli")
    assert not _compact(hook_env, cwd, t, real=True).exists()


def test_fork_signal_names_the_parent(hook_env, cwd):
    signal = _compact(hook_env, cwd, _transcript(hook_env.home / "t", forked_from=PARENT))
    assert json.loads(signal.read_text())["forked_from"] == PARENT
