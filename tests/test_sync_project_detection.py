"""Tests for auto-memory sync canonical project detection.

The sync used to derive vault folder names from the Claude project dir slug
(``claude_dir_to_project_name``), producing cwd-fragment folders like
``Apps-arena`` that ignored project_mappings / git remote and re-fragmented the
vault on every run. It now reads the true ``cwd`` from a session transcript
(validated against the dir's ``/``→``-`` encoding) and feeds the canonical
``detect_project`` — the same identity memos/sessions use.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from memex.scripts.sync_auto_memory import _cwd_from_session, _project_names


@pytest.fixture(autouse=True)
def _clear_cache():
    # _project_names and _cwd_from_session are lru_cached per process; isolate tests.
    _project_names.cache_clear()
    _cwd_from_session.cache_clear()
    yield
    _project_names.cache_clear()
    _cwd_from_session.cache_clear()


def _session_dir(parent: Path, encoded_name: str, lines: list[dict | str]) -> Path:
    """Create a Claude-style project dir (named as the /→- encoded cwd) with a session."""
    d = parent / encoded_name
    d.mkdir(parents=True, exist_ok=True)
    body = "\n".join(o if isinstance(o, str) else json.dumps(o) for o in lines) + "\n"
    (d / "s.jsonl").write_text(body, encoding="utf-8")
    return d


# --- _cwd_from_session ------------------------------------------------------

def test_cwd_from_session_reads_matching_cwd(tmp_path: Path) -> None:
    d = _session_dir(tmp_path, "-Users-x-Documents-Apps-arena", [
        {"type": "summary"},                                  # meta line, no cwd
        {"type": "user", "cwd": "/Users/x/Documents/Apps/arena"},
    ])
    assert _cwd_from_session(d) == "/Users/x/Documents/Apps/arena"


def test_cwd_from_session_none_when_no_jsonl(tmp_path: Path) -> None:
    d = tmp_path / "-Users-x-Documents-Apps-arena"
    d.mkdir()
    assert _cwd_from_session(d) is None


def test_cwd_from_session_rejects_mismatched_cwd(tmp_path: Path) -> None:
    # A cwd that does not encode to this dir name (e.g. a stray tool-recorded
    # path) must NOT be trusted — it could mis-map the project. (kimi finding #3)
    d = _session_dir(tmp_path, "-Users-x-Documents-Apps-arena", [
        {"cwd": "/somewhere/else/entirely"},
    ])
    assert _cwd_from_session(d) is None


def test_cwd_from_session_skips_non_dict_and_bad_json(tmp_path: Path) -> None:
    # `[{"cwd": ...}]` is valid JSON, contains the "cwd" substring, but is a LIST
    # → must be skipped by the isinstance(dict) guard, not crash. The matching
    # dict line that follows is returned.
    d = _session_dir(tmp_path, "-Users-x-Documents-Apps-arena", [
        "not json at all",
        '[{"cwd": "/Users/x/Documents/Apps/arena"}]',
        {"cwd": "/Users/x/Documents/Apps/arena"},
    ])
    assert _cwd_from_session(d) == "/Users/x/Documents/Apps/arena"


def test_cwd_from_session_survives_one_unreadable_session(tmp_path: Path) -> None:
    # A broken symlink matching *.jsonl must not sink the whole dir (kimi #1):
    # its stat() fails in the sort key and its open() fails — both are tolerated,
    # and the good session still resolves.
    d = _session_dir(tmp_path, "-Users-x-Documents-Apps-arena", [
        {"cwd": "/Users/x/Documents/Apps/arena"},
    ])
    broken = d / "broken.jsonl"
    os.symlink(d / "does-not-exist", broken)
    assert _cwd_from_session(d) == "/Users/x/Documents/Apps/arena"


# --- _project_names ---------------------------------------------------------

def test_project_names_canonical_with_cwd(tmp_path: Path) -> None:
    # cwd matches the dir encoding → detect_project (no mapping/git for this
    # nonexistent path) falls through to the folder name "myproj".
    d = _session_dir(tmp_path, "-Users-x-Documents-Apps-myproj", [
        {"cwd": "/Users/x/Documents/Apps/myproj"},
    ])
    display, memex = _project_names(d)
    assert display == "myproj" and memex == "myproj"
    assert not memex.startswith("Apps-")     # the regression this fix prevents


def test_project_names_falls_back_to_slug_without_session(tmp_path: Path) -> None:
    d = tmp_path / "-Users-alice-Documents-myapp"
    d.mkdir()
    _display, memex = _project_names(d)
    assert memex == "myapp"


# --- get_config / Settings fix ---------------------------------------------

def test_settings_declares_project_mappings_field() -> None:
    # The root cause of split-brain detection: project_mappings was not a Settings
    # field, so extra="ignore" silently dropped it from model_dump() (→ get_config),
    # making detect_project's mapping check a no-op in CLI context.
    from memex.config import Settings
    assert "project_mappings" in Settings.model_fields
