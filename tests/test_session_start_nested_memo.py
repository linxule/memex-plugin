"""A pending signal whose memo sits in memos/<sub>/ is covered (gauntlet round 1)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SID = "00000000-0000-4000-8000-0000000000d1"


def _hook():
    path = Path(__file__).resolve().parents[1] / "hooks" / "session-start.py"
    spec = importlib.util.spec_from_file_location("session_start_nested", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_nested_memo_covers_its_session(tmp_path):
    memo = tmp_path / "projects" / "p" / "memos" / "archive" / "2026-10-01-old.md"
    memo.parent.mkdir(parents=True)
    memo.write_text(f"---\ntype: memo\nsession_id: {SID}\n---\n\n# old\n")
    found = _hook().stamped_sessions(tmp_path, {SID})
    assert SID in found
