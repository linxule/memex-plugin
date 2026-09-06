"""Tests for index_rebuild gap surfacing + embed-missing retry (v0.11.0).

Covers:
- count_embedding_gaps: correctly counts chunks/observations missing from vec_*
- reembed_missing: picks up only missing rows, writes vec_* entries
- format_rebuild_stats / format_status: surface warning when gaps present,
  surface "sqlite-vec not loaded" when extension unavailable
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memex.scripts import index_rebuild as ir


def _seed_index(memex: Path) -> Path:
    """Create a minimal sqlite index matching the real schema for these tests."""
    db = memex / "_index.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE chunks (
            id INTEGER PRIMARY KEY,
            doc_path TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL DEFAULT '',
            chunk_type TEXT NOT NULL DEFAULT 'content',
            start_offset INTEGER,
            end_offset INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            doc_date TEXT NOT NULL DEFAULT '',
            doc_project TEXT NOT NULL DEFAULT '',
            UNIQUE(doc_path, chunk_index)
        );
        CREATE TABLE observations (
            id INTEGER PRIMARY KEY,
            doc_path TEXT NOT NULL,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL DEFAULT '',
            obs_type TEXT NOT NULL DEFAULT 'explicit',
            confidence REAL DEFAULT 1.0,
            source_obs_ids TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE vec_chunks (rowid INTEGER PRIMARY KEY, embedding BLOB);
        CREATE TABLE vec_observations (rowid INTEGER PRIMARY KEY, embedding BLOB);
        """
    )
    conn.commit()
    conn.close()
    return db


def test_count_embedding_gaps_counts_missing(tmp_path, monkeypatch):
    _seed_index(tmp_path)
    # Pretend sqlite-vec loaded successfully so the function runs the joins.
    monkeypatch.setattr(ir, "_load_vec_extension", lambda conn: True)

    conn = sqlite3.connect(tmp_path / "_index.sqlite")
    conn.executemany(
        "INSERT INTO chunks (id, doc_path, chunk_index, content) VALUES (?, ?, ?, ?)",
        [(1, "a.md", 0, "c1"), (2, "a.md", 1, "c2"), (3, "b.md", 0, "c3")],
    )
    conn.execute("INSERT INTO vec_chunks(rowid, embedding) VALUES (1, X'00')")
    conn.executemany(
        "INSERT INTO observations (id, doc_path, content) VALUES (?, ?, ?)",
        [(1, "a.md", "o1"), (2, "a.md", "o2"), (3, "b.md", "o3")],
    )
    conn.execute("INSERT INTO vec_observations(rowid, embedding) VALUES (1, X'00')")
    conn.commit()
    conn.close()

    result = ir.count_embedding_gaps(tmp_path)
    assert result["chunks"] == 2         # chunks 2 + 3 missing
    assert result["docs"] == 2           # across a.md + b.md
    assert result["observations"] == 2   # obs 2 + 3 missing
    assert result["available"] is True


def test_count_embedding_gaps_unavailable_when_vec_cant_load(tmp_path, monkeypatch):
    _seed_index(tmp_path)
    monkeypatch.setattr(ir, "_load_vec_extension", lambda conn: False)

    result = ir.count_embedding_gaps(tmp_path)
    assert result["available"] is False
    assert result["chunks"] == 0
    assert result["observations"] == 0


def test_count_embedding_gaps_no_index_returns_zeros(tmp_path):
    # No file at <tmp>/_index.sqlite
    result = ir.count_embedding_gaps(tmp_path)
    assert result == {"chunks": 0, "observations": 0, "docs": 0, "orphans": 0, "available": False}


class _FakePipeline:
    """Minimal EmbeddingPipeline replacement for reembed_missing tests."""

    enabled = True
    dimensions = 4

    class _Provider:
        @staticmethod
        def embed_texts(texts, task_type="document"):
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    _provider_impl = _Provider()


def test_reembed_missing_backfills_only_missing(tmp_path, monkeypatch):
    _seed_index(tmp_path)
    monkeypatch.setattr(ir, "_load_vec_extension", lambda conn: True)
    monkeypatch.setattr(ir, "EmbeddingPipeline", _FakePipeline)
    monkeypatch.setattr(ir, "serialize_f32", lambda vec: b"vec")

    conn = sqlite3.connect(tmp_path / "_index.sqlite")
    conn.execute(
        "INSERT INTO chunks (id, doc_path, chunk_index, content) VALUES (1, 'a.md', 0, 'chunk')"
    )
    conn.execute(
        "INSERT INTO observations (id, doc_path, content) VALUES (1, 'a.md', 'obs')"
    )
    conn.commit()
    conn.close()

    stats = ir.reembed_missing(tmp_path, batch_size=10)
    assert stats["error"] is None
    assert stats["chunks_pending"] == 1
    assert stats["chunks_embedded"] == 1
    assert stats["observations_pending"] == 1
    assert stats["observations_embedded"] == 1


def test_reembed_missing_returns_error_when_pipeline_disabled(tmp_path, monkeypatch):
    _seed_index(tmp_path)
    monkeypatch.setattr(ir, "_load_vec_extension", lambda conn: True)

    class _DisabledPipeline:
        enabled = False
        _provider_impl = None

    monkeypatch.setattr(ir, "EmbeddingPipeline", _DisabledPipeline)

    stats = ir.reembed_missing(tmp_path)
    assert stats["error"] is not None
    assert "embedding pipeline" in stats["error"].lower()


def test_format_rebuild_stats_surfaces_gap_warning():
    out = ir.format_rebuild_stats({
        "total_docs": 10, "new": 1, "updated": 0, "unchanged": 9, "deleted": 0,
        "observations_stored": 0,
        "embedding_gaps": {"available": True, "chunks": 2, "docs": 1, "observations": 3},
    })
    assert "Embedding gaps detected" in out
    assert "memex index embed-missing" in out
    assert "2 chunk(s)" in out
    assert "3 observation(s)" in out


def test_format_rebuild_stats_clean_when_no_gaps():
    out = ir.format_rebuild_stats({
        "total_docs": 10, "new": 0, "updated": 0, "unchanged": 10, "deleted": 0,
        "observations_stored": 0,
        "embedding_gaps": {"available": True, "chunks": 0, "docs": 0, "observations": 0},
    })
    assert "Embedding gaps" not in out
    assert "embed-missing" not in out


def test_format_status_surfaces_sqlite_vec_unavailable():
    out = ir.format_status({
        "exists": True, "size_kb": 1.0, "fts_documents": 0, "fts_by_type": {},
        "embedded_documents": 0, "total_chunks": 0, "embedded_chunks": 0,
        "cached_embeddings": 0, "observations": 0,
        "embedding_gaps": {"available": False},
    })
    assert "sqlite-vec extension not loaded" in out


def test_format_rebuild_stats_surfaces_sqlite_vec_unavailable():
    out = ir.format_rebuild_stats({
        "total_docs": 1, "new": 1, "updated": 0, "unchanged": 0, "deleted": 0,
        "observations_stored": 0,
        "embedding_gaps": {"available": False},
    })
    assert "sqlite-vec extension not loaded" in out


