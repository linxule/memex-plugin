"""Filing receipts: <state_dir>/last-archives.jsonl (v0.22.0 I6)."""

from __future__ import annotations

import json

from filelock import FileLock

import memex.routing as routing
from memex.routing import (
    RECEIPTS_FILE,
    ProjectRoute,
    append_filing_receipt,
    filing_receipt,
    read_filing_receipts,
)

SID_A = "00000000-0000-4000-8000-00000000aaaa"
SID_B = "00000000-0000-4000-8000-00000000bbbb"


def test_receipt_carries_route_and_archive(tmp_path):
    guess = filing_receipt(
        ProjectRoute("catchall", "detect", "chan_UNMAPPED01"),
        session_id=SID_A, archived="projects/catchall/transcripts/x.md", cwd="/tmp/catch-all", ts="2026-10-28T11:48:02",
    )
    assert guess == {
        "ts": "2026-10-28T11:48:02", "session_id": SID_A, "project": "catchall", "rule": "detect",
        "unmapped_channel": "chan_UNMAPPED01", "archived": "projects/catchall/transcripts/x.md", "cwd": "/tmp/catch-all",
    }
    mapped = filing_receipt(ProjectRoute("teaching", "channel"), session_id=SID_B, archived="a.md", cwd="/x")
    assert mapped["unmapped_channel"] is None and mapped["rule"] == "channel"
    assert mapped["ts"][:4].isdigit()


def test_append_then_read_round_trips_in_order(tmp_path):
    first = filing_receipt(ProjectRoute("a", "detect"), session_id=SID_A, archived="a.md", cwd="/a")
    second = filing_receipt(ProjectRoute("b", "channel"), session_id=SID_B, archived="b.md", cwd="/b")
    assert append_filing_receipt(tmp_path, first) == tmp_path / RECEIPTS_FILE
    append_filing_receipt(tmp_path, second)
    assert read_filing_receipts(tmp_path) == [first, second]
    assert read_filing_receipts(tmp_path, limit=1) == [second]
    lines = (tmp_path / RECEIPTS_FILE).read_text().splitlines()
    assert [json.loads(line)["session_id"] for line in lines] == [SID_A, SID_B]  # one JSON object per line


def test_file_is_trimmed_to_the_newest_receipts(tmp_path, monkeypatch):
    monkeypatch.setattr(routing, "RECEIPTS_TRIM_BYTES", 2_000)
    monkeypatch.setattr(routing, "RECEIPTS_KEEP", 5)
    for i in range(40):
        append_filing_receipt(tmp_path, {"session_id": f"s{i:02d}", "archived": "x" * 50})
    kept = read_filing_receipts(tmp_path)
    assert (tmp_path / RECEIPTS_FILE).stat().st_size <= 2_000 + 200
    assert [r["session_id"] for r in kept][-1] == "s39"
    assert len(kept) <= 5 + 20  # trimmed, not unbounded
    assert "s00" not in {r["session_id"] for r in kept}


def test_long_lines_trim_to_a_byte_target_not_every_append(tmp_path, monkeypatch):
    """500 receipts can exceed the trigger size; a line-count-only trim would
    then rewrite the whole file on every SessionEnd."""
    monkeypatch.setattr(routing, "RECEIPTS_TRIM_BYTES", 4_000)
    monkeypatch.setattr(routing, "RECEIPTS_KEEP", 500)
    path = tmp_path / RECEIPTS_FILE
    rewrites = []
    real_trim = routing._trim_receipts
    monkeypatch.setattr(routing, "_trim_receipts", lambda p: (rewrites.append(1), real_trim(p)))
    for i in range(60):
        append_filing_receipt(tmp_path, {"session_id": f"s{i:02d}", "cwd": "/very/long/path/" * 20})
    assert path.stat().st_size <= 4_000 + 400
    assert 1 <= len(rewrites) <= 60 // 5, f"trimmed {len(rewrites)} times in 60 appends"
    assert read_filing_receipts(tmp_path)[-1]["session_id"] == "s59"


def test_an_oversized_newest_receipt_survives_its_own_trim(tmp_path, monkeypatch):
    monkeypatch.setattr(routing, "RECEIPTS_TRIM_BYTES", 100)
    append_filing_receipt(tmp_path, {"session_id": "big", "cwd": "x" * 500})
    assert [r["session_id"] for r in read_filing_receipts(tmp_path)] == ["big"]


def test_limit_zero_returns_nothing(tmp_path):
    append_filing_receipt(tmp_path, {"session_id": SID_A})
    assert read_filing_receipts(tmp_path, limit=0) == []
    assert read_filing_receipts(tmp_path, limit=5) == [{"session_id": SID_A}]


def test_busy_lock_drops_the_receipt_instead_of_waiting(tmp_path, monkeypatch):
    monkeypatch.setattr(routing, "_RECEIPT_LOCK_TIMEOUT", 0.01)
    path = tmp_path / RECEIPTS_FILE
    with FileLock(str(path) + ".lock"):
        assert append_filing_receipt(tmp_path, {"session_id": SID_A}) is None
    assert not path.exists()
    assert append_filing_receipt(tmp_path, {"session_id": SID_B}) == path
    assert read_filing_receipts(tmp_path) == [{"session_id": SID_B}]


def test_reader_skips_malformed_lines_and_missing_file(tmp_path):
    assert read_filing_receipts(tmp_path) == []
    (tmp_path / RECEIPTS_FILE).write_text('{"session_id": "a"}\nnot json\n[1]\n{"session_id": "b"}\n')
    assert [r["session_id"] for r in read_filing_receipts(tmp_path)] == ["a", "b"]
