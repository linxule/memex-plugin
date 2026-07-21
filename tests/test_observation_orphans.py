"""Orphaned observation-mirror rows.

`observations` is mirrored by `fts_observations`, `vec_observations`, and
`observation_topics`, all keyed by observation id. Deleting a parent without
its mirrors leaves rows that every JOIN-ing read path silently discards — so
the damage is invisible except in counters that forget to join, where it shows
up as observations the vault claims to have and cannot return.

These tests pin the two halves of that: deletion paths must clean every mirror,
and counters must join.
"""

from __future__ import annotations

import sqlite3

import pytest

from memex.observations import (
    _OBS_MIRROR_TABLES,
    unchecked_mirror_tables,
    count_orphaned_observation_rows,
    delete_observation_ids,
    delete_observations_for_doc,
    delete_orphaned_observation_rows,
    fetch_observations_by_topic,
    init_observation_schema,
    retag_topic,
    store_observation_topics,
    topic_observation_counts,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    init_observation_schema(conn, 8)
    return conn


def _add_obs(conn: sqlite3.Connection, obs_id: int, doc_path: str, content: str) -> None:
    conn.execute(
        "INSERT INTO observations (id, doc_path, content, content_hash, obs_type, "
        "confidence, source_obs_ids, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (obs_id, doc_path, content, f"h{obs_id}", "explicit", "high", "[]", "2026-07-21"),
    )
    conn.execute(
        "INSERT INTO fts_observations (rowid, content, obs_type) VALUES (?, ?, ?)",
        (obs_id, content, "explicit"),
    )


# ── the registry is the single source of truth ────────────────────────────


def test_mirror_registry_covers_every_table_referencing_observations():
    """Any table keyed to observation ids must be in `_OBS_MIRROR_TABLES`.

    The dreamer's merge path and `delete_observations_for_doc` each used to
    hand-maintain this list; they disagreed, and the disagreement leaked 515
    orphaned `observation_topics` rows into the live vault. A new mirror table
    added to `init_observation_schema` without a registry entry must fail here,
    at CI time, not in production as silently-wrong counts.
    """
    conn = _conn()
    try:
        registered = {t for t, _ in _OBS_MIRROR_TABLES}
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }
        # Shadow tables of the FTS5/vec0 virtual tables are managed by SQLite.
        real = {
            t
            for t in tables
            if not any(
                t.startswith(v + "_") for v in ("fts_observations", "vec_observations")
            )
            and t not in ("observations",)
        }
        assert real <= registered, f"unregistered obs mirror table(s): {real - registered}"
    finally:
        conn.close()


# ── deletion cleans every mirror ──────────────────────────────────────────


def test_delete_observation_ids_clears_all_mirrors():
    conn = _conn()
    try:
        _add_obs(conn, 1, "projects/p/memos/a.md", "alpha")
        store_observation_topics(conn, 1, ["topic-x", "topic-y"])

        assert delete_observation_ids(conn, [1]) == 1

        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM fts_observations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM observation_topics").fetchone()[0] == 0
    finally:
        conn.close()


def test_delete_observation_ids_returns_measured_count_not_input_length():
    """Passing ids that do not exist must not inflate the reported count."""
    conn = _conn()
    try:
        _add_obs(conn, 1, "projects/p/memos/a.md", "alpha")
        assert delete_observation_ids(conn, [1, 999, 1000]) == 1
    finally:
        conn.close()


def test_delete_observation_ids_empty_is_measured_zero():
    conn = _conn()
    try:
        assert delete_observation_ids(conn, []) == 0
    finally:
        conn.close()


def test_delete_for_doc_leaves_concurrently_inserted_row_intact():
    """A row inserted after the id SELECT survives WITH its mirrors.

    `backfill obs` holds a SHARED advisory lock, so another writer can insert
    for the same doc mid-call. A `WHERE doc_path = ?` DELETE would remove that
    row while leaving its mirror rows behind — manufacturing exactly the
    orphans this module exists to prevent.
    """
    conn = _conn()
    try:
        _add_obs(conn, 1, "projects/p/memos/a.md", "alpha")
        rows = conn.execute(
            "SELECT id FROM observations WHERE doc_path = ?",
            ("projects/p/memos/a.md",),
        ).fetchall()
        # Concurrent writer lands between the SELECT and the DELETE.
        _add_obs(conn, 2, "projects/p/memos/a.md", "beta")
        store_observation_topics(conn, 2, ["topic-x"])

        delete_observation_ids(conn, [r[0] for r in rows])

        assert conn.execute("SELECT id FROM observations").fetchall() == [(2,)]
        assert conn.execute("SELECT rowid FROM fts_observations").fetchall() == [(2,)]
        assert count_orphaned_observation_rows(conn)["observation_topics"] == 0
    finally:
        conn.close()


