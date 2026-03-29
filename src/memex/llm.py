"""Thin wrapper around `claude` CLI for structured LLM calls."""
from __future__ import annotations

import json
import shutil
import subprocess


def claude_available() -> bool:
    return shutil.which("claude") is not None


def call_claude_json(
    prompt: str,
    json_schema: dict,
    model: str = "sonnet",
    timeout: int = 120,
) -> dict | None:
    schema_str = json.dumps(json_schema)
    try:
        result = subprocess.run(
            [
                "claude", "-p",
                "--model", model,
                "--output-format", "json",
                "--json-schema", schema_str,
                "--no-session-persistence",
                "--tools", "",
            ],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return None
        envelope = json.loads(result.stdout)
        if isinstance(envelope, dict):
            if envelope.get("is_error"):
                return None
            if "structured_output" in envelope:
                return envelope["structured_output"]
        return envelope
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None
