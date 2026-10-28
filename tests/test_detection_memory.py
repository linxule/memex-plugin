"""A repo keeps its project when its remote changes (v0.22.0 B2).

On 2026-10-22 a repo split repointed a checkout's origin to a new
``<name>-internal`` repo, and every later session from that checkout minted a
new folder. The first git-rule resolution of a repo is now recorded in
``<state_dir>/cwd-projects.json`` (keyed by git root) and wins over a later,
different git answer with rule ``remembered``.

Every test runs against a throwaway HOME, vault and state dir.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from memex import config as memex_config
from memex.routing import resolve_session_project
from memex.scripts import utils
from memex.scripts.utils import CWD_MEMORY_FILENAME, detect_project, detect_project_explained

REPO = "/Users/x/Documents/Papers/alpha"
OTHER_REPO = "/Users/x/Documents/Apps/arena"


@pytest.fixture
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    state = tmp_path / "state"
    for d in (home / ".memex", vault / "projects", state):
        d.mkdir(parents=True, exist_ok=True)
    for name in ("alpha", "arena"):
        (vault / "projects" / name).mkdir()
    for key in list(os.environ):
        if key.startswith("MEMEX_") or key == "CLAUDE_PLUGIN_ROOT":
            monkeypatch.delenv(key)
    monkeypatch.setenv("HOME", str(home))
    (home / ".memex" / "config.json").write_text(json.dumps({
        "memex_path": str(vault), "state_dir": str(state), "embeddings": {"enabled": False},
    }))
    memex_config.reset_settings()
    utils._project_names_in.cache_clear()

    # Synthetic repos: origin per git root, mutable between calls.
    remotes = {REPO: "alpha", OTHER_REPO: "arena"}

    def root_of(p) -> str | None:
        s = str(p)
        return next((r for r in remotes if s == r or s.startswith(r + "/")), None)

    def git_project(p):
        root = root_of(p)
        return remotes[root] if root else None

    def git_root(p):
        root = root_of(p)
        return Path(root) if root else None

    monkeypatch.setattr(utils, "get_git_project", git_project)
    monkeypatch.setattr(utils, "get_git_root", git_root)
    yield SimpleNamespace(home=home, vault=vault, state=state, remotes=remotes,
                          memory=state / CWD_MEMORY_FILENAME)
    memex_config.reset_settings()
    utils._project_names_in.cache_clear()


def _memory(box) -> dict:
    return json.loads(box.memory.read_text())["cwds"]


def test_first_git_resolution_is_recorded_by_git_root(box) -> None:
    assert detect_project_explained(REPO) == ("alpha", "git-remote")
    mem = _memory(box)
    assert mem[REPO]["project"] == "alpha"
    assert mem[REPO]["rule"] == "git-remote"
    assert mem[REPO]["first_seen"]
    # A second repo gets its own entry; the first is untouched.
    assert detect_project_explained(OTHER_REPO) == ("arena", "git-remote")
    mem = _memory(box)
    assert {REPO, OTHER_REPO} <= set(mem)
    assert mem[REPO]["project"] == "alpha"


def test_changed_remote_returns_the_remembered_project(box) -> None:
    assert detect_project(REPO) == "alpha"
    box.remotes[REPO] = "alpha-internal"
    assert detect_project_explained(REPO) == ("alpha", "remembered")
    assert detect_project(REPO) == "alpha"
    # A subdirectory shares the git root, so it is remembered too.
    assert detect_project_explained(f"{REPO}/paper/sections") == ("alpha", "remembered")
    # The other repo is unaffected by this one's memory.
    assert detect_project_explained(OTHER_REPO) == ("arena", "git-remote")
    # Memory is not rewritten by the new remote.
    assert _memory(box)[REPO]["project"] == "alpha"


def test_unchanged_remote_reports_the_git_rule(box) -> None:
    detect_project(REPO)
    assert detect_project_explained(REPO) == ("alpha", "git-remote")


def test_routing_reports_remembered(box) -> None:
    detect_project(REPO)
    box.remotes[REPO] = "alpha-internal"
    route = resolve_session_project(REPO, {}, config={})
    assert (route.project, route.rule, route.is_guess) == ("alpha", "remembered", False)


def test_git_root_rule_is_remembered_when_a_remote_appears(box, monkeypatch) -> None:
    box.remotes.clear()
    local = "/Users/x/Code/arena"
    monkeypatch.setattr(utils, "get_git_project", lambda p: box.remotes.get(local))
    monkeypatch.setattr(utils, "get_git_root", lambda p: Path(local) if str(p).startswith(local) else None)
    assert detect_project_explained(local) == ("arena", "git-root")
    assert _memory(box)[local]["rule"] == "git-root"
    box.remotes[local] = "arena-site"
    assert detect_project_explained(local) == ("arena", "remembered")


def test_pin_beats_memory(box, monkeypatch) -> None:
    detect_project(REPO)
    box.remotes[REPO] = "alpha-internal"
    monkeypatch.setattr(utils, "get_config", lambda: {"project_mappings": {"Papers/alpha": "pinned"}})
    assert detect_project_explained(REPO) == ("pinned", "pin")


def test_remembered_folder_gone_from_vault_yields_to_git(box) -> None:
    detect_project(REPO)
    box.remotes[REPO] = "alpha-internal"
    # The owner moved the folder away on purpose: memory is stale, not truth.
    shutil.rmtree(box.vault / "projects" / "alpha")
    utils._project_names_in.cache_clear()
    assert detect_project_explained(REPO) == ("alpha-internal", "git-remote")
    assert _memory(box)[REPO]["project"] == "alpha-internal"


def test_memory_is_honoured_when_the_vault_is_unavailable(box) -> None:
    detect_project(REPO)
    box.remotes[REPO] = "alpha-internal"
    shutil.rmtree(box.vault / "projects")
    utils._project_names_in.cache_clear()
    assert detect_project_explained(REPO) == ("alpha", "remembered")


def test_temp_and_scratchpad_cwds_are_never_recorded(box, monkeypatch) -> None:
    monkeypatch.setattr(utils, "get_git_project", lambda p: "alpha")
    monkeypatch.setattr(utils, "get_git_root", lambda p: Path(str(p)))
    scratch = ("/private/tmp/claude-501/-Users-x-Documents-Apps-gone/"
               "0b4189ab-b782-47d4-9426-ff74a3cae698/scratchpad/wt")
    (box.home / ".claude" / "projects").mkdir(parents=True)
    utils.cwd_from_session.cache_clear()
    assert detect_project_explained("/tmp/tmpab12") == ("alpha", "git-remote")
    assert detect_project_explained(scratch) == ("alpha", "git-remote")
    assert not box.memory.exists()
    # …while an ordinary repo is.
    detect_project("/Users/x/Code/alpha")
    assert list(_memory(box)) == ["/Users/x/Code/alpha"]


def test_corrupt_memory_is_replaced_not_fatal(box) -> None:
    box.memory.write_text("{not json")
    assert detect_project_explained(REPO) == ("alpha", "git-remote")
    assert _memory(box)[REPO]["project"] == "alpha"


def test_malformed_entry_counts_as_absent(box) -> None:
    box.memory.write_text(json.dumps({"version": 1, "cwds": {REPO: "alpha", OTHER_REPO: {"project": ""}}}))
    assert detect_project_explained(REPO) == ("alpha", "git-remote")
    assert detect_project_explained(OTHER_REPO) == ("arena", "git-remote")
    mem = _memory(box)
    assert mem[REPO]["project"] == "alpha" and mem[OTHER_REPO]["project"] == "arena"


def test_lock_timeout_is_best_effort(box, monkeypatch) -> None:
    from filelock import Timeout

    class Busy:
        def __init__(self, path, timeout):
            self.path = path

        def __enter__(self):
            raise Timeout(self.path)

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(utils, "FileLock", Busy)
    assert detect_project_explained(REPO) == ("alpha", "git-remote")
    assert not box.memory.exists()


def test_write_is_atomic_and_leaves_no_temp_files(box) -> None:
    detect_project(REPO)
    detect_project(OTHER_REPO)
    leftovers = [p.name for p in box.state.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    assert json.loads(box.memory.read_text())["version"] == 1


def _entry(project: str, first: str, last: str | None = None) -> dict:
    e = {"project": project, "rule": "git-remote", "first_seen": first}
    if last:
        e["last_seen"] = last
    return e


def test_memory_is_bounded_least_recently_seen_first(box, monkeypatch) -> None:
    monkeypatch.setattr(utils, "_CWD_MEMORY_MAX", 3)
    box.remotes.update({"/w/old-but-used": "arena", "/w/new-but-idle": "arena", "/w/stale": "arena",
                        "/w/fresh": "arena"})
    box.memory.write_text(json.dumps({"version": 1, "cwds": {
        # Recorded long ago but seen recently: the long-lived repo B2 protects.
        "/w/old-but-used": _entry("arena", "2000-01-01T00:00:00", "2099-01-01T00:00:00"),
        "/w/new-but-idle": _entry("arena", "2001-01-01T00:00:00", "2001-02-01T00:00:00"),
        "/w/stale": _entry("arena", "2000-06-01T00:00:00"),
    }}))
    detect_project("/w/fresh")
    assert set(_memory(box)) == {"/w/old-but-used", "/w/new-but-idle", "/w/fresh"}


def test_a_hit_refreshes_last_seen_at_most_weekly(box) -> None:
    box.memory.write_text(json.dumps({"version": 1, "cwds": {
        REPO: _entry("alpha", "2000-01-01T00:00:00", "2000-01-02T00:00:00"),
    }}))
    detect_project(REPO)
    seen = _memory(box)[REPO]["last_seen"]
    assert seen > "2000-01-02T00:00:00"
    assert _memory(box)[REPO]["first_seen"] == "2000-01-01T00:00:00"   # still the first resolution
    before = box.memory.stat().st_mtime_ns
    detect_project(REPO)                                                 # fresh: no write
    assert box.memory.stat().st_mtime_ns == before


# --- the same thing with a real git repo ---------------------------------------

_REAL_GIT_PROJECT = utils.get_git_project
_REAL_GIT_ROOT = utils.get_git_root


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_real_repo_whose_origin_is_repointed(box, tmp_path, monkeypatch) -> None:
    # tmp_path is itself under a temp root; let the git rules see it.
    monkeypatch.setattr(utils, "_TEMP_ROOT_RE", re.compile(r"(?!)"))
    monkeypatch.setattr(utils, "get_git_project", _REAL_GIT_PROJECT)
    monkeypatch.setattr(utils, "get_git_root", _REAL_GIT_ROOT)
    repo = tmp_path / "checkout"
    (repo / "sub").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "remote", "add", "origin", "git@github.com:x/alpha.git")
    assert detect_project_explained(str(repo)) == ("alpha", "git-remote")
    _git(repo, "remote", "set-url", "origin", "git@github.com:x/alpha-internal.git")
    assert detect_project_explained(str(repo)) == ("alpha", "remembered")
    assert detect_project_explained(str(repo / "sub")) == ("alpha", "remembered")
    assert list(_memory(box)) == [str(repo.resolve())]
    assert _memory(box)[str(repo.resolve())]["repo_id"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_a_different_checkout_at_the_same_path_starts_over(box, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(utils, "_TEMP_ROOT_RE", re.compile(r"(?!)"))
    monkeypatch.setattr(utils, "get_git_project", _REAL_GIT_PROJECT)
    monkeypatch.setattr(utils, "get_git_root", _REAL_GIT_ROOT)
    repo = tmp_path / "checkout"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "remote", "add", "origin", "https://github.com/x/alpha.git")
    assert detect_project(str(repo)) == "alpha"
    # Deleted and re-cloned from an unrelated repo (the old one moved aside so
    # its .git inode cannot be reused for the new one).
    repo.rename(tmp_path / "moved-away")
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "remote", "add", "origin", "https://github.com/x/arena.git")
    assert detect_project_explained(str(repo)) == ("arena", "git-remote")
    assert _memory(box)[str(repo.resolve())]["project"] == "arena"
    # …and the new checkout is protected from here on.
    _git(repo, "remote", "set-url", "origin", "https://github.com/x/arena-site.git")
    assert detect_project_explained(str(repo)) == ("arena", "remembered")
