"""`memex session discover|import`: deliberate skips, short sessions, routing (v0.22.0 A4 + C2).

Everything runs against a throwaway HOME holding a fake ``~/.claude/projects``
tree and a tmp vault; nothing reads or writes the real ``~/.memex`` or vault.
Sessions are synthetic.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import memex.scripts.discover_sessions as ds

CATCH_ALL_CWD = "/Users/tester/Dropbox"  # a "place to stand": hosts many projects
NORMAL_CWD = "/Users/tester/Documents/Apps/arena"
EVAL_CWD = "/private/var/folders/zz/abc123/T/tmpabcdef"  # an eval-fleet mkdtemp

CHANNEL = "chan_TESTCHANNEL0000000001"
OTHER_CHANNEL = "chan_TESTCHANNEL0000000002"


def _encode(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]", "-", cwd.rstrip("/"))


@pytest.fixture
def world(tmp_path, monkeypatch):
    """HOME + config + vault + an (initially empty) Claude projects tree."""
    from memex import config as memex_config
    from memex.scripts import utils

    home = tmp_path / "home"
    vault = tmp_path / "vault"
    (home / ".memex").mkdir(parents=True)
    (vault / "projects").mkdir(parents=True)
    (vault / "topics").mkdir()
    projects = home / ".claude" / "projects"
    projects.mkdir(parents=True)

    class World:
        pass

    w = World()
    w.home, w.vault, w.projects = home, vault, projects

    def configure(**extra):
        cfg = {
            "memex_path": str(vault),
            "state_dir": str(home / ".memex"),
            "embeddings": {"enabled": False},
            "project_mappings": {CATCH_ALL_CWD: "catch-all"},
            **extra,
        }
        (home / ".memex" / "config.json").write_text(json.dumps(cfg))
        memex_config.reset_settings()

    w.configure = configure

    monkeypatch.setenv("HOME", str(home))
    for var in [v for v in list(__import__("os").environ) if v.startswith("MEMEX_")]:
        monkeypatch.delenv(var)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setattr(ds, "PROJECTS_DIR", projects)
    utils.cwd_from_session.cache_clear()
    utils.project_names_for_claude_dir.cache_clear()
    configure(discover_ignore=["/var/folders/*/T/tmp*"])
    yield w
    memex_config.reset_settings()
    utils.cwd_from_session.cache_clear()
    utils.project_names_for_claude_dir.cache_clear()


def write_session(
    world,
    cwd: str,
    sid: str,
    *,
    seconds: int = 120,
    turns: int = 8,
    first_text: str = "",
    timestamps: bool = True,
    preamble: int = 0,
) -> Path:
    """A convertible session of ``turns`` records spread over ``seconds`` seconds.

    ``preamble`` bookkeeping records (a resumed session's summaries) come first.
    """
    d = world.projects / _encode(cwd)
    d.mkdir(parents=True, exist_ok=True)
    t0 = datetime(2026, 10, 20, 10, 0, 0)
    lines = [json.dumps({"type": "summary", "summary": f"earlier work {i}"}) for i in range(preamble)]
    for i in range(turns):
        role = "user" if i % 2 == 0 else "assistant"
        text = (first_text + " " + "pad " * 400) if i == 0 else f"turn {i}"
        record = {
            "type": role,
            "cwd": cwd,
            "sessionId": sid,
            "message": {
                "role": role,
                "content": text if role == "user" else [{"type": "text", "text": text}],
            },
        }
        if timestamps:
            record["timestamp"] = (t0 + timedelta(seconds=seconds * i / max(1, turns - 1))).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        lines.append(json.dumps(record))
    path = d / f"{sid}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run_main(monkeypatch, capsys, *argv: str) -> str:
    monkeypatch.setattr(sys, "argv", ["discover_sessions.py", *argv])
    try:
        ds.main()
    except SystemExit as e:
        assert not e.code, f"discover exited {e.code}"
    return capsys.readouterr().out


def ids(sessions) -> set[str]:
    return {s["session_id"][:8] for s in sessions}


NORMAL = "11111111-aaaa-bbbb-cccc-000000000001"
EVAL_1 = "22222222-aaaa-bbbb-cccc-000000000002"
EVAL_2 = "22222223-aaaa-bbbb-cccc-000000000003"
HUSK = "33333333-aaaa-bbbb-cccc-000000000004"


# ── matching ────────────────────────────────────────────────────────

def test_match_substring_glob_and_private_alias():
    m = ds.match_discover_ignore
    assert m("/Users/t/work/eval-fleet/run1", ["eval-fleet"]) == "eval-fleet"          # substring
    assert m("/Users/t/work/other", ["eval-fleet"]) is None
    glob = "/var/folders/*/T/tmp*"
    assert m("/private/var/folders/ab/cd/T/tmpxyz", [glob]) == glob                    # alias: /private optional
    assert m("/var/folders/ab/cd/T/tmpxyz", [glob]) == glob
    assert m("/private/var/folders/ab/cd/T/other-leaf", [glob]) is None                # glob really anchors on tmp*
    assert m("/private/var/folders/ab/cd/T/tmpxyz", ["/private/var/folders/*/T/tmp*"]) is not None
    assert m("/var/folders/ab/cd/T/tmpxyz", ["/private/var/folders/*/T/tmp*"]) is not None
    assert m("/Users/t/a", ["[bad"]) is None                                           # malformed glob never raises
    assert m("/Users/t/Projects/[draft]/x", ["Projects/[draft]"]) == "Projects/[draft]"  # literal brackets still match as text
    assert m(None, ["x"]) is None and m("", ["x"]) is None


def test_blank_or_garbage_patterns_never_match_everything(world, monkeypatch):
    world.configure(discover_ignore=["", "   ", "eval-fleet"])
    assert ds.discover_ignore_patterns() == ["eval-fleet"]  # a blank substring would match every cwd
    assert ds.match_discover_ignore("/anything", []) is None
    # the config model rejects odd shapes, but the reader must not trust that
    monkeypatch.setattr(ds, "get_config", lambda: {"discover_ignore": ["", 7, None, " x "]})
    assert ds.discover_ignore_patterns() == ["x"]
    monkeypatch.setattr(ds, "get_config", lambda: {"discover_ignore": "not-a-list"})
    assert ds.discover_ignore_patterns() == []
    monkeypatch.setattr(ds, "get_config", lambda: {})
    assert ds.discover_ignore_patterns() == []


def test_tilde_patterns_expand(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert ds.match_discover_ignore(f"{tmp_path}/scratch/x", ["~/scratch"]) == "~/scratch"


# ── discovery ───────────────────────────────────────────────────────

def test_discover_hides_ignored_sessions_and_counts_them(world):
    write_session(world, NORMAL_CWD, NORMAL)
    write_session(world, EVAL_CWD, EVAL_1)
    write_session(world, EVAL_CWD, EVAL_2)

    stats: dict = {}
    found = ds.discover_unprocessed(stats=stats)
    assert ids(found) == {NORMAL[:8]}, "ignored eval-fleet sessions leaked into the listing"
    assert stats == {"ignored": 2, "short": 0}
    assert found[0]["cwd"] == NORMAL_CWD

    # control: the same tree without the ignore lists all three, so the config is what hid them
    world.configure(discover_ignore=[])
    stats = {}
    assert ids(ds.discover_unprocessed(stats=stats)) == {NORMAL[:8], EVAL_1[:8], EVAL_2[:8]}
    assert stats == {"ignored": 0, "short": 0}


def test_an_ignored_folder_never_answers_a_project_query_but_is_counted(world):
    write_session(world, NORMAL_CWD, NORMAL)
    write_session(world, EVAL_CWD, EVAL_1)
    stats: dict = {}
    assert ids(ds.discover_unprocessed(project_filter="arena", stats=stats)) == {NORMAL[:8]}
    assert stats["ignored"] == 0, "a filtered-out ignored folder is not 'hidden' from this query"
    stats = {}
    # Since v0.22.0 B1 an unknown leaf under a temp root resolves to _uncategorized,
    # not to the leaf name, so that is the project an ignored eval-fleet cwd answers to.
    assert ds.discover_unprocessed(project_filter="_uncategorized", stats=stats) == []
    assert stats["ignored"] == 1, "asking for an ignored project must say why nothing came back"


def test_report_says_how_many_sessions_the_ignore_hid(world, monkeypatch, capsys):
    write_session(world, NORMAL_CWD, NORMAL)
    write_session(world, EVAL_CWD, EVAL_1)
    out = run_main(monkeypatch, capsys)
    assert "Unprocessed sessions: 1" in out
    assert "1 session(s) hidden by discover_ignore" in out
    assert "tmpabcdef" not in out

    as_json = json.loads(run_main(monkeypatch, capsys, "--json"))
    assert [s["session_id"] for s in as_json] == [NORMAL], "--json must stay a plain list of sessions"


def test_all_projects_lists_everything_but_marks_ignored(world, monkeypatch, capsys):
    write_session(world, NORMAL_CWD, NORMAL)
    write_session(world, EVAL_CWD, EVAL_1)
    rows = {p["dir_name"]: p for p in json.loads(run_main(monkeypatch, capsys, "--all-projects", "--json"))}
    assert rows[_encode(EVAL_CWD)]["ignored"] is True
    assert rows[_encode(NORMAL_CWD)]["ignored"] is False
    text = run_main(monkeypatch, capsys, "--all-projects")
    assert text.count("(ignored: discover_ignore)") == 1


# ── import: never an ignored session, never a husk ──────────────────

def test_import_never_takes_an_ignored_session_even_when_handed_one(world):
    path = write_session(world, EVAL_CWD, EVAL_1)
    session = {
        "session_id": EVAL_1, "project_display": "x", "project_memex": "tmpabcdef",
        "source_path": str(path), "size_bytes": path.stat().st_size, "cwd": EVAL_CWD,
    }
    (result,) = ds.import_sessions([session], dry_run=False)
    assert result["status"] == "skipped"
    assert result["reason"] == "discover_ignore: /var/folders/*/T/tmp*"
    assert not list(world.vault.glob("projects/**/*.md"))


def test_short_sessions_are_neither_listed_nor_imported_even_with_a_permissive_min_score(
    world, monkeypatch, capsys
):
    write_session(world, NORMAL_CWD, NORMAL, seconds=120)
    short = write_session(world, NORMAL_CWD, HUSK, seconds=13, turns=10)
    # the husk would pass `--min-score 4` on its own: the duration rule is what stops it
    assert ds.triage_session(short)["score"] >= 4

    stats: dict = {}
    assert ids(ds.discover_unprocessed(stats=stats)) == {NORMAL[:8]}
    assert stats["short"] == 1

    listing = run_main(monkeypatch, capsys)
    assert "Unprocessed sessions: 1" in listing and HUSK[:8] not in listing
    assert "1 session(s) under 30s not listed" in listing

    dry = run_main(monkeypatch, capsys, "--triage", "--min-score", "4", "--import")
    assert "Would import 1 sessions" in dry and HUSK[:8] not in dry

    out = run_main(monkeypatch, capsys, "--triage", "--min-score", "4", "--import", "--apply")
    assert "Imported: 1" in out
    imported = sorted(p.name for p in world.vault.glob("projects/*/transcripts/*.md"))
    assert imported == [f"{NORMAL}.md"], imported


def test_short_session_result_carries_its_reason(world):
    path = write_session(world, NORMAL_CWD, HUSK, seconds=13)
    session = {
        "session_id": HUSK, "project_display": "arena", "project_memex": "arena",
        "source_path": str(path), "size_bytes": path.stat().st_size, "cwd": NORMAL_CWD,
    }
    (result,) = ds.import_sessions([session], dry_run=True)
    assert result["status"] == "skipped"
    assert result["reason"] == "short session (13s < 30s)"
    assert result["duration_seconds"] == 13.0


@pytest.mark.parametrize("seconds, status", [(29, "skipped"), (30, "would_import"), (31, "would_import")])
def test_the_30_second_boundary(world, seconds, status):
    path = write_session(world, NORMAL_CWD, NORMAL, seconds=seconds)
    session = {
        "session_id": NORMAL, "project_display": "arena", "project_memex": "arena",
        "source_path": str(path), "size_bytes": path.stat().st_size, "cwd": NORMAL_CWD,
    }
    (result,) = ds.import_sessions([session], dry_run=True)
    assert result["status"] == status, result


def test_a_session_without_timestamps_is_not_called_short(world):
    path = write_session(world, NORMAL_CWD, NORMAL, timestamps=False)
    assert ds.session_duration_seconds(path) is None
    session = {
        "session_id": NORMAL, "project_display": "arena", "project_memex": "arena",
        "source_path": str(path), "size_bytes": path.stat().st_size, "cwd": NORMAL_CWD,
    }
    (result,) = ds.import_sessions([session], dry_run=True)
    assert result["status"] == "would_import"


def test_duration_reads_the_tail_and_survives_one_huge_last_record(tmp_path):
    p = tmp_path / "s.jsonl"
    first = json.dumps({"type": "user", "timestamp": "2026-10-20T10:00:00Z", "message": {"content": "hi"}})
    # the last record is bigger than the tail window and carries its timestamp at the start
    huge = json.dumps({"timestamp": "2026-10-20T10:02:00Z", "type": "assistant", "blob": "x" * 400_000})
    p.write_text(first + "\n" + huge + "\n")
    assert ds.session_duration_seconds(p) == 120.0

    q = tmp_path / "t.jsonl"
    middle = "\n".join(
        json.dumps({"timestamp": f"2026-10-20T10:00:{i:02d}Z", "type": "user"}) for i in range(1, 50)
    )
    q.write_text(first + "\n" + middle + "\n")
    assert ds.session_duration_seconds(q) == 49.0
    assert ds.session_duration_seconds(tmp_path / "missing.jsonl") is None


def test_exclude_is_honoured_by_a_real_import(world, monkeypatch, capsys):
    other = "44444444-aaaa-bbbb-cccc-000000000005"
    write_session(world, NORMAL_CWD, NORMAL)
    write_session(world, NORMAL_CWD, other)
    out = run_main(monkeypatch, capsys, "--import", "--apply", "--exclude", NORMAL[:8])
    assert f"excluded: {NORMAL[:8]}" in out
    assert "Imported: 1" in out
    assert sorted(p.name for p in world.vault.glob("projects/*/transcripts/*.md")) == [f"{other}.md"]


# ── import: routing through resolve_session_project (C2) ────────────

@pytest.fixture
def relay_metadata(monkeypatch):
    """What the relay unit's ``extract_session_metadata`` adds: ``relay_channel`` from the first turn."""
    from memex.scripts import transcript_to_md as ttm

    original = ttm.extract_session_metadata

    def with_channel(messages):
        metadata = dict(original(messages))
        match = re.search(r"chan_[A-Za-z0-9]+", json.dumps(messages))
        if match:
            metadata["relay_channel"] = match.group(0)
        return metadata

    monkeypatch.setattr(ttm, "extract_session_metadata", with_channel)


def test_import_routes_relay_sessions_by_channel_and_others_by_cwd(world, relay_metadata, monkeypatch, capsys):
    world.configure(channel_mappings={CHANNEL: "radar-test"}, discover_ignore=[])
    relay = "55555555-aaaa-bbbb-cccc-000000000006"
    plain = "66666666-aaaa-bbbb-cccc-000000000007"
    unmapped = "77777777-aaaa-bbbb-cccc-000000000008"
    write_session(world, CATCH_ALL_CWD, relay, first_text=f'<relay from="coordinator"> <project id="{CHANNEL}">')
    write_session(world, CATCH_ALL_CWD, plain, first_text="an ordinary session in the catch-all cwd")
    write_session(world, CATCH_ALL_CWD, unmapped, first_text=f'<project id="{OTHER_CHANNEL}">')

    dry = run_main(monkeypatch, capsys, "--import")
    assert re.search(rf"{relay[:8]}.*-> radar-test", dry), dry
    assert re.search(rf"{plain[:8]}.*-> catch-all", dry), dry
    assert re.search(rf"{unmapped[:8]}.*-> catch-all", dry), dry
    assert f"unmapped relay channel {OTHER_CHANNEL} (1 session(s))" in dry
    assert CHANNEL not in dry.replace("-> radar-test", ""), "a mapped channel must not be reported as unmapped"
    assert not list(world.vault.glob("projects/**/*.md")), "dry run wrote files"

    out = run_main(monkeypatch, capsys, "--import", "--apply")
    assert "Imported: 3" in out
    assert (world.vault / "projects" / "radar-test" / "transcripts" / f"{relay}.md").exists()
    assert (world.vault / "projects" / "catch-all" / "transcripts" / f"{plain}.md").exists()
    assert (world.vault / "projects" / "catch-all" / "transcripts" / f"{unmapped}.md").exists()
    assert not (world.vault / "projects" / "catch-all" / "transcripts" / f"{relay}.md").exists()
    text = (world.vault / "projects" / "radar-test" / "transcripts" / f"{relay}.md").read_text()
    assert re.search(r"^project: radar-test$", text, re.M), text[:400]


def test_results_say_which_rule_routed_the_session(world, relay_metadata):
    world.configure(channel_mappings={CHANNEL: "radar-test"}, discover_ignore=[])
    relay = write_session(world, CATCH_ALL_CWD, "88888888-aaaa-bbbb-cccc-000000000009",
                          first_text=f'<project id="{CHANNEL}">')
    plain = write_session(world, CATCH_ALL_CWD, "99999999-aaaa-bbbb-cccc-00000000000a", first_text="plain")
    sessions = [
        {"session_id": p.stem, "project_display": "Dropbox", "project_memex": "catch-all",
         "source_path": str(p), "size_bytes": p.stat().st_size, "cwd": CATCH_ALL_CWD}
        for p in (relay, plain)
    ]
    by_id = {r["session_id"]: r for r in ds.import_sessions(sessions, dry_run=True)}
    assert by_id[relay.stem]["project_memex"] == "radar-test"
    assert by_id[relay.stem]["route_rule"] == "channel"
    assert by_id[relay.stem]["unmapped_channel"] is None
    assert by_id[plain.stem]["project_memex"] == "catch-all"
    assert by_id[plain.stem]["route_rule"] != "channel"


def test_same_cwd_different_channels_are_routed_separately(world, relay_metadata):
    """The per-run route cache is keyed by (cwd, channel), not by cwd alone."""
    world.configure(channel_mappings={CHANNEL: "radar-test", OTHER_CHANNEL: "teaching-test"}, discover_ignore=[])
    paths = [
        write_session(world, CATCH_ALL_CWD, f"{n}0000000-aaaa-bbbb-cccc-00000000000{n}",
                      first_text=f'<project id="{channel}">')
        for n, channel in ((1, CHANNEL), (2, OTHER_CHANNEL), (3, CHANNEL))
    ]
    sessions = [
        {"session_id": p.stem, "project_display": "Dropbox", "project_memex": "catch-all",
         "source_path": str(p), "size_bytes": p.stat().st_size, "cwd": CATCH_ALL_CWD}
        for p in paths
    ]
    routed = [r["project_memex"] for r in ds.import_sessions(sessions, dry_run=True)]
    assert routed == ["radar-test", "teaching-test", "radar-test"]


def test_the_channel_is_found_however_many_bookkeeping_records_come_first(world, relay_metadata):
    world.configure(channel_mappings={CHANNEL: "radar-test"}, discover_ignore=[])
    path = write_session(world, CATCH_ALL_CWD, NORMAL, first_text=f'<project id="{CHANNEL}">', preamble=150)
    session = {"session_id": NORMAL, "project_display": "Dropbox", "project_memex": "catch-all",
               "source_path": str(path), "size_bytes": path.stat().st_size, "cwd": CATCH_ALL_CWD}
    (result,) = ds.import_sessions([session], dry_run=True)
    assert result["project_memex"] == "radar-test" and result["route_rule"] == "channel"


def test_metadata_failure_falls_back_to_the_cwd_rules(world, monkeypatch):
    from memex.scripts import transcript_to_md as ttm

    world.configure(discover_ignore=[])

    def boom(messages):
        raise RuntimeError("metadata exploded")

    monkeypatch.setattr(ttm, "extract_session_metadata", boom)
    path = write_session(world, CATCH_ALL_CWD, NORMAL)
    session = {"session_id": NORMAL, "project_display": "Dropbox", "project_memex": "stale-name",
               "source_path": str(path), "size_bytes": path.stat().st_size, "cwd": CATCH_ALL_CWD}
    (result,) = ds.import_sessions([session], dry_run=True)
    assert result["status"] == "would_import"
    assert result["project_memex"] == "catch-all", "routing must still run on the cwd when metadata is unreadable"


def test_missing_cwd_in_the_session_dict_is_recovered_from_the_transcript(world):
    world.configure(discover_ignore=[])
    path = write_session(world, CATCH_ALL_CWD, NORMAL)
    session = {"session_id": NORMAL, "project_display": "x", "project_memex": "stale-name",
               "source_path": str(path), "size_bytes": path.stat().st_size}
    (result,) = ds.import_sessions([session], dry_run=True)
    assert result["project_memex"] == "catch-all"


def test_without_a_cwd_the_discovery_name_is_kept_not_downgraded(world):
    """No cwd anywhere -> the router can only say _uncategorized; discovery's slug name is better."""
    d = world.projects / "-Users-tester-Documents-Apps-lost"
    d.mkdir(parents=True)
    path = d / f"{NORMAL}.jsonl"
    path.write_text("\n".join(
        json.dumps({"type": "user" if i % 2 == 0 else "assistant", "sessionId": NORMAL,
                    "timestamp": f"2026-10-20T10:0{i}:00Z",
                    "message": {"role": "user", "content": "x" * 500}})
        for i in range(4)
    ) + "\n")
    session = {"session_id": NORMAL, "project_display": "lost", "project_memex": "slug-recovered",
               "source_path": str(path), "size_bytes": path.stat().st_size, "cwd": None}
    (result,) = ds.import_sessions([session], dry_run=True)
    assert result["status"] == "would_import"
    assert result["project_memex"] == "slug-recovered"
    assert result["route_rule"] == "fallback"
