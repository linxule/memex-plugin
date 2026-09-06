"""SessionEnd archives become visible only after successful conversion."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

import memex.scripts.transcript_to_md as converter
from memex.scripts.discover_sessions import get_memex_session_ids


SESSION_ID = "aaaa1111-2222-3333-4444-555566667777"


@pytest.fixture
def hook_env(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "hooks" / "session-end.py"
    spec = importlib.util.spec_from_file_location("session_end_hook", path)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    vault = tmp_path / "vault"
    source = tmp_path / f"{SESSION_ID}.jsonl"
    source.write_text("\n".join(json.dumps({
        "type": "user",
        "message": {"role": "user", "content": f"Keep conversation turn {i}"},
    }) for i in range(6)) + "\n", encoding="utf-8")
    phases = []
    errors = []
    monkeypatch.setattr(hook, "read_hook_input", lambda: {
        "session_id": SESSION_ID, "transcript_path": str(source), "cwd": "/demo",
    })
    monkeypatch.setattr(hook, "get_memex_path", lambda: vault)
    monkeypatch.setattr(hook, "detect_project", lambda cwd: "demo")
    monkeypatch.setattr(hook, "is_session_processed", lambda *args: False)
    monkeypatch.setattr(hook, "get_session_memo_saved", lambda sid: True)
    monkeypatch.setattr(hook, "mark_session_phase", lambda *args: phases.append(args))
    monkeypatch.setattr(hook, "log_error", errors.append)
    for name in ("log_info", "log_warning"):
        monkeypatch.setattr(hook, name, lambda *args: None)
    for name in ("log_info", "log_warning", "log_error"):
        monkeypatch.setattr(converter, name, lambda *args: None)
    return hook, vault, source, phases, errors


def _run(hook):
    with pytest.raises(SystemExit) as exc:
        hook.main()
    assert exc.value.code == 0  # Hooks remain non-blocking even on failure.


def test_failed_conversion_does_not_hide_session_from_discovery(hook_env, monkeypatch):
    hook, vault, source, phases, errors = hook_env
    original = source.read_bytes()
    monkeypatch.setattr(hook, "convert_transcript_file", lambda **kwargs: None)

    _run(hook)

    assert phases == []
    assert errors
    assert get_memex_session_ids(vault) == (set(), set())
    assert list((vault / "projects/demo/transcripts").iterdir()) == []
    assert source.read_bytes() == original


def test_interrupted_conversion_removes_partial_output(hook_env, monkeypatch):
    hook, vault, source, phases, errors = hook_env

    def interrupted(**kwargs):
        kwargs["output_path"].write_text("partial transcript")
        raise OSError("disk write interrupted")

    monkeypatch.setattr(hook, "convert_transcript_file", interrupted)
    _run(hook)

    assert phases == []
    assert get_memex_session_ids(vault) == (set(), set())
    assert list((vault / "projects/demo/transcripts").iterdir()) == []
    assert source.exists()


def test_successful_conversion_publishes_both_artifacts(hook_env):
    hook, vault, source, phases, errors = hook_env
    _run(hook)

    archived = vault / "projects/demo/transcripts"
    assert len(list(archived.iterdir())) == 2
    assert next(archived.glob("*.jsonl")).read_bytes() == source.read_bytes()
    markdown = next(archived.glob("*.md")).read_text()
    assert "Keep conversation turn 5" in markdown
    assert "has_memo: true" in markdown
    assert phases == [(SESSION_ID, "transcript_archived")]
    assert not errors


def test_staged_conversion_is_not_visible_to_indexing(hook_env, monkeypatch):
    from memex.scripts.index_rebuild import find_documents

    hook, vault, _, _, _ = hook_env

    def convert_while_indexing(**kwargs):
        output = converter.convert_transcript_file(**kwargs)
        assert output.exists()
        assert find_documents(vault) == []
        return output

    monkeypatch.setattr(hook, "convert_transcript_file", convert_while_indexing)
    _run(hook)
    assert any(path.parent.name == "transcripts" for path in find_documents(vault))


@pytest.mark.parametrize(("content", "expected"), [
    ("\n" * 10, True),
    ("{}\n" * 5, True),
    ("{}\n" * 6, False),
    ('{"type":"tool_use"}\n', False),
])
def test_viability_preserves_line_and_tool_thresholds(hook_env, content, expected):
    hook, _, source, _, _ = hook_env
    source.write_text(content)
    assert hook.is_trivial_transcript(source) is expected


def test_viability_stops_reading_after_six_nonempty_lines(hook_env, monkeypatch):
    hook, _, source, _, _ = hook_env

    class GrowingTranscript(io.StringIO):
        def __next__(self):
            line = super().__next__()
            assert self.tell() <= 18, "viability scanned past the sixth line"
            return line

    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: GrowingTranscript("{}\n" * 100))
    assert hook.is_trivial_transcript(source) is False
