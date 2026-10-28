"""claude.ai relay workers are recognised from the transcript head (v0.22.0 C1).

Fixtures are synthetic: the record SHAPES (queue-operation enqueue carrying
the relay opener, a queued <wake><project id=…> message, then an isMeta user
record with ``entrypoint: claude-desktop``) were read off real relays; no
real ids, prompts or paths are used.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import yaml

import memex.scripts.transcript_to_md as converter
from memex.scripts.transcript_to_md import (
    RELAY_HARNESS,
    RELAY_HEAD_RECORDS,
    convert_to_markdown,
    convert_transcript_file,
    read_relay_metadata,
    relay_metadata,
)

SID = "00000000-0000-4000-8000-0000000a11a1"
CHANNEL = "chan_TESTMAPPED"
COORD = "session_TESTCOORD0001"
_T0 = datetime(2026, 10, 28, 10, 0, 0)


def _ts(seconds: int) -> str:
    return (_T0 + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def relay_opener(coordinator: str = COORD) -> str:
    return (
        f'<relay from="coordinator" session="{coordinator}" current-time="2026-10-28T10:00:00Z" reason="spawn">'
        "Synthetic spawn.\n<note>Check the fixture and report back.</note>\n</relay>"
    )


def wake(channel: str = CHANNEL) -> str:
    return (
        '<wake reason="message" current-time="2026-10-28T10:00:01Z">\n'
        f'<project id="{channel}" type="project">\n'
        '<message trigger="mention" from="tester" trust="owner" id="m1">synthetic wake</message>\n'
        "</project>\n</wake>\n"
    )


def relay_rows(
    *,
    sid: str = SID,
    cwd: str = "/tmp/catch-all",
    channel: str | None = CHANNEL,
    coordinator: str = COORD,
    tool_uses: int = 3,
    entrypoint: str = "claude-desktop",
    first_prompt: str | None = None,
    pad: int = 0,
) -> list[dict]:
    """A relay worker's JSONL rows. ``first_prompt`` replaces the relay opener
    (an ordinary desktop-app session); ``pad`` grows each tool result."""
    base = {"sessionId": sid}
    opener = relay_opener(coordinator) if first_prompt is None else first_prompt
    rows: list[dict] = [
        {"type": "queue-operation", "operation": "enqueue", "timestamp": _ts(0), "content": opener, **base},
        {"type": "queue-operation", "operation": "dequeue", "timestamp": _ts(0), **base},
    ]
    if channel:
        rows.append({"type": "queue-operation", "operation": "enqueue", "timestamp": _ts(1), "content": wake(channel), **base})
    common = {"cwd": cwd, "entrypoint": entrypoint, **base}
    # Real relay openers are isMeta records; an ordinary first prompt is not.
    rows.append({"type": "user", "isMeta": first_prompt is None, "timestamp": _ts(2),
                 "message": {"role": "user", "content": opener}, **common})
    rows.append({"type": "attachment", "timestamp": _ts(2),
                 "attachment": {"type": "skill_listing", "content": "synthetic " * 200}, **common})
    for i in range(tool_uses):
        tid = f"toolu_{i:05d}"
        rows.append({"type": "assistant", "timestamp": _ts(10 + 2 * i), **common, "message": {
            "role": "assistant", "id": f"msg_{i:05d}", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": f"step {i}"},
                        {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": f"echo {i}"}}]}})
        rows.append({"type": "user", "timestamp": _ts(11 + 2 * i), **common, "message": {
            "role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": f"out {i} " + "z" * pad}]}})
    rows.append({"type": "assistant", "timestamp": _ts(20 + 2 * tool_uses), **common, "message": {
        "role": "assistant", "id": "msg_final", "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": "Reported back to the coordinator."}]}})
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def _frontmatter(markdown: str) -> dict:
    return yaml.safe_load(markdown.split("---\n")[1])


# --- detection ---------------------------------------------------------------

def test_relay_worker_yields_harness_coordinator_and_channel():
    meta = relay_metadata(relay_rows())
    assert meta == {"harness": RELAY_HARNESS, "relay_coordinator": COORD, "relay_channel": CHANNEL}


def test_second_relay_keeps_its_own_ids():
    meta = relay_metadata(relay_rows(channel="chan_OTHERCHANNEL01", coordinator="session_OTHERCOORD999"))
    assert meta["relay_channel"] == "chan_OTHERCHANNEL01"
    assert meta["relay_coordinator"] == "session_OTHERCOORD999"


def test_relay_without_a_queued_project_has_no_channel():
    meta = relay_metadata(relay_rows(channel=None))
    assert meta == {"harness": RELAY_HARNESS, "relay_coordinator": COORD}


def test_desktop_session_with_an_ordinary_first_prompt_is_not_a_relay():
    """entrypoint: claude-desktop alone is the desktop app, not a relay — even
    when a later message quotes relay tags (e.g. a session debugging relays)."""
    rows = relay_rows(first_prompt="Please look at the fixture.")
    rows.insert(5, {"type": "user", "entrypoint": "claude-desktop", "sessionId": SID, "timestamp": _ts(3),
                    "message": {"role": "user", "content": relay_opener() + wake()}})
    assert relay_metadata(rows) == {}


def test_relay_opener_from_a_cli_session_is_not_a_relay():
    assert relay_metadata(relay_rows(entrypoint="cli")) == {}
    assert relay_metadata(relay_rows(entrypoint="sdk-cli")) == {}


def test_meta_scaffolding_before_the_opener_is_skipped():
    rows = relay_rows()
    caveat = {"type": "user", "isMeta": True, "entrypoint": "claude-desktop", "sessionId": SID, "timestamp": _ts(1),
              "message": {"role": "user", "content": "<local-command-caveat>Caveat: synthetic.</local-command-caveat>"}}
    rows.insert(3, caveat)
    assert relay_metadata(rows)["relay_coordinator"] == COORD
    # ...but a real (non-meta) first prompt still decides
    plain = {**caveat, "isMeta": False, "message": {"role": "user", "content": "An ordinary first prompt."}}
    rows[3] = plain
    assert relay_metadata(rows) == {}


def test_opener_must_start_the_first_user_turn():
    rows = relay_rows(first_prompt="note: " + relay_opener())
    assert relay_metadata(rows) == {}


def test_malformed_ids_are_never_stamped():
    """Ids go into YAML frontmatter verbatim: anything outside [A-Za-z0-9] is refused."""
    assert relay_metadata(relay_rows(coordinator="session_x\nproject: evil")) == {}
    assert relay_metadata(relay_rows(coordinator="other_TESTCOORD0001")) == {}
    meta = relay_metadata(relay_rows(channel='chan_bad" injected="1'))
    assert "relay_channel" not in meta and meta["relay_coordinator"] == COORD


def test_opener_must_come_from_the_coordinator():
    rows = relay_rows(first_prompt=relay_opener().replace('from="coordinator"', 'from="peer"'))
    assert relay_metadata(rows) == {}


def test_channel_outside_the_head_window_is_ignored():
    rows = relay_rows(channel=None, tool_uses=RELAY_HEAD_RECORDS)
    rows.append({"type": "queue-operation", "operation": "enqueue", "timestamp": _ts(9999), "content": wake(), "sessionId": SID})
    assert "relay_channel" not in relay_metadata(rows)
    early = relay_rows(channel=None, tool_uses=RELAY_HEAD_RECORDS)
    early.insert(6, {"type": "queue-operation", "operation": "enqueue", "timestamp": _ts(5), "content": wake(), "sessionId": SID})
    assert relay_metadata(early)["relay_channel"] == CHANNEL


# --- file head reader ----------------------------------------------------------

def test_head_reader_matches_the_in_memory_parse(tmp_path):
    path = write_jsonl(tmp_path / f"{SID}.jsonl", relay_rows(tool_uses=5))
    messages, _ = converter.parse_transcript_jsonl(path)
    assert read_relay_metadata(path) == relay_metadata(messages)


def test_head_reader_stops_early_on_a_huge_transcript(tmp_path, monkeypatch):
    """SessionEnd's 1.5 s budget: a relay is recognised from a few KB, never the whole file."""
    path = write_jsonl(tmp_path / f"{SID}.jsonl", relay_rows(tool_uses=400, pad=10_000))
    assert path.stat().st_size > 4_000_000
    read = []
    real_open = open

    def counting_open(*args, **kwargs):
        fh = real_open(*args, **kwargs)
        orig = fh.readline

        def readline(*a):
            line = orig(*a)
            read.append(len(line))
            return line

        fh.readline = readline
        return fh

    monkeypatch.setattr(converter, "open", counting_open, raising=False)
    assert read_relay_metadata(path)["relay_channel"] == CHANNEL
    assert sum(read) < 20_000, f"head scan read {sum(read)} bytes"


