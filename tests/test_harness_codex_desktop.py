"""Codex Desktop harness adapter (v0.22.0, plan D2).

Fixtures under tests/fixtures/codex_desktop/ are synthetic: invented ids, paths and
text, in the record shapes real rollouts use. Strings ending in -SENTINEL mark
content that must never reach a transcript.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest
import yaml

from memex.harness import SessionRef, render_transcript_md
from memex.harness import codex_desktop as cd
from memex.harness.codex_desktop import CodexDesktopAdapter, CodexSessionRef, clean_user_text

ROOT = Path(__file__).parent / "fixtures" / "codex_desktop" / "sessions"
ALPHA = "019a2b3c-0000-7000-8000-00000000000a"
BETA = "019a2b3c-1111-7000-8000-00000000000b"
AUTOMATION = "019a5555-0000-7000-8000-00000000000c"
AUTOMATION_FOLLOWUP = "019a6666-0000-7000-8000-00000000000d"
INJECTED_ONLY = "019a8888-0000-7000-8000-00000000000f"

ALPHA_TURNS = [
    ("user", "Plan the alpha refactor."),
    ("assistant", "Looking at the module layout.\n\nHere is the plan."),
    ("user", "Check the rendered page.\n\n[image]"),
    ("assistant", "1. Fix the header."),
    ("user", "Q: Which branch?\nA: main"),
    ("assistant", "Using main."),
    ("user", "Also update the changelog."),
    ("assistant", "Changelog updated."),
    ("user", "Now ship it."),
    ("assistant", "Shipped."),
]


def _refs(root: Path = ROOT, adapter: CodexDesktopAdapter | None = None) -> dict[str, CodexSessionRef]:
    return {r.session_id: r for r in (adapter or CodexDesktopAdapter(root)).discover()}


def _all_text(session) -> str:
    return "\n".join(t.text for t in session.turns)


def _pairs(session) -> list[tuple[str, str]]:
    return [(t.role, t.text) for t in session.turns]


def _line(ts: str, rtype: str, payload: dict) -> str:
    return json.dumps({"timestamp": ts, "ordinal": 0, "type": rtype, "payload": payload}, separators=(",", ":"))


def _msg(role: str, *texts: str, mid: str | None = None) -> dict:
    kind = "output_text" if role == "assistant" else "input_text"
    payload = {"type": "message", "role": role, "content": [{"type": kind, "text": t} for t in texts]}
    if mid:
        payload["id"] = mid
    return payload


def _task(trigger: str | None) -> dict:
    return {"type": "task_started", "turn_attribution": {"turn_trigger": trigger} if trigger else None}


def _write_rollout(root: Path, tid: str, stamp: str, records: list, *, thread_source: str = "user") -> Path:
    """A synthetic rollout: ``records`` are (ts, type, payload) tuples after a session_meta."""
    day = stamp[:10]
    path = root / day.replace("-", "/") / f"rollout-{day}T{stamp[11:19].replace(':', '-')}-{tid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"id": tid, "session_id": tid, "originator": "Codex Desktop", "source": "vscode",
            "thread_source": thread_source, "cwd": "/work/t"}
    lines = [_line(stamp, "session_meta", meta)] + [_line(*r) for r in records]
    path.write_text("\n".join(lines) + "\n")
    return path


# --- discover ----------------------------------------------------------------


def test_discover_yields_top_level_desktop_threads_with_skip_reasons():
    scan = CodexDesktopAdapter(ROOT).scan()
    assert {r.session_id for r in scan.refs} == {ALPHA, BETA, AUTOMATION, AUTOMATION_FOLLOWUP, INJECTED_ONLY}
    assert all(r.harness == "codex-desktop" for r in scan.refs)
    assert scan.files_seen == 15
    # Reason per rollout FILE: S1 has two files; exec probe; codex_exec; pre-session_meta format; 0 bytes.
    assert dict(scan.skipped) == {"subagent": 5, "exec": 1, "originator": 1, "legacy-format": 1, "empty": 1}
    assert set(scan.skipped) <= set(cd.SKIP_REASONS)
    assert dict(scan.skipped_originators) == {"codex_exec": 1}


def test_pooled_head_reads_match_sequential_ones(monkeypatch):
    sequential = CodexDesktopAdapter(ROOT, workers=1).scan()
    monkeypatch.setattr(cd, "PARALLEL_HEADS_ABOVE", 0)
    pooled = CodexDesktopAdapter(ROOT, workers=4).scan()
    assert pooled.refs == sequential.refs and pooled.skipped == sequential.skipped
    assert pooled.subagent_counts == sequential.subagent_counts


def test_resume_files_merge_by_full_id_not_prefix():
    refs = _refs()
    assert ALPHA[:8] == BETA[:8]  # the collision real UUIDv7 ids produce
    alpha, beta = refs[ALPHA], refs[BETA]
    assert [p.parent.name for p in alpha.files] == ["21", "22"]  # time order across days
    assert len(beta.files) == 1 and beta.files[0] not in alpha.files
    assert (alpha.cwd, beta.cwd) == ("/work/alpha", "/work/beta")
    assert alpha.started_at == "2026-10-21T09:00:00.000Z"


def test_subagents_are_folded_into_their_top_level_thread():
    scan = CodexDesktopAdapter(ROOT).scan()
    refs = {r.session_id: r for r in scan.refs}
    # S1 (two resume files, counted once) + S2 (child of S1, found only via the parent
    # chain) -> alpha; S3 (parent gone, found only via its session_id) -> beta;
    # S4 has neither -> seen but folded nowhere.
    assert refs[ALPHA].subagent_count == 2
    assert refs[BETA].subagent_count == 1
    assert refs[AUTOMATION].subagent_count == 0
    assert (scan.subagent_threads, scan.subagents_folded) == (4, 3)


class _CountingReader:
    def __init__(self, fh):
        self._fh = fh
        self.consumed = 0

    def readline(self, limit=-1):
        data = self._fh.readline(limit)
        self.consumed += len(data)
        return data


def test_discover_reads_only_the_first_record(tmp_path, monkeypatch):
    tree = tmp_path / "sessions"
    shutil.copytree(ROOT, tree)
    big = tree / "2026" / "10" / "21" / f"rollout-2026-10-21T15-00-00-{'019a1234-0000-7000-8000-0000000000aa'}.jsonl"
    meta = {"timestamp": "2026-10-21T15:00:00Z", "ordinal": 0, "type": "session_meta",
            "payload": {"id": "019a1234-0000-7000-8000-0000000000aa", "originator": "Codex Desktop",
                        "source": "vscode", "cwd": "/work/big"}}
    first = json.dumps(meta, separators=(",", ":")) + "\n"
    big.write_text(first + ("x" * (3 << 20)) + "\n")  # 3 MB tail discover must not touch

    consumed: dict[str, int] = {}
    real_open = cd._open_binary

    from contextlib import contextmanager

    @contextmanager
    def counting_open(path):
        with real_open(path) as fh:
            reader = _CountingReader(fh)
            yield reader
            consumed[path.name] = reader.consumed

    monkeypatch.setattr(cd, "_open_binary", counting_open)
    refs = _refs(tree)
    assert "019a1234-0000-7000-8000-0000000000aa" in refs
    assert consumed[big.name] == len(first)
    for path in tree.rglob("*.jsonl"):
        with path.open("rb") as fh:
            assert consumed[path.name] <= len(fh.readline())


def test_missing_root_discovers_nothing(tmp_path):
    assert CodexDesktopAdapter(tmp_path / "nope").discover() == []


def test_default_root_honours_codex_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert cd.default_root() == tmp_path / "sessions"
    monkeypatch.delenv("CODEX_HOME")
    assert cd.default_root() == Path.home() / ".codex" / "sessions"


# --- load --------------------------------------------------------------------


def test_load_merges_resume_files_and_drops_replayed_history():
    adapter = CodexDesktopAdapter(ROOT)
    session = adapter.load(_refs(adapter=adapter)[ALPHA])
    assert [(t.role, t.text) for t in session.turns] == ALPHA_TURNS
    assert session.user_turns == 5
    assert session.models == ["gpt-test-1", "gpt-test-2"]
    assert (session.started_at, session.ended_at) == ("2026-10-21T09:00:00.000Z", "2026-10-22T08:30:00.000Z")
    assert session.cwd == "/work/alpha" and session.subagent_count == 2
    assert len(session.source_files) == 2
    # One replay caught by message id, one (no id) by timestamp + role + text; one torn line.
    assert session.metadata["duplicates_dropped"] == 2
    assert session.metadata["parse_errors"] == 1
    assert session.metadata["originator"] == "Codex Desktop"


def test_injected_blocks_tool_io_and_reasoning_never_reach_turns():
    adapter = CodexDesktopAdapter(ROOT)
    session = adapter.load(_refs(adapter=adapter)[ALPHA])
    text = _all_text(session)
    assert "SENTINEL" not in text
    for marker in ("AGENTS.md", "<environment_context", "<image", "proposed_plan", "system-reminder", "oai-mem"):
        assert marker not in text
    # AGENTS.md + environment_context, the image label, the subagent notification.
    assert session.metadata["injected_blocks_dropped"] == 4


def test_automation_turns_are_skipped():
    adapter = CodexDesktopAdapter(ROOT)
    refs = _refs(adapter=adapter)
    session, reason = adapter.load_explained(refs[AUTOMATION])
    assert session is None and reason == "automation-only"
    followup = adapter.load(refs[AUTOMATION_FOLLOWUP])
    # The automation's own turns (and the untagged preamble) go; the person's question stays.
    assert [(t.role, t.text) for t in followup.turns] == [
        ("user", "Why did step 2 fail?"),
        ("assistant", "Because the pin moved."),
    ]
    assert followup.metadata["automation_messages_dropped"] == 3


def test_heartbeat_turn_in_a_persons_thread_is_dropped():
    """A heartbeat automation on a user thread, with no turn attribution: its wake-up and
    the reply go; the person's next message (no new task_started) ends that turn."""
    adapter = CodexDesktopAdapter(ROOT)
    beta = adapter.load(_refs(adapter=adapter)[BETA])
    assert [(t.role, t.text) for t in beta.turns] == [
        ("user", "Beta question?"),
        ("assistant", "Beta answer."),
        ("user", "Thanks, carry on."),
        ("assistant", "Will do."),
    ]
    assert beta.metadata["automation_messages_dropped"] == 2
    assert "SENTINEL" not in _all_text(beta)


