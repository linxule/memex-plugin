"""Retrieval regressions using local FTS5 and sqlite-vec, without providers."""

from __future__ import annotations

import sqlite3
import struct
from datetime import datetime

import pytest

from memex.db_utils import load_vec_extension
from memex.scripts import hybrid_search as hs


class LocalQueryPipeline:
    enabled = True

    def embed_query(self, query: str) -> bytes:
        return struct.pack("2f", 1.0, 0.0)


@pytest.fixture(params=[False, True], ids=["legacy-vec", "metadata-vec"])
def search_index(request):
    conn = sqlite3.connect(":memory:")
    assert load_vec_extension(conn)
    conn.execute(
        "CREATE VIRTUAL TABLE fts_content USING fts5(path, title, content, type, project, date)"
    )
    conn.execute("CREATE TABLE chunks (id INTEGER PRIMARY KEY, doc_path TEXT, content TEXT)")
    extra_columns = ", doc_project text, doc_type text, doc_date integer" if request.param else ""
    conn.execute(f"CREATE VIRTUAL TABLE vec_chunks USING vec0(embedding float[2]{extra_columns})")

    def insert(path, date, project="demo", embedding=(1.0, 0.0), chunks=1):
        conn.execute(
            "INSERT INTO fts_content VALUES (?, ?, ?, ?, ?, ?)",
            (path, f"Title {path}", "retrieval", "memo", project, date),
        )
        for _ in range(chunks):
            chunk_id = conn.execute(
                "INSERT INTO chunks(doc_path, content) VALUES (?, ?)", (path, "retrieval chunk"),
            ).lastrowid
            values = [chunk_id, struct.pack("2f", *embedding)]
            columns = "rowid, embedding"
            if request.param:
                columns += ", doc_project, doc_type, doc_date"
                values.extend([project, "memo", int(date.replace("-", "")) if date else 0])
            conn.execute(
                f"INSERT INTO vec_chunks({columns}) VALUES ({','.join('?' for _ in values)})",
                values,
            )
        conn.commit()

    try:
        yield conn, insert
    finally:
        conn.close()


@pytest.mark.parametrize("mode", ["fts", "vector", "hybrid"])
def test_date_filters_agree_at_day_boundaries(search_index, monkeypatch, mode):
    conn, insert = search_index
    insert("old.md", "2026-09-03")
    insert("cutoff.md", "2026-09-04")
    insert("included.md", "2026-09-05")
    insert("following-day.md", "2026-09-06")
    insert("undated.md", "")
    insert("other-project.md", "2026-09-05", project="other")
    # Relative --since expressions include the current time, while indexed
    # dates and the keyword predicate operate at calendar-day precision.
    monkeypatch.setattr(hs, "parse_since_duration", lambda value: datetime(2026, 9, 4, 13, 45))

    results = hs.hybrid_search(
        conn, "retrieval", pipeline=LocalQueryPipeline(), mode=mode,
        project="demo", since="2d", before="2026-09-05",
    )

    assert {result.path for result in results} == {"cutoff.md", "included.md"}


def test_vector_enrichment_scans_fts_once_for_repeated_chunks(search_index):
    conn, insert = search_index
    insert("first.md", "2026-09-05", chunks=12)
    insert("second.md", "2026-09-05", chunks=12)
    statements = []
    conn.set_trace_callback(statements.append)

    results = hs.hybrid_search(
        conn, "retrieval", pipeline=LocalQueryPipeline(), mode="vector",
        project="demo", since="2026-09-04", before="2026-09-05",
    )

    assert {result.path for result in results} == {"first.md", "second.md"}
    assert all(result.title == f"Title {result.path}" for result in results)
    metadata_reads = [
        sql for sql in statements
        if sql.lstrip().upper().startswith("SELECT ") and "FROM fts_content " in sql
    ]
    assert len(metadata_reads) == 1, metadata_reads


@pytest.mark.parametrize("search_index", [True], indirect=True, ids=["metadata-vec"])
def test_vector_before_pushdown_does_not_waste_knn_slot(search_index):
    conn, insert = search_index
    insert("following-day.md", "2026-09-06", embedding=(1.0, 0.0))
    insert("included.md", "2026-09-05", embedding=(0.0, 1.0))

    results = hs.vector_search(conn, struct.pack("2f", 1.0, 0.0), limit=1, before_int=20260906)

    assert [result.doc_path for result in results] == ["included.md"]


def test_metadata_batch_respects_sqlite_parameter_limit(search_index):
    conn, insert = search_index
    paths = [f"document-{i}.md" for i in range(12)]
    for path in paths:
        insert(path, "2026-09-05")
    conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 8)

    metadata = hs._get_doc_metadata_batch(conn, paths + ["missing.md"])

    assert all(metadata[path]["title"] == f"Title {path}" for path in paths)
    assert metadata["missing.md"] == {
        "title": "missing", "type": "unknown", "project": "", "date": None,
    }


@pytest.mark.parametrize("query", ["AI", "ML", "AI/ML", "AI retrieval"])
def test_short_acronyms_remain_searchable(query):
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE fts_content USING fts5(path, title, content, type, project, date)")
        conn.executemany(
            "INSERT INTO fts_content VALUES (?, ?, ?, 'memo', 'demo', '2026-09-05')",
            [("matching.md", "Model notes", "AI ML"), ("other.md", "Other notes", "gardening")],
        )

        results = hs.hybrid_search(conn, query, mode="fts")

        assert [result.path for result in results] == ["matching.md"]
    finally:
        conn.close()


@pytest.mark.parametrize("query", ["", "   ", "???", "___", "--- ()"])
def test_empty_or_punctuation_query_returns_no_matches_without_search(query, monkeypatch):
    class NoQueryPipeline:
        enabled = True

        def embed_query(self, query):
            pytest.fail("a query without searchable terms must not call an embedding provider")

    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(hs, "EmbeddingPipeline", NoQueryPipeline)
    try:
        # No tables: an invalid query must return before touching the index.
        assert hs.hybrid_search(conn, query, pipeline=NoQueryPipeline()) == []
        assert hs.observation_search(conn, query) == []
    finally:
        conn.close()