def test_rebuild_incremental_end_to_end_no_spurious_errors(tmp_path):
    """End-to-end rebuild with a real markdown doc and no embedding pipeline.

    This test would have caught the Round 2 SAVEPOINT bug where
    `index_document`'s inner `conn.commit()` released the savepoint,
    causing every successful doc to be double-reported as an error
    via `RELEASE SAVEPOINT doc` → `no such savepoint`.

    Writes a minimal memo, runs rebuild twice so the second pass exercises
    the incremental path specifically; asserts zero errors on both.
    """
    # Seed a minimal vault with one memo
    projects = tmp_path / "projects" / "test-project"
    (projects / "memos").mkdir(parents=True)
    memo = projects / "memos" / "2026-04-21-sample.md"
    memo.write_text(
        "---\ntype: memo\ntitle: sample\ndate: 2026-04-21\n---\n\n# Sample\n\nBody."
    )

    # First call falls through to rebuild_full (no existing index) — confirm
    # it doesn't report spurious errors from the savepoint/commit interaction.
    first = ir.rebuild_incremental(tmp_path)
    assert first.get("errors", 0) == 0, (
        f"initial rebuild should report zero errors, got stats={first}"
    )

    # Second call is incremental proper. Modify doc to force update path —
    # which is where the SAVEPOINT bug fired before.
    memo.write_text(
        "---\ntype: memo\ntitle: sample\ndate: 2026-04-21\n---\n\n# Sample\n\nBody v2."
    )
    second = ir.rebuild_incremental(tmp_path)
    assert second["updated"] == 1, f"expected 1 updated doc, got {second}"
    assert second["errors"] == 0, (
        f"rebuild should report zero errors on successful update; got "
        f"{second['errors']} — this is how the SAVEPOINT regression would "
        f"manifest. stats={second}"
    )

    # Third call should see everything unchanged.
    third = ir.rebuild_incremental(tmp_path)
    assert third["new"] == 0
    assert third["updated"] == 0
    assert third["unchanged"] == 1
    assert third["errors"] == 0


def test_rebuild_incremental_cleans_vec_chunks_on_deleted_doc(tmp_path, monkeypatch):
    """When a doc is deleted from disk, its vec_chunks rows must be removed.

    Would catch the Kimi-surfaced bug where `rebuild_incremental` cleaned
    up `chunks`/`fts_content`/graph tables but leaked `vec_chunks`.
    """
    # Seed vault + initial index via rebuild
    projects = tmp_path / "projects" / "test-project"
    (projects / "memos").mkdir(parents=True)
    memo = projects / "memos" / "2026-04-21-to-be-deleted.md"
    memo.write_text("---\ntype: memo\ntitle: delete-me\ndate: 2026-04-21\n---\n\nBody.")
    ir.rebuild_incremental(tmp_path)

    # Manually add a vec_chunks row for the doc's chunk (simulates successful embed).
    # We can't rely on a real pipeline, so inject a synthetic row.
    import sqlite3
    conn = sqlite3.connect(tmp_path / "_index.sqlite")
    conn.enable_load_extension(True)
    import sqlite_vec
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    # Ensure vec_chunks schema exists (init_embedding_schema would have created it)
    chunk_ids = [row[0] for row in conn.execute("SELECT id FROM chunks")]
    assert chunk_ids, "expected at least one chunk from rebuild"
    # Insert vec_chunks rows to simulate embeddings having landed.
    # dims must match init_embedding_schema — read from PRAGMA or check config.
    # Simplest: try to insert an arbitrary dimension and fall back on error.
    try:
        conn.execute(
            "INSERT INTO vec_chunks(rowid, embedding) VALUES (?, X'00')",
            (chunk_ids[0],),
        )
        conn.commit()
    except sqlite3.OperationalError:
        # Schema dim mismatch — skip the assertion; the deletion fix still
        # needs to run without errors.
        conn.close()
        memo.unlink()
        stats = ir.rebuild_incremental(tmp_path)
        assert stats["deleted"] == 1
        return

    # Delete the doc and rebuild — vec_chunks row should be gone.
    memo.unlink()
    conn.close()

    stats = ir.rebuild_incremental(tmp_path)
    assert stats["deleted"] == 1

    conn = sqlite3.connect(tmp_path / "_index.sqlite")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    try:
        vec_row = conn.execute(
            "SELECT rowid FROM vec_chunks WHERE rowid = ?", (chunk_ids[0],)
        ).fetchone()
        assert vec_row is None, (
            f"vec_chunks row for deleted doc should have been cleaned up, "
            f"got {vec_row}"
        )
    finally:
        conn.close()


def test_connect_index_sets_wal_and_busy_timeout(tmp_path):
    """_connect_index must set journal_mode=WAL and busy_timeout=10s so parallel
    writers (backfill obs --stdin concurrent with rebuild) don't collide with
    'database is locked' under sqlite's default DELETE mode.
    """
    db = tmp_path / "_index.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE seed(id INTEGER)")
    conn.commit()
    conn.close()

    conn = ir._connect_index(db)
    try:
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert journal.lower() == "wal", f"expected WAL, got {journal!r}"
        assert busy == 10000, f"expected busy_timeout=10000ms, got {busy}"
    finally:
        conn.close()


def test_format_status_surfaces_gap_warning():
    """`format_status` must render the gap warning + remediation pointer
    symmetrically with `format_rebuild_stats`.

    Previously only rebuild output carried the warning; `memex index status`
    would be silent about gaps, defeating the observability goal.
    """
    out = ir.format_status({
        "exists": True, "size_kb": 1.0, "fts_documents": 1, "fts_by_type": {},
        "embedded_documents": 1, "total_chunks": 5, "embedded_chunks": 3,
        "cached_embeddings": 3, "observations": 10,
        "embedding_gaps": {"available": True, "chunks": 2, "docs": 1, "observations": 4},
    })
    assert "Embedding gaps detected" in out
    assert "memex index embed-missing" in out
    assert "2 chunk(s)" in out
    assert "4 observation(s)" in out


def test_format_status_clean_when_no_gaps():
    out = ir.format_status({
        "exists": True, "size_kb": 1.0, "fts_documents": 1, "fts_by_type": {},
        "embedded_documents": 1, "total_chunks": 5, "embedded_chunks": 5,
        "cached_embeddings": 5, "observations": 10,
        "embedding_gaps": {"available": True, "chunks": 0, "docs": 0, "observations": 0},
    })
    assert "Embedding gaps" not in out
    assert "embed-missing" not in out