def test_injected_context_only_is_no_session():
    adapter = CodexDesktopAdapter(ROOT)
    assert adapter.load_explained(_refs(adapter=adapter)[INJECTED_ONLY]) == (None, "no-text")


def test_vanished_files_are_unreadable_not_empty(tmp_path):
    tree = tmp_path / "sessions"
    shutil.copytree(ROOT, tree)
    adapter = CodexDesktopAdapter(tree)
    ref = _refs(adapter=adapter)[BETA]
    for path in ref.files:
        path.unlink()
    assert adapter.load_explained(ref) == (None, "unreadable")
    assert "unreadable" in cd.LOAD_SKIP_REASONS


@pytest.mark.parametrize("adapter_root", [ROOT, None, Path("/nonexistent/elsewhere")])
def test_plain_session_ref_is_ordered_and_counted_lazily(adapter_root):
    """A ref built elsewhere (e.g. from a stored id) loads the same as a discovered one;
    its subagents are counted in the tree its files live in, not the adapter's root."""
    files = tuple(sorted(ROOT.rglob(f"rollout-*-{ALPHA}.jsonl"), reverse=True))
    session = CodexDesktopAdapter(adapter_root).load(SessionRef("codex-desktop", ALPHA, files))
    assert _pairs(session) == ALPHA_TURNS
    assert session.subagent_count == 2


