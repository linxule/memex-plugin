from __future__ import annotations

import sqlite3
from pathlib import Path

from memex.extract import Observation, detect_contradictions, store_observations
from memex.observations import fetch_observations, init_observation_schema


class DisabledPipeline:
    enabled = False
    dimensions = 8


def _init_index(index_path: Path) -> None:
    conn = sqlite3.connect(index_path)
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE fts_content
            USING fts5(path, title, content, type, project, date, tokenize='porter unicode61')
            """
        )
        init_observation_schema(conn, 8)
        conn.execute(
            "INSERT INTO fts_content (path, title, content, type, project, date) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "projects/memex/memos/2026-03-16-existing.md",
                "Existing",
                "Old memo",
                "memo",
                "memex",
                "2026-03-16",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_store_and_retrieve_observations(tmp_path: Path) -> None:
    """Test storing Claude-generated observations and retrieving them."""
    index_path = tmp_path / "_index.sqlite"
    _init_index(index_path)

    pipeline = DisabledPipeline()
    observations = [
        Observation(
            content="Decision (2026-03-16): Chose typed config with pydantic-settings over raw JSON loading.",
            obs_type="explicit",
            confidence="high",
        ),
        Observation(
            content="Decision (2026-03-16): All extraction is Claude-driven, no regex heuristics.",
            obs_type="explicit",
            confidence="high",
        ),
    ]
    stored = store_observations(
        index_path,
        "projects/memex/memos/2026-03-16-test.md",
        observations,
        pipeline,
    )

    conn = sqlite3.connect(index_path)
    try:
        stored_rows = fetch_observations(conn)
    finally:
        conn.close()

    assert stored == {
        "inserted": 2, "embedded": 0, "embed_failed": 0,
        "replaced": 0, "skipped_duplicate": 0,
    }
    assert len(stored_rows) == 2
    assert any("pydantic-settings" in row.content for row in stored_rows)


def test_store_and_detect_contradictions(tmp_path: Path) -> None:
    index_path = tmp_path / "_index.sqlite"
    _init_index(index_path)

    pipeline = DisabledPipeline()
    existing = [
        Observation(
            content="Decision (2026-03-16): typed config remained enabled for memex.",
            obs_type="explicit",
            confidence="high",
        )
    ]
    store_observations(
        index_path,
        "projects/memex/memos/2026-03-16-existing.md",
        existing,
        pipeline,
    )

    new_observations = [
        Observation(
            content="Decision (2026-03-17): typed config was disabled for memex.",
            obs_type="explicit",
            confidence="high",
        )
    ]
    stored = store_observations(
        index_path,
        "projects/memex/memos/2026-03-17-new.md",
        new_observations,
        pipeline,
    )

    contradictions = detect_contradictions(index_path, new_observations, pipeline)

    conn = sqlite3.connect(index_path)
    try:
        stored_rows = fetch_observations(conn)
    finally:
        conn.close()

    assert stored == {
        "inserted": 1, "embedded": 0, "embed_failed": 0,
        "replaced": 0, "skipped_duplicate": 0,
    }
    assert len(stored_rows) == 2
    assert contradictions
    assert contradictions[0].obs_type == "contradiction"


# ---------------------------------------------------------------------------
# `backfill obs` is REPLACE-all-for-doc. It destroyed 12 observations live on
# 2026-07-21 while printing `{"stored": 5}` — a true statement about what it
# stored and total silence about what it deleted. Same defect class as the
# v0.15.11 preservation line: a report that says what it did, not what it undid.
#
# This exact data loss also occurred 2026-03-16 (store_observations called
# twice; second call wiped the first). That was fixed at the CALL SITES, by
# being careful. It recurred four months later. These tests pin the reporting
# so the next recurrence is visible rather than silent.
# ---------------------------------------------------------------------------

def _obs(content: str, topics: list[str] | None = None) -> Observation:
    return Observation(
        content=content, obs_type="explicit", confidence="high",
        topics=topics or [],
    )


DOC = "projects/memex/memos/2026-03-16-existing.md"
DOC_B = "projects/memex/memos/2026-03-16-other.md"


def test_replace_mode_reports_how_many_it_destroyed(tmp_path: Path) -> None:
    """The reported incident: replacing 2 rows with 1 must say so."""
    index_path = tmp_path / "_index.sqlite"
    _init_index(index_path)
    pipeline = DisabledPipeline()

    store_observations(index_path, DOC, [_obs("first"), _obs("second")], pipeline)
    result = store_observations(index_path, DOC, [_obs("third")], pipeline)

    assert result["replaced"] == 2, (
        f"destroyed 2 rows and reported {result['replaced']}"
    )
    assert result["inserted"] == 1
    assert result["skipped_duplicate"] == 0

    conn = sqlite3.connect(index_path)
    try:
        rows = fetch_observations(conn, doc_path=DOC)
    finally:
        conn.close()
    assert [r.content for r in rows] == ["third"]


def test_first_extraction_reports_zero_replaced(tmp_path: Path) -> None:
    """0 here is a measured zero — nothing existed — not an unmeasured one."""
    index_path = tmp_path / "_index.sqlite"
    _init_index(index_path)

    result = store_observations(index_path, DOC, [_obs("only")], DisabledPipeline())
    assert result["replaced"] == 0
    assert result["inserted"] == 1


def test_append_mode_preserves_prior_observations(tmp_path: Path) -> None:
    """The new opt-in escape hatch: add without destroying."""
    index_path = tmp_path / "_index.sqlite"
    _init_index(index_path)
    pipeline = DisabledPipeline()

    store_observations(index_path, DOC, [_obs("first"), _obs("second")], pipeline)
    result = store_observations(index_path, DOC, [_obs("third")], pipeline, mode="append")

    assert result["replaced"] == 0
    assert result["inserted"] == 1

    conn = sqlite3.connect(index_path)
    try:
        rows = fetch_observations(conn, doc_path=DOC)
    finally:
        conn.close()
    assert sorted(r.content for r in rows) == ["first", "second", "third"]


def test_global_duplicate_skip_is_counted_not_silent(tmp_path: Path) -> None:
    """content_hash is globally unique, so an identical obs under ANOTHER doc
    is silently dropped. Previously the only symptom was stored < total."""
    index_path = tmp_path / "_index.sqlite"
    _init_index(index_path)
    pipeline = DisabledPipeline()

    store_observations(index_path, DOC_B, [_obs("shared claim")], pipeline)
    result = store_observations(
        index_path, DOC, [_obs("shared claim"), _obs("unique claim")], pipeline
    )

    assert result["skipped_duplicate"] == 1, (
        "a dropped duplicate was not reported"
    )
    assert result["inserted"] == 1
    assert result["replaced"] == 0

    conn = sqlite3.connect(index_path)
    try:
        rows = fetch_observations(conn, doc_path=DOC)
    finally:
        conn.close()
    assert [r.content for r in rows] == ["unique claim"]


def test_duplicate_within_one_batch_is_counted(tmp_path: Path) -> None:
    """A duplicate repeated inside a SINGLE batch collides with its own
    earlier insert — caught by codex during design review."""
    index_path = tmp_path / "_index.sqlite"
    _init_index(index_path)

    result = store_observations(
        index_path, DOC, [_obs("same text"), _obs("same text")], DisabledPipeline()
    )
    assert result["inserted"] == 1
    assert result["skipped_duplicate"] == 1


def test_invalid_mode_rejected(tmp_path: Path) -> None:
    index_path = tmp_path / "_index.sqlite"
    _init_index(index_path)
    try:
        store_observations(index_path, DOC, [_obs("x")], DisabledPipeline(), mode="wipe")
    except ValueError as exc:
        assert "replace" in str(exc) and "append" in str(exc)
    else:
        raise AssertionError("invalid mode was accepted")


def test_delete_returns_rowcount_not_preselected_id_count(tmp_path: Path) -> None:
    """`replaced` must come from the DELETE's rowcount, so a concurrent writer
    landing between the SELECT and the DELETE can't make it under-report."""
    from memex.observations import delete_observations_for_doc

    index_path = tmp_path / "_index.sqlite"
    _init_index(index_path)
    store_observations(index_path, DOC, [_obs("a"), _obs("b")], DisabledPipeline())

    conn = sqlite3.connect(index_path)
    try:
        real = conn.execute("SELECT id FROM observations WHERE doc_path=?", (DOC,))
        assert len(real.fetchall()) == 2
        assert delete_observations_for_doc(conn, DOC) == 2
        conn.commit()
        remaining = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE doc_path=?", (DOC,)
        ).fetchone()[0]
        assert remaining == 0
    finally:
        conn.close()


