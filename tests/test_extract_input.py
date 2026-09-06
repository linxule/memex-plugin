"""Exercise the actual CLI with pipes and files, without a provider or live state."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
DOC = "projects/test/memos/2026-09-06-input.md"


@pytest.fixture
def extraction_cli(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    index = tmp_path / "index.sqlite"
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith("MEMEX_")
        and "API_KEY" not in key
        and key not in {"CLAUDE_PLUGIN_ROOT", "GOOGLE_APPLICATION_CREDENTIALS"}
    }
    env.update(
        HOME=str(tmp_path / "home"),
        MEMEX_MEMEX_PATH=str(vault),
        MEMEX_STATE_DIR=str(tmp_path / "state"),
        PYTHONPATH=str(ROOT / "src"),
        NO_COLOR="1",
    )
    command = [
        sys.executable, "-m", "memex.cli", "backfill", "obs",
        "--replace", "--no-embed", "--doc-path", DOC, "--index", str(index),
    ]
    return command, {"env": env, "cwd": tmp_path, "capture_output": True, "text": True, "timeout": 30}, index


def _payload(content):
    return json.dumps([{"content": content, "obs_type": "explicit", "confidence": "high"}])


def _stored_content(index):
    with sqlite3.connect(index) as conn:
        return conn.execute("SELECT content FROM observations WHERE doc_path = ?", (DOC,)).fetchall()


@pytest.mark.parametrize("input_mode", ["pipe", "redirect", "file", "printf"])
def test_cli_preserves_json_content_from_pipes_and_files(extraction_cli, tmp_path, input_mode):
    command, kwargs, index = extraction_cli
    # Apostrophes break single-quoted shell literals; echo may decode JSON's
    # backslash escapes. Neither should be rewritten by the application.
    content = "Claude's note: use \\n literally.\nA second line with café and 中文."
    payload = _payload(content)
    source = tmp_path / "observations.json"
    source.write_text(payload, encoding="utf-8")
    if input_mode == "redirect":
        with source.open(encoding="utf-8") as handle:
            result = subprocess.run([*command, "--stdin"], stdin=handle, **kwargs)
    elif input_mode == "file":
        result = subprocess.run([*command, "--store-json", str(source)], **kwargs)
    elif input_mode == "printf":
        result = subprocess.run(
            ["/bin/sh", "-c", "json=$1; shift; printf '%s\\n' \"$json\" | \"$@\"", "test", payload, *command, "--stdin"],
            **kwargs,
        )
    else:
        result = subprocess.run([*command, "--stdin"], input=payload + "\n", **kwargs)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["stored"] == 1
    assert _stored_content(index) == [(content,)]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("", "no observations JSON received"),
        ("[broken", "invalid observations JSON at line 1, column 2"),
        ("{}", "must be an array of objects"),
        ("[null]", "observation 1 must be an object"),
        ('[{"content": "replacement"}]', "observation 1"),
        ('[{"content": 7, "obs_type": "explicit", "confidence": "high"}]', "content must be a non-empty string"),
        ('[{"content": "replacement", "obs_type": "explicit", "confidence": "high", "topics": "topic"}]', "topics must be an array of strings"),
    ],
)
def test_invalid_input_is_actionable_and_preserves_existing_rows(extraction_cli, payload, expected):
    command, kwargs, index = extraction_cli
    seeded = subprocess.run([*command, "--stdin"], input=_payload("keep this observation"), **kwargs)
    assert seeded.returncode == 0, seeded.stderr

    result = subprocess.run([*command, "--stdin"], input=payload, **kwargs)

    assert result.returncode == 2
    assert expected in result.stderr
    assert "--store-json observations.json" in result.stderr
    assert "Traceback" not in result.stderr
    assert not result.stdout
    assert _stored_content(index) == [("keep this observation",)]


def test_zsh_echo_corruption_reports_recovery_without_writing(extraction_cli):
    shell = shutil.which("zsh")
    if shell is None:
        pytest.skip("zsh is needed to reproduce its echo escape expansion")
    command, kwargs, index = extraction_cli
    result = subprocess.run(
        [shell, "-f", "-c", 'echo "$1" | "${@:2}"', "test", _payload("first line\nsecond line"), *command, "--stdin"],
        **kwargs,
    )

    assert result.returncode == 2
    assert "invalid observations JSON" in result.stderr
    assert "Shell quoting or echo can alter JSON" in result.stderr
    assert "printf '%s\\n'" in result.stderr
    assert "Traceback" not in result.stderr
    assert not index.exists()


def test_invalid_file_reports_source_and_does_not_create_index(extraction_cli, tmp_path):
    command, kwargs, index = extraction_cli
    source = tmp_path / "invalid.json"
    source.write_text("[broken", encoding="utf-8")
    result = subprocess.run([*command, "--store-json", str(source)], **kwargs)

    assert result.returncode == 2
    assert str(source) in result.stderr
    assert "invalid observations JSON" in result.stderr
    assert "Traceback" not in result.stderr
    assert not index.exists()