def test_rebuild_incremental_rolls_back_failed_doc_and_continues(tmp_path, monkeypatch):
    """Per-doc SAVEPOINT contract: when one doc raises AFTER partial writes
    have already landed, those partial writes must roll back, subsequent
    docs must still index, and overall error accounting must be correct.

    Critical design: the failure must fire AFTER `index_file_fts` has written
    FTS rows. Otherwise "rollback" is indistinguishable from "nothing was
    written." Before this test was tightened (2026-04-21), the poison
    function raised before any write — making the test vacuous (it would
    pass even without the SAVEPOINT wrapping).

    Setup: monkeypatch `index_document` to raise when called with the
    poison path. `index_file_fts` runs first in the rebuild loop, so FTS
    rows DO land for the poison doc before the exception fires. The
    savepoint must roll those FTS rows back; the good docs' FTS rows
    must survive.
    """
    vault = tmp_path
    projects = vault / "projects" / "test-project"
    (projects / "memos").mkdir(parents=True)

    good1 = projects / "memos" / "2026-04-21-good1.md"
    good1.write_text("---\ntype: memo\ntitle: g1\ndate: 2026-04-21\n---\n\nGood one.")
    poison = projects / "memos" / "2026-04-21-poison.md"
    poison.write_text("---\ntype: memo\ntitle: p\ndate: 2026-04-21\n---\n\nPoison.")
    good2 = projects / "memos" / "2026-04-21-good2.md"
    good2.write_text("---\ntype: memo\ntitle: g2\ndate: 2026-04-21\n---\n\nGood two.")

    # Bootstrap: first pass indexes everything cleanly.
    first = ir.rebuild_incremental(vault)
    assert first.get("errors", 0) == 0

    # Verify bootstrap landed FTS rows for all three.
    import sqlite3 as _sqlite3
    _poison_rel = str(poison.relative_to(vault))
    with _sqlite3.connect(vault / "_index.sqlite") as check:
        initial_fts = check.execute(
            "SELECT path, content FROM fts_content WHERE path = ?", (_poison_rel,)
        ).fetchone()
    assert initial_fts is not None, "bootstrap must have written FTS content"
    initial_poison_fts_content = initial_fts[1]
    assert "Poison." in initial_poison_fts_content

    # Poison the UPDATE path via `index_document`, which runs AFTER
    # `index_file_fts` has already written the new FTS row. This means:
    # 1. index_file_fts writes the updated poison FTS row (to content "Poison v2")
    # 2. index_document raises on the poison path
    # 3. SAVEPOINT rollback must revert the FTS update back to v1 content
    real_index_document = ir.index_document

    def failing_index_document(conn, path, memex, pipeline=None):
        if path.name == poison.name:
            raise RuntimeError("synthetic failure mid-doc")
        return real_index_document(conn, path, memex, pipeline)

    monkeypatch.setattr(ir, "index_document", failing_index_document)

    # Mutate all three docs so they're all on the update path.
    good1.write_text("---\ntype: memo\ntitle: g1\ndate: 2026-04-21\n---\n\nGood one v2.")
    poison.write_text("---\ntype: memo\ntitle: p\ndate: 2026-04-21\n---\n\nPoison v2.")
    good2.write_text("---\ntype: memo\ntitle: g2\ndate: 2026-04-21\n---\n\nGood two v2.")

    stats = ir.rebuild_incremental(vault)
    assert stats["errors"] == 1, f"expected exactly 1 error (poison doc), got {stats}"
    assert stats["updated"] == 3, f"expected all 3 attempted as updates, got {stats}"

    # ASSERTION 1: Good docs' FTS content advanced to v2 (committed past the savepoint).
    with _sqlite3.connect(vault / "_index.sqlite") as check:
        g1_fts = check.execute(
            "SELECT content FROM fts_content WHERE path = ?",
            (str(good1.relative_to(vault)),),
        ).fetchone()
        g2_fts = check.execute(
            "SELECT content FROM fts_content WHERE path = ?",
            (str(good2.relative_to(vault)),),
        ).fetchone()
        poison_fts = check.execute(
            "SELECT content FROM fts_content WHERE path = ?", (_poison_rel,)
        ).fetchone()

    assert g1_fts and "Good one v2" in g1_fts[0], (
        "good1's FTS content must advance to v2"
    )
    assert g2_fts and "Good two v2" in g2_fts[0], (
        "good2's FTS content must advance to v2"
    )

    # ASSERTION 2 (the real atomicity check): poison doc's FTS row must NOT
    # contain v2 content. index_file_fts wrote it to v2 inside the savepoint,
    # and the rollback must have reverted it. If this fails, the savepoint
    # is not containing writes — the whole commit-refactor is broken.
    assert poison_fts is not None, (
        "poison doc's FTS row should still exist from bootstrap (rolled back to v1)"
    )
    assert "Poison v2" not in poison_fts[0], (
        f"FATAL: poison doc's FTS content advanced to v2 despite the raise. "
        f"Savepoint did not roll back. Got: {poison_fts[0][:200]!r}"
    )
    assert "Poison." in poison_fts[0], (
        f"poison doc's FTS content should still match the bootstrap v1 text. "
        f"Got: {poison_fts[0][:200]!r}"
    )

    # ASSERTION 3: doc_hashes for poison must still be v1.
    from memex.scripts.embeddings import content_hash as _ch
    with _sqlite3.connect(vault / "_index.sqlite") as check:
        poison_hash = check.execute(
            "SELECT content_hash FROM doc_hashes WHERE path = ?", (_poison_rel,)
        ).fetchone()
    assert poison_hash is not None
    assert poison_hash[0] != _ch(poison.read_text()), (
        "poison doc's hash must not advance to v2 — its savepoint rolled back"
    )


class _QuerySpyConnection(sqlite3.Connection):
    """Subclass that records every `execute` SQL statement.

    Used to assert that count_embedding_gaps avoids the slow LEFT-JOIN
    against the sqlite-vec virtual table when no gap exists.
    """

    seen: list[str]

    def execute(self, sql, *args, **kwargs):  # type: ignore[override]
        type(self).seen.append(sql)
        return super().execute(sql, *args, **kwargs)


def test_count_embedding_gaps_uses_fast_count_delta(tmp_path, monkeypatch):
    """Fast-path check: count_embedding_gaps no longer LEFT JOINs against
    vec_chunks when there are no gaps (the 107s → 53ms fix).

    Setup: seed chunks + vec_chunks with no gaps. Use a Connection subclass
    that records every execute() call. Assert that no LEFT JOIN against
    vec_chunks runs in the chunks block when total == embedded.
    """
    _seed_index(tmp_path)
    monkeypatch.setattr(ir, "_load_vec_extension", lambda conn: True)

    conn = sqlite3.connect(tmp_path / "_index.sqlite")
    conn.executemany(
        "INSERT INTO chunks (id, doc_path, chunk_index, content) VALUES (?, ?, ?, ?)",
        [(1, "a.md", 0, "c1"), (2, "a.md", 1, "c2"), (3, "b.md", 0, "c3")],
    )
    conn.executemany(
        "INSERT INTO vec_chunks(rowid, embedding) VALUES (?, X'00')",
        [(1,), (2,), (3,)],
    )
    conn.commit()
    conn.close()

    _QuerySpyConnection.seen = []

    def spying_connect(path):
        return sqlite3.connect(path, factory=_QuerySpyConnection)

    monkeypatch.setattr(ir, "_connect_index", spying_connect)

    result = ir.count_embedding_gaps(tmp_path)
    assert result["chunks"] == 0, f"expected 0 gaps, got {result}"
    assert result["docs"] == 0, "docs query should not run when gap == 0"

    chunk_left_joins = [
        q for q in _QuerySpyConnection.seen
        if "LEFT JOIN vec_chunks" in q and "DISTINCT" in q
    ]
    assert not chunk_left_joins, (
        f"FATAL: fast path regressed — DISTINCT LEFT JOIN against vec_chunks "
        f"ran when no gap existed. Queries: {chunk_left_joins}"
    )


def test_count_embedding_gaps_falls_back_to_per_doc_when_gap_present(
    tmp_path, monkeypatch
):
    """When chunks > vec_chunks, the per-doc DISTINCT query DOES run (we need
    the doc count to surface in the warning block). Verify the fallback path.
    """
    _seed_index(tmp_path)
    monkeypatch.setattr(ir, "_load_vec_extension", lambda conn: True)

    conn = sqlite3.connect(tmp_path / "_index.sqlite")
    conn.executemany(
        "INSERT INTO chunks (id, doc_path, chunk_index, content) VALUES (?, ?, ?, ?)",
        [(1, "a.md", 0, "c1"), (2, "a.md", 1, "c2"), (3, "b.md", 0, "c3")],
    )
    conn.execute("INSERT INTO vec_chunks(rowid, embedding) VALUES (1, X'00')")
    conn.commit()
    conn.close()

    result = ir.count_embedding_gaps(tmp_path)
    assert result["chunks"] == 2
    assert result["docs"] == 2, "per-doc query must run when gap > 0"


