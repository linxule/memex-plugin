"""Malformed records must not discard the rest of a transcript."""

import json

import pytest

from memex.scripts import transcript_to_md


@pytest.fixture(autouse=True)
def isolate_logging(monkeypatch):
    """Converter diagnostics must not write to the user's memex state."""
    for name in ("log_info", "log_warning", "log_error"):
        monkeypatch.setattr(transcript_to_md, name, lambda *_: None)


def test_mixed_invalid_records_preserve_conversation(tmp_path):
    source = tmp_path / "session.jsonl"
    user = {
        "type": "user",
        "message": {"role": "user", "content": "Please preserve this request."},
    }
    assistant = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "The response survives too."}],
        },
    }
    lines = [
        "null",
        json.dumps(user),
        "[]",
        '"a scalar"',
        "42",
        "true",
        '{"unfinished":',
        "",
        json.dumps(assistant),
    ]
    source.write_text("\n".join(lines) + "\n", encoding="utf-8")

    markdown, metadata = transcript_to_md.convert_to_markdown(source)

    assert "Please preserve this request." in markdown
    assert "The response survives too." in markdown
    assert metadata["parse_errors"] == 6
    assert metadata["total_messages"] == 2
    assert metadata["total_turns"] == 1
    assert "parse_errors: 6" in markdown


def test_all_invalid_records_create_no_artifact(tmp_path):
    source = tmp_path / "session.jsonl"
    source.write_text('null\n[]\n"scalar"\n42\ntrue\n{\n', encoding="utf-8")
    output = tmp_path / "output" / "session.md"

    result = transcript_to_md.convert_transcript_file(source, output)

    assert result is None
    assert not output.exists()
    assert not output.parent.exists()
