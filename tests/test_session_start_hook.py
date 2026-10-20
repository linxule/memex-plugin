"""SessionStart announces sidecars the index keeps rejecting (2026-10-20)."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from memex.sidecars import REJECTIONS_META_KEY


@pytest.fixture
def hook(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "hooks" / "session-start.py"
    spec = importlib.util.spec_from_file_location("session_start_hook", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("log_info", "log_warning"):
        monkeypatch.setattr(module, name, lambda *args: None)
    return module


def _index(tmp_path: Path, rejected: list[dict] | None) -> Path:
    index = tmp_path / "_index.sqlite"
    conn = sqlite3.connect(index)
    conn.execute("CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT)")
    if rejected is not None:
        conn.execute(
            "INSERT INTO index_meta VALUES (?, ?)", (REJECTIONS_META_KEY, json.dumps(rejected))
        )
    conn.commit()
    conn.close()
    return index


def test_hook_key_matches_sidecars_module(hook) -> None:
    assert hook._REJECTIONS_META_KEY == REJECTIONS_META_KEY


def test_notice_names_the_rejected_sidecar(hook, tmp_path) -> None:
    index = _index(tmp_path, [
        {"doc_path": "projects/p/memos/a.md", "error": "malformed sidecar: 5: invalid JSON"},
        {"doc_path": "projects/p/memos/b.md", "error": "unreadable: boom"},
    ])
    notice = hook.sidecar_rejection_notice(index)
    assert "2 observation sidecar(s) rejected by the last index update" in notice
    assert "projects/p/memos/a.md (malformed sidecar: 5: invalid JSON) and 1 more" in notice
    assert "memex obs sidecars" in notice


@pytest.mark.parametrize("rejected", [None, []])
def test_no_notice_when_nothing_is_rejected(hook, tmp_path, rejected) -> None:
    assert hook.sidecar_rejection_notice(_index(tmp_path, rejected)) is None


def test_no_notice_and_no_crash_without_an_index(hook, tmp_path) -> None:
    assert hook.sidecar_rejection_notice(tmp_path / "missing.sqlite") is None
    legacy = tmp_path / "legacy.sqlite"
    sqlite3.connect(legacy).close()  # no index_meta table
    assert hook.sidecar_rejection_notice(legacy) is None


def test_startup_combines_pending_memos_and_rejections(hook, monkeypatch, capsys) -> None:
    monkeypatch.setattr(hook, "read_hook_input", lambda: {"source": "startup", "cwd": "/x"})
    monkeypatch.setattr(hook, "get_pending_memos", lambda: [{"cwd": "/x"}])
    monkeypatch.setattr(hook, "filter_pending_for_cwd", lambda p, c: (p, False))
    monkeypatch.setattr(hook, "sidecar_rejection_notice", lambda: "SIDECAR NOTICE")
    with pytest.raises(SystemExit):
        hook.main()
    out = json.loads(capsys.readouterr().out)  # one JSON object, not two
    context = out["hookSpecificOutput"]["additionalContext"]
    assert "1 memo(s) pending retry" in context
    assert context.endswith("SIDECAR NOTICE")


def test_notice_survives_an_entry_without_doc_path(hook, tmp_path) -> None:
    notice = hook.sidecar_rejection_notice(_index(tmp_path, [{"error": "x"}]))
    assert notice is not None and "1 observation sidecar(s)" in notice