def test_head_reader_bounds_a_giant_first_line(tmp_path):
    path = tmp_path / "giant.jsonl"
    rows = relay_rows()
    rows.insert(0, {"type": "attachment", "content": "q" * (converter.RELAY_HEAD_BYTES + 10)})
    write_jsonl(path, rows)
    assert read_relay_metadata(path) == {}  # the opener lies past the byte bound


def test_unreadable_or_non_json_file_is_not_a_relay(tmp_path):
    assert read_relay_metadata(tmp_path / "missing.jsonl") == {}
    junk = tmp_path / "junk.jsonl"
    junk.write_bytes(b"\xff\xfe not json\n[1, 2]\n\n")
    assert read_relay_metadata(junk) == {}


# --- frontmatter ----------------------------------------------------------------

def test_relay_frontmatter_carries_harness_channel_coordinator(tmp_path):
    path = write_jsonl(tmp_path / f"{SID}.jsonl", relay_rows(tool_uses=4))
    markdown, metadata = convert_to_markdown(path, session_id=SID, project="teaching")
    fm = _frontmatter(markdown)
    assert fm["harness"] == RELAY_HARNESS
    assert fm["relay_channel"] == CHANNEL
    assert fm["relay_coordinator"] == COORD
    # existing keys and the entrypoint logic are untouched
    assert fm["entrypoint"] == "claude-desktop"
    assert fm["type"] == "transcript" and fm["session_id"] == SID and fm["project"] == "teaching"
    assert fm["tool_uses"] == 4
    assert metadata["relay_channel"] == CHANNEL


def test_non_relay_frontmatter_has_no_relay_keys(tmp_path):
    path = write_jsonl(tmp_path / f"{SID}.jsonl", relay_rows(first_prompt="An ordinary prompt.", entrypoint="cli"))
    markdown, _ = convert_to_markdown(path, session_id=SID)
    fm = _frontmatter(markdown)
    assert fm["entrypoint"] == "cli"
    assert not {"harness", "relay_channel", "relay_coordinator"} & set(fm)


def test_convert_file_hands_back_its_metadata(tmp_path):
    path = write_jsonl(tmp_path / f"{SID}.jsonl", relay_rows(tool_uses=7))
    out: dict = {}
    result = convert_transcript_file(path, tmp_path / "out.md", session_id=SID, metadata_out=out)
    assert result == tmp_path / "out.md"
    assert out["tool_uses"] == 7 and out["harness"] == RELAY_HARNESS
    assert out["relay_channel"] == CHANNEL