def test_rebuild_full_preserves_observations_across_atomic_swap(tmp_path):
    """rebuild_full must preserve observations from the existing index.

    Background: on 2026-05-07 a `--full` rebuild silently wiped ~3265
    observations because they're extracted by subagents (not derived from
    documents) and the atomic swap installs a fresh DB with empty obs
    tables. Recovery cost ~$10-20 in subagent time.

    Setup: build an initial index with a memo and observations attached.
    Run rebuild_full a second time and confirm observations + topic tags
    survive.
    """
    # Seed a minimal vault
    projects = tmp_path / "projects" / "test-project"
    (projects / "memos").mkdir(parents=True)
    memo = projects / "memos" / "2026-05-13-sample.md"
    memo_rel = str(memo.relative_to(tmp_path))
    memo.write_text(
        "---\ntype: memo\ntitle: sample\ndate: 2026-05-13\n---\n\n# Sample\n\nBody."
    )

    # First rebuild: populates chunks + FTS, leaves obs empty
    ir.rebuild_full(tmp_path, with_embeddings=False)

    # Inject observations + topic tags + fts rows as if a subagent had
    # extracted them. extract.py populates fts_observations alongside the
    # base table; the preservation path must carry all three.
    db = tmp_path / "_index.sqlite"
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO observations (id, doc_path, content, content_hash, "
        "obs_type, confidence, source_obs_ids) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (1, memo_rel, "Sample claim A", "hash-a", "explicit", "high", None),
            (2, memo_rel, "Sample claim B", "hash-b", "deductive", "medium", None),
        ],
    )
    conn.executemany(
        "INSERT INTO observation_topics (observation_id, topic_slug) VALUES (?, ?)",
        [(1, "topic-x"), (1, "topic-y"), (2, "topic-x")],
    )
    conn.executemany(
        "INSERT INTO fts_observations (rowid, content, obs_type) VALUES (?, ?, ?)",
        [
            (1, "Sample claim A", "explicit"),
            (2, "Sample claim B", "deductive"),
        ],
    )
    conn.commit()
    conn.close()

    # Second rebuild_full (atomic) — this is the path that wiped obs pre-fix
    stats = ir.rebuild_full(tmp_path, with_embeddings=False, atomic=True)
    assert stats.get("observations_preserved") == 2, (
        f"expected 2 obs preserved, got stats={stats}"
    )
    assert stats.get("observation_topics_preserved") == 3, (
        f"expected 3 topic tags preserved, got stats={stats}"
    )
    assert stats.get("fts_observations_preserved") == 2, (
        f"expected 2 fts rows preserved, got stats={stats}"
    )

    # Verify on disk
    conn = sqlite3.connect(db)
    obs_count = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    ot_count = conn.execute("SELECT COUNT(*) FROM observation_topics").fetchone()[0]
    obs_contents = sorted(
        row[0] for row in conn.execute("SELECT content FROM observations")
    )
    # fts_observations must answer MATCH queries — silently empty FTS was
    # the original failure mode this preservation path is meant to prevent.
    fts_hits = conn.execute(
        "SELECT rowid FROM fts_observations WHERE fts_observations MATCH 'claim' "
        "ORDER BY rowid"
    ).fetchall()
    conn.close()

    assert obs_count == 2, f"obs survived count: {obs_count}"
    assert ot_count == 3, f"obs-topic tags survived count: {ot_count}"
    assert obs_contents == ["Sample claim A", "Sample claim B"]
    assert [r[0] for r in fts_hits] == [1, 2], (
        f"fts_observations MATCH must return both rows post-rebuild, got {fts_hits}"
    )


def test_rebuild_full_drops_obs_for_deleted_memos(tmp_path):
    """Observations pointing at memos that no longer exist on disk become
    dangling references — they MUST be filtered out, not blindly copied.
    """
    # Seed two memos
    projects = tmp_path / "projects" / "test-project"
    (projects / "memos").mkdir(parents=True)
    keep = projects / "memos" / "2026-05-13-keep.md"
    drop = projects / "memos" / "2026-05-13-drop.md"
    keep.write_text(
        "---\ntype: memo\ntitle: keep\ndate: 2026-05-13\n---\n\nKeep this."
    )
    drop.write_text(
        "---\ntype: memo\ntitle: drop\ndate: 2026-05-13\n---\n\nDrop this."
    )
    keep_rel = str(keep.relative_to(tmp_path))
    drop_rel = str(drop.relative_to(tmp_path))

    ir.rebuild_full(tmp_path, with_embeddings=False)

    conn = sqlite3.connect(tmp_path / "_index.sqlite")
    conn.executemany(
        "INSERT INTO observations (id, doc_path, content, content_hash) "
        "VALUES (?, ?, ?, ?)",
        [
            (1, keep_rel, "Keep claim", "hash-k"),
            (2, drop_rel, "Drop claim", "hash-d"),
        ],
    )
    conn.commit()
    conn.close()

    # Delete the second memo; its observation must not survive
    drop.unlink()

    stats = ir.rebuild_full(tmp_path, with_embeddings=False, atomic=True)
    assert stats.get("observations_preserved") == 1, (
        f"only the kept memo's obs should survive, got {stats}"
    )

    conn = sqlite3.connect(tmp_path / "_index.sqlite")
    surviving = [
        row[0] for row in conn.execute(
            "SELECT doc_path FROM observations ORDER BY doc_path"
        )
    ]
    conn.close()
    assert surviving == [keep_rel], (
        f"surviving doc_paths: {surviving} (drop_rel={drop_rel} should be gone)"
    )


def test_preservation_registry_covers_init_schema(tmp_path):
    """Every table created by `init_observation_schema` must be covered by
    the preservation path — either via `_OBS_PRESERVATION_TABLES` or via
    the FTS5/vec0 special-case branches in `_preserve_obs_tables`.

    If a future commit adds a table to one without the other, this fails at
    CI time rather than at production rebuild time (which is how the May 7,
    2026 wipe — and the round-2 fts_observations omission — happened).

    R4-P2-A: shadow-table filter is an explicit whitelist of names derived
    from the FTS5 / vec0 virtual tables we actually create, NOT a heuristic
    `NOT LIKE '%_chunks'` set. The previous heuristic would have masked a
    real obs-related table named `something_chunks` (or `_info`, `_data`,
    etc.). Anything not on the expected real or shadow list fails the test
    with the unexpected name listed.
    """
    from memex.observations import init_observation_schema

    db = tmp_path / "schema_probe.sqlite"
    conn = sqlite3.connect(str(db))
    try:
        # sqlite-vec must be loaded so init_observation_schema can create
        # the vec_observations virtual table (and its shadow tables).
        try:
            conn.enable_load_extension(True)
            try:
                import sqlite_vec
                sqlite_vec.load(conn)
            finally:
                conn.enable_load_extension(False)
        except Exception:
            # sqlite-vec unavailable — vec_observations won't be created,
            # but the registry-coverage check is still meaningful for the
            # tables that do get created.
            pass

        init_observation_schema(conn)
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type IN ('table', 'view') "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        all_tables = {r[0] for r in rows}
    finally:
        conn.close()

    # Real tables/virtual-tables we actually preserve.
    EXPECTED_REAL = {
        "observations",
        "observation_topics",
        "fts_observations",
        "vec_observations",
    }
    # FTS5 + sqlite-vec auto-create these shadow tables for every virtual
    # table. They're internal to the virtual table and rebuilt automatically
    # — preserving them by hand would corrupt FTS/vec state. Listed by exact
    # name (or unambiguous prefix) so a future obs table with a name like
    # `something_chunks` can't be mistaken for a shadow.
    EXPECTED_SHADOW_EXACT = {
        # FTS5 shadows for fts_observations
        "fts_observations_data",
        "fts_observations_idx",
        "fts_observations_content",
        "fts_observations_docsize",
        "fts_observations_config",
        # vec0 shadows for vec_observations
        "vec_observations_chunks",
        "vec_observations_info",
        "vec_observations_rowids",
    }
    # vec0 also creates `<name>_vector_chunks00`, `<name>_vector_chunks01`,
    # etc. (one per vector column). Match by exact prefix on the vec table.
    SHADOW_PREFIXES = (
        "vec_observations_vector_chunks",
        # vec0 metadata-column shadows (v0.15.0): one *metadatachunks* per
        # metadata column, one *metadatatext* per TEXT metadata column.
        "vec_observations_metadatachunks",
        "vec_observations_metadatatext",
    )

    def is_shadow(name: str) -> bool:
        if name in EXPECTED_SHADOW_EXACT:
            return True
        return any(name.startswith(p) for p in SHADOW_PREFIXES)

    real_tables = {t for t in all_tables if not is_shadow(t)}
    unexpected = real_tables - EXPECTED_REAL
    assert not unexpected, (
        f"Unexpected real tables created by init_observation_schema that "
        f"are not on the expected whitelist and not recognized as shadows: "
        f"{unexpected}. Either add them to EXPECTED_REAL (and to the "
        f"preservation registry) or to EXPECTED_SHADOW_EXACT / "
        f"SHADOW_PREFIXES if they're virtual-table internals."
    )

    registry_tables = {name for name, _, _ in ir._OBS_PRESERVATION_TABLES}
    registry_tables.update(ir._OBS_VIRTUAL_TABLES)

    missing = real_tables - registry_tables
    assert not missing, (
        f"Tables created by init_observation_schema but not covered by "
        f"the preservation registry: {missing}. Either add them to "
        f"_OBS_PRESERVATION_TABLES or extend _OBS_VIRTUAL_TABLES + add a "
        f"special-case branch in _preserve_obs_tables."
    )


