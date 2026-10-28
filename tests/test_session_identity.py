"""memex session whoami: each agent names its OWN session (v0.22.0)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from memex.session_identity import CLAUDE, CODEX, KIMI, classify, kimi_session_for, nearest_harness, whoami

CLAUDE_SID = "7bd0af95-47cc-4338-bee5-8ec0ebb633c2"
CODEX_TID = "01a1826b-5011-7f52-9a54-3cd78272400d"


def _table(rows: dict[int, tuple[int, str]]):
    return lambda pid: rows.get(pid)


def test_classify_known_launchers():
    assert classify("/Users/x/.local/share/claude/versions/2.1.312 --resume") == CLAUDE
    assert classify("claude -p hi") == CLAUDE
    assert classify("/Users/x/.bun/install/global/node_modules/@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex exec") == CODEX
    assert classify("/Applications/Codex.app/Contents/Resources/codex app-server") == CODEX
    assert classify("bun /Users/x/.bun/install/global/node_modules/@moonshot-ai/kimi-code/dist/main.mjs -p x") == KIMI
    assert classify("kimi-code") == KIMI  # its live process title (ps args), 2.3.3
    assert classify("/bin/zsh -lc memex session whoami") is None
    assert classify("uv run memex") is None


def test_nearest_harness_wins_over_an_outer_one():
    # zsh(10) <- kimi(9) <- zsh(8) <- claude(7): a Kimi session started from Claude Code
    rows = {10: (9, "/bin/zsh -lc memex session whoami"),
            9: (8, "bun /x/@moonshot-ai/kimi-code/dist/main.mjs"),
            8: (7, "/bin/zsh"),
            7: (1, "/Users/x/.local/share/claude/versions/2.1.312")}
    assert nearest_harness(10, _table(rows)) == KIMI


def test_inherited_variables_never_name_the_inner_session(tmp_path):
    env = {"CLAUDE_CODE_SESSION_ID": CLAUDE_SID}  # inherited from the outer Claude Code
    assert whoami(env=env, harness=KIMI, cwd=str(tmp_path), kimi_home=tmp_path / "nokimi") is None
    codex = whoami(env={**env, "CODEX_THREAD_ID": CODEX_TID}, harness=CODEX)
    assert codex.harness == CODEX and codex.session_id == CODEX_TID


def _kimi_home(tmp_path: Path, sessions: list[tuple[str, str, float]]) -> Path:
    home = tmp_path / ".kimi-code"
    (home / "sessions").mkdir(parents=True)
    with (home / "session_index.jsonl").open("w") as fh:
        for sid, wd, mtime in sessions:
            fh.write(json.dumps({"sessionId": sid, "workDir": wd}) + "\n")
            wire = home / "sessions" / "wd_x_00" / sid / "agents" / "main" / "wire.jsonl"
            wire.parent.mkdir(parents=True)
            wire.write_text('{"type":"metadata","protocol_version":"1.5"}\n')
            os.utime(wire, (mtime, mtime))
    return home


def test_kimi_picks_the_newest_session_for_this_cwd(tmp_path):
    here = tmp_path / "repo"
    (here / "src").mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    home = _kimi_home(tmp_path, [
        ("session_00000000-0000-4000-8000-0000000000a1", str(here), 1000.0),
        ("session_00000000-0000-4000-8000-0000000000a2", str(here), 2000.0),
        ("session_00000000-0000-4000-8000-0000000000b1", str(other), 3000.0),  # newer, other cwd
    ])
    assert kimi_session_for(str(here / "src"), home) == "00000000-0000-4000-8000-0000000000a2"
    who = whoami(env={"CLAUDE_CODE_SESSION_ID": CLAUDE_SID}, harness=KIMI, cwd=str(here), kimi_home=home)
    assert who.harness == KIMI and who.session_id == "00000000-0000-4000-8000-0000000000a2"


def test_no_harness_falls_back_only_to_an_unambiguous_variable(monkeypatch):
    import memex.session_identity as si

    monkeypatch.setattr(si, "nearest_harness_process", lambda *a, **k: None)
    one = whoami(env={"CLAUDE_CODE_SESSION_ID": CLAUDE_SID})
    assert one.harness == CLAUDE and one.session_id == CLAUDE_SID and "no harness process" in one.source
    assert whoami(env={"CLAUDE_CODE_SESSION_ID": CLAUDE_SID, "CODEX_THREAD_ID": CODEX_TID}) is None
    assert whoami(env={}) is None


def _locks(tmp_path, entries):
    import json as _json

    home = tmp_path / ".kimi-code"
    locks = home / "locks" / "sessions"
    locks.mkdir(parents=True)
    for sid, data in entries.items():
        (locks / f"session_{sid}.lock").write_text(data if isinstance(data, str) else _json.dumps(data))
    return home


def test_kimi_lock_names_the_exact_session_even_with_two_in_one_cwd(tmp_path):
    from memex.session_identity import kimi_session_from_lock

    a, b = "00000000-0000-4000-8000-0000000000c1", "00000000-0000-4000-8000-0000000000c2"
    home = _locks(tmp_path, {a: {"pid": 4101, "hostname": "h", "startedAt": 2_000_000},
                             b: {"pid": 4202, "hostname": "h", "startedAt": 2_000_000},
                             "broken": "{not json"})
    assert kimi_session_from_lock(4101, home, hostname="h", process_start=1999.0) == (a, True)
    assert kimi_session_from_lock(4202, home, hostname="h", process_start=1999.0) == (b, True)
    assert kimi_session_from_lock(9999, home, hostname="h", process_start=1999.0) == (None, True)
    assert kimi_session_from_lock(4101, tmp_path / "nokimi", hostname="h") == (None, False)


def test_a_dead_sessions_lock_on_a_reused_pid_never_names_the_caller(tmp_path):
    """gauntlet round 2: a crashed session's lock carries a pid the OS reused."""
    from memex.session_identity import kimi_session_from_lock

    stale, live, other_host = ("00000000-0000-4000-8000-0000000000d1", "00000000-0000-4000-8000-0000000000d2",
                               "00000000-0000-4000-8000-0000000000d3")
    home = _locks(tmp_path, {stale: {"pid": 77, "hostname": "h", "startedAt": 1_000_000},   # 1000 s
                             live: {"pid": 77, "hostname": "h", "startedAt": 5_000_000},    # 5000 s
                             other_host: {"pid": 77, "hostname": "elsewhere", "startedAt": 6_000_000}})
    assert kimi_session_from_lock(77, home, hostname="h", process_start=4990.0) == (live, True)
    only_stale = _locks(tmp_path / "x", {stale: {"pid": 77, "hostname": "h", "startedAt": 1_000_000}})
    assert kimi_session_from_lock(77, only_stale, hostname="h", process_start=4990.0) == (None, True)


