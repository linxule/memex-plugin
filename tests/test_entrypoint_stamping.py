"""Tests for archive-time entrypoint stamping (v0.16.5).

cc-fleet / sdk-cli fan-out workers run from scratch cwds and mint fake
project folders; the clean discriminator is the JSONL's top-level
``entrypoint`` field ("cli" interactive vs "sdk-cli" workers). Stamping it
into transcript frontmatter at conversion time makes drift triage durable —
no re-opening the source JSONL after archive.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from memex.scripts.transcript_to_md import (
    convert_to_markdown,
    extract_session_metadata,
)

SESSION_ID = "bbbb1111-2222-3333-4444-555566667777"


def _msg(i, role="user", entrypoint="cli", **extra):
    d = {
        "type": role,
        "sessionId": SESSION_ID,
        "cwd": "/tmp/x",
        "timestamp": f"2026-08-01T10:{i:02d}:00Z",
        "message": (
            {"role": "user", "content": f"prompt {i}"}
            if role == "user"
            else {"role": "assistant", "content": [{"type": "text", "text": f"reply {i}"}]}
        ),
    }
    if entrypoint is not None:
        d["entrypoint"] = entrypoint
    d.update(extra)
    return d


def test_single_entrypoint_stamped_as_scalar():
    meta = extract_session_metadata([_msg(0, entrypoint="sdk-cli"), _msg(1, "assistant", "sdk-cli")])
    assert meta["entrypoint"] == "sdk-cli"


def test_missing_entrypoint_omits_key():
    meta = extract_session_metadata([_msg(0, entrypoint=None), _msg(1, "assistant", None)])
    assert "entrypoint" not in meta


def test_mixed_entrypoints_stamped_as_sorted_list():
    meta = extract_session_metadata([_msg(0, entrypoint="sdk-cli"), _msg(1, "assistant", "cli")])
    assert meta["entrypoint"] == ["cli", "sdk-cli"]


def test_non_string_entrypoint_ignored():
    msgs = [_msg(0, entrypoint=None), _msg(1, "assistant", None)]
    msgs[0]["entrypoint"] = 42
    msgs[1]["entrypoint"] = ""
    meta = extract_session_metadata(msgs)
    assert "entrypoint" not in meta


def test_convert_frontmatter_carries_entrypoint(tmp_path):
    jsonl = tmp_path / f"{SESSION_ID}.jsonl"
    lines = [json.dumps(_msg(0, entrypoint="sdk-cli")),
             json.dumps(_msg(1, "assistant", "sdk-cli"))]
    jsonl.write_text("\n".join(lines) + "\n", encoding="utf-8")

    markdown, metadata = convert_to_markdown(jsonl, session_id=SESSION_ID)

    assert metadata["entrypoint"] == "sdk-cli"
    frontmatter = markdown.split("---")[1]
    assert "entrypoint: sdk-cli" in frontmatter


def test_convert_without_entrypoint_stays_clean(tmp_path):
    jsonl = tmp_path / f"{SESSION_ID}.jsonl"
    lines = [json.dumps(_msg(0, entrypoint=None)),
             json.dumps(_msg(1, "assistant", None))]
    jsonl.write_text("\n".join(lines) + "\n", encoding="utf-8")

    markdown, metadata = convert_to_markdown(jsonl, session_id=SESSION_ID)

    assert "entrypoint" not in metadata
    assert "entrypoint:" not in markdown.split("---")[1]
