"""Tests for discover_sessions import-flow fixes (2026-08-05).

1. ``--triage --import`` used to display the triage report and ``return``
   without importing — the tool's own recommended command
   (``--triage --min-score=9 --import --apply``) silently never imported.
2. ``--exclude <session-id-prefix>`` skips sessions on import (for
   currently-running sessions whose transcripts are still growing).
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import memex.scripts.discover_sessions as ds


SESSION_ID = "aaaa1111-2222-3333-4444-555566667777"


@pytest.fixture()
def claude_env(tmp_path, monkeypatch):
    projects = tmp_path / "claude" / "projects"
    vault = tmp_path / "vault"
    (vault / "projects").mkdir(parents=True)
    d = projects / "-Users-x-Documents-Apps-arena"
    d.mkdir(parents=True)
    lines = [
        json.dumps({
            "type": "user",
            "cwd": "/Users/x/Documents/Apps/arena",
            "sessionId": SESSION_ID,
            "timestamp": "2026-08-01T10:00:00Z",
            "message": {"role": "user", "content": "do the thing " + "x" * 4000},
        }),
        json.dumps({
            "type": "assistant",
            "cwd": "/Users/x/Documents/Apps/arena",
            "sessionId": SESSION_ID,
            "timestamp": "2026-08-01T10:01:00Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
        }),
    ]
    (d / f"{SESSION_ID}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(ds, "PROJECTS_DIR", projects)
    monkeypatch.setattr(ds, "get_memex_path", lambda: vault)
    # import_sessions does a scripts-dir sibling import (transcript_to_md) that
    # only resolves via the CLI shim's sys.path; the flow under test is what
    # REACHES it, so stub it faithfully to its dry-run return shape.
    monkeypatch.setattr(ds, "import_sessions", lambda sessions, dry_run=True: [
        {"status": "would_import" if dry_run else "imported",
         "session_id": s["session_id"], "project_display": s["project_display"],
         "size_bytes": s.get("size_bytes", 0), "project_memex": s.get("memex_name", "x")}
        for s in sessions
    ])
    return projects


def _run_main(monkeypatch, capsys, argv):
    monkeypatch.setattr(sys, "argv", ["discover_sessions.py"] + argv)
    try:
        ds.main()
    except SystemExit as e:  # argparse/tool exits are fine if zero
        assert not e.code
    return capsys.readouterr().out


def test_triage_plus_import_reaches_import_branch(claude_env, monkeypatch, capsys):
    out = _run_main(monkeypatch, capsys, ["--triage", "--import"])
    assert "Would import" in out, f"triage+import never reached the import branch:\n{out}"


def test_triage_alone_still_reports(claude_env, monkeypatch, capsys):
    out = _run_main(monkeypatch, capsys, ["--triage"])
    assert "Triage results" in out
    assert "Would import" not in out


def test_exclude_skips_session(claude_env, monkeypatch, capsys):
    out = _run_main(monkeypatch, capsys, ["--import", "--exclude", SESSION_ID[:8]])
    assert "excluded:" in out
    assert "Would import 0" in out


def test_exclude_prefix_only_matches_target(claude_env, monkeypatch, capsys):
    out = _run_main(monkeypatch, capsys, ["--import", "--exclude", "ffffffff"])
    assert "excluded:" not in out
    assert "Would import 1" in out
