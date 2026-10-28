"""SessionStart decides from the vault (v0.22.0 hooks-core: I1, I2, I5, I7).

Every case runs the hook end to end against a throwaway home + vault
(``hook_env``); fixtures are synthetic.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, time
from pathlib import Path

import pytest
from hook_helpers import HOOKS_DIR

SID = "a1000000-0000-4000-8000-0000000000a1"
OTHER = "b2000000-0000-4000-8000-0000000000b2"
PARENT = "c3000000-0000-4000-8000-0000000000c3"
RECIPE = HOOKS_DIR.parent / "skills" / "memo-writing" / "recovery.md"


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


def _signal(env, sid: str, *, cwd: str, transcript: Path | None = None, project: str = "radar") -> Path:
    pending = env.state_dir / "pending-memos"
    pending.mkdir(parents=True, exist_ok=True)
    f = pending / f"{sid[:16]}.json"
    f.write_text(json.dumps({
        "session_id": sid, "project": project, "cwd": cwd,
        "transcript_path": str(transcript or env.home / "missing.jsonl"),
        "timestamp": "2026-10-28T10:00:00",
    }))
    return f


def _at_1140(path: Path) -> Path:
    ts = datetime.combine(date.today(), time(11, 40)).timestamp()
    os.utime(path, (ts, ts))
    return path


@pytest.fixture
def project_cwd(hook_env):
    """A project cwd pinned to `radar` (pins win over every cwd rule)."""
    cwd = hook_env.home / "work" / "radar"
    cwd.mkdir(parents=True)
    hook_env.write_config(project_mappings={str(cwd.resolve()): "radar"})
    return cwd


def _start(env, source: str, cwd: Path, transcript: Path | None = None, **extra):
    payload = {"session_id": SID, "hook_event_name": "SessionStart", "source": source,
               "cwd": str(cwd), "transcript_path": str(transcript or env.home / "none.jsonl"), **extra}
    result = env.run("session-start", payload)
    assert result.returncode == 0, result.stderr
    return result


# ── I2: post-compaction tells the truth ──────────────────────────────────

def test_compact_with_stamped_memo_names_it(hook_env, project_cwd):
    memo = _at_1140(hook_env.write_memo("radar", "2026-10-28-work.md", f"type: memo\nsession_id: {SID}"))
    _signal(hook_env, SID, cwd=str(project_cwd))  # PreCompact raced the memo
    result = _start(hook_env, "compact", project_cwd, _transcript(hook_env.home / "t"))
    assert result.context == (
        f"📚 Session compacted. This session's memo is `{memo}` (saved 11:40). "
        f"If substantial work followed, extend it: add a dated section, then re-send the "
        f"COMPLETE observation set (earlier + new) in one "
        f"`memex backfill obs --stdin --replace --doc-path \"projects/radar/memos/2026-10-28-work.md\"` call."
    )
    assert "Memo needed" not in result.context and "Task(" not in result.context


def test_compact_without_memo_points_at_the_recovery_recipe(hook_env, project_cwd):
    t = _transcript(hook_env.home / "t")
    hook_env.write_memo("radar", "2026-10-28-other.md", f"session_id: {OTHER}")  # someone else's
    _signal(hook_env, SID, cwd=str(project_cwd), transcript=t, project="radar")
    result = _start(hook_env, "compact", project_cwd, t)
    summaries = hook_env.state_dir.resolve() / "compact-summaries" / SID
    assert result.context == (
        f"📚 Session compacted. ⚠️ **Memo needed**: no memo is stamped with this session's id. "
        f"Spawn ONE background subagent with the Agent tool (subagent_type 'general-purpose', "
        f"model 'sonnet', run_in_background true) whose prompt says: follow `{RECIPE}` "
        f"for session `{SID}`, project `radar`; read the compaction summaries in "
        f"`{summaries}/` first, the transcript `{t}` second."
    )
    paths = len(str(RECIPE)) + len(str(summaries)) + len(str(t))
    assert len(result.context) - paths < 450  # was a 1.7k-char Task(...) literal
    assert "Task(" not in result.context


def test_compact_recorded_hint_is_reverified(hook_env, project_cwd):
    """A memo path recorded in session state counts only while it carries the stamp."""
    stale = hook_env.write_memo("radar", "2026-10-28-moved.md", f"session_id: {OTHER}")
    state = hook_env.state_dir / "session-state"
    state.mkdir(parents=True)
    (state / f"{SID[:16]}.json").write_text(json.dumps({"memo_path": str(stale)}))
    result = _start(hook_env, "compact", project_cwd, _transcript(hook_env.home / "t"))
    assert "Memo needed" in result.context
    real = hook_env.write_memo("radar", "2026-10-28-real.md", f"session_ids:\n  - {OTHER}\n  - {SID}")
    result = _start(hook_env, "compact", project_cwd, _transcript(hook_env.home / "t"))
    assert f"This session's memo is `{real}`" in result.context


def test_compact_uses_the_hint_precompact_recorded(hook_env, project_cwd):
    memo = hook_env.write_memo("elsewhere", "2026-10-28-x.md", f"session_id: {SID}")
    state = hook_env.state_dir / "session-state"
    state.mkdir(parents=True)
    (state / f"{SID[:16]}.memo").write_text(json.dumps({"session_id": OTHER, "memo_path": str(memo)}))
    assert f"memo is `{memo}`" in _start(hook_env, "compact", project_cwd, _transcript(hook_env.home / "t")).context
    (state / f"{SID[:16]}.memo").write_text(json.dumps({"session_id": SID, "memo_path": str(memo)}))
    assert f"memo is `{memo}`" in _start(hook_env, "compact", project_cwd, _transcript(hook_env.home / "t")).context


def test_compact_after_mark_saved_without_stamp(hook_env, project_cwd):
    (hook_env.home / ".memex" / "state.json").write_text(json.dumps(
        {"schema_version": 1, "processed_sessions": {SID: {"memo_generated_at": "2026-10-28T09:00:00"}}}))
    result = _start(hook_env, "compact", project_cwd, _transcript(hook_env.home / "t"))
    assert "Memo needed" not in result.context
    assert f"add `session_id: {SID}` to its frontmatter" in result.context


def test_compact_of_a_fork_says_whose_records_are_whose(hook_env, project_cwd):
    t = _transcript(hook_env.home / "t", forked_from=PARENT)
    result = _start(hook_env, "compact", project_cwd, t)
    assert result.context.endswith(f"This session was forked from {PARENT}: cover only the work after the fork.")


@pytest.mark.parametrize("how", ["env", "transcript"])
def test_sdk_cli_seat_gets_silence(hook_env, project_cwd, how):
    t = _transcript(hook_env.home / "t", entrypoint="sdk-cli" if how == "transcript" else "cli")
    _signal(hook_env, OTHER, cwd=str(project_cwd))
    env = {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"} if how == "env" else None
    for source in ("compact", "startup", "resume", "fork"):
        payload = {"session_id": SID, "source": source, "cwd": str(project_cwd), "transcript_path": str(t)}
        result = hook_env.run("session-start", payload, env=env)
        assert result.returncode == 0, result.stderr
        assert result.stdout == "", f"{source}: {result.stdout}"


# ── I1 consumers: the vault view announces only what is owed ─────────────

def test_this_sessions_case_no_memo_needed_no_vault_alert(hook_env):
    """Memo written in-session, `mark-saved` never run, PreCompact left a signal."""
    memo = hook_env.write_memo("memex", "2026-10-28-pass.md", f"type: memo\nsession_id: {SID}")
    sig = _signal(hook_env, SID, cwd=str(hook_env.vault), project="memex")
    t = _transcript(hook_env.home / "t")
    result = _start(hook_env, "startup", hook_env.vault, t)
    assert "pending retry" not in result.context
    assert not sig.exists()  # covered by a stamped memo: cleared, as reconcile-orphans --apply would
    result = _start(hook_env, "compact", hook_env.vault, t)
    assert "Memo needed" not in result.context and str(memo) in result.context


def test_vault_view_counts_only_signals_without_a_stamped_memo(hook_env):
    hook_env.write_memo("p", "2026-10-28-a.md", f"session_id: {OTHER}")
    covered = _signal(hook_env, OTHER, cwd="/elsewhere/a")
    owed = _signal(hook_env, SID, cwd="/elsewhere/b")
    seat_t = _transcript(hook_env.home / "seat", PARENT, entrypoint="sdk-cli")
    seat = _signal(hook_env, PARENT, cwd="/elsewhere/c", transcript=seat_t)
    result = _start(hook_env, "startup", hook_env.vault, _transcript(hook_env.home / "t", "ff000000-0000-4000-8000-00000000ffff"))
    assert "⚠️ 1 orphan memo(s) pending retry (no matching project cwd)" in result.context
    assert f"follows `{RECIPE}`" in result.context and "Agent tool" in result.context
    assert not covered.exists() and not seat.exists()
    assert owed.exists()  # a genuine retry is never cleared


def test_resume_in_vault_uses_the_same_rule(hook_env):
    hook_env.write_memo("p", "2026-10-28-a.md", f"session_id: {OTHER}")
    _signal(hook_env, OTHER, cwd="/elsewhere/a")
    result = _start(hook_env, "resume", hook_env.vault, _transcript(hook_env.home / "t"))
    assert result.stdout == ""
    _signal(hook_env, SID, cwd="/elsewhere/b")
    result = _start(hook_env, "resume", hook_env.vault, _transcript(hook_env.home / "t"))
    assert result.context.startswith("📝 Note: 1 orphan memo(s) pending retry")


def test_project_view_skips_covered_signals(hook_env, project_cwd):
    hook_env.write_memo("radar", "2026-10-28-a.md", f"session_id: {OTHER}")
    _signal(hook_env, OTHER, cwd=str(project_cwd))
    result = _start(hook_env, "resume", project_cwd, _transcript(hook_env.home / "t"))
    assert "pending retry" not in result.context
    _signal(hook_env, SID, cwd=str(project_cwd))
    result = _start(hook_env, "resume", project_cwd, _transcript(hook_env.home / "t"))
    assert "1 memo(s) pending retry for this project" in result.context


def test_fork_gets_no_memo_nags(hook_env):
    _signal(hook_env, OTHER, cwd=str(hook_env.vault))
    fork_t = _transcript(hook_env.home / "fork", forked_from=PARENT)
    plain_t = _transcript(hook_env.home / "plain")
    assert _start(hook_env, "fork", hook_env.vault, plain_t).stdout == ""
    # a fork that arrives as `resume` (e.g. --fork-session) is known by its copied records
    assert _start(hook_env, "resume", hook_env.vault, fork_t).stdout == ""
    assert "pending retry" in _start(hook_env, "startup", hook_env.vault, plain_t).context


def test_env_cli_outranks_a_transcript_that_began_headless(hook_env, project_cwd):
    t = _transcript(hook_env.home / "t", entrypoint="sdk-cli")
    payload = {"session_id": SID, "source": "compact", "cwd": str(project_cwd), "transcript_path": str(t)}
    assert hook_env.run("session-start", payload).stdout == ""
    result = hook_env.run("session-start", payload, env={"CLAUDE_CODE_ENTRYPOINT": "cli"})
    assert "Memo needed" in result.context


def test_signal_entrypoint_field_decides_and_mixed_transcripts_are_kept(hook_env):
    seat_t = _transcript(hook_env.home / "a", OTHER, entrypoint="cli")  # transcript says cli...
    marked = _signal(hook_env, OTHER, cwd="/elsewhere/a", transcript=seat_t)
    marked.write_text(json.dumps({**json.loads(marked.read_text()), "entrypoint": "sdk-cli"}))  # ...signal says seat
    mixed_t = _transcript(hook_env.home / "b", PARENT, entrypoint="sdk-cli")
    with mixed_t.open("a") as fh:  # resumed interactively later
        fh.write(json.dumps({"type": "user", "sessionId": PARENT, "entrypoint": "cli"}) + "\n")
    mixed = _signal(hook_env, PARENT, cwd="/elsewhere/b", transcript=mixed_t)
    result = _start(hook_env, "startup", hook_env.vault, _transcript(hook_env.home / "t"))
    assert not marked.exists()
    assert mixed.exists() and "1 orphan memo(s) pending retry" in result.context


# ── I5: the session-start pointer ────────────────────────────────────────

OVERVIEW = """---
type: project
name: radar
created: 2026-01-02
condensed: 2026-10-20
memos_digested: 3
---