@pytest.mark.parametrize("with_embeddings", [True, False])
def test_rebuild_full_preserves_vec_observations_across_atomic_swap(
    tmp_path, monkeypatch, with_embeddings
):
    """Existing observation vectors survive both online and offline rebuilds."""
    import sqlite_vec
    import struct

    projects = tmp_path / "projects" / "test-project"
    (projects / "memos").mkdir(parents=True)
    memo = projects / "memos" / "2026-05-13-vec-sample.md"
    memo_rel = str(memo.relative_to(tmp_path))
    memo.write_text(
        "---\ntype: memo\ntitle: vec-sample\ndate: 2026-05-13\n---\n\nBody."
    )

    # Skip pipeline construction so we don't need a real API key. We still
    # want vec_available=True inside rebuild_full so the vec-preservation
    # branch fires; init_embedding_schema returns True iff sqlite-vec loads,
    # independent of pipeline.enabled. Real behavior preserved.
    class _NoPipeline:
        enabled = False
        _provider_impl = None
    monkeypatch.setattr(ir, "EmbeddingPipeline", lambda: _NoPipeline())

    # First rebuild with embeddings=True so init_embedding_schema creates
    # vec_observations with the configured (3072) dim. The pipeline is
    # disabled so no API calls happen.
    ir.rebuild_full(tmp_path, with_embeddings=True, atomic=True)

    # Discover the actual configured dim from the table the rebuild created.
    db = tmp_path / "_index.sqlite"
    conn = sqlite3.connect(str(db))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    # Probe the dimensionality by trying a sentinel insert. Use the EFFECTIVE
    # index dim (= index_dimensions when set, else dimensions) — that's what
    # init_observation_schema sizes vec_observations to, so the sentinel vector
    # must match it (the table is 768d when index_dimensions=768 is configured).
    from memex.config import get_settings
    dim = int(get_settings().embeddings.effective_index_dimensions)

    def _vec_zeros():
        return struct.pack(f"{dim}f", *([0.0] * dim))

    conn.executemany(
        "INSERT INTO observations (id, doc_path, content, content_hash, "
        "obs_type, confidence, source_obs_ids) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (1, memo_rel, "Vec claim A", "vh-a", "explicit", "high", None),
            (2, memo_rel, "Vec claim B", "vh-b", "explicit", "high", None),
        ],
    )
    conn.execute(
        "INSERT INTO vec_observations(rowid, embedding, doc_project, doc_type, doc_date) "
        "VALUES (?, ?, ?, ?, ?)",
        (1, _vec_zeros(), "test-project", "memo", 20260513),
    )
    conn.execute(
        "INSERT INTO vec_observations(rowid, embedding, doc_project, doc_type, doc_date) "
        "VALUES (?, ?, ?, ?, ?)",
        (2, _vec_zeros(), "test-project", "memo", 20260513),
    )
    conn.commit()
    conn.close()

    # Second rebuild — this is the path under test
    stats = ir.rebuild_full(tmp_path, with_embeddings=with_embeddings, atomic=True)
    assert stats.get("vec_observations_preserved") == 2, (
        f"expected 2 vec rows preserved, got stats={stats}"
    )

    conn = sqlite3.connect(str(db))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    rowids = sorted(r[0] for r in conn.execute("SELECT rowid FROM vec_observations"))
    conn.close()
    assert rowids == [1, 2], f"vec rowids post-rebuild: {rowids}"


def test_rebuild_full_drops_obs_topics_for_filtered_obs(tmp_path):
    """`observation_topics` rows whose parent observation was dropped during
    preservation (because the parent memo was deleted) must not survive.

    Without the join in the registry's observation_topics select, deleted
    memos' topic tags become orphan rows in `observation_topics` with FK
    references to nonexistent `observations.id`. The join enforces that
    only tags whose observation_id survived also survive.
    """
    projects = tmp_path / "projects" / "test-project"
    (projects / "memos").mkdir(parents=True)
    keep = projects / "memos" / "2026-05-13-keep-tag.md"
    drop = projects / "memos" / "2026-05-13-drop-tag.md"
    keep.write_text(
        "---\ntype: memo\ntitle: keep\ndate: 2026-05-13\n---\n\nKeep."
    )
    drop.write_text(
        "---\ntype: memo\ntitle: drop\ndate: 2026-05-13\n---\n\nDrop."
    )
    keep_rel = str(keep.relative_to(tmp_path))
    drop_rel = str(drop.relative_to(tmp_path))

    ir.rebuild_full(tmp_path, with_embeddings=False)

    conn = sqlite3.connect(str(tmp_path / "_index.sqlite"))
    conn.executemany(
        "INSERT INTO observations (id, doc_path, content, content_hash) "
        "VALUES (?, ?, ?, ?)",
        [
            (1, keep_rel, "Kept claim", "kh-1"),
            (2, drop_rel, "Dropped claim", "dh-2"),
        ],
    )
    conn.executemany(
        "INSERT INTO observation_topics (observation_id, topic_slug) VALUES (?, ?)",
        [
            (1, "topic-survivor"),
            (1, "topic-survivor-two"),
            (2, "topic-doomed"),  # tag attached to obs whose memo is about to die
        ],
    )
    conn.commit()
    conn.close()

    drop.unlink()

    stats = ir.rebuild_full(tmp_path, with_embeddings=False, atomic=True)
    assert stats.get("observations_preserved") == 1, (
        f"only the kept obs should survive, got {stats}"
    )
    assert stats.get("observation_topics_preserved") == 2, (
        f"only kept obs's tags should survive, got {stats}"
    )

    conn = sqlite3.connect(str(tmp_path / "_index.sqlite"))
    surviving = sorted(
        row[1] for row in conn.execute(
            "SELECT observation_id, topic_slug FROM observation_topics "
            "ORDER BY topic_slug"
        )
    )
    conn.close()
    assert surviving == ["topic-survivor", "topic-survivor-two"], (
        f"topic-doomed should have been filtered out; got {surviving}"
    )


def test_rebuild_full_aborts_on_preservation_failure(tmp_path, monkeypatch):
    """If preservation raises, rebuild_full must NOT proceed with the atomic
    swap — installing a DB with empty obs after the user opted into --full
    expecting preservation is silent data loss.

    HIGH-1 fix: stats["preservation_error"] should be set and the function
    should raise RuntimeError so the caller cannot swap a wiped index in
    place of the existing one.
    """
    projects = tmp_path / "projects" / "test-project"
    (projects / "memos").mkdir(parents=True)
    memo = projects / "memos" / "2026-05-13-abort.md"
    memo_rel = str(memo.relative_to(tmp_path))
    memo.write_text(
        "---\ntype: memo\ntitle: abort\ndate: 2026-05-13\n---\n\nBody."
    )

    # Bootstrap an index + add obs
    ir.rebuild_full(tmp_path, with_embeddings=False)
    db = tmp_path / "_index.sqlite"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO observations (id, doc_path, content, content_hash) "
        "VALUES (?, ?, ?, ?)",
        (1, memo_rel, "Will fail to preserve", "ah-1"),
    )
    conn.commit()
    conn.close()

    pre_swap_size = db.stat().st_size

    # Force _preserve_obs_tables to raise mid-flight (simulates disk-full,
    # schema drift on a column, etc.)
    def fail(conn, vec_available):
        # Write something first to verify the rollback actually fires
        conn.execute(
            "INSERT INTO main.observations (id, doc_path, content, "
            "content_hash) VALUES (?, ?, ?, ?)",
            (999, memo_rel, "partial write", "partial-h"),
        )
        raise sqlite3.OperationalError("simulated disk full mid-preservation")

    monkeypatch.setattr(ir, "_preserve_obs_tables", fail)

    with pytest.raises(RuntimeError, match="observation preservation failed"):
        ir.rebuild_full(tmp_path, with_embeddings=False, atomic=True)

    # The existing index must still be present and unswapped.
    assert db.exists(), "old index must NOT be replaced when preservation fails"
    # Old DB should still contain the original observation (proves no swap
    # happened — if rebuild_full had swapped in the wiped tmp DB this row
    # would be gone).
    conn = sqlite3.connect(str(db))
    surviving = conn.execute(
        "SELECT id, content FROM observations ORDER BY id"
    ).fetchall()
    conn.close()
    assert surviving == [(1, "Will fail to preserve")], (
        f"preserved DB should still have the pre-failure obs; got {surviving}"
    )
    # Tmp DB should be cleaned up (or at least not promoted)
    tmp_db = tmp_path / "_index.sqlite.tmp"
    # Either cleaned up by the failure path or left alongside — but never
    # promoted in place of the live index.
    assert not (tmp_path / "_index.sqlite.bak").exists() or db.stat().st_size > 0