def test_delete_observations_for_doc_removes_topic_tags():
    conn = _conn()
    try:
        _add_obs(conn, 1, "projects/p/memos/a.md", "alpha")
        store_observation_topics(conn, 1, ["topic-x"])
        assert delete_observations_for_doc(conn, "projects/p/memos/a.md") == 1
        assert count_orphaned_observation_rows(conn)["observation_topics"] == 0
    finally:
        conn.close()


# ── counters must join ────────────────────────────────────────────────────


def test_topic_counts_exclude_orphaned_tags():
    """`memex obs stats` must not count tags whose observation is gone.

    The live vault reported 41,185 tagged observations while 40,670 existed —
    515 of the count were rows pointing at deleted observations, which
    `fetch_observations_by_topic` (which joins) would never return.
    """
    conn = _conn()
    try:
        _add_obs(conn, 1, "projects/p/memos/a.md", "alpha")
        store_observation_topics(conn, 1, ["topic-x"])
        store_observation_topics(conn, 99, ["topic-x"])  # orphan: no obs 99

        counts = dict(topic_observation_counts(conn))
        assert counts["topic-x"] == 1
        # The counter and the retriever must agree.
        assert len(fetch_observations_by_topic(conn, "topic-x")) == counts["topic-x"]
    finally:
        conn.close()


def test_retag_reports_only_live_observations():
    conn = _conn()
    try:
        _add_obs(conn, 1, "projects/p/memos/a.md", "alpha")
        store_observation_topics(conn, 1, ["old-slug"])
        store_observation_topics(conn, 99, ["old-slug"])  # orphan

        assert retag_topic(conn, "old-slug", "new-slug") == 1
    finally:
        conn.close()


# ── detection and repair ──────────────────────────────────────────────────


def test_count_orphans_is_zero_on_healthy_index():
    conn = _conn()
    try:
        _add_obs(conn, 1, "projects/p/memos/a.md", "alpha")
        store_observation_topics(conn, 1, ["topic-x"])
        counts = count_orphaned_observation_rows(conn)
        assert counts["fts_observations"] == 0
        assert counts["observation_topics"] == 0
    finally:
        conn.close()


def test_count_and_delete_orphans_agree():
    conn = _conn()
    try:
        _add_obs(conn, 1, "projects/p/memos/a.md", "alpha")
        store_observation_topics(conn, 1, ["topic-x"])
        # Bypass the safe deleter, exactly as the leaking path did.
        conn.execute("DELETE FROM observations WHERE id = 1")

        before = count_orphaned_observation_rows(conn)
        assert before["fts_observations"] == 1
        assert before["observation_topics"] == 1

        removed = delete_orphaned_observation_rows(conn)
        assert removed["fts_observations"] == 1
        assert removed["observation_topics"] == 1

        after = count_orphaned_observation_rows(conn)
        assert after["fts_observations"] == 0
        assert after["observation_topics"] == 0
    finally:
        conn.close()


def test_delete_orphans_spares_live_rows():
    conn = _conn()
    try:
        _add_obs(conn, 1, "projects/p/memos/a.md", "alpha")
        store_observation_topics(conn, 1, ["topic-x"])
        store_observation_topics(conn, 99, ["topic-x"])

        delete_orphaned_observation_rows(conn)

        assert conn.execute("SELECT COUNT(*) FROM fts_observations").fetchone()[0] == 1
        assert conn.execute(
            "SELECT observation_id FROM observation_topics"
        ).fetchall() == [(1,)]
    finally:
        conn.close()


def test_absent_mirror_table_is_omitted_not_reported_as_zero():
    """A mirror table that does not exist must be ABSENT from the result.

    Reporting 0 for a table that was never created is the `missing is not
    zero` defect: an unasked question rendered as a measured absence of
    orphans. The condition is constructed here rather than depending on
    whether sqlite-vec happens to be installed — a test gated on the local
    environment does not exercise the branch on the machines that have the
    dependency, which is all of CI.
    """
    conn = sqlite3.connect(":memory:")
    try:
        init_observation_schema(conn, 8)
        conn.execute("DROP TABLE IF EXISTS vec_observations")

        counts = count_orphaned_observation_rows(conn)
        assert "vec_observations" not in counts
        assert "fts_observations" in counts
        assert unchecked_mirror_tables(conn, counts) == ["vec_observations"]
    finally:
        conn.close()