@pytest.mark.parametrize("chunk", [7, 256])  # 7: no line head ever matches; 256: heads match, lines span chunks
def test_chunked_reads_give_the_same_session(monkeypatch, chunk):
    adapter = CodexDesktopAdapter(ROOT)
    refs = _refs(adapter=adapter)
    expected = {tid: _pairs(adapter.load(refs[tid])) for tid in (ALPHA, BETA)}
    monkeypatch.setattr(cd, "READ_CHUNK", chunk)
    assert {tid: _pairs(adapter.load(refs[tid])) for tid in (ALPHA, BETA)} == expected


def test_load_never_holds_or_parses_an_unneeded_long_line(tmp_path, monkeypatch):
    tid = "019a4321-0000-7000-8000-0000000000bb"
    path = tmp_path / "sessions" / "2026" / "10" / "24" / f"rollout-2026-10-24T09-00-00-{tid}.jsonl"
    path.parent.mkdir(parents=True)

    def rec(ts, rtype, payload):
        return json.dumps({"timestamp": ts, "ordinal": 0, "type": rtype, "payload": payload}, separators=(",", ":"))

    def message(role, text):
        kind = "input_text" if role == "user" else "output_text"
        return {"type": "message", "role": role, "content": [{"type": kind, "text": text}]}

    screenshot = message("user", "See the screenshot.")
    # 300 KB of base64, three times the record cap: the typed text must survive anyway.
    screenshot["content"].append({"type": "input_image", "image_url": "data:image/png;base64," + "QUJD" * 75_000})
    lines = [
        rec("2026-10-24T09:00:00Z", "session_meta", {"id": tid, "originator": "Codex Desktop", "source": "vscode", "cwd": "/w"}),
        rec("2026-10-24T09:00:01Z", "response_item", message("user", "Short prompt.")),
        rec("2026-10-24T09:00:02Z", "response_item", {"type": "custom_tool_call_output", "call_id": "c", "output": "T" * 300_000}),
        rec("2026-10-24T09:00:03Z", "response_item", message("user", "P" * 300_000)),
        rec("2026-10-24T09:00:04Z", "response_item", message("assistant", "Done.")),
        rec("2026-10-24T09:00:05Z", "response_item", screenshot),
        rec("2026-10-24T09:00:06Z", "response_item", message("assistant", "Looks fine.")),
    ]
    path.write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(cd, "READ_CHUNK", 4096)
    monkeypatch.setattr(cd, "MAX_RECORD_BYTES", 100_000)

    parsed: list[int] = []
    real_loads = json.loads

    def spy(data, *a, **kw):
        if isinstance(data, bytes | bytearray):
            parsed.append(len(data))
            assert b"custom_tool_call_output" not in data
        return real_loads(data, *a, **kw)

    monkeypatch.setattr(cd.json, "loads", spy)
    adapter = CodexDesktopAdapter(tmp_path / "sessions")
    session = adapter.load(adapter.discover()[0])
    assert [(t.role, t.text) for t in session.turns] == [
        ("user", "Short prompt."),
        ("assistant", "Done."),
        ("user", "See the screenshot.\n\n[image]"),
        ("assistant", "Looks fine."),
    ]
    assert session.metadata["oversized_records"] == 1 and session.metadata["parse_errors"] == 0
    assert session.ended_at == "2026-10-24T09:00:06Z"
    assert max(parsed) < 4096  # the base64 image never reached the JSON parser


