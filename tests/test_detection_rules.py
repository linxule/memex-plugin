"""``detect_project_explained`` names the rule that decided (v0.22.0 B).

One parametrized case per cwd rule in ``memex.routing.RULES``; each asserts the
rule AND that ``detect_project(cwd) == detect_project_explained(cwd)[0]``.
Throwaway HOME, vault and state dir; git is simulated per case.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from memex import config as memex_config
from memex.routing import RULES
from memex.scripts import utils
from memex.scripts.utils import detect_project, detect_project_explained

UUID = "0b4189ab-b782-47d4-9426-ff74a3cae698"
ENC = "-Users-x-Documents-Apps-owner"
ENC_LEAF = "-Users-x-Documents-Apps-leafowner"
OWNER = "/Users/x/Documents/Apps/owner"
REPO = "/Users/x/Documents/Papers/alpha"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home, vault, state = tmp_path / "home", tmp_path / "vault", tmp_path / "state"
    for d in (home / ".memex", vault / "projects", state):
        d.mkdir(parents=True, exist_ok=True)
    for name in ("paperb", "alpha"):
        (vault / "projects" / name).mkdir()
    for enc, cwd in ((ENC, OWNER), (ENC_LEAF, "/Users/x/Documents/Apps/leafowner")):
        owner = home / ".claude" / "projects" / enc
        owner.mkdir(parents=True)
        (owner / f"{UUID}.jsonl").write_text(json.dumps({"cwd": cwd}) + "\n")
    for key in list(os.environ):
        if key.startswith("MEMEX_") or key == "CLAUDE_PLUGIN_ROOT":
            monkeypatch.delenv(key)
    monkeypatch.setenv("HOME", str(home))
    (home / ".memex" / "config.json").write_text(json.dumps({
        "memex_path": str(vault), "state_dir": str(state),
        "project_mappings": {"/Users/x/pinned-place": "pinned"},
    }))
    memex_config.reset_settings()
    utils._project_names_in.cache_clear()
    utils.cwd_from_session.cache_clear()
    remotes: dict[str, str | None] = {
        REPO: "alpha", OWNER: "owner", "/Users/x/Code/plain": None, "/Users/x/Documents": None,
    }
    monkeypatch.setattr(utils, "get_git_project", lambda p: remotes.get(str(p)))
    monkeypatch.setattr(utils, "get_git_root",
                        lambda p: Path(str(p)) if str(p) in remotes else None)
    yield remotes
    memex_config.reset_settings()
    utils._project_names_in.cache_clear()
    utils.cwd_from_session.cache_clear()


def _repointed(remotes) -> None:
    detect_project(REPO)
    remotes[REPO] = "alpha-internal"


@pytest.mark.parametrize(("cwd", "prepare", "expected"), [
    ("/Users/x/pinned-place/sub", None, ("pinned", "pin")),
    (f"/private/tmp/claude-501/{ENC}/{UUID}/scratchpad/banks", None, ("owner", "scratchpad")),
    # An owner named by a guess keeps the guess visible through the hop.
    (f"/private/tmp/claude-501/{ENC_LEAF}/{UUID}/scratchpad/banks", None, ("leafowner", "leaf")),
    ("/private/var/folders/ab/cd/T/paperb-v018-gemini-ci1pdz6a", None, ("paperb", "temp-prefix")),
    (REPO, _repointed, ("alpha", "remembered")),
    (REPO, None, ("alpha", "git-remote")),
    ("/Users/x/Code/plain", None, ("plain", "git-root")),
    ("/Users/x/Documents", None, ("_uncategorized", "fallback")),     # repo at a reserved name
    ("/Users/x/Documents/Apps/leafy", None, ("leafy", "leaf")),
    ("/Users/x/Downloads", None, ("_uncategorized", "fallback")),
    ("/tmp/zzz-unknown", None, ("_uncategorized", "fallback")),
    ("/", None, ("_uncategorized", "fallback")),
], ids=["pin", "scratchpad", "scratchpad-owner-guess", "temp-prefix", "remembered", "git-remote",
        "git-root", "reserved-git-root", "leaf", "reserved-leaf", "temp-unknown", "root"])
def test_rule_reported_and_entry_points_agree(env, cwd, prepare, expected) -> None:
    if prepare:
        prepare(env)
    explained = detect_project_explained(cwd)
    assert explained == expected
    assert explained[1] in RULES
    assert detect_project(cwd) == explained[0]


def test_every_cwd_rule_is_covered() -> None:
    covered = {"pin", "scratchpad", "temp-prefix", "remembered", "git-remote", "git-root", "leaf", "fallback"}
    # "channel" is decided in memex.routing; "detect" is the retired coarse answer.
    assert covered == set(RULES) - {"channel", "detect"}
