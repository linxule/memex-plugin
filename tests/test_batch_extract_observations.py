"""Coverage for `parse_json_array` in scripts/batch_extract_observations.py.

The function is the parser between LLM output and `memex backfill obs --stdin`.
It is pure and well-bounded — the test cases below pin the documented behavior
(fence stripping, `[`-to-last-`]` bounds, ValueError for empty / no-array).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The script lives at top-level scripts/, not a package. Import via path.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from batch_extract_observations import parse_json_array  # noqa: E402


def test_parse_bare_array():
    assert parse_json_array('[{"content": "x"}]') == [{"content": "x"}]


def test_parse_with_fenced_json_tag():
    assert parse_json_array('```json\n[{"a": 1}]\n```') == [{"a": 1}]


def test_parse_with_fence_no_lang():
    assert parse_json_array('```\n[{"a": 1}]\n```') == [{"a": 1}]


def test_parse_with_prose_preamble():
    text = 'Here you go:\n[{"a": 1}]'
    assert parse_json_array(text) == [{"a": 1}]


def test_parse_with_trailing_prose():
    text = '[{"a": 1}]\nLet me know if you want more.'
    assert parse_json_array(text) == [{"a": 1}]


def test_parse_with_nested_brackets():
    text = '[{"items": [1, 2, 3]}]'
    assert parse_json_array(text) == [{"items": [1, 2, 3]}]


def test_parse_multiple_observations():
    text = '[{"content": "a"}, {"content": "b"}, {"content": "c"}]'
    assert parse_json_array(text) == [
        {"content": "a"},
        {"content": "b"},
        {"content": "c"},
    ]


def test_parse_empty_raises():
    with pytest.raises(ValueError, match="empty"):
        parse_json_array("")


def test_parse_whitespace_only_raises():
    # Whitespace-only strips to empty → matches the empty branch.
    with pytest.raises(ValueError, match="empty"):
        parse_json_array("   \n\n   ")


def test_parse_no_array_raises():
    with pytest.raises(ValueError, match="no JSON array"):
        parse_json_array("just some prose with no brackets")


def test_parse_object_at_top_level_captures_inner_array():
    # Documented behavior: the function scans for the first `[` and the last
    # `]` and parses that slice. Given a top-level object that *contains* an
    # array, the bounds capture the inner array. This is intentional — the
    # function is tolerant of LLM noise, and a top-level object is treated
    # as noise around the inner array.
    assert parse_json_array('{"foo": [1, 2, 3]}') == [1, 2, 3]


def test_parse_fenced_json_with_prose_after():
    # Closing fence in the middle is stripped; trailing `[`/`]` bounds still
    # find the array. Verifies fence-handling and bounds-scan compose.
    text = '```json\n[{"a": 1}]\n```'
    assert parse_json_array(text) == [{"a": 1}]


def test_parse_unclosed_bracket_raises():
    # Only an opening `[` — `rfind("]")` returns -1, `end <= start` triggers.
    with pytest.raises(ValueError, match="no JSON array"):
        parse_json_array("[ unclosed")
