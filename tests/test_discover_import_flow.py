"""Tests for discover_sessions import-flow fixes (2026-08-05).

1. ``--triage --import`` used to display the triage report and ``return``
   without importing — the tool's own recommended command
   (``--triage --min-score=9 --import --apply``) silently never imported.
2. ``--exclude <session-id-prefix>`` skips sessions on import (for
   currently-running sessions whose transcripts are still growing).
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import memex.scripts.discover_sessions as ds


SESSION_ID = "aaaa1111-2222-3333-4444-555566667777"


@pytest.fixture()
def claude_env(tmp_path, monkeypatch):
    projects = tmp_path / "claude" / "projects"
    vault = tmp_path / "vault"
    (vault / "projects").mkdir(parents=True)
    d = projects / "-Users-x-Documents-Apps-arena"
    d.mkdir(parents=True)
    # Ten user turns so the session triages above 9 — the threshold the tool's
    # own recommended command uses. Without that the --min-score test would
    # pass for the wrong reason (filtered to empty, import branch never proven).
    lines = [
        json.dumps({
            "type": "user",
            "cwd": "/Users/x/Documents/Apps/arena",
            "sessionId": SESSION_ID,
            "timestamp": "2026-08-01T10:00:00Z",
            "message": {"role": "user", "content": "do the thing " + "x" * 4000},
        }),
    ] + [
        json.dumps({
            "type": "user",
            "cwd": "/Users/x/Documents/Apps/arena",
            "sessionId": SESSION_ID,
            "timestamp": f"2026-08-01T10:{i:02d}:00Z",
            "message": {"role": "user", "content": f"follow-up {i}"},
        })
        for i in range(1, 10)
    ] + [
        json.dumps({
            "type": "assistant",
            "cwd": "/Users/x/Documents/Apps/arena",
            "sessionId": SESSION_ID,
            "timestamp": "2026-08-01T10:11:00Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
        }),
    ]
    (d / f"{SESSION_ID}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(ds, "PROJECTS_DIR", projects)
    monkeypatch.setattr(ds, "get_memex_path", lambda: vault)
    # import_sessions does a scripts-dir sibling import (transcript_to_md) that
    # only resolves via the CLI shim's sys.path; the flow under test is what
    # REACHES it, so stub it faithfully to its dry-run return shape. Every key
    # is read straight off the session dict — a .get() with a default would let
    # the stub invent a value discover_unprocessed never produced, so a renamed
    # or missing key would pass here and fail in production.
    monkeypatch.setattr(ds, "import_sessions", lambda sessions, dry_run=True: [
        {"status": "would_import" if dry_run else "imported",
         "session_id": s["session_id"], "project_display": s["project_display"],
         "size_bytes": s["size_bytes"], "project_memex": s["project_memex"]}
        for s in sessions
    ])
    return projects


def _run_main(monkeypatch, capsys, argv):
    monkeypatch.setattr(sys, "argv", ["discover_sessions.py"] + argv)
    try:
        ds.main()
    except SystemExit as e:  # argparse/tool exits are fine if zero
        assert not e.code
    return capsys.readouterr().out


def test_triage_plus_import_reaches_import_branch(claude_env, monkeypatch, capsys):
    out = _run_main(monkeypatch, capsys, ["--triage", "--import"])
    assert "Would import" in out, f"triage+import never reached the import branch:\n{out}"


def test_recommended_command_imports(claude_env, monkeypatch, capsys):
    """`--triage --min-score=9 --import` — the exact string the report prints.

    This is the combination the bug hit: the triage branch returned before the
    import branch, so the command the tool recommends to its own user displayed
    scores and exited. Fixture triages above 9, so a non-empty import proves
    both the fall-through and the score filter.
    """
    out = _run_main(monkeypatch, capsys, ["--triage", "--min-score=9", "--import"])
    assert "Would import 1" in out, f"recommended command did not import:\n{out}"


def test_min_score_above_fixture_filters_everything_out(claude_env, monkeypatch, capsys):
    """The filter is real, not a rubber stamp that lets everything through."""
    out = _run_main(monkeypatch, capsys, ["--triage", "--min-score=999", "--import"])
    assert "Would import 0" in out


def test_triage_alone_still_reports(claude_env, monkeypatch, capsys):
    out = _run_main(monkeypatch, capsys, ["--triage"])
    assert "Triage results" in out
    assert "Would import" not in out


def _faithful_import_stub(monkeypatch):
    """Stub shaped like the real `import_sessions`, which spreads `**session`.

    The fixture's stub deliberately projects onto five keys; these tests are
    about what the *session dicts* carry into import, so they need the spread.
    """
    monkeypatch.setattr(ds, "import_sessions", lambda sessions, dry_run=True: [
        {**s, "status": "would_import" if dry_run else "imported"} for s in sessions
    ])


def test_triage_with_import_attaches_scores_without_filtering(claude_env, monkeypatch, capsys):
    """`--triage --import` (no --min-score) scores every session, drops none.

    Before v0.16.3 the import branch only triaged when `--min-score > 0`, so
    `--triage` alongside `--import` was inert: no scores anywhere, and the
    flag looked like it had been honoured.
    """
    _faithful_import_stub(monkeypatch)
    out = _run_main(monkeypatch, capsys, ["--triage", "--import", "--json"])
    results = json.loads(out)
    # `score`/`grade` are the discriminators for THIS release, verified by
    # running this test against the real v0.16.2 module: v0.16.2 already fell
    # through to the import branch, so `status` is present pre-fix too — it
    # only ever triaged when --min-score > 0, so score/grade were absent.
    # `status` stays as a cheap guard that the import branch ran at all.
    # Keep both; neither subsumes the other. (.get, not [...], so a checkout
    # older still fails on the assertion message rather than a KeyError.)
    assert results[0].get("status") == "would_import", "never reached the import branch"
    assert len(results) == 1, "triage must not filter without --min-score"
    assert isinstance(results[0].get("score"), int), "triage did not attach scores"
    assert results[0].get("grade")


def test_plain_import_stays_untriaged(claude_env, monkeypatch, capsys):
    """No `--triage`, no `--min-score` — unchanged behaviour, no scoring cost."""
    _faithful_import_stub(monkeypatch)
    results = json.loads(_run_main(monkeypatch, capsys, ["--import", "--json"]))
    assert len(results) == 1
    assert "score" not in results[0]


def test_triage_with_import_prints_score_in_report(claude_env, monkeypatch, capsys):
    """The human-readable import report surfaces the score it just computed."""
    _faithful_import_stub(monkeypatch)
    out = _run_main(monkeypatch, capsys, ["--triage", "--import"])
    assert "Would import 1" in out
    assert "score " in out, f"triage ran but no score reached the report:\n{out}"


def test_exclude_skips_session(claude_env, monkeypatch, capsys):
    out = _run_main(monkeypatch, capsys, ["--import", "--exclude", SESSION_ID[:8]])
    assert "excluded:" in out
    assert "Would import 0" in out


def test_exclude_prefix_only_matches_target(claude_env, monkeypatch, capsys):
    out = _run_main(monkeypatch, capsys, ["--import", "--exclude", "ffffffff"])
    assert "excluded:" not in out
    assert "Would import 1" in out


SESSION_ID_2 = "bbbb1111-2222-3333-4444-555566667777"


def _add_second_session(projects):
    """A second, unexcluded session — the survivor that makes absence legible."""
    d = projects / "-Users-x-Documents-Apps-arena"
    (d / f"{SESSION_ID_2}.jsonl").write_text("\n".join(
        json.dumps({
            "type": "user",
            "cwd": "/Users/x/Documents/Apps/arena",
            "sessionId": SESSION_ID_2,
            "timestamp": "2026-08-02T10:00:00Z",
            "message": {"role": "user", "content": "second session " + "y" * 2000},
        }) for _ in range(3)
    ) + "\n", encoding="utf-8")


def test_exclude_removes_only_the_matched_session(claude_env, monkeypatch, capsys):
    """Two sessions, one excluded — the survivor must survive.

    v0.16.3 replaced the O(n·m) `s not in excluded` dict-equality scan with a
    session_id set. Both implementations pass this, so it is a guard on the
    rewrite's result, not a proof of the rewrite: it pins that excluding one
    session of several leaves the others alone, which is the property a
    mis-written membership test would break.
    """
    _add_second_session(claude_env)
    out = _run_main(monkeypatch, capsys, ["--import", "--exclude", SESSION_ID[:8]])
    assert f"excluded: {SESSION_ID[:8]}" in out
    assert "Would import 1" in out
    assert SESSION_ID_2[:8] in out, f"the unmatched session was dropped too:\n{out}"


def test_exclude_under_json_emits_parseable_json(claude_env, monkeypatch, capsys):
    """`--exclude` used to print its notice to stdout ahead of `json.dumps`.

    That made `--import --exclude X --json` unparseable for the caller the
    flag exists to serve. Two sessions, not one: with only the excluded
    session present the expected result is `[]`, which is also what a
    completely broken fixture returns — so absence would prove nothing. The
    survivor makes it legible.
    """
    _faithful_import_stub(monkeypatch)
    _add_second_session(claude_env)
    out = _run_main(monkeypatch, capsys, ["--import", "--exclude", SESSION_ID[:8], "--json"])
    assert "excluded:" not in out
    results = json.loads(out)
    assert [r["session_id"] for r in results] == [SESSION_ID_2]


# ── v0.16.4: empty-husk sessions + scrubbed import writes ────────────────
#
# 3. Sessions holding only bookkeeping lines (file-history-snapshot etc.)
#    convert to None forever; import now marks them ``skipped_empty`` in
#    state.json so discover stops re-listing them (2026-08-25 audit found
#    109 husks re-listed on every run).
# 4. ``memex session import`` writes the .md as the SOLE vault artifact (no
#    sibling .jsonl), so that write is now routed through the scrubber.

HUSK_ID = "dddd9999-8888-7777-6666-555544443333"

# Captured at import time: the claude_env fixture monkeypatches
# ds.import_sessions with a stub, so tests that need the REAL function must
# hold a reference from before any fixture runs.
_REAL_IMPORT_SESSIONS = ds.import_sessions


def _write_husk(projects_dir: Path, session_id: str = HUSK_ID) -> Path:
    d = projects_dir / "-Users-x-Documents-Apps-arena"
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"type": "file-history-snapshot", "messageId": f"m{i}",
                    "snapshot": {"trackedFiles": ["a.py"], "pad": "x" * 200}})
        for i in range(8)
    ]
    p = d / f"{session_id}.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    """Point ~/.memex at tmp so mark_session_phase/load_state stay isolated."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def test_is_empty_husk_detection(tmp_path):
    husk = tmp_path / "husk.jsonl"
    husk.write_text(json.dumps({"type": "file-history-snapshot"}) + "\n")
    real = tmp_path / "real.jsonl"
    real.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
    assert ds._is_empty_husk(husk)
    assert not ds._is_empty_husk(real)