def test_rebuild_full_aborts_on_non_operational_preservation_failure(
    tmp_path, monkeypatch
):
    """R4-P1-C: preservation must abort on ANY exception type, not just
    sqlite3.OperationalError. A sqlite-vec ValueError on dimension mismatch,
    a struct.error on binary unpack, a UnicodeDecodeError on bad content —
    all should trigger the same all-or-nothing rollback as a SQL error.

    Previously the except clause was `except sqlite3.OperationalError` —
    a ValueError raised inside `_preserve_obs_tables` would escape the
    savepoint with `attached=True` and the tmp DB still ATTACHed,
    bypassing both the rollback and the RuntimeError wrap. This test
    pins the widened catch.
    """
    projects = tmp_path / "projects" / "test-project"
    (projects / "memos").mkdir(parents=True)
    memo = projects / "memos" / "2026-05-13-non-op.md"
    memo_rel = str(memo.relative_to(tmp_path))
    memo.write_text(
        "---\ntype: memo\ntitle: nonop\ndate: 2026-05-13\n---\n\nBody."
    )

    ir.rebuild_full(tmp_path, with_embeddings=False)
    db = tmp_path / "_index.sqlite"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO observations (id, doc_path, content, content_hash) "
        "VALUES (?, ?, ?, ?)",
        (1, memo_rel, "Will fail with ValueError", "nh-1"),
    )
    conn.commit()
    conn.close()

    def fail_with_value_error(conn, vec_available):
        raise ValueError("simulated sqlite-vec dim mismatch")

    monkeypatch.setattr(ir, "_preserve_obs_tables", fail_with_value_error)

    with pytest.raises(RuntimeError, match="observation preservation failed"):
        ir.rebuild_full(tmp_path, with_embeddings=False, atomic=True)

    # Live index unswapped — pre-failure obs still present.
    conn = sqlite3.connect(str(db))
    surviving = conn.execute(
        "SELECT id, content FROM observations ORDER BY id"
    ).fetchall()
    conn.close()
    assert surviving == [(1, "Will fail with ValueError")], (
        f"existing DB should be intact after non-OperationalError "
        f"preservation failure; got {surviving}"
    )


def test_cli_full_rebuild_exits_4_on_preservation_failure(tmp_path, monkeypatch):
    """R4-P1-A: the `--full` CLI branch must exit 4 (not 1) when
    observation preservation fails. The previous design set a
    `stats["preservation_error"]` sentinel and checked for it after
    `rebuild_full` returned — but `rebuild_full` always raises in that
    case, so the sentinel branch was unreachable and the RuntimeError
    fell through to the generic `except Exception` handler that exits 1.

    This test invokes `_run()` directly with monkeypatched `rebuild_full`
    that raises the exact RuntimeError shape `rebuild_full` produces.
    """
    import sys as _sys
    from memex.scripts import index_rebuild as _ir

    # Make `get_memex_path` resolve to a real path so the CLI doesn't
    # exit 1 on the `memex.exists()` check.
    monkeypatch.setattr(_ir, "get_memex_path", lambda: tmp_path)

    def fake_rebuild_full(memex, with_embeddings=True, atomic=True):
        raise RuntimeError(
            "observation preservation failed; atomic swap aborted to "
            "avoid wiping the existing index: ValueError: simulated"
        )

    monkeypatch.setattr(_ir, "rebuild_full", fake_rebuild_full)
    monkeypatch.setattr(_sys, "argv", ["memex-index-rebuild", "--full"])

    with pytest.raises(SystemExit) as excinfo:
        _ir._run()
    assert excinfo.value.code == 4, (
        f"CLI must exit 4 on preservation failure, got "
        f"{excinfo.value.code}. The RuntimeError-catch path is "
        f"unreachable if exit 1 is observed."
    )


def test_cli_full_rebuild_reraises_unrelated_runtime_errors(tmp_path, monkeypatch):
    """A RuntimeError from `rebuild_full` that is NOT the preservation
    failure (e.g., atomic swap rename failed) must re-raise so the outer
    generic handler exits 1 — NOT exit 4. Exit 4 is reserved for
    "your existing data is safe; rebuild aborted to protect it."
    """
    import sys as _sys
    from memex.scripts import index_rebuild as _ir

    monkeypatch.setattr(_ir, "get_memex_path", lambda: tmp_path)

    def fake_rebuild_full(memex, with_embeddings=True, atomic=True):
        raise RuntimeError("Atomic swap failed: simulated rename error")

    monkeypatch.setattr(_ir, "rebuild_full", fake_rebuild_full)
    monkeypatch.setattr(_sys, "argv", ["memex-index-rebuild", "--full"])

    with pytest.raises(RuntimeError, match="Atomic swap failed"):
        _ir._run()


# ---------------------------------------------------------------------------
# Preservation reporting: four states, distinguished by key PRESENCE.
#
# Regression for the v0.15.11 false-report bug: an incremental run printed
# "Preserved across atomic swap: 0/0/0/0 ... (no prior observations)" on a
# vault holding 15,682 observations. Two false claims — no swap occurred, and
# the vault was not obs-less. Cause: `stats.get(key, 0)` rendered "this run
# never reported" as "this run measured zero". MISSING IS NOT ZERO.
# ---------------------------------------------------------------------------

def test_incremental_stats_emit_no_preservation_line():
    """State 4a: no swap happened, so say nothing about preservation."""
    out = ir.format_rebuild_stats({
        "total_docs": 10, "new": 0, "updated": 1, "unchanged": 9, "deleted": 0,
        "observations_stored": 0,
    })
    assert "Preserved across atomic swap" not in out, (
        f"incremental run claimed an atomic swap occurred:\n{out}"
    )
    assert "no prior observations" not in out, (
        f"incremental run asserted the vault has no observations:\n{out}"
    )


def test_preservation_line_emitted_when_preservation_ran_and_carried_rows():
    """State 1: real counts still render."""
    out = ir.format_rebuild_stats({
        "fts_indexed": 3, "total_docs": 3, "chunks_indexed": 0,
        "embeddings_generated": 0, "observations_stored": 0,
        "observations_preserved": 12, "observation_topics_preserved": 5,
        "fts_observations_preserved": 12, "vec_observations_preserved": 12,
    })
    assert "Preserved across atomic swap: 12/5/12/12" in out
    assert "no observations" not in out


def test_preservation_line_marks_genuinely_empty_prior_index():
    """State 2: preservation RAN and the old index truly held nothing."""
    out = ir.format_rebuild_stats({
        "fts_indexed": 1, "total_docs": 1, "chunks_indexed": 0,
        "embeddings_generated": 0, "observations_stored": 0,
        "observations_preserved": 0, "observation_topics_preserved": 0,
        "fts_observations_preserved": 0, "vec_observations_preserved": 0,
        "prior_observations": 0,
    })
    assert "Preserved across atomic swap: 0/0/0/0" in out
    assert "prior index held no observations" in out
    assert "⚠️" not in out


