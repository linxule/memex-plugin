"""Tests for _cwd_encodes_to — the dot-path encoding fix (2026-08-05).

Claude Code's project-dir encoder maps EVERY non-alphanumeric character to
``-`` (``/Users/x/.slock/agents`` → ``-Users-x--slock-agents``). The old
validator only encoded ``/``, so any cwd containing a dot was rejected and
the caller fell back to the lossy slug — minting fragment folders for
.slock agent workspaces and .claude-worktrees sessions.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from memex.scripts.utils import _cwd_encodes_to, cwd_from_session


@pytest.fixture(autouse=True)
def _clear_cache():
    cwd_from_session.cache_clear()
    yield
    cwd_from_session.cache_clear()


def _session_dir(parent: Path, encoded_name: str, lines: list[dict | str]) -> Path:
    d = parent / encoded_name
    d.mkdir(parents=True, exist_ok=True)
    body = "\n".join(o if isinstance(o, str) else json.dumps(o) for o in lines) + "\n"
    (d / "s.jsonl").write_text(body, encoding="utf-8")
    return d


# --- _cwd_encodes_to unit ----------------------------------------------------

def test_plain_slash_path_still_matches():
    assert _cwd_encodes_to("/Users/x/Documents/Apps/arena", "-Users-x-Documents-Apps-arena")


def test_hidden_dot_dir_matches():
    assert _cwd_encodes_to(
        "/Users/x/.slock/agents/abc123", "-Users-x--slock-agents-abc123"
    )


def test_worktree_dot_dir_matches():
    assert _cwd_encodes_to(
        "/Users/x/Documents/Apps/commons/.claude-worktrees/hidden-bouncing-stallman",
        "-Users-x-Documents-Apps-commons--claude-worktrees-hidden-bouncing-stallman",
    )


def test_underscore_path_matches():
    assert _cwd_encodes_to("/Users/x/Papers/a_b", "-Users-x-Papers-a-b")


def test_dot_preserving_encoder_dir_still_matches():
    """Some real dirs keep the dot verbatim — only the slash-only branch matches.

    Claude Code has shipped two encoders and one machine holds dirs from both.
    This pins the shape the regex branch REJECTS, so deleting the slash-only
    branch as "dead code" fails here instead of silently reintroducing the
    fragment-folder bug this module exists to prevent.
    """
    assert _cwd_encodes_to(
        "/Users/x/Documents/Apps/mcp/alcor-tmux/.alcor-v2",
        "-Users-x-Documents-Apps-mcp-alcor-tmux-.alcor-v2",
    )
    # ...and the regex branch alone would not have matched it.
    import re as _re
    assert _re.sub(r"[^A-Za-z0-9-]", "-", "/Users/x/Documents/Apps/mcp/alcor-tmux/.alcor-v2") != \
        "-Users-x-Documents-Apps-mcp-alcor-tmux-.alcor-v2"


def test_non_ascii_path_accepted_under_both_encoder_shapes():
    """Non-ASCII cwds: we accept BOTH candidate encodings, deliberately.

    No dir on the audited machine carries a non-ASCII character, so which
    shape Claude Code emits for one is unverified — we have the two encoder
    generations documented in `_cwd_encodes_to` and no sample that
    discriminates between them for a character outside ``[A-Za-z0-9-]`` that
    is also not ``/``. Rather than guess, both branches answer:

    - the slash-only branch passes ``é`` through verbatim, matching a dir that
      preserves it;
    - the regex branch folds ``é`` into ``-`` (it is outside the class),
      matching a dir that transliterates it.

    Accepting both is the safe side of the asymmetry. A false negative costs a
    fallback to the lossy slug — a fragment folder, visible to
    ``memex check --folders`` and repairable with ``memex obs reassign``. A
    false positive silently binds a session to the wrong project, which
    nothing surfaces. Widening acceptance here cannot produce the latter:
    every accepted string is an encoding of the cwd actually recorded in the
    transcript under one of the two real encoders.

    This test pins current behaviour. If a real non-ASCII dir ever turns up
    and settles the question, narrow to the observed shape and say so here.
    """
    cwd = "/Users/x/Documents/Café/proj"
    assert _cwd_encodes_to(cwd, "-Users-x-Documents-Café-proj")   # verbatim
    assert _cwd_encodes_to(cwd, "-Users-x-Documents-Caf--proj")   # transliterated


def test_non_ascii_path_does_not_match_a_different_project():
    """Widened acceptance is not a wildcard — a wrong cwd is still rejected."""
    assert not _cwd_encodes_to(
        "/Users/x/Documents/Café/proj", "-Users-x-Documents-Bar-proj"
    )
    # ...and the fold is not so lossy that any accented name matches any other.
    assert not _cwd_encodes_to(
        "/Users/x/Documents/Café/proj", "-Users-x-Documents-Süd--proj"
    )


def test_unrelated_path_still_rejected():
    assert not _cwd_encodes_to("/Users/x/somewhere/else", "-Users-x--slock-agents-abc123")


def test_trailing_slash_stripped():
    assert _cwd_encodes_to("/Users/x/.slock/agents/abc123/", "-Users-x--slock-agents-abc123")


# --- integration through cwd_from_session ------------------------------------

def test_cwd_from_session_recovers_dot_path(tmp_path: Path) -> None:
    d = _session_dir(tmp_path, "-Users-x--slock-agents-abc123", [
        {"type": "user", "cwd": "/Users/x/.slock/agents/abc123"},
    ])
    assert cwd_from_session(d) == "/Users/x/.slock/agents/abc123"


def test_cwd_from_session_still_rejects_mismatch(tmp_path: Path) -> None:
    d = _session_dir(tmp_path, "-Users-x--slock-agents-abc123", [
        {"type": "user", "cwd": "/Users/x/Documents/other"},
    ])
    assert cwd_from_session(d) is None