def test_vec_mirror_rows_are_deleted_when_the_table_is_present():
    """The vec branch must actually run, not merely be skipped as absent.

    `_add_obs` seeds no vec row, so without this the first entry in the
    registry is never exercised by any test — the branch where the swallowed
    OperationalError used to live.
    """
    conn = sqlite3.connect(":memory:")
    try:
        init_observation_schema(conn, 8)
        # Stand in for the vec0 virtual table with a plain one of the same
        # name: the deleter addresses it by name and rowid either way, and
        # this makes the branch reachable without depending on whether
        # sqlite-vec loaded in this environment.
        conn.execute("DROP TABLE IF EXISTS vec_observations")
        conn.execute(
            "CREATE TABLE vec_observations (rowid INTEGER PRIMARY KEY, embedding BLOB)"
        )
        _add_obs(conn, 1, "projects/p/memos/a.md", "alpha")
        conn.execute("INSERT INTO vec_observations (rowid, embedding) VALUES (1, x'00')")

        assert delete_observation_ids(conn, [1]) == 1
        assert conn.execute("SELECT COUNT(*) FROM vec_observations").fetchone()[0] == 0
    finally:
        conn.close()


def test_dropped_mirror_table_surfaces_as_unchecked_not_as_clean():
    """A missing mirror table must never make the report look clean.

    Absence is reported the same way for every mirror: the key is omitted and
    the table is listed as unchecked, so a caller sees "I could not answer for
    this table" rather than a total that silently covers fewer tables than it
    appears to.
    """
    conn = _conn()
    try:
        _add_obs(conn, 1, "projects/p/memos/a.md", "alpha")
        store_observation_topics(conn, 1, ["topic-x"])
        conn.execute("DROP TABLE observation_topics")

        counts = count_orphaned_observation_rows(conn)
        assert "observation_topics" not in counts
        assert "observation_topics" in unchecked_mirror_tables(conn, counts)
    finally:
        conn.close()


def test_json_output_reports_unchecked_tables():
    """`--json` must carry the unchecked list and exit non-zero.

    The human-readable path warned about unqueryable mirrors while the
    machine-readable path emitted a bare zero — the false green on the surface
    a cron job or hook actually consumes.
    """
    import json as json_mod

    from typer.testing import CliRunner

    from memex.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["obs", "orphans", "--json"])
    if result.exit_code not in (0, 1, 2):
        # Vault not configured in this environment — nothing to assert.
        return
    payload = json_mod.loads(result.stdout)
    assert "unchecked" in payload, "JSON output must state what it could not check"
    assert isinstance(payload["unchecked"], list)
    # An unchecked mirror must not be reported as a clean run.
    if payload["unchecked"]:
        assert result.exit_code != 0


class _UnreadableTableConn:
    """Wraps a real connection, making one table raise like an unloadable vec0.

    Constructed rather than gated on whether sqlite-vec happens to be
    installed: a test skipped on the machines that have the dependency does not
    exercise the branch anywhere that matters.
    """

    def __init__(self, conn: sqlite3.Connection, table: str) -> None:
        self._conn = conn
        self._table = table

    def execute(self, sql, params=()):
        if f"FROM {self._table} " in sql or sql.endswith(f"FROM {self._table}"):
            raise sqlite3.OperationalError("no such module: vec0")
        if sql.startswith(f"DELETE FROM {self._table} "):
            raise sqlite3.OperationalError("no such module: vec0")
        return self._conn.execute(sql, params)


def test_unreachable_mirror_table_refuses_delete_with_actionable_message():
    """A mirror table present but unqueryable must abort the delete.

    `no such table` and `no such module: vec0` share an exception type and mean
    opposite things. Treating the second as absence would skip that mirror and
    delete the parent anyway — orphaning rows inside the function contracted to
    prevent exactly that.
    """
    conn = _conn()
    try:
        _add_obs(conn, 1, "projects/p/memos/a.md", "alpha")
        store_observation_topics(conn, 1, ["topic-x"])
        wrapped = _UnreadableTableConn(conn, "observation_topics")

        with pytest.raises(sqlite3.OperationalError) as excinfo:
            delete_observation_ids(wrapped, [1])

        message = str(excinfo.value)
        assert "observation_topics" in message
        assert "init_observation_schema" in message, "error must name the fix"

        # The parent survives, which is the invariant that matters: mirrors
        # ahead of the failing one HAVE been deleted (fts is gone here), so the
        # observation is temporarily unsearchable — recoverable by
        # re-extraction, and detectable by comparing counts. The reverse
        # ordering would have left orphans, which are neither.
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM fts_observations").fetchone()[0] == 0
    finally:
        conn.close()


def test_refusal_does_not_delete_parent_first():
    """The parent must outlive a mirror the deleter could not clear.

    Registry order puts the mirrors ahead of `observations` precisely so a
    failure partway through cannot leave orphans. A refusal that destroyed the
    parent first would be the same bug in a new hat.
    """
    conn = _conn()
    try:
        _add_obs(conn, 1, "projects/p/memos/a.md", "alpha")
        # Fail on the LAST mirror, so the two before it have already deleted.
        wrapped = _UnreadableTableConn(conn, "observation_topics")

        with pytest.raises(sqlite3.OperationalError):
            delete_observation_ids(wrapped, [1])

        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
    finally:
        conn.close()