# radar

Synthetic overview.

## What's Active

- [ ] not a thread (wrong section)

## Open Threads

- [ ] first
- [x] done already
- plain bullet thread
  - [ ] nested detail, not a thread

### Later

- [ ] second, in a sub-group

```bash
# a comment, not a heading
- not a bullet either
```

## Related
- [ ] also not a thread
"""


def _overview(env, project: str = "radar", text: str = OVERVIEW) -> Path:
    p = env.vault / "projects" / project / "_project.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_pointer_for_a_project_with_a_real_overview(hook_env, project_cwd):
    _overview(hook_env)
    hook_env.write_memo("radar", "2026-10-25-older.md", "type: memo\nharness: claude-code")
    hook_env.write_memo("radar", "2026-10-27-newest.md", "type: memo\nharness: codex-desktop")
    hook_env.write_memo("radar", "20261001-0900-legacy.md", "type: memo")
    result = _start(hook_env, "startup", project_cwd, _transcript(hook_env.home / "t"))
    line = ("memex: radar — overview condensed 2026-10-20, 3 open threads, last memo 2026-10-27 "
            "(codex-desktop). `memex read projects/radar/_project.md`")
    assert result.context == line
    assert len(line) <= 300


def test_pointer_follows_a_consolidation_redirect(hook_env, project_cwd):
    _overview(hook_env, "radar", "---\ntype: project\nstatus: archived\nredirect_to: research-radar\n---\n# moved\n")
    _overview(hook_env, "research-radar", OVERVIEW.replace("name: radar", "name: research-radar"))
    result = _start(hook_env, "startup", project_cwd, _transcript(hook_env.home / "t"))
    assert result.context == ("memex: research-radar — overview condensed 2026-10-20, 3 open threads. "
                              "`memex read projects/research-radar/_project.md`")


def test_no_pointer_for_the_template_stub(hook_env, project_cwd):
    _overview(hook_env, text=(
        "---\ntype: project\nname: radar\ncreated: 2026-10-28\n---\n\n# radar\n\n## Overview\n\n"
        "Project workspace: `/x`\n\n## Session History\n\n"
        "<!-- Transcripts and memos will accumulate in this project folder -->\n"))
    hook_env.write_memo("radar", "2026-10-27-a.md", "type: memo")
    assert _start(hook_env, "startup", project_cwd, _transcript(hook_env.home / "t")).stdout == ""


@pytest.mark.parametrize("case", ["vault", "agent_type", "config_off", "resume", "archived"])
def test_no_pointer_where_it_does_not_belong(hook_env, project_cwd, case):
    _overview(hook_env)
    cwd, extra, source = project_cwd, {}, "startup"
    if case == "vault":
        _overview(hook_env, "memex")
        hook_env.write_config(project_mappings={str(hook_env.vault.resolve()): "memex"})
        cwd = hook_env.vault
    elif case == "agent_type":
        extra = {"agent_type": "reviewer"}
    elif case == "config_off":
        hook_env.write_config(project_mappings={str(project_cwd.resolve()): "radar"}, session_start_pointer=False)
    elif case == "resume":
        source = "resume"
    elif case == "archived":
        _overview(hook_env, text="---\ntype: project\nstatus: archived\ncondensed: 2026-10-01\n---\n" + "x\n" * 60)
    result = _start(hook_env, source, cwd, _transcript(hook_env.home / "t"), **extra)
    assert "memex read projects/" not in result.stdout


def test_fork_gets_the_pointer(hook_env, project_cwd):
    _overview(hook_env)
    t = _transcript(hook_env.home / "t", forked_from=PARENT)
    assert _start(hook_env, "fork", project_cwd, t).context.startswith("memex: radar — overview condensed")


def test_pointer_and_pending_and_sidecar_notice_share_one_output(hook_env, project_cwd):
    _overview(hook_env)
    _signal(hook_env, OTHER, cwd=str(project_cwd))
    result = _start(hook_env, "startup", project_cwd, _transcript(hook_env.home / "t"))
    first, second = result.context.split("\n\n")
    assert first.startswith("memex: radar") and "1 memo(s) pending retry for this project" in second


def test_unstamped_but_maintained_overview_counts(hook_env, project_cwd):
    body = "---\ntype: project\nname: radar\n---\n# radar\n\n## Open Threads\n\n- [ ] one\n" + "text\n" * 50
    _overview(hook_env, text=body)
    result = _start(hook_env, "startup", project_cwd, _transcript(hook_env.home / "t"))
    assert result.context == "memex: radar — overview unstamped, 1 open thread. `memex read projects/radar/_project.md`"


# ── in-process: the helper checks are bounded ────────────────────────────

def test_helper_checks_stop_at_the_budget(hook_env, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("ss_hook_budget", HOOKS_DIR / "session-start.py")
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    for name in ("log_info", "log_warning"):
        monkeypatch.setattr(hook, name, lambda *a: None)
    seat_t = _transcript(hook_env.home / "seat", OTHER, entrypoint="sdk-cli")
    sig = {"session_id": OTHER, "project": "p", "transcript_path": str(seat_t)}
    monkeypatch.setattr(hook, "HELPER_CHECK_BUDGET_S", 0.0)
    assert hook.settle_signals([dict(sig)], hook_env.vault) == [sig]  # unchecked → still announced
    monkeypatch.setattr(hook, "HELPER_CHECK_BUDGET_S", 5.0)
    assert hook.settle_signals([dict(sig)], hook_env.vault) == []