def _run_cli(monkeypatch, capsys, tmp_path, doc, obs_json, extra_args=None):
    """Drive extract.main() end-to-end over stdin, returning (stdout, stderr)."""
    import io, json as _json, sys as _sys
    from memex import extract as _ex

    index_path = tmp_path / "_index.sqlite"
    argv = ["memex-backfill-obs", "--stdin", "--doc-path", doc,
            "--index", str(index_path), "--no-embed"]
    argv += extra_args or []
    monkeypatch.setattr(_sys, "argv", argv)
    monkeypatch.setattr(_sys, "stdin", io.StringIO(_json.dumps(obs_json)))
    try:
        _ex.main()
    except SystemExit as e:
        if e.code not in (0, None):
            pass
    return capsys.readouterr()


def _obs_json(*contents):
    return [
        {"content": c, "obs_type": "explicit", "confidence": "high", "topics": []}
        for c in contents
    ]


def test_cli_warns_on_net_loss(tmp_path, monkeypatch, capsys) -> None:
    """The incident, at the CLI boundary: 2 replaced by 1 must warn loudly."""
    _init_index(tmp_path / "_index.sqlite")
    _run_cli(monkeypatch, capsys, tmp_path, DOC, _obs_json("a", "b"))
    out, err = _run_cli(monkeypatch, capsys, tmp_path, DOC, _obs_json("c"))

    import json as _json
    payload = _json.loads(out)
    assert payload["replaced"] == 2
    assert payload["stored"] == 1
    assert payload["mode"] == "replace"
    assert "Net loss of 1 observation(s)" in err
    assert "--append" in err


