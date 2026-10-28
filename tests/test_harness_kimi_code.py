"""Kimi Code harness adapter (v0.22.0, plan D3). Fixtures are synthetic."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from memex.harness import HARNESSES, SessionRef, kimi_code, render_transcript_md
from memex.harness.kimi_code import KNOWN_PROTOCOLS, KimiCodeAdapter

FIXTURE = Path(__file__).parent / "fixtures" / "kimi_code"
SESSIONS = FIXTURE / "sessions"
ALPHA = SESSIONS / "wd_alpha_0a1b2c3d4e5f"
BETA = SESSIONS / "wd_beta_6f5e4d3c2b1a"


def _uuid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-{n:012x}"


def _ref(session_dir: Path) -> SessionRef:
    name = session_dir.name
    return SessionRef("kimi-code", name.split("_", 1)[1], (session_dir / "agents" / "main" / "wire.jsonl",))


def _by_id(adapter: KimiCodeAdapter) -> dict[str, SessionRef]:
    return {ref.session_id: ref for ref in adapter.discover(root=SESSIONS)}


def _frontmatter(md: str) -> dict:
    assert md.startswith("---\n")
    return yaml.safe_load(md.split("---\n", 2)[1])


def _all_text(session) -> str:
    return "\n".join(t.text for t in session.turns)


# --- discover ----------------------------------------------------------------


def test_discover_yields_top_level_sessions_with_full_ids_and_counts_skips():
    adapter = KimiCodeAdapter()
    refs = list(adapter.discover(root=SESSIONS))
    assert [r.session_id for r in refs] == [_uuid(1), _uuid(2), _uuid(7), _uuid(5), _uuid(6)]
    assert all(r.harness == "kimi-code" for r in refs)
    # one file per session: the main wire, never a subagent's
    assert all(len(r.files) == 1 and r.files[0].parts[-2:] == ("main", "wire.jsonl") for r in refs)
    assert adapter.skipped == {"protocol:9.9": 1, "no_wire": 1, "deleted": 1, "no_metadata": 1}
    assert adapter.protocols == {"1.3": 1, "1.5": 2, "9.9": 1, "1.0": 2, "1.4": 1}


def test_discover_can_leave_out_imported_kimi_cli_sessions():
    adapter = KimiCodeAdapter(include_imported=False)
    ids = set(_by_id(adapter))
    assert _uuid(7) not in ids and {_uuid(1), _uuid(2), _uuid(5), _uuid(6)} <= ids
    assert adapter.skipped["imported_kimi_cli"] == 1


def test_discover_missing_root_is_empty(tmp_path):
    adapter = KimiCodeAdapter()
    assert list(adapter.discover(root=tmp_path / "absent")) == []
    assert not adapter.skipped


def test_discover_reads_only_the_first_line(tmp_path, monkeypatch):
    """discover parses one record per wire file; load streams the rest and counts errors."""
    root = tmp_path / "kimi" / "sessions"
    shutil.copytree(ALPHA / f"session_{_uuid(1)}", root / "wd_x_000000000000" / f"session_{_uuid(1)}")
    wire = root / "wd_x_000000000000" / f"session_{_uuid(1)}" / "agents" / "main" / "wire.jsonl"
    with wire.open("a") as fh:
        fh.write("not json\n[1, 2]\n")
    parsed: list[str] = []

    def counting_loads(text):
        parsed.append(text)
        return json.loads(text)

    adapter = KimiCodeAdapter()
    with monkeypatch.context() as m:
        m.setattr(kimi_code, "json", SimpleNamespace(loads=counting_loads))
        (ref,) = adapter.discover(root=root)
    assert len(parsed) == 1 and '"metadata"' in parsed[0]
    assert adapter.parse_errors == 0
    session = adapter.load(ref)
    assert session is not None and adapter.parse_errors == 2
    assert session.metadata["parse_errors"] == 2
    # no session_index.jsonl / workspaces.json beside this root: cwd is unknown, not invented
    assert session.cwd == ""


# --- load: native protocol ---------------------------------------------------


def test_load_protocol_13_resumed_session():
    adapter = KimiCodeAdapter()
    session = adapter.load(_by_id(adapter)[_uuid(1)])
    assert session is not None
    assert [t.role for t in session.turns] == ["user", "assistant", "user", "assistant"]
    assert session.turns[0].text == "Please summarize the fixture plan."
    assert session.turns[1].text == "Here is the summary: part one."
    # the resumed half lives in the same file; two parts of one turn merge
    assert session.turns[2].text == "Second question after resume."
    assert session.turns[3].text == "Answer to the second question.\n\nA follow-up paragraph in the same turn."
    assert session.turns[0].timestamp == "2026-01-01T00:01:00Z"
    text = _all_text(session)
    for leaked in ("INJECTED", "THINKING", "TOOL-OUTPUT", "SUBAGENT", "SYSTEM-PROMPT"):
        assert leaked not in text
    assert adapter.dropped["origin:injection"] == 2
    assert session.models == ["test-provider/model-a", "test-provider/model-b"]
    assert session.started_at == "2026-01-01T00:00:00Z" and session.ended_at == "2026-01-01T00:25:00Z"
    assert session.cwd == "/home/tester/projects/alpha"  # from session_index.jsonl
    assert session.subagent_count == 1 and session.parent_session_id is None
    assert session.metadata == {
        "kimi_session_id": f"session_{_uuid(1)}",
        "format": "native",
        "protocol_version": "1.3",
    }


def test_load_protocol_15_ignores_duplicate_copies_and_injections():
    adapter = KimiCodeAdapter()
    session = adapter.load(_by_id(adapter)[_uuid(2)])
    assert session is not None
    assert [(t.role, t.text) for t in session.turns] == [
        ("user", "Start the goal."),
        # goal continuation is injected: its answer joins the running assistant turn
        ("assistant", "Working on the goal.\n\nContinuing the goal."),
        ("user", "Also check the edge case."),  # a mid-turn steer the person typed
        ("assistant", "Edge case checked."),
        ("user", "[image]\n\nWhat is in this picture?"),
        ("assistant", "A synthetic picture."),
    ]
    md = render_transcript_md(session, project="alpha", has_memo=False)
    assert md.count("Start the goal.") == 1 and md.count("Working on the goal.") == 1
    for leaked in ("GOAL-CONTINUATION", "SKILL-BODY", "TASK-NOTIFICATION", "THINKING", "SYSTEM-PROMPT", "SUBAGENT"):
        assert leaked not in md
    assert adapter.dropped["origin:system_trigger"] == 1
    assert adapter.dropped["origin:skill_activation"] == 1
    assert adapter.dropped["origin:task"] == 1
    assert session.cwd == "/home/tester/projects/alpha/nested"  # state.json cwd beats the index
    assert session.models == ["test-provider/model-c"]
    assert session.subagent_count == 2  # nested subagents fold into the top-level session
    assert session.metadata["protocol_version"] == "1.5"
    assert session.metadata["app_version"] == "9.9.9-test"
    assert session.metadata["parse_errors"] == 1 and adapter.parse_errors == 1


def test_load_protocol_10_takes_cwd_from_workspaces_json():
    adapter = KimiCodeAdapter()
    session = adapter.load(_by_id(adapter)[_uuid(6)])
    assert session is not None
    assert [t.text for t in session.turns] == ["An early-protocol question.", "An early-protocol answer."]
    assert session.cwd == "/home/tester/projects/beta"
    assert session.metadata["protocol_version"] == "1.0"


def test_automation_only_session_is_not_a_transcript():
    adapter = KimiCodeAdapter()
    assert adapter.load(_by_id(adapter)[_uuid(5)]) is None
    assert adapter.skipped["no_user_turns"] == 1
    assert adapter.dropped["origin:cron_job"] == 1
    assert adapter.dropped["assistant_before_user"] == 1


def test_unknown_protocol_is_skipped_at_load_too():
    adapter = KimiCodeAdapter()
    assert adapter.load(_ref(ALPHA / f"session_{_uuid(3)}")) is None
    assert adapter.skipped == {"protocol:9.9": 1}
    assert "9.9" not in KNOWN_PROTOCOLS


def _write_wire(path: Path, records: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def test_native_user_text_is_verbatim_and_other_agents_are_dropped(tmp_path):
    root = tmp_path / "sessions"
    session_dir = root / "wd_x_000000000000" / "session_ABCDEF01-0000-4000-8000-0000000000AA"
    user = {"kind": "user"}
    _write_wire(session_dir / "agents" / "main" / "wire.jsonl", [
        {"type": "metadata", "protocol_version": "1.5", "created_at": 1767225600000},
        {"type": "context.append_message", "time": 1767225660000, "message": {
            "role": "user", "origin": user, "content": [{"type": "text", "text": "Keep <system-reminder>this</system-reminder> literal"}]}},
        {"type": "context.append_message", "time": 1767225661000, "message": {"role": "user", "origin": user, "content": []}},
        {"type": "context.append_loop_event", "agentId": "agent-0", "time": 1767225662000,
         "event": {"type": "content.part", "part": {"type": "text", "text": "OTHER-AGENT-TEXT"}}},
        {"type": "usage.record", "agentId": "agent-0", "model": "sub-model", "time": 1767225662000},
        {"type": "context.append_loop_event", "time": 1767225663000,
         "event": {"type": "content.part", "part": {"type": "text", "text": "Main answer."}}},
        {"type": "usage.record", "agentId": "main", "model": "main-model", "time": 1767225663000},
    ])
    adapter = KimiCodeAdapter()
    (ref,) = adapter.discover(root=root)
    assert ref.session_id == "abcdef01-0000-4000-8000-0000000000aa"  # bare, lowercased UUID
    session = adapter.load(ref)
    assert [(t.role, t.text) for t in session.turns] == [
        ("user", "Keep <system-reminder>this</system-reminder> literal"),
        ("assistant", "Main answer."),
    ]
    assert session.models == ["main-model"]
    assert adapter.dropped == {"empty_user": 1, "other_agent": 2}
    assert session.metadata["kimi_session_id"] == session_dir.name


def test_load_without_metadata_is_skipped():
    adapter = KimiCodeAdapter()
    assert adapter.load(_ref(BETA / f"session_{_uuid(9)}")) is None
    assert adapter.skipped == {"no_metadata": 1}


def test_load_refuses_another_harness():
    ref = SessionRef("codex-desktop", _uuid(1), (ALPHA / f"session_{_uuid(1)}" / "agents" / "main" / "wire.jsonl",))
    with pytest.raises(ValueError):
        KimiCodeAdapter().load(ref)


# --- load: imported Kimi CLI sessions ----------------------------------------


def test_load_imported_kimi_cli_session():
    adapter = KimiCodeAdapter()
    session = adapter.load(_by_id(adapter)[_uuid(7)])
    assert session is not None
    assert [(t.role, t.text) for t in session.turns] == [
        ("user", "What does the legacy format look like?"),
        ("assistant", "It looks like this.\n\nAnd a second paragraph."),
        ("user", "A second legacy question."),
        ("assistant", "A second legacy answer."),
    ]
    assert all(t.timestamp is None for t in session.turns)  # the legacy shape has no per-record time
    text = _all_text(session)
    for leaked in ("REMINDER", "ONLY-INJECTED", "THINKING", "TOOL-OUTPUT"):
        assert leaked not in text
    assert adapter.dropped["injected"] == 1
    assert session.started_at == "2026-01-02T09:00:00Z" and session.ended_at == "2026-01-02T09:45:00Z"
    assert session.models == ["test-provider/legacy-model"]
    assert session.cwd == "/home/tester/projects/beta"
    assert session.metadata["format"] == "kimi-cli-import"
    assert session.metadata["imported_from"] == "kimi-cli"
    assert session.metadata["kimi_session_id"] == f"ses_{_uuid(7)}"


# --- render ------------------------------------------------------------------


def test_rendered_transcript_frontmatter():
    adapter = KimiCodeAdapter()
    assert adapter.name in HARNESSES
    session = adapter.load(_by_id(adapter)[_uuid(1)])
    md = render_transcript_md(session, project="alpha", has_memo=False)
    fm = _frontmatter(md)
    assert fm["harness"] == "kimi-code" and fm["type"] == "transcript"
    assert fm["session_id"] == _uuid(1)
    assert fm["turns"] == 2 and fm["subagents"] == 1 and fm["duration_minutes"] == 25
    assert fm["models"] == ["test-provider/model-a", "test-provider/model-b"]
    assert fm["source_cwd"] == "/home/tester/projects/alpha" and fm["project"] == "alpha"
    assert str(fm["date"]) == "2026-01-01"
    assert md.count("### User") == 2 and md.count("### Assistant") == 2


def test_every_discovered_fixture_session_renders_or_is_counted():
    adapter = KimiCodeAdapter()
    rendered = 0
    for ref in adapter.discover(root=SESSIONS):
        session = adapter.load(ref)
        if session is None:
            continue
        fm = _frontmatter(render_transcript_md(session, project="p", has_memo=False))
        assert fm["harness"] == "kimi-code" and fm["session_id"] == ref.session_id
        rendered += 1
    assert rendered == 4 and adapter.skipped["no_user_turns"] == 1


def test_fixtures_are_synthetic():
    for path in FIXTURE.rglob("*"):
        if path.is_file():
            body = path.read_text()
            assert "/Users/" not in body and "/private/" not in body, path
