"""session import routes relay workers by their claude.ai channel (v0.22.0 C2, wave 2)."""

from __future__ import annotations

from memex.scripts import discover_sessions as ds
from test_relay_capture import CHANNEL, relay_rows, write_jsonl


def test_import_route_reads_the_relay_channel(tmp_path, monkeypatch):
    import memex.routing as routing

    monkeypatch.setattr(routing, "get_config", lambda: {"channel_mappings": {CHANNEL: "teaching"}})
    src = write_jsonl(tmp_path / "relay.jsonl", relay_rows())
    session = {"source_path": str(src), "session_id": "00000000-0000-4000-8000-0000000a11a1",
               "cwd": str(tmp_path / "Documents" / "catchall")}
    route = ds.route_session(session)
    assert route is not None and route.project == "teaching" and route.rule == "channel"


def test_unmapped_relay_channel_is_carried_on_import(tmp_path, monkeypatch):
    import memex.routing as routing

    monkeypatch.setattr(routing, "get_config", lambda: {"channel_mappings": {}})
    src = write_jsonl(tmp_path / "relay.jsonl", relay_rows())
    route = ds.route_session({"source_path": str(src), "session_id": "x" * 36, "cwd": str(tmp_path)})
    assert route.unmapped_channel == CHANNEL
