"""Tests for v0.15.0 vec0 metadata + Matryoshka-truncation migration.

Covers the truncation helper, the in-place `migrate_vec_metadata` migration
(schema change + dimension truncation without re-embedding), KNN filter
pushdown, idempotency, and backward-compatibility with pre-migration indexes.

Uses tiny synthetic vectors (8d → 4d) so the truncation/migration logic is
exercised without the real 3072→768 cost.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import sqlite_vec  # noqa: E402

from memex.scripts import embeddings as emb  # noqa: E402
from memex.scripts import index_rebuild as ir  # noqa: E402


def _vec(xs: list[float]) -> bytes:
    return struct.pack(f"{len(xs)}f", *xs)


def _floats(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob)//4}f", blob))


# ---------------------------------------------------------------------------
# truncate_unit_vector
# ---------------------------------------------------------------------------

def test_truncate_unit_vector_length_and_norm():
    blob = _vec([0.5] * 8)  # not unit-norm at 8d
    out = emb.truncate_unit_vector(blob, 4)
    fl = _floats(out)
    assert len(fl) == 4
    assert abs(sum(x * x for x in fl) ** 0.5 - 1.0) < 1e-6


def test_truncate_unit_vector_noop_when_target_ge_dim():
    blob = _vec([0.1, 0.2, 0.3, 0.4])
    assert emb.truncate_unit_vector(blob, 4) == blob
    assert emb.truncate_unit_vector(blob, 8) == blob
    assert emb.truncate_unit_vector(blob, 0) == blob


def test_truncate_preserves_direction_prefix():
    # First-N dims preserved up to a positive scalar (renormalization).
    blob = _vec([3.0, 4.0, 1.0, 1.0])  # first-2 prefix is (3,4)
    out = _floats(emb.truncate_unit_vector(blob, 2))
    # (3,4) normalized -> (0.6, 0.8)
    assert abs(out[0] - 0.6) < 1e-6
    assert abs(out[1] - 0.8) < 1e-6


# ---------------------------------------------------------------------------
# date / project helpers
# ---------------------------------------------------------------------------

def test_date_to_yyyymmdd():
    assert emb.date_to_yyyymmdd("2026-06-17") == 20260617
    assert emb.date_to_yyyymmdd("projects/x/memos/2026-01-02-foo.md") == 20260102
    assert emb.date_to_yyyymmdd("") == 0
    assert emb.date_to_yyyymmdd("no-date-here") == 0


def test_project_from_path():
    assert emb.project_from_path("projects/memex/memos/x.md") == "memex"
    assert emb.project_from_path("topics/y.md") == ""
    assert emb.project_from_path("") == ""


# ---------------------------------------------------------------------------
# Migration end-to-end on a synthetic index
# ---------------------------------------------------------------------------

def _build_premigration_index(path: Path, dim: int = 8):
    """A pre-v0.15.0 index: bare vec0 tables (embedding only), 3 chunks."""
    import sqlite3

    conn = sqlite3.connect(path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    conn.execute(
        "CREATE TABLE chunks (id INTEGER PRIMARY KEY, doc_path TEXT, "
        "chunk_index INTEGER, content TEXT, content_hash TEXT, "
        "doc_date TEXT DEFAULT '', doc_project TEXT DEFAULT '')"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE fts_content USING fts5(path, title, content, type, project, date)"
    )
    conn.execute(
        "CREATE TABLE observations (id INTEGER PRIMARY KEY, doc_path TEXT, content TEXT)"
    )
    conn.execute(f"CREATE VIRTUAL TABLE vec_chunks USING vec0(embedding float[{dim}])")
    conn.execute(f"CREATE VIRTUAL TABLE vec_observations USING vec0(embedding float[{dim}])")

    rows = [
        # (id, doc_path, project, date, type, embedding)
        (1, "projects/memex/memos/2026-06-17-a.md", "memex", "2026-06-17", "memo",
         [1, 0, 0, 0, 0, 0, 0, 0]),
        (2, "projects/memex/transcripts/2026-01-01-b.md", "memex", "2026-01-01", "transcript",
         [0.9, 0.1, 0, 0, 0, 0, 0, 0]),
        (3, "projects/alcor/memos/2026-06-15-c.md", "alcor", "2026-06-15", "memo",
         [0.8, 0.2, 0, 0, 0, 0, 0, 0]),
    ]
    for cid, dpath, proj, date, typ, vec in rows:
        conn.execute(
            "INSERT INTO chunks (id, doc_path, chunk_index, content, content_hash, doc_date, doc_project) "
            "VALUES (?, ?, 0, ?, ?, ?, ?)",
            (cid, dpath, f"content {cid}", f"hash{cid}", date, proj),
        )
        conn.execute(
            "INSERT INTO fts_content (path, title, content, type, project, date) VALUES (?,?,?,?,?,?)",
            (dpath, f"title {cid}", f"content {cid}", typ, proj, date),
        )
        conn.execute("INSERT INTO vec_chunks(rowid, embedding) VALUES (?, ?)", (cid, _vec(vec)))
        conn.execute("INSERT INTO observations (id, doc_path, content) VALUES (?, ?, ?)",
                     (cid, dpath, f"obs {cid}"))
        conn.execute("INSERT INTO vec_observations(rowid, embedding) VALUES (?, ?)", (cid, _vec(vec)))

    conn.commit()
    conn.close()


def test_migration_truncates_and_adds_metadata(tmp_path, monkeypatch):
    _build_premigration_index(tmp_path / "_index.sqlite", dim=8)
    monkeypatch.setattr(emb, "get_vector_dimensions", lambda config=None: 4)

    stats = ir.migrate_vec_metadata(tmp_path, batch_size=2)
    assert stats["error"] is None
    assert stats["vec_chunks"] == "migrated"
    assert stats["chunks_migrated"] == 3
    assert stats["vec_observations"] == "migrated"
    assert stats["target_dim"] == 4

    import sqlite3
    conn = sqlite3.connect(tmp_path / "_index.sqlite")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    # Dimension truncated to 4
    row = conn.execute("SELECT embedding FROM vec_chunks LIMIT 1").fetchone()
    assert len(row[0]) // 4 == 4
    # Metadata columns present + populated
    cols = {r[1] for r in conn.execute("PRAGMA table_info(vec_chunks)")}
    assert {"doc_project", "doc_type", "doc_date"}.issubset(cols)
    meta = conn.execute(
        "SELECT doc_project, doc_type, doc_date FROM vec_chunks WHERE rowid = 1"
    ).fetchone()
    assert meta == ("memex", "memo", 20260617)
    conn.close()


def test_migration_enables_filter_pushdown(tmp_path, monkeypatch):
    _build_premigration_index(tmp_path / "_index.sqlite", dim=8)
    monkeypatch.setattr(emb, "get_vector_dimensions", lambda config=None: 4)
    ir.migrate_vec_metadata(tmp_path, batch_size=10)

    import sqlite3
    conn = sqlite3.connect(tmp_path / "_index.sqlite")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    q = emb.truncate_unit_vector(_vec([1, 0, 0, 0, 0, 0, 0, 0]), 4)

    # project filter
    got = conn.execute(
        "SELECT rowid FROM vec_chunks WHERE embedding MATCH ? AND k = 5 AND doc_project = ?",
        (q, "alcor"),
    ).fetchall()
    assert [r[0] for r in got] == [3]

    # date range filter (since 2026-06-01) -> rows 1 and 3
    got = conn.execute(
        "SELECT rowid FROM vec_chunks WHERE embedding MATCH ? AND k = 5 AND doc_date >= ?",
        (q, 20260601),
    ).fetchall()
    assert sorted(r[0] for r in got) == [1, 3]

    # combined project + type
    got = conn.execute(
        "SELECT rowid FROM vec_chunks WHERE embedding MATCH ? AND k = 5 "
        "AND doc_project = ? AND doc_type = ?",
        (q, "memex", "memo"),
    ).fetchall()
    assert [r[0] for r in got] == [1]
    conn.close()


def test_migration_idempotent(tmp_path, monkeypatch):
    _build_premigration_index(tmp_path / "_index.sqlite", dim=8)
    monkeypatch.setattr(emb, "get_vector_dimensions", lambda config=None: 4)

    first = ir.migrate_vec_metadata(tmp_path, batch_size=10)
    assert first["vec_chunks"] == "migrated"
    second = ir.migrate_vec_metadata(tmp_path, batch_size=10)
    assert second["vec_chunks"] == "skip"
    assert second["vec_observations"] == "skip"
    assert second["error"] is None


def test_vec_stored_dim_reports_table_dimension(tmp_path):
    import sqlite3
    p = tmp_path / "_index.sqlite"
    conn = sqlite3.connect(p)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    # absent table → None
    assert emb.vec_stored_dim(conn, "vec_chunks") is None
    conn.execute("CREATE VIRTUAL TABLE vec_chunks USING vec0(embedding float[8])")
    # present but empty → the declared float[N] (an emptied table — e.g. its
    # only vector was a pruned orphan — must still take N-dim inserts)
    assert emb.vec_stored_dim(conn, "vec_chunks") == 8
    conn.execute("INSERT INTO vec_chunks(rowid, embedding) VALUES (1, ?)", (_vec([0.0] * 8),))
    conn.commit()
    # populated → stored dim
    assert emb.vec_stored_dim(conn, "vec_chunks") == 8
    conn.close()


def test_insert_dim_aligns_to_table_not_config(tmp_path, monkeypatch):
    """Finding #1: in the post-config / pre-migrate window the table is still at
    the old dim. Insert paths resolve `vec_stored_dim(...) or config` so a write
    aligns to the LIVE table (8 here), never the mismatched config dim (4)."""
    import sqlite3
    p = tmp_path / "_index.sqlite"
    conn = sqlite3.connect(p)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("CREATE VIRTUAL TABLE vec_chunks USING vec0(embedding float[8])")
    conn.execute("INSERT INTO vec_chunks(rowid, embedding) VALUES (1, ?)", (_vec([0.1] * 8),))
    conn.commit()
    monkeypatch.setattr(emb, "get_vector_dimensions", lambda config=None: 4)
    # Table is 8d, config says 4d → the insert path must pick 8 (table wins).
    resolved = emb.vec_stored_dim(conn, "vec_chunks") or emb.get_vector_dimensions()
    assert resolved == 8
    # On an empty/fresh table, config dim is used.
    conn.execute("DROP TABLE vec_chunks")
    conn.execute("CREATE VIRTUAL TABLE vec_chunks USING vec0(embedding float[4])")
    conn.commit()
    resolved_empty = emb.vec_stored_dim(conn, "vec_chunks") or emb.get_vector_dimensions()
    assert resolved_empty == 4
    conn.close()


def test_match_query_dim_truncates_to_stored(tmp_path):
    import sqlite3
    p = tmp_path / "_index.sqlite"
    conn = sqlite3.connect(p)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("CREATE VIRTUAL TABLE vec_chunks USING vec0(embedding float[4])")
    conn.execute("INSERT INTO vec_chunks(rowid, embedding) VALUES (1, ?)",
                 (emb.truncate_unit_vector(_vec([1, 0, 0, 0]), 4),))
    conn.commit()

    full = _vec([1, 0, 0, 0, 0, 0, 0, 0])  # 8d query
    matched = emb.match_query_dim(conn, "vec_chunks", full)
    assert len(matched) // 4 == 4  # truncated to stored dim
    conn.close()


# ---------------------------------------------------------------------------
# v0.15.2 — embed_query returns native dim; query sites align per-table
# ---------------------------------------------------------------------------

def test_embed_query_returns_native_dimension(monkeypatch):
    """embed_query must NOT pre-truncate to index_dimensions. It returns the
    model's native vector; each query site truncates to its own table's stored
    dim. Pre-truncating would degrade vector search to FTS-only in the window
    after index_dimensions is lowered but before migrate-vec runs."""
    pipe = emb.EmbeddingPipeline.__new__(emb.EmbeddingPipeline)
    monkeypatch.setattr(pipe, "embed_text", lambda q, task_type=None: [0.5] * 8)
    # Config says index_dimensions=4, but embed_query must ignore it.
    monkeypatch.setattr(emb, "get_vector_dimensions", lambda config=None: 4)
    out = pipe.embed_query("hello")
    assert out is not None
    assert len(out) // 4 == 8  # native dim preserved, NOT truncated to 4


def test_embed_query_none_when_provider_returns_none(monkeypatch):
    pipe = emb.EmbeddingPipeline.__new__(emb.EmbeddingPipeline)
    monkeypatch.setattr(pipe, "embed_text", lambda q, task_type=None: None)
    assert pipe.embed_query("hello") is None


def test_ask_vector_candidates_aligns_native_query_to_truncated_table(tmp_path, monkeypatch):
    """ask._vector_candidates must align a native-dim query to vec_chunks'
    stored dim. A native 8d query against a migrated 4d table would raise a
    dimension mismatch (→ []) without match_query_dim; with it, the nearest
    row is returned."""
    from memex import ask

    _build_premigration_index(tmp_path / "_index.sqlite", dim=8)
    monkeypatch.setattr(emb, "get_vector_dimensions", lambda config=None: 4)
    ir.migrate_vec_metadata(tmp_path, batch_size=10)  # vec_chunks now 4d

    import sqlite3
    conn = sqlite3.connect(tmp_path / "_index.sqlite")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    native_q = _vec([1, 0, 0, 0, 0, 0, 0, 0])  # 8d, as embed_query now returns
    rows = ask._vector_candidates(conn, native_q, project=None, limit=5)
    assert rows, "native-dim query should match a truncated table via match_query_dim"
    assert rows[0]["path"] == "projects/memex/memos/2026-06-17-a.md"
    conn.close()


def test_ask_vector_candidates_project_filter_pushdown(tmp_path, monkeypatch):
    """_vector_candidates must run the KNN first (vec_chunks→chunks only) and
    push the project filter into the KNN via the v0.15.0 doc_project metadata
    column — never join fts_content inside the KNN (that join makes the planner
    scan fts_content as the outer loop and re-run the vec match per doc, an
    O(docs × KNN) hang on a real-size index)."""
    from memex import ask

    _build_premigration_index(tmp_path / "_index.sqlite", dim=8)
    monkeypatch.setattr(emb, "get_vector_dimensions", lambda config=None: 4)
    ir.migrate_vec_metadata(tmp_path, batch_size=10)

    import sqlite3
    conn = sqlite3.connect(tmp_path / "_index.sqlite")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    native_q = _vec([1, 0, 0, 0, 0, 0, 0, 0])
    rows = ask._vector_candidates(conn, native_q, project="alcor", limit=5)
    assert rows, "project-scoped KNN should still return the alcor row"
    assert all(r["project"] == "alcor" for r in rows)
    assert {r["path"] for r in rows} == {"projects/alcor/memos/2026-06-15-c.md"}
    conn.close()


# ---------------------------------------------------------------------------
# v0.15.2 — VACUUM reclaim after migrate-vec
# ---------------------------------------------------------------------------

def test_format_migrate_stats_includes_vacuum_hint():
    s = ir.format_migrate_stats({
        "target_dim": 768, "vec_chunks": "migrated", "chunks_migrated": 3,
        "vec_observations": "migrated", "observations_migrated": 2, "error": None,
    })
    assert "vacuum" in s.lower()


def test_vacuum_index_reclaims_space(tmp_path):
    import sqlite3
    p = tmp_path / "_index.sqlite"
    # Persistent WAL mode — the production journal mode (connect_index sets it).
    # In WAL, VACUUM stages into the -wal sidecar; the main file only shrinks
    # after a TRUNCATE checkpoint. This must exercise that path, not DELETE mode.
    conn = sqlite3.connect(p)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("CREATE TABLE big (id INTEGER PRIMARY KEY, blob BLOB)")
    conn.executemany("INSERT INTO big (blob) VALUES (?)",
                     [(b"x" * 4096,) for _ in range(2000)])
    conn.commit()
    conn.execute("DELETE FROM big")
    conn.commit()
    conn.close()

    stats = ir.vacuum_index(tmp_path)
    assert stats["error"] is None
    assert stats["size_after"] <= stats["size_before"]
    assert stats["reclaimed"] > 0
    # Main file actually compacted, and the -wal sidecar is gone (TRUNCATE).
    wal = p.with_name(p.name + "-wal")
    assert (not wal.exists()) or wal.stat().st_size == 0


def test_vacuum_index_missing_errors(tmp_path):
    stats = ir.vacuum_index(tmp_path)  # no _index.sqlite present
    assert stats["error"] is not None
    assert "no index" in stats["error"]


def test_format_vacuum_stats():
    s = ir.format_vacuum_stats({
        "size_before": 10 * 1024 * 1024, "size_after": 4 * 1024 * 1024,
        "reclaimed": 6 * 1024 * 1024, "error": None,
    })
    assert "VACUUM complete" in s
    assert "reclaimed" in s
    assert ir.format_vacuum_stats({"error": "boom"}).startswith("vacuum failed")