def test_import_marks_husk_skipped_empty(claude_env, isolated_state, monkeypatch, tmp_path):
    _write_husk(claude_env)
    real_import = _REAL_IMPORT_SESSIONS
    sessions = ds.discover_unprocessed()
    husks = [s for s in sessions if s["session_id"] == HUSK_ID]
    assert husks, "husk must be discovered before it is marked"

    results = real_import(husks, dry_run=False)
    assert results[0]["status"] == "skipped_empty", results[0]

    from memex.scripts.utils import is_session_processed
    assert is_session_processed(HUSK_ID, "skipped_empty")

    # Discover must now skip it.
    remaining = [s for s in ds.discover_unprocessed() if s["session_id"] == HUSK_ID]
    assert not remaining, "marked husk must not re-list as unprocessed"


def test_import_write_is_scrubbed(claude_env, isolated_state, monkeypatch):
    """A secret in the transcript must not survive into the imported .md.
    Fixture is runtime-concatenated so source bytes never carry the shape
    (see tests/test_scrub.py builder pattern)."""
    secret = "sk-ant" + "-api03-" + "A" * 56
    d = claude_env / "-Users-x-Documents-Apps-arena"
    sid = "cccc1111-2222-3333-4444-555566667777"
    (d / f"{sid}.jsonl").write_text("\n".join([
        json.dumps({"type": "user", "cwd": "/Users/x/Documents/Apps/arena",
                    "sessionId": sid, "timestamp": "2026-08-01T10:00:00Z",
                    "message": {"role": "user", "content": "key is " + secret + " " + "x" * 2000}}),
        json.dumps({"type": "assistant", "cwd": "/Users/x/Documents/Apps/arena",
                    "sessionId": sid, "timestamp": "2026-08-01T10:01:00Z",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "noted"}]}}),
    ]) + "\n", encoding="utf-8")

    real_import = _REAL_IMPORT_SESSIONS
    sessions = [s for s in ds.discover_unprocessed() if s["session_id"] == sid]
    assert sessions
    results = real_import(sessions, dry_run=False)
    assert results[0]["status"] == "imported", results[0]

    md = Path(results[0]["target"]).read_text()
    assert secret not in md, "import path must scrub secrets (sole vault artifact)"


