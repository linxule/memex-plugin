"""Temp-root cwds route to an existing project, never a new folder (v0.22.0 B1).

Fleet seats run from mkdtemp dirs named after the launching project
(``/T/<project>-<purpose>-<random>``) or from a generic child of one
(``/tmp/<something>.<random>/repo``). The leaf rule minted one vault folder per
seat. Now a temp path resolves to the longest existing project whose name
prefixes a path component (separator required), else a remote naming an
existing project, else ``_uncategorized``.

Every test runs against a throwaway HOME + vault; nothing reads ~/.memex.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from memex import config as memex_config
from memex.routing import RULES
from memex.scripts import utils
from memex.scripts.utils import (
    _project_prefix_match,
    _temp_root_parts,
    _vault_project_names,
    detect_project,
    detect_project_explained,
)

PROJECTS = ("paper", "paperb", "alpha", "kimi", "kimi-plugin-cc", "claude")
UUID = "0b4189ab-b782-47d4-9426-ff74a3cae698"
ENC = "-Users-x-Documents-Apps-parentproj"
SCRATCH = f"/private/tmp/claude-501/{ENC}/{UUID}/scratchpad"
VF = "/private/var/folders/ab/cdef0123/T"


def _sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, config: dict | None = None):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    state = home / ".memex"
    for d in (vault / "projects", state):
        d.mkdir(parents=True, exist_ok=True)
    for key in list(os.environ):
        if key.startswith("MEMEX_") or key == "CLAUDE_PLUGIN_ROOT":
            monkeypatch.delenv(key)
    monkeypatch.setenv("HOME", str(home))
    cfg = {"memex_path": str(vault), "state_dir": str(state), "embeddings": {"enabled": False}}
    cfg.update(config or {})
    (state / "config.json").write_text(json.dumps(cfg))
    memex_config.reset_settings()
    utils._project_names_in.cache_clear()
    return SimpleNamespace(home=home, vault=vault, state=state)


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    box = _sandbox(tmp_path, monkeypatch)
    for name in PROJECTS:
        (box.vault / "projects" / name).mkdir()
    # Never shell out to git for these synthetic (non-existent) paths.
    monkeypatch.setattr(utils, "get_git_project", lambda p: None)
    monkeypatch.setattr(utils, "get_git_root", lambda p: None)
    yield box
    memex_config.reset_settings()
    utils._project_names_in.cache_clear()


# --- temp roots ---------------------------------------------------------------

@pytest.mark.parametrize(("path", "parts"), [
    ("/private/tmp/paperb-x", ["paperb-x"]),
    ("/tmp/paperb-x/repo", ["paperb-x", "repo"]),
    (f"{VF}/alpha-y", ["alpha-y"]),
    ("/var/folders/ab/cdef0123/T/alpha-y", ["alpha-y"]),
    ("/private/tmp", []),
    (f"{VF}/", []),
])
def test_temp_root_parts_accepts_both_spellings(path: str, parts: list[str]) -> None:
    assert _temp_root_parts(path) == parts


@pytest.mark.parametrize("path", [
    "/Users/x/tmp/paperb-x",                     # a folder named tmp elsewhere
    "/private/tmpx/paperb-x",                    # prefix, not a path segment
    "/private/var/folders/ab/cdef0123/C/x",      # the cache dir, not T
    "/private/var/folders/ab/T/x",               # too shallow for the T layout
    "/Users/x/Documents/Apps/paperb",
])
def test_temp_root_parts_rejects_non_temp(path: str) -> None:
    assert _temp_root_parts(path) is None


# --- prefix match --------------------------------------------------------------

@pytest.mark.parametrize(("name", "expected"), [
    ("paperb-v018-gemini-ci1pdz6a", "paperb"),   # longest: paperb over paper
    ("paper-review", "paper"),                   # the shorter name still owns its own prefix
    ("paperb", "paperb"),                        # exact
    ("paperb.Jl689Y", "paperb"),                 # mktemp -t style
    ("paperb_tmp", "paperb"),
    ("paperbx-foo", None),                       # no separator after any name
    ("signal-x", None),
    ("kimi-plugin-cc-release", "kimi-plugin-cc"),
    ("kimi-code-x", "kimi"),
    ("zzz-abc", None),
])
def test_prefix_match_prefers_longest_and_needs_separator(name: str, expected: str | None) -> None:
    assert _project_prefix_match(name, frozenset(PROJECTS)) == expected


@pytest.mark.parametrize("order", [PROJECTS, tuple(reversed(PROJECTS))])
def test_longest_match_does_not_depend_on_iteration_order(order) -> None:
    # Set order is hash-seeded; feed both fixed orders so "first match" fails.
    assert _project_prefix_match("paperb-v018-gemini-x", order) == "paperb"
    assert _project_prefix_match("kimi-plugin-cc-release", order) == "kimi-plugin-cc"


# --- detect_project on temp cwds ----------------------------------------------

@pytest.mark.parametrize(("cwd", "expected"), [
    (f"{VF}/paperb-v018-gemini-ci1pdz6a", ("paperb", "temp-prefix")),
    ("/tmp/alpha-direction-claude-cjpm6dd6", ("alpha", "temp-prefix")),
    ("/private/tmp/paper-review-k2", ("paper", "temp-prefix")),
    ("/tmp/alpha-x1/repo", ("alpha", "temp-prefix")),            # generic leaf → walk up
    ("/tmp/alpha-x1/wt/checkout", ("alpha", "temp-prefix")),     # two generic levels
    (f"{VF}/paperb-fixture-seat-_wp73tdk/docs", ("paperb", "temp-prefix")),  # a subdir of the seat dir
    (f"{VF}/kimi-plugin-cc-2.4/src", ("kimi-plugin-cc", "temp-prefix")),
    ("/tmp/zzz-abc", ("_uncategorized", "fallback")),                     # unknown → never a new folder
    ("/tmp/zzz-abc/repo", ("_uncategorized", "fallback")),
    (f"{VF}/tmpq8x0n2fe", ("_uncategorized", "fallback")),
    ("/tmp/claude-501/zzz", ("_uncategorized", "fallback")),             # claude-<uid> never captures
    ("/private/tmp", ("_uncategorized", "fallback")),
    (VF, ("_uncategorized", "fallback")),
])
def test_temp_cwd_routes_to_existing_project_or_uncategorized(sandbox, cwd: str, expected: tuple[str, str]) -> None:
    assert detect_project_explained(cwd) == expected
    assert detect_project(cwd) == expected[0]


def test_temp_cwd_never_mints_a_folder(sandbox) -> None:
    known = set(PROJECTS) | {"_uncategorized"}
    for cwd in ("/tmp/repo", "/tmp/a/b/c", f"{VF}/signa3-x", "/tmp/mcp-host.Jl689Y/repo", f"{VF}/T"):
        assert detect_project(cwd) in known, cwd


def test_non_temp_paths_keep_the_leaf_rule(sandbox) -> None:
    # Outside a temp root a leaf that merely starts with a project name is
    # still its own project (only temp paths are never projects).
    assert detect_project_explained("/Users/x/Documents/Apps/paperb-site") == ("paperb-site", "leaf")


def test_remote_naming_an_existing_project_wins_in_a_random_temp_dir(sandbox, monkeypatch) -> None:
    monkeypatch.setattr(utils, "get_git_project", lambda p: "paperb" if "tmpab12" in str(p) else "brand-new")
    assert detect_project_explained(f"{VF}/tmpab12") == ("paperb", "git-remote")
    # …but a remote naming no vault project is not allowed to mint one.
    assert detect_project_explained(f"{VF}/tmpcd34") == ("_uncategorized", "fallback")


def test_prefix_match_beats_the_remote_in_a_named_seat_dir(sandbox, monkeypatch) -> None:
    # A seat dir named for the launching project keeps it even when its
    # checkout's origin was repointed (the 10-22 split).
    monkeypatch.setattr(utils, "get_git_project", lambda p: "alpha-internal")
    assert detect_project_explained("/tmp/alpha-direction-claude-cjpm6dd6") == ("alpha", "temp-prefix")


def test_pin_beats_temp_prefix(sandbox, monkeypatch) -> None:
    monkeypatch.setattr(utils, "get_config", lambda: {"project_mappings": {"/paperb-v018-": "pinned"}})
    assert detect_project_explained(f"{VF}/paperb-v018-gemini-ci1pdz6a") == ("pinned", "pin")
    assert detect_project_explained(f"{VF}/paperb-fixture-seat-x") == ("paperb", "temp-prefix")


# --- the v0.20.2 scratchpad rule still wins under /private/tmp -------------------

def _owner_transcript(home: Path, cwd: str) -> None:
    d = home / ".claude" / "projects" / ENC
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{UUID}.jsonl").write_text(json.dumps({"type": "user", "cwd": cwd}) + "\n")


def test_scratchpad_worker_routes_to_its_owner_not_its_leaf_prefix(sandbox, monkeypatch) -> None:
    utils.cwd_from_session.cache_clear()
    owner = "/Users/x/Documents/Apps/parentproj"
    _owner_transcript(sandbox.home, owner)
    try:
        assert detect_project(f"{SCRATCH}/paperb-clone") == "parentproj"
        # The owner here resolves by its leaf (a guess): the guess shows through…
        assert detect_project_explained(f"{SCRATCH}/paperb-clone") == ("parentproj", "leaf")
        # …while a confidently named owner reports the scratchpad rule.
        monkeypatch.setattr(utils, "get_git_project", lambda p: "parentproj" if str(p) == owner else None)
        assert detect_project_explained(f"{SCRATCH}/paperb-clone") == ("parentproj", "scratchpad")
    finally:
        utils.cwd_from_session.cache_clear()


def test_unresolvable_scratchpad_keeps_v0202_fallthrough(sandbox) -> None:
    utils.cwd_from_session.cache_clear()
    (sandbox.home / ".claude" / "projects").mkdir(parents=True)
    # Owner transcript gone: the worker leaf falls through to the usual rules
    # (v0.20.2), the temp rule does not take over; the root stays nobody's.
    assert detect_project_explained(f"{SCRATCH}/banks") == ("banks", "leaf")
    assert detect_project_explained(SCRATCH) == ("_uncategorized", "fallback")


# --- the project list ----------------------------------------------------------

def test_project_list_skips_names_detection_cannot_produce(tmp_path, monkeypatch) -> None:
    box = _sandbox(tmp_path, monkeypatch)
    projects = box.vault / "projects"
    for name in ("paperb", "alpha", "_uncategorized", ".obsidian", "example.com", "tmp", "with space"):
        (projects / name).mkdir()
    (projects / "notes.md").write_text("x")
    try:
        assert _vault_project_names() == frozenset({"paperb", "alpha"})
    finally:
        utils._project_names_in.cache_clear()
        memex_config.reset_settings()


def test_project_list_is_cached_until_the_folder_changes(sandbox, monkeypatch) -> None:
    scans: list[str] = []
    real_scandir = os.scandir

    def counting_scandir(path):
        scans.append(str(path))
        return real_scandir(path)

    monkeypatch.setattr(utils.os, "scandir", counting_scandir)
    assert detect_project("/tmp/newproj-x") == "_uncategorized"
    assert detect_project("/tmp/alpha-x") == "alpha"
    assert len(scans) == 1                                    # cached while unchanged
    # A folder created mid-run (e.g. by the same `session import`) is seen.
    (sandbox.vault / "projects" / "newproj").mkdir()
    assert detect_project("/tmp/newproj-x") == "newproj"
    assert len(scans) == 2


def test_ancestor_match_yields_to_a_remote_naming_a_project(sandbox, monkeypatch) -> None:
    # (3) is weaker than (2): a clone inside some other seat dir keeps its remote.
    monkeypatch.setattr(utils, "get_git_project", lambda p: "paperb" if str(p).endswith("clone-of-it") else None)
    assert detect_project_explained("/tmp/kimi-seat-x/clone-of-it") == ("paperb", "git-remote")
    assert detect_project_explained("/tmp/kimi-seat-x/notes") == ("kimi", "temp-prefix")


@pytest.mark.parametrize("vault_setting", ["missing-dir", "unset"])
def test_missing_or_unconfigured_vault_is_tolerated(tmp_path, monkeypatch, vault_setting) -> None:
    box = _sandbox(tmp_path, monkeypatch)
    cfg = {"state_dir": str(box.state)}
    if vault_setting == "missing-dir":
        cfg["memex_path"] = str(tmp_path / "no-such-vault")
    (box.state / "config.json").write_text(json.dumps(cfg))
    memex_config.reset_settings()
    try:
        assert _vault_project_names() is None
        assert detect_project_explained("/tmp/paperb-x") == ("_uncategorized", "fallback")
    finally:
        utils._project_names_in.cache_clear()
        memex_config.reset_settings()


def test_hook_context_without_pydantic_reads_raw_config(sandbox, monkeypatch) -> None:
    # Hooks run under `uv run --script` without pydantic: memex.paths falls
    # back to the raw ~/.memex/config.json. Same answer.
    from memex import paths

    monkeypatch.setattr(paths, "_try_get_settings", lambda: None)
    utils._project_names_in.cache_clear()
    assert detect_project_explained(f"{VF}/paperb-v018-gemini-ci1pdz6a") == ("paperb", "temp-prefix")


# --- every rule is a RULES name, and the two entry points agree -------------------

def test_every_reported_rule_is_a_routing_rule(sandbox) -> None:
    for cwd in (f"{VF}/paperb-x", "/tmp/zzz", "/Users/x/Documents/Apps/leafy", "/", "/Users/x/Downloads"):
        assert detect_project_explained(cwd)[1] in RULES
