"""Session-scratchpad cwds resolve to their parent session's project.

Fleet workers launched from ``/private/tmp/claude-<uid>/<encoded-parent>/
<uuid>/scratchpad/<name>`` used to mint one project folder per worker leaf
(2026-09-30: audit, council, banks, filmtaste, filmtaste2, outside, plugsmoke,
taste, taste2 — all sdk-cli workers of llm-world or research-calls). The
encoded segment is the parent's ``~/.claude/projects/`` dir, and the uuid is
the owner session, whose transcript carries the true cwd.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from memex.scripts import utils
from memex.scripts.utils import (
    _SCRATCHPAD_CWD_RE,
    _SCRATCHPAD_DIR_RE,
    cwd_from_session,
    detect_project,
    project_names_for_claude_dir,
    scratchpad_parent_cwd,
)

UUID = "0b4189ab-b782-47d4-9426-ff74a3cae698"
UUID2 = "bc3ccd0d-daee-428f-9c6a-1a21ff99ab1b"
ENC = "-Users-x-Documents-Apps-llm-world"
PARENT = "/Users/x/Documents/Apps/llm-world"
SCRATCH = f"/private/tmp/claude-501/{ENC}/{UUID}/scratchpad"


@pytest.fixture(autouse=True)
def _clear_cache():
    cwd_from_session.cache_clear()
    project_names_for_claude_dir.cache_clear()
    yield
    cwd_from_session.cache_clear()
    project_names_for_claude_dir.cache_clear()


def _write_session(root: Path, enc: str, name: str, cwd: str) -> Path:
    d = root / enc
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{name}.jsonl"
    f.write_text(json.dumps({"type": "user", "cwd": cwd}) + "\n", encoding="utf-8")
    return f


def _projects_root(tmp_path: Path, enc: str = ENC, cwd: str = PARENT, name: str = UUID) -> Path:
    root = tmp_path / ".claude" / "projects"
    _write_session(root, enc, name, cwd)
    return root


@pytest.fixture
def _no_git_no_config(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(utils, "get_config", lambda: {"project_mappings": {}})
    monkeypatch.setattr(utils, "get_git_project", lambda p: None)
    monkeypatch.setattr(utils, "get_git_root", lambda p: None)


# --- patterns ----------------------------------------------------------------

@pytest.mark.parametrize("cwd", [
    f"{SCRATCH}/banks",
    f"{SCRATCH}/probe/cc_work",
    f"{SCRATCH}/wt/studio-1",
    SCRATCH,
    f"/tmp/claude-501/{ENC}/{UUID}/scratchpad/banks",
    f"/private/tmp/claude-0/{ENC}/{UUID}/scratchpad/x",
    f"/var/folders/93/abc/T/claude-501/{ENC}/{UUID}/scratchpad/x",   # relocated temp root
])
def test_pattern_matches_session_scratchpads(cwd: str) -> None:
    m = _SCRATCHPAD_CWD_RE.match(cwd)
    assert m and m.group("enc") == ENC and m.group("uuid") == UUID


@pytest.mark.parametrize("cwd", [
    f"/private/tmp/claude-501/{ENC}/{UUID}/other/banks",      # not a scratchpad
    f"/private/tmp/claude-501/{ENC}/not-a-uuid/scratchpad/x",   # no session uuid
    f"/private/tmp/claude-501/{ENC}/{UUID}/scratchpadx/y",      # prefix, not segment
    f"/private/tmp/claude-abc/{ENC}/{UUID}/scratchpad/x",       # uid is numeric
    f"/private/tmp/claude-501/Users-x-Apps/{UUID}/scratchpad/x",  # enc dirs start with '-'
    f"/private/tmp/ncs-open/{ENC}/{UUID}/scratchpad/x",          # other tmp layout
    PARENT,
])
def test_pattern_rejects_non_scratchpads(cwd: str) -> None:
    assert _SCRATCHPAD_CWD_RE.match(cwd) is None


def test_dir_pattern_recovers_owner_from_encoded_scratchpad_dir() -> None:
    name = f"-private-tmp-claude-501-{ENC}-{UUID2}-scratchpad-wt-studio-1"
    m = _SCRATCHPAD_DIR_RE.match(name)
    assert m and m.group("enc") == ENC and m.group("uuid") == UUID2
    assert _SCRATCHPAD_DIR_RE.match(f"-private-tmp-claude-501-{ENC}-{UUID2}-scratchpad")
    assert _SCRATCHPAD_DIR_RE.match(ENC) is None
    assert _SCRATCHPAD_DIR_RE.match(f"-private-tmp-claude-501-{ENC}-{UUID2}-other") is None


# --- scratchpad_parent_cwd ---------------------------------------------------

def test_parent_cwd_read_from_owner_transcript(tmp_path: Path) -> None:
    root = _projects_root(tmp_path)
    assert scratchpad_parent_cwd(f"{SCRATCH}/banks", root) == PARENT


def test_owner_transcript_wins_over_newest_under_encoding_collision(tmp_path: Path) -> None:
    # "Research Calls" and "Research-Calls" encode to the same dir name; both
    # validate. The owner uuid's own transcript is exact; newest-first is not.
    enc = "-Users-x-Documents-Research-Calls"
    root = tmp_path / ".claude" / "projects"
    _write_session(root, enc, UUID, "/Users/x/Documents/Research Calls")
    newer = _write_session(root, enc, "ffffffff-0000-4000-8000-000000000000", "/Users/x/Documents/Research-Calls")
    import os, time
    t = time.time() + 100
    os.utime(newer, (t, t))
    cwd = f"/private/tmp/claude-501/{enc}/{UUID}/scratchpad/audit"
    assert scratchpad_parent_cwd(cwd, root) == "/Users/x/Documents/Research Calls"


def test_parent_cwd_falls_back_to_dir_scan_when_owner_transcript_gone(tmp_path: Path) -> None:
    root = _projects_root(tmp_path, name="other-session")
    assert scratchpad_parent_cwd(f"{SCRATCH}/banks", root) == PARENT


def test_parent_cwd_none_when_parent_dir_missing(tmp_path: Path) -> None:
    root = tmp_path / ".claude" / "projects"
    root.mkdir(parents=True)
    assert scratchpad_parent_cwd(f"{SCRATCH}/banks", root) is None


def test_parent_cwd_none_when_recorded_cwd_does_not_encode_to_dir(tmp_path: Path) -> None:
    # Validation: a stray tool-recorded path can't mis-map, in either read path.
    root = _projects_root(tmp_path, cwd="/Users/x/Documents/Apps/other")
    _write_session(root, ENC, "second", "/Users/x/Documents/Apps/other")
    assert scratchpad_parent_cwd(f"{SCRATCH}/banks", root) is None


def test_parent_cwd_none_for_non_scratchpad(tmp_path: Path) -> None:
    root = _projects_root(tmp_path)
    assert scratchpad_parent_cwd(PARENT, root) is None


def test_parent_cwd_defaults_to_home_claude_projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _projects_root(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert scratchpad_parent_cwd(f"{SCRATCH}/banks") == PARENT


# --- detect_project end to end ----------------------------------------------

@pytest.mark.usefixtures("_no_git_no_config")
def test_detect_project_routes_scratchpad_worker_to_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _projects_root(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert detect_project(f"{SCRATCH}/banks") == "llm-world"
    assert detect_project(f"{SCRATCH}/probe/cc_work") == "llm-world"


@pytest.mark.usefixtures("_no_git_no_config")
def test_detect_project_falls_back_to_leaf_without_parent_transcript(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert detect_project(f"{SCRATCH}/banks") == "banks"
    # …but the scratchpad root itself never becomes a project…
    assert detect_project(SCRATCH) == "_uncategorized"
    # …while a real directory that merely happens to be named "scratchpad"
    # outside the session layout is an ordinary project (Codex round-2 nit).
    assert detect_project("/Users/x/Documents/Apps/scratchpad") == "scratchpad"


def test_explicit_mapping_beats_scratchpad_routing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _projects_root(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(utils, "get_config", lambda: {"project_mappings": {"/scratchpad/banks": "pinned"}})
    monkeypatch.setattr(utils, "get_git_project", lambda p: None)
    monkeypatch.setattr(utils, "get_git_root", lambda p: None)
    assert detect_project(f"{SCRATCH}/banks") == "pinned"


@pytest.mark.usefixtures("_no_git_no_config")
def test_parent_mapping_applies_after_routing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The recursive call runs the full priority list on the parent cwd, so a pin
    # on the PARENT path (the common case) still names the project.
    enc = "-Users-x-Documents-linxule-Research-Calls"
    _projects_root(tmp_path, enc=enc, cwd="/Users/x/Documents/linxule/Research Calls")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(utils, "get_config", lambda: {
        "project_mappings": {"/Users/x/Documents/linxule/Research Calls": "research-calls"}})
    cwd = f"/private/tmp/claude-501/{enc}/{UUID}/scratchpad/audit"
    assert detect_project(cwd) == "research-calls"


@pytest.mark.usefixtures("_no_git_no_config")
def test_scratchpad_routing_runs_before_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A worker's checkout of some other repo inside the scratchpad belongs to
    # the session that spawned it, not to that checkout's remote.
    _projects_root(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(utils, "get_git_project",
                        lambda p: "arena" if "/scratchpad/" in str(p) else None)
    assert detect_project(f"{SCRATCH}/wt/arena-clone") == "llm-world"


@pytest.mark.usefixtures("_no_git_no_config")
def test_nested_scratchpads_resolve_two_hops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A parent whose recorded cwd is itself a scratchpad (nested fleets) hops
    # again; a one-hop implementation would return "wt".
    root = tmp_path / ".claude" / "projects"
    inner_enc = f"-private-tmp-claude-501-{ENC}-{UUID}-scratchpad-wt"
    _write_session(root, inner_enc, UUID2, f"{SCRATCH}/wt")
    _write_session(root, ENC, UUID, PARENT)
    monkeypatch.setenv("HOME", str(tmp_path))
    nested = f"/private/tmp/claude-501/{inner_enc}/{UUID2}/scratchpad/studio-1"
    assert detect_project(nested) == "llm-world"


@pytest.mark.usefixtures("_no_git_no_config")
def test_endless_parent_chain_terminates_at_depth_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    # Unreachable through validated data (each hop's encoded name is strictly
    # shorter), so force it: every "parent" is a fresh scratchpad.
    calls: list[str] = []

    def endless(cwd: str, claude_projects=None):
        calls.append(cwd)
        return f"/private/tmp/claude-501/-Users-x-h{len(calls)}/{UUID}/scratchpad/leaf{len(calls)}"

    monkeypatch.setattr(utils, "scratchpad_parent_cwd", endless)
    assert detect_project(f"{SCRATCH}/leaf0") == "leaf3"
    assert len(calls) == 3


# --- project_names_for_claude_dir (slug fallback) ---------------------------

@pytest.mark.usefixtures("_no_git_no_config")
def test_claude_dir_of_transcriptless_scratchpad_session_routes_via_owner(tmp_path: Path) -> None:
    # Codex review 2026-09-30: a real `…-scratchpad-site-test` dir with memory
    # but no transcripts slugged to `private-tmp-claude-501--Users-xulelin-…`.
    root = tmp_path / ".claude" / "projects"
    _write_session(root, ENC, UUID, PARENT)
    own = root / f"-private-tmp-claude-501-{ENC}-{UUID2}-scratchpad-site-test"
    (own / "memory").mkdir(parents=True)
    assert project_names_for_claude_dir(own) == ("llm-world", "llm-world")


def test_claude_dir_slug_fallback_unchanged_for_ordinary_dirs(tmp_path: Path) -> None:
    # The lossy slug parser is untouched for non-scratchpad dirs (it yields the
    # known cwd-fragment form here; that is the pre-existing behaviour).
    d = tmp_path / ".claude" / "projects" / "-Users-x-Documents-Apps-arena"
    d.mkdir(parents=True)
    expected = utils.claude_dir_to_project_name(d.name)
    assert project_names_for_claude_dir(d) == (expected, utils.sanitize_project_name(expected))
