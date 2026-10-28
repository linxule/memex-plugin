"""The next vault session tells the user where SessionEnd filed by a guess (v0.22.0 I6)."""

from __future__ import annotations

import json

from memex.routing import ProjectRoute, append_filing_receipt, filing_receipt

SID = "00000000-0000-4000-8000-00000000r001"


def _receipt(env, project, rule, *, chan=None, ts, n=1):
    route = ProjectRoute(project, rule, chan)
    rec = filing_receipt(route, session_id=f"00000000-0000-4000-8000-{n:012d}", archived=f"projects/{project}/transcripts/x{n}.md",
                         cwd="/Users/x/Documents/catchall", ts=ts)
    append_filing_receipt(env.state_dir, rec)


def _start(env, cwd):
    payload = {"session_id": SID, "transcript_path": "", "cwd": str(cwd),
               "hook_event_name": "SessionStart", "source": "startup"}
    r = env.run("session-start", payload)
    assert r.returncode == 0, r.stderr
    return r


def test_vault_session_names_guessed_filings_to_user_and_model(hook_env):
    _receipt(hook_env, "catchall", "leaf", chan="chan_UNMAPPED01", ts="2026-10-28T11:48:30", n=1)
    _receipt(hook_env, "catchall", "leaf", chan="chan_UNMAPPED01", ts="2026-10-28T11:49:30", n=2)
    _receipt(hook_env, "some-film", "leaf", ts="2026-10-28T12:00:30", n=3)
    _receipt(hook_env, "teaching", "channel", ts="2026-10-28T12:01:30", n=4)  # a sure filing: not shown
    r = _start(hook_env, hook_env.vault)
    msg = r.system_message
    assert "projects/catchall/ (unmapped relay channel chan_UNMAPPED01) ×2" in msg
    assert "projects/some-film/ (folder name)" in msg
    assert "teaching" not in msg
    assert msg in r.context  # the curator sees the same line


def test_each_receipt_is_shown_once(hook_env):
    _receipt(hook_env, "catchall", "leaf", ts="2026-10-28T11:48:30")
    assert _start(hook_env, hook_env.vault).system_message
    again = _start(hook_env, hook_env.vault)
    assert again.system_message == "" and "filed by a guess" not in again.context
    cursor = json.loads((hook_env.state_dir / "receipts-shown.json").read_text())
    assert cursor["until"] == "2026-10-28T11:48:30"


def test_outside_the_vault_nothing_is_shown_or_consumed(hook_env, tmp_path):
    _receipt(hook_env, "catchall", "fallback", ts="2026-10-28T11:48:30")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert _start(hook_env, elsewhere).system_message == ""
    assert not (hook_env.state_dir / "receipts-shown.json").exists()
    assert "no rule matched" in _start(hook_env, hook_env.vault).system_message


def test_only_sure_filings_advance_the_cursor_silently(hook_env):
    _receipt(hook_env, "teaching", "channel", ts="2026-10-28T12:01:30")
    _receipt(hook_env, "memex", "git-remote", ts="2026-10-28T12:02:30", n=2)
    r = _start(hook_env, hook_env.vault)
    assert r.system_message == ""
    assert json.loads((hook_env.state_dir / "receipts-shown.json").read_text())["until"] == "2026-10-28T12:02:30"
