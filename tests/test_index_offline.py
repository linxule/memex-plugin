"""Local index maintenance must work without embedding services or sqlite-vec."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from memex import db_utils
from memex.scripts import embeddings
from memex.scripts import index_rebuild as ir


@pytest.fixture(autouse=True)
def no_embedding_provider(monkeypatch):
    def forbidden_provider():
        pytest.fail("offline index maintenance constructed an embedding provider")

    monkeypatch.setattr(ir, "EmbeddingPipeline", forbidden_provider)


def _memo(vault, name="sample.md"):
    path = vault / "projects" / "sample" / "memos" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\ntype: memo\ntitle: Sample\ntags: [example]\n"
        "aliases: [sample-alias]\n---\n\n# Sample\n\n"
        "A useful observation about [[another-topic]].\n\n- [ ] Follow up\n"
    )
    return path


@pytest.mark.parametrize("vec_available", [True, False])
def test_offline_rebuild_maintains_local_data_and_deletions(
    tmp_path, monkeypatch, vec_available
):
    if not vec_available:
        monkeypatch.setattr(db_utils, "load_vec_extension", lambda conn: False)
    memo = _memo(tmp_path)
    first = ir.rebuild_incremental(tmp_path, with_embeddings=False)
    assert first["errors"] == 0
    assert first["chunks_indexed"] == 1
    assert first["embeddings_generated"] == 0

    with sqlite3.connect(tmp_path / "_index.sqlite") as conn:
        for table in (
            "fts_content", "doc_hashes", "chunks", "wikilinks", "tasks",
            "sections", "doc_tags", "doc_aliases",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1

    second = ir.rebuild_incremental(tmp_path, with_embeddings=False)
    assert second["unchanged"] == 1
    assert second["new"] == second["updated"] == second["errors"] == 0

    memo.write_text(memo.read_text().replace("Follow up", "Follow up tomorrow"))
    third = ir.rebuild_incremental(tmp_path, with_embeddings=False)
    assert third["updated"] == 1
    assert third["errors"] == 0
    with sqlite3.connect(tmp_path / "_index.sqlite") as conn:
        assert conn.execute("SELECT task_text FROM tasks").fetchone()[0] == "Follow up tomorrow"

    memo.unlink()
    deleted = ir.rebuild_incremental(tmp_path, with_embeddings=False)
    assert deleted["deleted"] == 1
    assert deleted["errors"] == 0
    with sqlite3.connect(tmp_path / "_index.sqlite") as conn:
        for table in (
            "fts_content", "doc_hashes", "chunks", "wikilinks", "tasks",
            "sections", "doc_tags", "doc_aliases",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_offline_rebuild_removes_deleted_legacy_fts_only_document(tmp_path):
    with sqlite3.connect(tmp_path / "_index.sqlite") as conn:
        ir.init_fts_schema(conn)
        conn.execute(
            "INSERT INTO fts_content(path, content) VALUES (?, ?)",
            ("projects/sample/memos/deleted.md", "No longer present"),
        )
    stats = ir.rebuild_incremental(tmp_path, with_embeddings=False)
    assert stats["deleted"] == 1
    with sqlite3.connect(tmp_path / "_index.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM fts_content").fetchone()[0] == 0


def test_fts_write_failure_rolls_back_old_document_and_keeps_hash(tmp_path, monkeypatch):
    bad = _memo(tmp_path, "bad.md")
    good = _memo(tmp_path, "good.md")
    ir.rebuild_incremental(tmp_path, with_embeddings=False)
    old_bad = bad.read_text()
    bad.write_text(old_bad + "\nChanged bad document.\n")
    good.write_text(good.read_text() + "\nChanged good document.\n")

    class FailingFtsConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql.startswith("INSERT INTO fts_content") and parameters[0].endswith("bad.md"):
                raise sqlite3.OperationalError("synthetic FTS insert failure")
            return super().execute(sql, parameters)

    monkeypatch.setattr(
        ir, "_connect_index",
        lambda path: sqlite3.connect(path, factory=FailingFtsConnection),
    )
    stats = ir.rebuild_incremental(tmp_path, with_embeddings=False)
    assert stats["errors"] == 1
    with sqlite3.connect(tmp_path / "_index.sqlite") as conn:
        assert conn.execute(
            "SELECT content FROM fts_content WHERE path = ?",
            (str(bad.relative_to(tmp_path)),),
        ).fetchone()[0] == old_bad
        assert conn.execute(
            "SELECT content_hash FROM doc_hashes WHERE path = ?",
            (str(bad.relative_to(tmp_path)),),
        ).fetchone()[0] == embeddings.content_hash(old_bad)
        assert "Changed good document" in conn.execute(
            "SELECT content FROM fts_content WHERE path = ?",
            (str(good.relative_to(tmp_path)),),
        ).fetchone()[0]


def test_full_rebuild_counts_only_successful_fts_writes(tmp_path, monkeypatch):
    _memo(tmp_path, "bad.md")
    _memo(tmp_path, "good.md")
    original = ir.index_document

    def fail_after_fts(conn, path, memex, pipeline=None):
        if path.name == "bad.md":
            raise RuntimeError("synthetic failure after FTS write")
        return original(conn, path, memex, pipeline)

    monkeypatch.setattr(ir, "index_document", fail_after_fts)
    stats = ir.rebuild_full(tmp_path, with_embeddings=False)
    assert stats["errors"] == 1
    assert stats["fts_indexed"] == stats["chunks_indexed"] == 1
    with sqlite3.connect(tmp_path / "_index.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM fts_content").fetchone()[0] == 1


def test_empty_document_hash_is_recorded_for_incremental_runs(tmp_path):
    empty = tmp_path / "topics" / "empty.md"
    empty.parent.mkdir()
    empty.write_text("")
    first = ir.rebuild_incremental(tmp_path, with_embeddings=False)
    assert first["errors"] == 0
    assert first["chunks_indexed"] == 0
    second = ir.rebuild_incremental(tmp_path, with_embeddings=False)
    assert second["unchanged"] == 1


def test_failed_atomic_rebuild_preserves_existing_docs_and_observations(tmp_path, monkeypatch):
    bad = _memo(tmp_path, "bad.md")
    good = _memo(tmp_path, "good.md")
    ir.rebuild_full(tmp_path, with_embeddings=False)
    old_content = {str(path.relative_to(tmp_path)): path.read_text() for path in (bad, good)}
    with sqlite3.connect(tmp_path / "_index.sqlite") as conn:
        conn.execute(
            "INSERT INTO observations(id, doc_path, content, content_hash) VALUES (1, ?, ?, ?)",
            (str(bad.relative_to(tmp_path)), "Retain this observation", "unique-observation"),
        )
        conn.execute(
            "INSERT INTO fts_observations(rowid, content, obs_type) VALUES (1, ?, 'explicit')",
            ("Retain this observation",),
        )
        conn.execute("INSERT INTO observation_topics VALUES (1, 'example')")

    bad.write_text(bad.read_text() + "\nChanged bad document.\n")
    good.write_text(good.read_text() + "\nChanged good document.\n")
    original = ir.index_document

    def fail_after_fts(conn, path, memex, pipeline=None):
        if path.name == "bad.md":
            raise RuntimeError("synthetic document failure")
        return original(conn, path, memex, pipeline)

    monkeypatch.setattr(ir, "index_document", fail_after_fts)
    with pytest.raises(RuntimeError, match="atomic swap aborted to preserve the existing index"):
        ir.rebuild_full(tmp_path, with_embeddings=False)

    with sqlite3.connect(tmp_path / "_index.sqlite") as conn:
        assert dict(conn.execute("SELECT path, content FROM fts_content")) == old_content
        assert conn.execute("SELECT content FROM observations").fetchone()[0] == "Retain this observation"
        assert conn.execute("SELECT COUNT(*) FROM fts_observations").fetchone()[0] == 1
        assert conn.execute("SELECT topic_slug FROM observation_topics").fetchone()[0] == "example"


def test_offline_refresh_preserves_vectors_when_provider_dimensions_change(tmp_path, monkeypatch):
    config = {"dimensions": 4, "index_dimensions": 4}
    monkeypatch.setattr(embeddings, "get_embedding_config", lambda: config)
    _memo(tmp_path)
    ir.rebuild_incremental(tmp_path, with_embeddings=False)
    conn = db_utils.connect_index(tmp_path / "_index.sqlite")
    try:
        if not db_utils.load_vec_extension(conn):
            pytest.skip("sqlite-vec unavailable")
        chunk_id = conn.execute("SELECT id FROM chunks").fetchone()[0]
        vector = embeddings.serialize_f32([1.0, 0.0, 0.0, 0.0])
        conn.execute(
            "INSERT INTO vec_chunks(rowid, embedding, doc_project, doc_type, doc_date) "
            "VALUES (?, ?, 'sample', 'memo', 0)",
            (chunk_id, vector),
        )
        conn.commit()
    finally:
        conn.close()


    config.update(dimensions=2, index_dimensions=2)
    stats = ir.rebuild_incremental(tmp_path, with_embeddings=False)
    assert stats["unchanged"] == 1
    conn = db_utils.connect_index(tmp_path / "_index.sqlite")
    try:
        assert db_utils.load_vec_extension(conn)
        assert conn.execute("SELECT embedding FROM vec_chunks").fetchone()[0] == vector
        assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 1
    finally:
        conn.close()

@pytest.mark.parametrize("change", ["update", "delete"])
def test_unavailable_vec_extension_preserves_existing_document(tmp_path, monkeypatch, change):
    monkeypatch.setattr(
        embeddings, "get_embedding_config",
        lambda: {"dimensions": 4, "index_dimensions": 4},
    )
    memo = _memo(tmp_path)
    original_content = memo.read_text()
    ir.rebuild_incremental(tmp_path, with_embeddings=False)
    conn = db_utils.connect_index(tmp_path / "_index.sqlite")
    try:
        if not db_utils.load_vec_extension(conn):
            pytest.skip("sqlite-vec unavailable")
        chunk_id, original_chunk = conn.execute("SELECT id, content FROM chunks").fetchone()
        vector = embeddings.serialize_f32([1.0, 0.0, 0.0, 0.0])
        conn.execute(
            "INSERT INTO vec_chunks(rowid, embedding, doc_project, doc_type, doc_date) "
            "VALUES (?, ?, 'sample', 'memo', 0)",
            (chunk_id, vector),
        )
        conn.commit()
    finally:
        conn.close()

    if change == "update":
        memo.write_text("---\ntype: memo\n---\nCompletely unrelated replacement text.\n")
    else:
        memo.unlink()
    real_loader = db_utils.load_vec_extension
    monkeypatch.setattr(db_utils, "load_vec_extension", lambda conn: False)
    stats = ir.rebuild_incremental(tmp_path, with_embeddings=False)
    assert stats["errors"] == 1
    assert stats["deleted"] == 0
    conn = db_utils.connect_index(tmp_path / "_index.sqlite")
    try:
        assert real_loader(conn)
        assert conn.execute("SELECT content FROM fts_content").fetchone()[0] == original_content
        assert conn.execute("SELECT content_hash FROM doc_hashes").fetchone()[0] == embeddings.content_hash(original_content)
        assert conn.execute("SELECT id, content FROM chunks").fetchone() == (chunk_id, original_chunk)
        assert conn.execute("SELECT rowid, embedding FROM vec_chunks").fetchone() == (chunk_id, vector)
    finally:
        conn.close()


def test_incremental_cli_honors_no_embeddings_and_exclusive_lock(tmp_path, monkeypatch):
    _memo(tmp_path)
    monkeypatch.setattr(ir, "get_memex_path", lambda: tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["memex-index", "--incremental", "--no-embeddings"])

    with db_utils.writer_lock():
        with pytest.raises(SystemExit) as exc:
            ir._run()
    assert exc.value.code == 3
    assert not (tmp_path / "_index.sqlite").exists()

    ir._run()
    with sqlite3.connect(tmp_path / "_index.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM doc_hashes").fetchone()[0] == 1
