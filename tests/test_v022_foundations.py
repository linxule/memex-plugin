"""v0.22.0 wave 0: the shared pieces every unit builds on."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from memex.config import Settings
from memex.harness import HARNESSES, NormalizedSession, Turn, render_transcript_md
from memex.routing import ProjectRoute, resolve_session_project
from memex.scripts.utils import detect_project, detect_project_explained
from memex.session_memos import memo_for_session, memo_session_map, memo_stamps

SID = "7bd0af95-47cc-4338-bee5-8ec0ebb633c2"
OTHER = "83b7533e-1b62-42b2-9483-d3a643be3b95"


def _memo(vault: Path, project: str, name: str, fm: str, body: str = "# m\n") -> Path:
    p = vault / "projects" / project / "memos" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{fm.strip()}\n---\n\n{body}")
    return p


# --- session_memos ---------------------------------------------------------

def test_stamped_session_id_is_found(tmp_path):
    memo = _memo(tmp_path, "memex", "2026-10-28-a.md", f"type: memo\nsession_id: {SID}")
    assert memo_for_session(tmp_path, SID) == memo
    assert memo_for_session(tmp_path, SID.upper()) == memo


def test_session_ids_list_counts(tmp_path):
    memo = _memo(tmp_path, "alpha", "2026-10-22-b.md", f"type: memo\nsession_ids:\n  - {OTHER}\n  - {SID}")
    assert memo_for_session(tmp_path, SID) == memo
    assert memo_stamps({"session_ids": [SID, "nope", 3]}) == {SID}


def test_kimi_session_prefix_stamps_the_bare_uuid(tmp_path):
    memo = _memo(tmp_path, "kimi-plugin-cc", "2026-10-25-k.md", f"type: memo\nsession_id: session_{SID}")
    assert memo_for_session(tmp_path, SID) == memo
    assert memo_for_session(tmp_path, f"session_{SID}") == memo


def test_prefix_and_body_mentions_are_not_evidence(tmp_path):
    _memo(tmp_path, "memex", "a.md", "type: memo\nsession_id: 7bd0af95", body=f"mentions {SID} in the body\n")
    assert memo_for_session(tmp_path, SID) is None
    assert memo_for_session(tmp_path, SID[:8]) is None


def test_unparseable_frontmatter_is_not_evidence(tmp_path):
    _memo(tmp_path, "memex", "a.md", f"session_id: {SID}\nsession_id: {OTHER}")  # duplicate key
    assert memo_for_session(tmp_path, SID) is None


def test_project_orders_the_scan_but_any_project_counts(tmp_path):
    elsewhere = _memo(tmp_path, "teaching", "a.md", f"session_id: {SID}")
    assert memo_for_session(tmp_path, SID, project="catchall") == elsewhere
    here = _memo(tmp_path, "catchall", "z.md", f"session_id: {SID}")
    assert memo_for_session(tmp_path, SID, project="catchall") == here


def test_memos_in_subfolders_count(tmp_path):
    archived = _memo(tmp_path, "alpha", "archive/2026-10-01-old.md", f"session_id: {SID}")
    assert memo_for_session(tmp_path, SID, project="alpha") == archived
    assert memo_session_map(tmp_path)[SID] == ["projects/alpha/memos/archive/2026-10-01-old.md"]


def test_session_map_keeps_every_memo_of_a_session(tmp_path):
    _memo(tmp_path, "alpha", "2026-10-22-a.md", f"session_id: {SID}")
    _memo(tmp_path, "alpha", "2026-10-23-b.md", f"session_ids: [{SID}, {OTHER}]")
    m = memo_session_map(tmp_path)
    assert m[SID] == ["projects/alpha/memos/2026-10-22-a.md", "projects/alpha/memos/2026-10-23-b.md"]
    assert m[OTHER] == ["projects/alpha/memos/2026-10-23-b.md"]


# --- routing ---------------------------------------------------------------

def test_mapped_channel_wins_over_cwd():
    cfg = {"channel_mappings": {"chan_TESTCHAN00001": "teaching"}}
    route = resolve_session_project("/Users/x/Documents/catchall", {"relay_channel": "chan_TESTCHAN00001"}, config=cfg)
    assert route == ProjectRoute("teaching", "channel")
    assert not route.is_guess


def test_unmapped_channel_is_carried_and_flagged(tmp_path):
    route = resolve_session_project(str(tmp_path), {"relay_channel": "chan_new"}, config={})
    assert route.unmapped_channel == "chan_new"
    assert route.is_guess
    assert route.project == detect_project(str(tmp_path))


def test_no_cwd_falls_back():
    assert resolve_session_project("", None, config={}) == ProjectRoute("_uncategorized", "fallback")


def test_explained_project_matches_detect_project(tmp_path):
    assert detect_project_explained(str(tmp_path))[0] == detect_project(str(tmp_path))


# --- config ----------------------------------------------------------------

def test_new_settings_fields_have_safe_defaults(monkeypatch):
    monkeypatch.setattr(Settings, "_config_path", None)  # never merge the owner's real config
    # field defaults, not Settings(): that would read the owner's real ~/.memex/config.json
    fields = Settings.model_fields
    assert fields["channel_mappings"].default_factory() == {}
    assert fields["discover_ignore"].default_factory() == []
    assert fields["session_start_pointer"].default is True
    dumped = Settings(channel_mappings={"chan_x": "p"}).model_dump()
    assert dumped["channel_mappings"] == {"chan_x": "p"}  # declared, so not dropped by extra="ignore"


# --- harness ---------------------------------------------------------------

def _session(**kw) -> NormalizedSession:
    base = dict(
        harness="codex-desktop", session_id="019a1b2c-0000-7000-8000-000000000001", cwd='/Users/x/Papers/o"ther',
        turns=[Turn("user", "hi"), Turn("assistant", "hello"), Turn("user", "next")],
        source_files=[], started_at="2026-10-22T09:00:00", ended_at="2026-10-22T10:30:00",
        models=["gpt-6"], subagent_count=4,
    )
    base.update(kw)
    return NormalizedSession(**base)


def test_transcript_frontmatter_parses_and_carries_harness():
    md = render_transcript_md(_session(), project="alpha", has_memo=False)
    fm = yaml.safe_load(md.split("---\n")[1])
    assert fm["harness"] == "codex-desktop" and fm["type"] == "transcript"
    assert fm["turns"] == 2 and fm["duration_minutes"] == 90 and fm["subagents"] == 4
    assert fm["source_cwd"] == '/Users/x/Papers/o"ther' and fm["project"] == "alpha"
    assert fm["has_memo"] is False and str(fm["date"]) == "2026-10-22"
    assert "## Turn 2" in md


def test_unknown_harness_is_refused():
    with pytest.raises(ValueError):
        render_transcript_md(_session(harness="cursor"), project="p", has_memo=False)
    assert "claude-desktop-relay" in HARNESSES


# --- hook helper -----------------------------------------------------------

def test_hook_helper_isolates_home(hook_env):
    """A startup in a non-vault cwd injects nothing, and every write lands in the tmp home."""
    result = hook_env.run("session-start", {"session_id": SID, "source": "startup", "cwd": str(hook_env.home)})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
    assert list((hook_env.home / ".memex" / "logs").glob("plugin-*.log"))