def test_when_kimi_keeps_locks_a_miss_is_unknown_not_a_cwd_guess(tmp_path, monkeypatch):
    import memex.session_identity as si

    here = tmp_path / "repo"
    here.mkdir()
    home = _kimi_home(tmp_path, [("session_00000000-0000-4000-8000-0000000000e1", str(here), 1000.0)])
    (home / "locks" / "sessions").mkdir(parents=True)  # locks dir exists, nothing names our pid
    monkeypatch.setattr(si, "nearest_harness_process", lambda *a, **k: (KIMI, 4242))
    assert whoami(env={}, cwd=str(here), kimi_home=home) is None
    # an older Kimi with no locks at all still gets the cwd fallback
    import shutil
    shutil.rmtree(home / "locks")
    assert whoami(env={}, cwd=str(here), kimi_home=home).session_id == "00000000-0000-4000-8000-0000000000e1"


def test_a_changed_hostname_suffix_still_matches_the_live_lock(tmp_path):
    from memex.session_identity import kimi_session_from_lock

    sid = "00000000-0000-4000-8000-0000000000f1"
    home = _locks(tmp_path, {sid: {"pid": 88, "hostname": "MacBook-Pro-1.home", "startedAt": 5_000_000}})
    assert kimi_session_from_lock(88, home, hostname="macbook-pro-1.local", process_start=4990.0) == (sid, True)
    assert kimi_session_from_lock(88, home, hostname="other-mac.home", process_start=4990.0) == (None, True)