def test_is_empty_husk_compact_json_format(tmp_path):
    """Real Claude Code JSONL is COMPACT (no space after colons) — round-2
    review verified 50k+ occurrences of '"type":"user"' in the live corpus and
    zero of the spaced form. The compact branch is the one guarding every real
    session against a permanent skipped_empty mark; pin it with a fixture that
    json.dumps defaults cannot produce."""
    compact = {"separators": (",", ":")}
    real = tmp_path / "real-compact.jsonl"
    real.write_text(json.dumps(
        {"type": "user", "message": {"content": "hi"}}, **compact) + "\n")
    husk = tmp_path / "husk-compact.jsonl"
    husk.write_text(json.dumps(
        {"type": "file-history-snapshot", "snapshot": {}}, **compact) + "\n")
    assert not ds._is_empty_husk(real), "compact real session must not read as husk"
    assert ds._is_empty_husk(husk)


def test_recommended_command_marks_husks_before_min_score_filter(
    claude_env, isolated_state, monkeypatch, capsys
):
    """The tool's own recommended command (--triage --min-score=9 --import
    --apply) must not drop husks before the marking pass: a husk triages to
    score 0, so marking has to happen pre-filter or the husk re-lists forever
    (round-2 finding; same shape as the v0.16.3 --triage --import fix)."""
    _write_husk(claude_env)
    out = _run_main(monkeypatch, capsys,
                    ["--triage", "--min-score=9", "--import", "--apply"])

    from memex.scripts.utils import is_session_processed
    assert is_session_processed(HUSK_ID, "skipped_empty"), (
        "husk must be marked even though min-score filters it out:\n" + out
    )
    remaining = [s for s in ds.discover_unprocessed()
                 if s["session_id"] == HUSK_ID]
    assert not remaining


def test_exclude_protects_live_husk_from_skipped_empty_mark(
    claude_env, isolated_state, monkeypatch, capsys
):
    """A just-started LIVE session can be >1KB of snapshot lines with no user
    turn yet — exactly what --exclude exists for. The husk pass must run
    AFTER exclusion, or the excluded live session gets permanently marked
    skipped_empty and discover never sees it again."""
    _write_husk(claude_env)  # stands in for a live session still warming up
    _run_main(monkeypatch, capsys,
              ["--import", "--apply", "--exclude", HUSK_ID[:8]])

    from memex.scripts.utils import is_session_processed
    assert not is_session_processed(HUSK_ID, "skipped_empty"), (
        "excluded (live) session must never be husk-marked"
    )
    # It must still be discoverable once the exclusion is lifted.
    assert [s for s in ds.discover_unprocessed() if s["session_id"] == HUSK_ID]