def test_cli_quiet_when_replacement_grows_the_set(tmp_path, monkeypatch, capsys) -> None:
    """Routine re-extraction (2 -> 3) must NOT warn.

    A warning that fires on every normal run is one the operator learns to
    ignore — which is how the March 2026 fix decayed into a recurrence.
    """
    _init_index(tmp_path / "_index.sqlite")
    _run_cli(monkeypatch, capsys, tmp_path, DOC, _obs_json("a", "b"))
    out, err = _run_cli(monkeypatch, capsys, tmp_path, DOC, _obs_json("c", "d", "e"))

    import json as _json
    assert _json.loads(out)["replaced"] == 2
    assert "Net loss" not in err


def test_cli_append_flag_does_not_destroy(tmp_path, monkeypatch, capsys) -> None:
    _init_index(tmp_path / "_index.sqlite")
    _run_cli(monkeypatch, capsys, tmp_path, DOC, _obs_json("a", "b"))
    out, err = _run_cli(
        monkeypatch, capsys, tmp_path, DOC, _obs_json("c"), extra_args=["--append"]
    )

    import json as _json
    payload = _json.loads(out)
    assert payload["mode"] == "append"
    assert payload["replaced"] == 0
    assert "Net loss" not in err

    conn = sqlite3.connect(tmp_path / "_index.sqlite")
    try:
        rows = fetch_observations(conn, doc_path=DOC)
    finally:
        conn.close()
    assert sorted(r.content for r in rows) == ["a", "b", "c"]


def test_cli_replace_and_append_are_mutually_exclusive(tmp_path, monkeypatch, capsys) -> None:
    """argparse must reject a contradictory intent rather than silently pick one."""
    import io, json as _json, sys as _sys
    import pytest as _pytest
    from memex import extract as _ex

    _init_index(tmp_path / "_index.sqlite")
    monkeypatch.setattr(_sys, "argv", [
        "memex-backfill-obs", "--stdin", "--doc-path", DOC,
        "--index", str(tmp_path / "_index.sqlite"), "--no-embed",
        "--replace", "--append",
    ])
    monkeypatch.setattr(_sys, "stdin", io.StringIO(_json.dumps(_obs_json("a"))))
    # Bypass _run_cli deliberately — that helper swallows SystemExit, which is
    # exactly the signal under test here.
    with _pytest.raises(SystemExit) as exc:
        _ex.main()
    assert exc.value.code == 2