def test_streaming_memory_stays_bounded(tmp_path, monkeypatch):
    """A 6 MB tool output (drained) and a 6 MB pasted image (dropped while streaming)
    never sit in memory whole."""
    import tracemalloc

    tid = "019a4321-0000-7000-8000-0000000000cc"
    image = _msg("user", "What is wrong here?")
    image["content"].append({"type": "input_image", "image_url": "data:image/png;base64," + "QUJD" * 1_500_000})
    _write_rollout(tmp_path, tid, "2026-10-24T10:00:00Z", [
        ("2026-10-24T10:00:01Z", "response_item", {"type": "function_call_output", "call_id": "c", "output": "T" * 6_000_000}),
        ("2026-10-24T10:00:02Z", "response_item", image),
        ("2026-10-24T10:00:03Z", "response_item", _msg("assistant", "A typo.")),
    ])
    monkeypatch.setattr(cd, "READ_CHUNK", 64 << 10)
    adapter = CodexDesktopAdapter(tmp_path)
    ref = adapter.discover()[0]
    tracemalloc.start()
    try:
        session = adapter.load(ref)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert _pairs(session) == [("user", "What is wrong here?\n\n[image]"), ("assistant", "A typo.")]
    assert peak < 1_000_000, peak


def test_base64_dropper_gives_the_same_line_for_any_chunking():
    line = json.dumps({"payload": {"content": [
        {"type": "input_text", "text": 'say "hi" \\ é data:image/png;base64,KEEP'},
        {"type": "input_image", "image_url": "data:image/png;base64," + "QUJD" * 300},
        {"type": "input_image", "image_url": "data:image/svg+xml,<svg a=\"1\"/>"},  # not base64: kept
        {"type": "input_image", "image_url": "data:image/jpeg;base64," + "/9j/" * 200 + "=="},
    ]}}, separators=(",", ":")).encode() + b"\n"
    expected = re.sub(rb'("image_url":"data:[\w.+/-]{1,100};base64,)[A-Za-z0-9+/=]*', rb"\1", line)
    assert len(expected) < 400 and json.loads(expected)["payload"]["content"][2]["image_url"].startswith("data:image/svg")
    for size in (1, 2, 3, 5, 13, 64, 159, 160, 161, 1000, len(line)):
        dropper = cd._Base64Dropper()
        chunks = [line[i : i + size] for i in range(0, len(line), size)]
        out = b"".join(dropper.feed(c, final=i == len(chunks) - 1) for i, c in enumerate(chunks))
        assert out == expected, size