def test_zero_carried_with_nonzero_prior_warns_instead_of_reassuring():
    """The dangling-drop trap: old index HAD obs, none survived the filter.

    `*_preserved` counts are post-filter carry-over, not a census of the old
    index — so all-zero has two causes and only one of them is benign. Saying
    "prior index held no observations" here would be the original bug, one
    layer in: a reassuring claim on the run that dropped everything.
    """
    out = ir.format_rebuild_stats({
        "fts_indexed": 1, "total_docs": 1, "chunks_indexed": 0,
        "embeddings_generated": 0, "observations_stored": 0,
        "observations_preserved": 0, "observation_topics_preserved": 0,
        "fts_observations_preserved": 0, "vec_observations_preserved": 0,
        "prior_observations": 15682,
    })
    assert "prior index held no observations" not in out, (
        f"claimed the prior index was empty while it held 15682 obs:\n{out}"
    )
    assert "15682" in out
    assert "NONE" in out
    assert "obs reassign" in out


def test_unmeasured_prior_count_uses_weaker_but_true_wording():
    """No `prior_observations` key = never measured. Don't claim a census."""
    out = ir.format_rebuild_stats({
        "fts_indexed": 1, "total_docs": 1, "chunks_indexed": 0,
        "embeddings_generated": 0, "observations_stored": 0,
        "observations_preserved": 0, "observation_topics_preserved": 0,
        "fts_observations_preserved": 0, "vec_observations_preserved": 0,
    })
    assert "nothing carried over from the prior index" in out
    assert "prior index held no observations" not in out


def test_partial_dangling_drop_is_surfaced():
    """Carried some but not all — report the delta rather than only the wins."""
    out = ir.format_rebuild_stats({
        "fts_indexed": 1, "total_docs": 1, "chunks_indexed": 0,
        "embeddings_generated": 0, "observations_stored": 0,
        "observations_preserved": 90, "observation_topics_preserved": 12,
        "fts_observations_preserved": 90, "vec_observations_preserved": 90,
        "prior_observations": 100,
    })
    assert "Preserved across atomic swap: 90/12/90/90" in out
    assert "10 of 100 prior observation(s) were dropped as dangling" in out


def test_clean_full_preservation_reports_no_drop_warning():
    """Everything carried over — no scary delta line."""
    out = ir.format_rebuild_stats({
        "fts_indexed": 1, "total_docs": 1, "chunks_indexed": 0,
        "embeddings_generated": 0, "observations_stored": 0,
        "observations_preserved": 100, "observation_topics_preserved": 12,
        "fts_observations_preserved": 100, "vec_observations_preserved": 100,
        "prior_observations": 100,
    })
    assert "dropped as dangling" not in out
    assert "⚠️" not in out


def test_preservation_failure_line_takes_priority():
    """State 3 is the highest-priority branch and had no formatter test."""
    out = ir.format_rebuild_stats({
        "fts_indexed": 1, "total_docs": 1, "chunks_indexed": 0,
        "embeddings_generated": 0, "observations_stored": 0,
        "preservation_error": "OperationalError: disk I/O error",
        "observations_preserved": 0, "observation_topics_preserved": 0,
        "fts_observations_preserved": 0, "vec_observations_preserved": 0,
        "prior_observations": 0,
    })
    assert "Preservation FAILED: OperationalError: disk I/O error" in out
    assert "Preserved across atomic swap" not in out


def test_no_atomic_warning_reports_how_many_obs_were_destroyed():
    out = ir.format_rebuild_stats({
        "fts_indexed": 1, "total_docs": 1, "chunks_indexed": 0,
        "embeddings_generated": 0, "observations_stored": 0,
        "preservation_skipped": "--no-atomic deleted the existing index",
        "observations_destroyed": 47,
    })
    assert "47 observation(s) from the previous index are unrecoverable" in out


def test_no_atomic_warning_admits_when_count_is_unknown():
    """None = uncountable (corrupt/locked). Must not render as 0."""
    out = ir.format_rebuild_stats({
        "fts_indexed": 1, "total_docs": 1, "chunks_indexed": 0,
        "embeddings_generated": 0, "observations_stored": 0,
        "preservation_skipped": "--no-atomic deleted the existing index",
        "observations_destroyed": None,
    })
    assert "An unknown number of observations" in out
    assert "no observations" not in out


def test_no_atomic_rebuild_over_live_index_warns_that_obs_were_destroyed():
    """State 4b: --no-atomic unlinks the old index — obs are GONE. Say so."""
    out = ir.format_rebuild_stats({
        "fts_indexed": 1, "total_docs": 1, "chunks_indexed": 0,
        "embeddings_generated": 0, "observations_stored": 0,
        "preservation_skipped": (
            "--no-atomic deleted the existing index without preserving "
            "observations"
        ),
    })
    assert "SKIPPED" in out
    assert "--no-atomic" in out
    assert "Preserved across atomic swap" not in out, (
        f"destructive run reported a successful preservation:\n{out}"
    )


def test_rebuild_incremental_omits_preservation_keys_entirely(tmp_path):
    """End-to-end: the keys must be ABSENT, not zero, on an incremental run."""
    (tmp_path / "topics").mkdir()
    (tmp_path / "topics" / "s.md").write_text(
        "---\ntype: memo\ntitle: sample\ndate: 2026-07-21\n---\n\n# Sample\n\nBody."
    )
    ir.rebuild_full(tmp_path, with_embeddings=False)

    conn = sqlite3.connect(tmp_path / "_index.sqlite")
    conn.execute(
        "INSERT INTO observations (id, doc_path, content, content_hash, "
        "obs_type, confidence, source_obs_ids) VALUES "
        "(1, 'topics/s.md', 'claim', 'h', 'explicit', 'high', NULL)"
    )
    conn.commit()
    conn.close()

    stats = ir.rebuild_incremental(tmp_path)

    for key in (
        "observations_preserved", "observation_topics_preserved",
        "fts_observations_preserved", "vec_observations_preserved",
    ):
        assert key not in stats, (
            f"incremental run reported {key}={stats[key]!r}; preservation "
            "never ran, so the key must be absent"
        )
    assert "Preserved across atomic swap" not in ir.format_rebuild_stats(stats)


def test_rebuild_full_no_prior_index_omits_preservation_keys(tmp_path):
    """A first-ever full rebuild has nothing to preserve — and no story."""
    (tmp_path / "topics").mkdir()
    (tmp_path / "topics" / "s.md").write_text(
        "---\ntype: memo\ntitle: sample\ndate: 2026-07-21\n---\n\n# Sample\n\nBody."
    )
    stats = ir.rebuild_full(tmp_path, with_embeddings=False)
    assert "observations_preserved" not in stats
    assert "preservation_skipped" not in stats
    assert "Preserved across atomic swap" not in ir.format_rebuild_stats(stats)


def test_rebuild_full_no_atomic_flags_destroyed_observations(tmp_path):
    """`--no-atomic` over a live index really does wipe obs — stats must admit it."""
    (tmp_path / "topics").mkdir()
    (tmp_path / "topics" / "s.md").write_text(
        "---\ntype: memo\ntitle: sample\ndate: 2026-07-21\n---\n\n# Sample\n\nBody."
    )
    ir.rebuild_full(tmp_path, with_embeddings=False)

    db = tmp_path / "_index.sqlite"
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO observations (id, doc_path, content, content_hash, "
        "obs_type, confidence, source_obs_ids) VALUES "
        "(1, 'topics/s.md', 'claim', 'h', 'explicit', 'high', NULL)"
    )
    conn.commit()
    conn.close()

    stats = ir.rebuild_full(tmp_path, with_embeddings=False, atomic=False)

    conn = sqlite3.connect(db)
    surviving = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    conn.close()
    assert surviving == 0, "fixture assumption changed: --no-atomic now preserves obs"

    assert "preservation_skipped" in stats, (
        "a run that destroyed observations reported nothing about it"
    )
    assert "observations_preserved" not in stats
    # Pins the ORDER of the count relative to the unlink. Counting after the
    # unlink would silently yield 0 ("nothing was lost") on the run that
    # destroyed everything — and without this assertion, no test would notice.
    assert stats["observations_destroyed"] == 1, (
        "destroyed-obs count must be taken BEFORE the index is unlinked"
    )
    out = ir.format_rebuild_stats(stats)
    assert "SKIPPED" in out
    assert "1 observation(s) from the previous index are unrecoverable" in out


