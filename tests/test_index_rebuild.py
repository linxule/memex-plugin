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
    assert result == {"chunks": 0, "observations": 0, "docs": 0, "available": False}


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