# --- user text cleaning ------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("<environment_context><cwd>/w</cwd></environment_context>", ""),
        ("<permissions instructions>sandbox</permissions instructions>\n\nreal ask", "real ask"),
        ("<skill>\n<name>x</name>\n</skill><turn_aborted>t</turn_aborted>  then this", "then this"),
        ("<codex_internal_context><objective>o</objective></codex_internal_context>", ""),
        ("<external_codex_apps_open_page>p</external_codex_apps_open_page>", ""),
        ("<subagent_notification>unclosed and long", ""),
        ('<relay from="x">typed by the person</relay>', '<relay from="x">typed by the person</relay>'),
        ("plain <environment_context>mid-text</environment_context> stays", "plain <environment_context>mid-text</environment_context> stays"),
        ("<send_user_message_question_reply>not json</send_user_message_question_reply>", "not json"),
    ],
)
def test_clean_user_text(raw, expected):
    assert clean_user_text(raw) == expected


def test_long_prompts_are_capped_like_claude_code_transcripts():
    text = cd._message_text("user", [{"type": "input_text", "text": "z" * (cd.MAX_USER_CHARS + 10)}], Counter())
    assert text == "z" * cd.MAX_USER_CHARS + "\n\n[...truncated...]"
    assert cd.MAX_USER_CHARS == 5000


# --- turn semantics (synthetic rollouts built per test) -----------------------

T1 = "019a1111-0000-7000-8000-000000000001"


def test_a_task_with_no_typed_prompt_does_not_glue_its_reply_onto_the_previous_answer(tmp_path):
    _write_rollout(tmp_path, T1, "2026-10-25T09:00:00Z", [
        ("2026-10-25T09:00:01Z", "event_msg", _task("composer")),
        ("2026-10-25T09:00:02Z", "response_item", _msg("user", "First ask.")),
        ("2026-10-25T09:00:03Z", "response_item", _msg("assistant", "Working.")),
        ("2026-10-25T09:00:04Z", "response_item", _msg("assistant", "Answer one.")),
        ("2026-10-25T09:01:00Z", "event_msg", _task("composer")),
        ("2026-10-25T09:01:01Z", "response_item", _msg("user", "<user_shell_command>git status</user_shell_command>")),
        ("2026-10-25T09:01:02Z", "response_item", _msg("assistant", "Answer two.")),
    ])
    adapter = CodexDesktopAdapter(tmp_path)
    assert _pairs(adapter.load(adapter.discover()[0])) == [
        ("user", "First ask."),
        ("assistant", "Working.\n\nAnswer one."),  # one task: commentary + answer merge
        ("assistant", "Answer two."),  # a new task: kept apart
    ]


def test_resent_and_edited_prompts_do_not_add_turns(tmp_path):
    _write_rollout(tmp_path, T1, "2026-10-25T09:00:00Z", [
        ("2026-10-25T09:00:01Z", "event_msg", _task("composer")),
        ("2026-10-25T09:00:02Z", "response_item", _msg("user", "Fix the bug.", mid="u1")),
        ("2026-10-25T09:00:03Z", "event_msg", _task("capacity_retry_manual")),
        ("2026-10-25T09:00:04Z", "response_item", _msg("user", "Fix the bug.", mid="u2")),  # re-sent, no reply between
        ("2026-10-25T09:00:05Z", "event_msg", _task("edit_user_message")),
        ("2026-10-25T09:00:06Z", "response_item", _msg("user", "Fix the parser bug.", mid="u3")),  # edit of the unanswered one
        ("2026-10-25T09:00:07Z", "response_item", _msg("assistant", "Fixed.", mid="a1")),
        ("2026-10-25T09:01:00Z", "event_msg", _task("edit_user_message")),
        ("2026-10-25T09:01:01Z", "response_item", _msg("user", "Fix the lexer bug too.", mid="u4")),  # edit after a reply: history
        ("2026-10-25T09:01:02Z", "response_item", _msg("assistant", "Done.", mid="a2")),
    ])
    adapter = CodexDesktopAdapter(tmp_path)
    session = adapter.load(adapter.discover()[0])
    assert _pairs(session) == [
        ("user", "Fix the parser bug."),
        ("assistant", "Fixed."),
        ("user", "Fix the lexer bug too."),
        ("assistant", "Done."),
    ]
    assert (session.metadata["resent_dropped"], session.metadata["edits_replaced"]) == (1, 1)