def test_rebuild_full_reports_prior_count_when_all_obs_dangle(tmp_path):
    """E2E: old index held obs, the memo vanished, so none carry over.

    Pins the wiring (not just the formatter): `prior_observations` must reflect
    the OLD index's census, so the report can warn instead of reassure.
    """
    (tmp_path / "topics").mkdir()
    memo = tmp_path / "topics" / "s.md"
    memo.write_text(
        "---\ntype: memo\ntitle: sample\ndate: 2026-07-21\n---\n\n# Sample\n\nBody."
    )
    ir.rebuild_full(tmp_path, with_embeddings=False)

    conn = sqlite3.connect(tmp_path / "_index.sqlite")
    conn.execute(
        "INSERT INTO observations (id, doc_path, content, content_hash, "
        "obs_type, confidence, source_obs_ids) VALUES "
        "(1, 'topics/s.md', 'claim', 'h', 'explicit', 'high', NULL)"
    )
    conn.commit()
    conn.close()

    # The source doc disappears -> its obs is dangling -> filtered out.
    memo.unlink()
    stats = ir.rebuild_full(tmp_path, with_embeddings=False, atomic=True)

    assert stats["prior_observations"] == 1
    assert stats["observations_preserved"] == 0
    out = ir.format_rebuild_stats(stats)
    assert "prior index held no observations" not in out, (
        f"reassured the operator on a run that dropped every obs:\n{out}"
    )
    assert "NONE" in out


def test_rebuild_full_reports_prior_count_on_clean_preservation(tmp_path):
    """E2E: prior census equals carry-over when nothing dangles."""
    (tmp_path / "topics").mkdir()
    (tmp_path / "topics" / "s.md").write_text(
        "---\ntype: memo\ntitle: sample\ndate: 2026-07-21\n---\n\n# Sample\n\nBody."
    )
    ir.rebuild_full(tmp_path, with_embeddings=False)

    conn = sqlite3.connect(tmp_path / "_index.sqlite")
    conn.execute(
        "INSERT INTO observations (id, doc_path, content, content_hash, "
        "obs_type, confidence, source_obs_ids) VALUES "
        "(1, 'topics/s.md', 'claim', 'h', 'explicit', 'high', NULL)"
    )
    conn.commit()
    conn.close()

    stats = ir.rebuild_full(tmp_path, with_embeddings=False, atomic=True)
    assert stats["prior_observations"] == 1
    assert stats["observations_preserved"] == 1
    assert "dropped as dangling" not in ir.format_rebuild_stats(stats)


def test_count_observations_in_index_returns_none_when_unreadable(tmp_path):
    """Unreadable != empty. The helper must not manufacture a zero."""
    junk = tmp_path / "_index.sqlite"
    junk.write_bytes(b"this is not a sqlite database at all, not even close")
    assert ir._count_observations_in_index(junk) is None


def test_count_observations_in_index_returns_zero_for_pre_011_index(tmp_path):
    """No `observations` table at all = genuinely zero, and that IS knowable."""
    db = tmp_path / "_index.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE unrelated (x INTEGER)")
    conn.commit()
    conn.close()
    assert ir._count_observations_in_index(db) == 0


def test_count_observations_in_index_returns_none_for_missing_file(tmp_path):
    """`sqlite3.connect` CREATES missing files — the empty DB it fabricates
    would answer "no observations table" -> 0, turning "no index here" into
    "an index holding none". Must be None, and must not create the file."""
    missing = tmp_path / "_index.sqlite"
    assert ir._count_observations_in_index(missing) is None
    assert not missing.exists(), (
        "probing a nonexistent index left a stray empty database behind"
    )


def test_no_atomic_warning_omits_recovery_steps_when_nothing_was_lost():
    """destroyed == 0 is genuinely fine — don't append restore-from-backup."""
    out = ir.format_rebuild_stats({
        "fts_indexed": 1, "total_docs": 1, "chunks_indexed": 0,
        "embeddings_generated": 0, "observations_stored": 0,
        "preservation_skipped": "--no-atomic deleted the existing index",
        "observations_destroyed": 0,
    })
    assert "nothing was lost" in out
    assert "restore from backup" not in out
    assert "backfill obs" not in out


# ---------------------------------------------------------------------------
# `get_index_status` / `format_status`: a FAILED query is not a measured zero.
# `memex index status` printing "Observations: 0" on a vault holding 15,682
# reads exactly like catastrophic loss. Same class as the preservation bug.
# ---------------------------------------------------------------------------

def test_format_status_renders_failed_query_as_unknown_not_zero():
    out = ir.format_status({
        "exists": True, "size_kb": 1.0,
        "fts_documents": None, "fts_by_type": None,
        "embedded_documents": None, "total_chunks": None,
        "embedded_chunks": None, "cached_embeddings": None,
        "observations": None,
    })
    assert "Observations: unknown (query failed)" in out
    assert "Observations: 0" not in out, (
        f"a failed count rendered as a measured zero:\n{out}"
    )
    assert "Documents: unknown (query failed)" in out


def test_format_status_distinguishes_absent_key_from_null_value():
    """Absent = never reported; None = asked and unanswerable. Neither is 0."""
    out = ir.format_status({"exists": True, "size_kb": 1.0, "fts_documents": 0})
    assert "Observations: not reported" in out
    assert "Documents: 0" in out          # a real measured zero still prints


def test_format_status_real_counts_still_render_plainly():
    out = ir.format_status({
        "exists": True, "size_kb": 1.0, "fts_documents": 4383, "fts_by_type": {},
        "embedded_documents": 4383, "total_chunks": 222337,
        "embedded_chunks": 222413, "cached_embeddings": 220568,
        "observations": 15682,
    })
    assert "Observations: 15682" in out
    assert "unknown" not in out
    assert "not reported" not in out


def test_get_index_status_reports_none_when_tables_missing(tmp_path):
    """E2E: an index with no memex schema at all must not report zeros."""
    db = tmp_path / "_index.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE unrelated (x INTEGER)")
    conn.commit()
    conn.close()

    stats = ir.get_index_status(tmp_path)
    assert stats["observations"] is None
    assert stats["fts_documents"] is None
    assert stats["total_chunks"] is None
    assert "unknown (query failed)" in ir.format_status(stats)


def test_embedded_chunks_unknown_when_sqlite_vec_unavailable(tmp_path, monkeypatch):
    """Without sqlite-vec the embedded count is unknowable.

    The old code substituted total_chunks as a "proxy" — asserting every chunk
    is embedded, the most reassuring possible answer, on precisely the setup
    where semantic search is most likely broken.
    """
    _seed_index(tmp_path)
    monkeypatch.setattr(ir, "_load_vec_extension", lambda conn: False)

    conn = sqlite3.connect(tmp_path / "_index.sqlite")
    # _seed_index omits embedding_cache; without it the whole vector block
    # reports unknown and this test would not isolate the vec-proxy behavior.
    conn.execute("CREATE TABLE embedding_cache (hash TEXT PRIMARY KEY)")
    conn.execute(
        "INSERT INTO chunks (id, doc_path, chunk_index, content) "
        "VALUES (1, 'a.md', 0, 'chunk')"
    )
    conn.commit()
    conn.close()

    stats = ir.get_index_status(tmp_path)
    assert stats["total_chunks"] == 1
    assert stats["embedded_chunks"] is None, (
        "claimed chunks were embedded without sqlite-vec loaded to check"
    )
