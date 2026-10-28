"""SessionEnd files relay workers by channel, leaves a receipt, asks for a memo
(v0.22.0 C2, I6, C4). Runs the real hook script against a throwaway home."""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest
import yaml

from memex.routing import ProjectRoute, read_filing_receipts
from test_relay_capture import CHANNEL, COORD, SID, relay_rows, write_jsonl

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "session-end.py"
UNMAPPED = "chan_UNMAPPEDCHAN01"
SID2 = "00000000-0000-4000-8000-0000000b22b2"


def _load_hook():
    spec = importlib.util.spec_from_file_location("session_end_relay_hook", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def relay_env(hook_env):
    catch_all = hook_env.home / "Documents" / "catchall"
    catch_all.mkdir(parents=True)
    # pytest's tmp dirs sit under a temp root, where detection (B1) files an
    # unknown leaf as _uncategorized; a real catch-all (~/Documents/catchall) is
    # not under one. Pin it so the test still routes by the cwd rules.
    hook_env.write_config(channel_mappings={CHANNEL: "teaching"},
                          project_mappings={str(catch_all): "catchall"})
    return hook_env, catch_all


def _end(env, cwd: Path, rows: list[dict], *, sid: str = SID, real: bool = False, extra_env=None):
    transcript = write_jsonl(env.home / f"{sid}.jsonl", rows)
    payload = {"session_id": sid, "transcript_path": str(transcript), "cwd": str(cwd),
               "hook_event_name": "SessionEnd", "reason": "other"}
    result = env.run("session-end", payload, real=real, env=extra_env, timeout=120)
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr, result.stderr
    return transcript


def _md(env, project: str) -> Path:
    found = list((env.vault / "projects" / project / "transcripts").glob("*.md"))
    assert len(found) == 1, found
    return found[0]


def _fm(path: Path) -> dict:
    return yaml.safe_load(path.read_text().split("---\n")[1])


def _signal(env, sid: str = SID) -> Path:
    return env.state_dir / "pending-memos" / f"{sid[:16]}.json"


# --- C2: channel before cwd ------------------------------------------------------

def test_mapped_relay_lands_in_its_channel_project(relay_env):
    env, catch_all = relay_env
    _end(env, catch_all, relay_rows(cwd=str(catch_all)))
    md = _md(env, "teaching")
    fm = _fm(md)
    assert fm["relay_channel"] == CHANNEL and fm["relay_coordinator"] == COORD
    assert fm["harness"] == "claude-desktop-relay" and fm["project"] == "teaching"
    assert not (env.vault / "projects" / "catchall").exists()
    (receipt,) = read_filing_receipts(env.state_dir)
    assert receipt["rule"] == "channel" and receipt["unmapped_channel"] is None
    assert receipt["project"] == "teaching" and receipt["session_id"] == SID
    assert receipt["archived"] == md.relative_to(env.vault).as_posix()
    assert receipt["cwd"] == str(catch_all)


def test_unmapped_relay_is_filed_by_cwd_and_the_receipt_names_the_channel(relay_env):
    env, catch_all = relay_env
    _end(env, catch_all, relay_rows(cwd=str(catch_all), channel=UNMAPPED))
    fm = _fm(_md(env, "catchall"))
    assert fm["relay_channel"] == UNMAPPED
    (receipt,) = read_filing_receipts(env.state_dir)
    assert receipt["unmapped_channel"] == UNMAPPED
    assert receipt["project"] == "catchall" and receipt["rule"] != "channel"


def test_ordinary_session_gets_a_receipt_and_no_relay_keys(relay_env):
    env, catch_all = relay_env
    _end(env, catch_all, relay_rows(cwd=str(catch_all), first_prompt="Plain prompt.", entrypoint="cli", tool_uses=40))
    fm = _fm(_md(env, "catchall"))
    assert "harness" not in fm and "relay_channel" not in fm
    (receipt,) = read_filing_receipts(env.state_dir)
    assert receipt["unmapped_channel"] is None
    assert not _signal(env).exists()  # C4 is for relays only


# --- C4: Layer-2 signal for substantial relays -------------------------------------

def test_substantial_relay_without_memo_gets_a_pending_signal(relay_env):
    env, catch_all = relay_env
    transcript = _end(env, catch_all, relay_rows(cwd=str(catch_all), channel=UNMAPPED, tool_uses=30))
    signal = json.loads(_signal(env).read_text())
    assert signal["session_id"] == SID and signal["project"] == "catchall"
    assert signal["transcript_path"] == str(transcript) and signal["cwd"] == str(catch_all)
    assert signal["source"] == "session-end" and signal["timestamp"]
    # the PreCompact shape: every key it writes is present
    assert {"session_id", "transcript_path", "project", "cwd", "timestamp"} <= set(signal)


def test_small_relay_gets_no_signal(relay_env):
    env, catch_all = relay_env
    _end(env, catch_all, relay_rows(cwd=str(catch_all), tool_uses=29))
    assert _md(env, "teaching")
    assert not _signal(env).exists()


def test_memo_in_the_channel_project_suppresses_the_signal(relay_env):
    env, catch_all = relay_env
    env.write_memo("teaching", "2026-10-28-relay.md", f"type: memo\nsession_id: {SID}")
    _end(env, catch_all, relay_rows(cwd=str(catch_all), tool_uses=35))
    assert not _signal(env).exists()


def test_memo_filed_by_cwd_suppresses_the_signal_for_a_mapped_relay(relay_env):
    """An in-session /memex:save files by cwd (the catch-all), not by channel."""
    env, catch_all = relay_env
    env.write_memo("catchall", "2026-10-28-relay.md", f"type: memo\nsession_ids: [{SID2}, {SID}]")
    _end(env, catch_all, relay_rows(cwd=str(catch_all), tool_uses=35))
    assert _md(env, "teaching")
    assert not _signal(env).exists()


def test_body_mention_or_other_sessions_memo_is_not_evidence(relay_env):
    env, catch_all = relay_env
    env.write_memo("teaching", "a.md", f"type: memo\nsession_id: {SID2}", body=f"mentions {SID}\n")
    _end(env, catch_all, relay_rows(cwd=str(catch_all), tool_uses=35))
    assert _signal(env).exists()


def test_existing_precompact_signal_is_left_alone(relay_env):
    env, catch_all = relay_env
    _signal(env).parent.mkdir(parents=True, exist_ok=True)
    _signal(env).write_text(json.dumps({"session_id": SID, "transcript_path": "/x", "project": "p",
                                        "cwd": "/x", "timestamp": "2026-10-28T09:00:00"}))
    _end(env, catch_all, relay_rows(cwd=str(catch_all), tool_uses=35))
    assert json.loads(_signal(env).read_text())["project"] == "p"


def test_sdk_cli_environment_never_signals(relay_env):
    env, catch_all = relay_env
    _end(env, catch_all, relay_rows(cwd=str(catch_all), tool_uses=35), extra_env={"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"})
    assert _md(env, "teaching")
    assert not _signal(env).exists()


def test_relay_path_runs_with_only_the_inline_dependencies(relay_env):
    """uv run --script: the strict YAML parser runs (a memo mentions the id), so
    a missing pyyaml in the PEP 723 block would fail here."""
    env, catch_all = relay_env
    env.write_memo("catchall", "b.md", f"type: memo\nsession_id: {SID}")
    _end(env, catch_all, relay_rows(cwd=str(catch_all), tool_uses=35), real=True)
    assert _fm(_md(env, "teaching"))["relay_channel"] == CHANNEL
    assert not _signal(env).exists()
    assert len(read_filing_receipts(env.state_dir)) == 1


# --- unit: the bounded memo scan --------------------------------------------------

def test_memo_scan_stops_at_the_deadline_and_reports_incomplete(tmp_path):
    hook = _load_hook()
    memos = tmp_path / "projects" / "p" / "memos"
    memos.mkdir(parents=True)
    (memos / "a.md").write_text(f"---\nsession_id: {SID}\n---\n")
    assert hook.stamped_memo_in_projects(tmp_path, SID, ["p"], time.monotonic() + 5) == (memos / "a.md", True)
    assert hook.stamped_memo_in_projects(tmp_path, SID, ["p"], time.monotonic() - 1) == (None, False)
    assert hook.stamped_memo_in_projects(tmp_path, SID2, ["p", "missing"], time.monotonic() + 5) == (None, True)
    assert hook.stamped_memo_in_projects(tmp_path, "not-a-uuid", ["p"], time.monotonic() + 5) == (None, True)


@pytest.fixture
def quiet_hook(tmp_path, monkeypatch):
    hook = _load_hook()
    pending = tmp_path / "pending"
    pending.mkdir()
    monkeypatch.setattr(hook, "get_pending_dir", lambda: pending)
    warnings = []
    monkeypatch.setattr(hook, "log_info", lambda *a: None)
    monkeypatch.setattr(hook, "log_warning", warnings.append)
    return hook, pending, warnings


def test_cut_short_scan_still_signals(tmp_path, quiet_hook):
    """Past the deadline the hook signals anyway: SessionStart and
    reconcile-orphans re-check the whole vault, while a lost signal is lost."""
    hook, pending, _ = quiet_hook
    memos = tmp_path / "projects" / "p" / "memos"
    memos.mkdir(parents=True)
    (memos / "a.md").write_text(f"---\nsession_id: {SID}\n---\n")
    route = ProjectRoute("p", "detect")
    in_time = hook.find_relay_memo(SID, "", route, tmp_path, time.monotonic() + 5)
    too_late = hook.find_relay_memo(SID, "", route, tmp_path, time.monotonic() - 1)
    assert in_time == (memos / "a.md", True) and too_late == (None, False)
    meta = {"harness": "claude-desktop-relay", "entrypoint": "claude-desktop", "tool_uses": 31}
    kwargs = dict(session_id=SID, transcript_path=tmp_path / "t.jsonl", cwd="", project="p", session_meta=meta)
    assert hook.signal_relay_memo(**kwargs, memo_scan=in_time) is None  # memo found in time
    assert hook.signal_relay_memo(**kwargs, memo_scan=None) is None  # no scan ran: state says saved
    written = hook.signal_relay_memo(**kwargs, memo_scan=too_late)
    assert written == pending / f"{SID[:16]}.json"
    assert json.loads(written.read_text())["source"] == "session-end"


def test_scan_error_reads_as_incomplete(tmp_path, quiet_hook, monkeypatch):
    hook, _, warnings = quiet_hook

    def boom(*a):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(hook, "stamped_memo_in_projects", boom)
    assert hook.find_relay_memo(SID, "", ProjectRoute("p", "detect"), tmp_path, time.monotonic() + 5) == (None, False)
    assert any("disk gone" in w for w in warnings)


def test_another_sessions_signal_with_the_same_prefix_is_not_clobbered(tmp_path, quiet_hook):
    hook, pending, warnings = quiet_hook
    other = SID[:16] + "ffff-ffffffffffff"
    (pending / f"{SID[:16]}.json").write_text(json.dumps({"session_id": other, "project": "q"}))
    meta = {"harness": "claude-desktop-relay", "entrypoint": "claude-desktop", "tool_uses": 31}
    assert hook.signal_relay_memo(session_id=SID, transcript_path=tmp_path / "t", cwd="", project="p",
                                  session_meta=meta, memo_scan=(None, True)) is None
    assert json.loads((pending / f"{SID[:16]}.json").read_text())["session_id"] == other
    assert any("another session" in w for w in warnings)


@pytest.mark.parametrize(("meta", "expected"), [
    ({"harness": "claude-desktop-relay", "entrypoint": "claude-desktop", "tool_uses": 30}, True),
    ({"harness": "claude-desktop-relay", "entrypoint": "claude-desktop", "tool_uses": 29}, False),
    ({"harness": "claude-desktop-relay", "entrypoint": ["claude-desktop", "sdk-cli"], "tool_uses": 99}, False),
    ({"entrypoint": "claude-desktop", "tool_uses": 99}, False),
    ({"harness": "claude-desktop-relay", "tool_uses": "lots"}, False),
])
def test_substantial_means_a_relay_with_enough_tool_calls(meta, expected, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    assert _load_hook().relay_is_substantial(meta) is expected