def test_replayed_automation_turn_stays_dropped(tmp_path):
    """A resume file that replays an automation turn before its first task_started."""
    auto = [("2026-10-25T09:00:02Z", "response_item", _msg("user", "Nightly check.", mid="m3")),
            ("2026-10-25T09:00:03Z", "response_item", _msg("assistant", "All green."))]
    _write_rollout(tmp_path, T1, "2026-10-25T09:00:00Z", [
        ("2026-10-25T09:00:00.5Z", "event_msg", _task("composer")),
        ("2026-10-25T09:00:00.6Z", "response_item", _msg("user", "Hello.", mid="m1")),
        ("2026-10-25T09:00:00.7Z", "response_item", _msg("assistant", "Hi.", mid="m2")),
        ("2026-10-25T09:00:01Z", "event_msg", _task("automation_cron_scheduled")),
        *auto,
    ])
    _write_rollout(tmp_path, T1, "2026-10-26T09:00:00Z", [
        *auto,  # replayed: one by message id, one (no id) by timestamp + role + text
        ("2026-10-26T09:00:01Z", "event_msg", _task("composer")),
        ("2026-10-26T09:00:02Z", "response_item", _msg("user", "Thanks.")),
    ])
    adapter = CodexDesktopAdapter(tmp_path)
    session = adapter.load(adapter.discover()[0])
    assert _pairs(session) == [("user", "Hello."), ("assistant", "Hi."), ("user", "Thanks.")]
    assert session.metadata["duplicates_dropped"] == 2


def test_heartbeat_sent_as_plain_string_content_is_an_automation(tmp_path):
    _write_rollout(tmp_path, T1, "2026-10-25T09:00:00Z", [
        ("2026-10-25T09:00:01Z", "event_msg", _task("composer")),
        ("2026-10-25T09:00:02Z", "response_item", _msg("user", "Hello there.")),
        ("2026-10-25T09:00:03Z", "response_item", _msg("assistant", "Hi.")),
        ("2026-10-25T09:00:04Z", "response_item", {"type": "message", "role": "user",
                                                   "content": "<heartbeat><instructions>x</instructions></heartbeat>"}),
        ("2026-10-25T09:00:05Z", "response_item", _msg("assistant", "Automation reply.")),
    ])
    adapter = CodexDesktopAdapter(tmp_path)
    assert _pairs(adapter.load(adapter.discover()[0])) == [("user", "Hello there."), ("assistant", "Hi.")]


def test_assistant_only_thread_is_not_a_session(tmp_path):
    """agent_created_thread: the creating agent's prompt is not a person's turn."""
    _write_rollout(tmp_path, T1, "2026-10-25T09:00:00Z", [
        ("2026-10-25T09:00:01Z", "response_item", {"type": "agent_message", "content": "do the thing"}),
        ("2026-10-25T09:00:02Z", "response_item", _msg("assistant", "Did the thing.")),
    ], thread_source="agent_created_thread")
    adapter = CodexDesktopAdapter(tmp_path)
    assert adapter.load_explained(adapter.discover()[0]) == (None, "no-user-turns")


# --- render ------------------------------------------------------------------


def test_rendered_transcript_frontmatter():
    adapter = CodexDesktopAdapter(ROOT)
    md = render_transcript_md(adapter.load(_refs(adapter=adapter)[ALPHA]), project="alpha", has_memo=False)
    fm = yaml.safe_load(md.split("---\n")[1])
    assert fm["harness"] == "codex-desktop" and fm["type"] == "transcript"
    assert fm["session_id"] == ALPHA and fm["turns"] == 5 and fm["subagents"] == 2
    assert fm["models"] == ["gpt-test-1", "gpt-test-2"] and fm["source_cwd"] == "/work/alpha"
    assert fm["duration_minutes"] == 1410 and str(fm["date"]) == "2026-10-21"
    assert "## Turn 5" in md and "SENTINEL" not in md


# --- compressed rollouts -----------------------------------------------------


def test_compressed_rollout_without_a_decoder_is_skipped_with_reason(tmp_path, monkeypatch):
    tree = tmp_path / "sessions"
    shutil.copytree(ROOT, tree)
    (tree / "2026" / "10" / "23" / "rollout-2026-10-23T01-00-00-019a0000-0000-7000-8000-0000000000cc.jsonl.zst").write_bytes(b"\x28\xb5\x2f\xfd")
    monkeypatch.setattr(cd, "_stdlib_zstd", lambda: None)
    monkeypatch.setattr(cd, "_zstd_cli", lambda: None)
    scan = CodexDesktopAdapter(tree).scan()
    assert scan.skipped["zstd-unavailable"] == 1 and scan.compressed_files == 1
    assert len(scan.refs) == 5


@pytest.mark.skipif(shutil.which("zstd") is None, reason="needs the zstd CLI to build a .zst fixture")
def test_compressed_resume_file_merges_with_plain_one(tmp_path):
    tree = tmp_path / "sessions"
    shutil.copytree(ROOT, tree)
    day1 = next(tree.rglob(f"rollout-2026-10-21T09-00-00-{ALPHA}.jsonl"))
    beta = next(tree.rglob(f"rollout-*-{BETA}.jsonl"))
    for path, keep_plain in ((day1, False), (beta, True)):
        subprocess.run(["zstd", "-q", "-f", str(path), "-o", f"{path}.zst"], check=True)
        if not keep_plain:
            path.unlink()
    adapter = CodexDesktopAdapter(tree)
    scan = adapter.scan()
    refs = {r.session_id: r for r in scan.refs}
    assert [p.name.endswith(".zst") for p in refs[ALPHA].files] == [True, False]
    session = adapter.load(refs[ALPHA])
    assert [(t.role, t.text) for t in session.turns] == ALPHA_TURNS
    assert session.metadata["compressed_files"] == 1
    # beta exists both ways: the plain twin wins, the .zst is counted, not read.
    assert refs[BETA].files == (beta,) and scan.skipped["duplicate-compressed"] == 1


needs_zstd_cli = pytest.mark.skipif(shutil.which("zstd") is None, reason="needs the zstd CLI")


def _compress(path: Path) -> Path:
    """Compress like Codex does: write the .zst, delete the plain rollout."""
    out = path.with_name(path.name + ".zst")
    subprocess.run(["zstd", "-q", "-f", str(path), "-o", str(out)], check=True)
    path.unlink()
    return out


@needs_zstd_cli
def test_a_rollout_compressed_after_discovery_still_loads(tmp_path):
    tree = tmp_path / "sessions"
    shutil.copytree(ROOT, tree)
    adapter = CodexDesktopAdapter(tree)
    ref = _refs(adapter=adapter)[ALPHA]
    _compress(ref.files[0])
    session = adapter.load(ref)
    assert _pairs(session) == ALPHA_TURNS and session.metadata["compressed_files"] == 1
    stored = adapter.load(SessionRef("codex-desktop", ALPHA, ref.files))  # e.g. paths kept by an importer
    assert _pairs(stored) == ALPHA_TURNS and stored.metadata["unreadable_files"] == 0


@needs_zstd_cli
def test_a_truncated_zst_is_reported_not_silently_shortened(tmp_path, monkeypatch):
    monkeypatch.setattr(cd, "_stdlib_zstd", lambda: None)  # the zstd CLI path, on any Python
    tree = tmp_path / "sessions"
    shutil.copytree(ROOT, tree)
    packed = _compress(next(tree.rglob(f"rollout-2026-10-21T09-00-00-{ALPHA}.jsonl")))
    data = packed.read_bytes()
    packed.write_bytes(data[: len(data) * 2 // 3])
    adapter = CodexDesktopAdapter(tree)
    scan = adapter.scan()
    assert scan.skipped["corrupt"] == 1
    ref = {r.session_id: r for r in scan.refs}[ALPHA]
    assert ref.files[0] == packed  # kept in its thread, by the id in its file name
    session = adapter.load(ref)
    assert session.metadata["corrupt_files"] == 1
    # Day 2 alone: its replayed records are new here, so they stay.
    assert _pairs(session) == ALPHA_TURNS[-4:]


class _TruncatedStream(io.BytesIO):
    """What compression.zstd yields for a truncated frame: the decoded prefix, then EOFError."""

    def readline(self, limit=-1):
        line = super().readline(limit)
        if not line:
            raise EOFError("Compressed file ended before the end-of-stream marker was reached")
        return line


def test_stdlib_zstd_decode_errors_keep_the_prefix_and_are_counted(tmp_path, monkeypatch):
    tree = tmp_path / "sessions"
    shutil.copytree(ROOT, tree)
    day1 = next(tree.rglob(f"rollout-2026-10-21T09-00-00-{ALPHA}.jsonl"))
    raw = day1.read_bytes()
    prefix = raw[: raw.index(b"\n", raw.index(b'"msg-a-4"')) + 1]  # through the first answer
    packed = day1.with_name(day1.name + ".zst")
    packed.write_bytes(b"\x28\xb5\x2f\xfd")
    day1.unlink()

    class FakeZstd:
        class ZstdError(Exception):
            pass

        @staticmethod
        def open(path, mode):
            assert Path(path) == packed and mode == "rb"
            return _TruncatedStream(prefix)

    monkeypatch.setattr(cd, "_stdlib_zstd", lambda: FakeZstd)
    adapter = CodexDesktopAdapter(tree)
    ref = _refs(adapter=adapter)[ALPHA]
    assert ref.files[0] == packed  # the head decodes fine; the damage is further in
    session = adapter.load(ref)
    assert session.metadata["corrupt_files"] == 1
    assert _pairs(session) == ALPHA_TURNS[:2] + ALPHA_TURNS[-4:]


def test_an_undecodable_part_of_a_known_thread_is_reported(tmp_path, monkeypatch):
    tree = tmp_path / "sessions"
    shutil.copytree(ROOT, tree)
    extra = tree / "2026" / "10" / "20" / f"rollout-2026-10-20T08-00-00-{ALPHA}.jsonl.zst"
    extra.parent.mkdir(parents=True)
    extra.write_bytes(b"\x28\xb5\x2f\xfd")
    monkeypatch.setattr(cd, "_stdlib_zstd", lambda: None)
    monkeypatch.setattr(cd, "_zstd_cli", lambda: None)
    adapter = CodexDesktopAdapter(tree)
    ref = _refs(adapter=adapter)[ALPHA]
    assert ref.files[0] == extra
    session = adapter.load(ref)
    assert _pairs(session) == ALPHA_TURNS and session.metadata["unreadable_files"] == 1


def test_a_damaged_record_in_an_automation_thread_is_unknown_not_automation_only(tmp_path):
    """Codex round 2: a person's turn whose record is malformed JSON was dropped,
    leaving only the automation's turns, so the thread read as automation-only and
    `check --harnesses` called the report complete. The lost record may be the
    person's, so the answer is "unreadable"."""
    path = _write_rollout(tmp_path, T1, "2026-10-25T09:00:00Z", [
        ("2026-10-25T09:00:01Z", "event_msg", _task("automation_cron_scheduled")),
        ("2026-10-25T09:00:02Z", "response_item", _msg("user", "Nightly check.")),
        ("2026-10-25T09:00:03Z", "response_item", _msg("assistant", "All green.")),
        ("2026-10-25T09:00:04Z", "event_msg", _task("composer")),
        ("2026-10-25T09:00:05Z", "response_item", _msg("user", "Why did step 2 fail?")),
    ], thread_source="automation")
    lines = path.read_text().splitlines()
    lines[-1] = lines[-1][: len(lines[-1]) // 2]  # the person's message, cut mid-record
    path.write_text("\n".join(lines) + "\n")
    adapter = CodexDesktopAdapter(tmp_path)
    assert adapter.load_explained(adapter.discover()[0]) == (None, "unreadable")


def test_an_oversized_person_record_in_an_automation_thread_is_unknown(tmp_path, monkeypatch):
    """Codex round 3: a person's message over the record cap is dropped (counted
    as oversized), which left only the automation's turns: "automation-only"."""
    monkeypatch.setattr(cd, "MAX_RECORD_BYTES", 4096)
    _write_rollout(tmp_path, T1, "2026-10-25T09:00:00Z", [
        ("2026-10-25T09:00:01Z", "event_msg", _task("automation_cron_scheduled")),
        ("2026-10-25T09:00:02Z", "response_item", _msg("user", "Nightly check.")),
        ("2026-10-25T09:00:04Z", "event_msg", _task("composer")),
        ("2026-10-25T09:00:05Z", "response_item", _msg("user", "Long question " + "x" * 8192)),
    ], thread_source="automation")
    adapter = CodexDesktopAdapter(tmp_path)
    assert adapter.load_explained(adapter.discover()[0]) == (None, "unreadable")
