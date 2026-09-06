"""Offline regressions for the compatibility search entry point."""
from __future__ import annotations

import fcntl
import json
import struct
from contextlib import closing
from pathlib import Path

import pytest

from memex.config import get_settings
from memex.db_utils import connect_index, load_vec_extension
from memex.scripts import embeddings, index_rebuild, search


@pytest.fixture(autouse=True)
def offline_index(monkeypatch, tmp_path):
    """Never construct a provider, use the live index, or contend with its lock."""
    def reject_pipeline(*args, **kwargs):
        pytest.fail("FTS search must not initialize an embedding provider")

    monkeypatch.setattr(embeddings, "EmbeddingPipeline", reject_pipeline)
    monkeypatch.setattr(index_rebuild, "EmbeddingPipeline", reject_pipeline)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))


def _memo(vault: Path, name: str, date: str, body: str = "temporal 100% claim x") -> Path:
    memo = vault / "projects" / "example" / "memos" / f"{name}.md"
    memo.parent.mkdir(parents=True, exist_ok=True)
    memo.write_text(f"---\ntitle: {name}\ntype: memo\ndate: {date}\n---\n\n{body}\n")
    return memo


@pytest.fixture
def dated_vault(tmp_path):
    for name, date in [
        ("older", "2026-03-14"),
        ("start", "2026-03-15"),
        ("end", "2026-03-16T23:59:59"),
        ("later", "2026-03-17"),
    ]:
        _memo(tmp_path, name, date)
    search.rebuild_index(tmp_path)
    return tmp_path


def test_fts_refresh_preserves_observations_and_unchanged_vectors(tmp_path):
    memo = _memo(tmp_path, "retained", "2026-03-15")
    changed = _memo(tmp_path, "changed", "2026-03-16", "obsolete phrase")
    assert search.rebuild_index(tmp_path) == 2
    memo_rel = str(memo.relative_to(tmp_path))
    dimensions = int(get_settings().embeddings.effective_index_dimensions)
    embedding = struct.pack(f"{dimensions}f", 1.0, *([0.0] * (dimensions - 1)))

    with closing(connect_index(tmp_path / "_index.sqlite")) as conn:
        assert load_vec_extension(conn), "sqlite-vec is a declared project dependency"
        embeddings.init_embedding_schema(conn)
        conn.execute(
            "INSERT INTO observations (id, doc_path, content, content_hash) VALUES (?, ?, ?, ?)",
            (42, memo_rel, "preserved observation", "observation-hash"),
        )
        conn.execute("INSERT INTO observation_topics VALUES (?, ?)", (42, "example-topic"))
        conn.execute(
            "INSERT INTO fts_observations (rowid, content, obs_type) VALUES (?, ?, ?)",
            (42, "preserved observation", "explicit"),
        )
        chunk_id = conn.execute(
            "SELECT id FROM chunks WHERE doc_path = ? LIMIT 1", (memo_rel,)
        ).fetchone()[0]
        for table, rowid in [("vec_observations", 42), ("vec_chunks", chunk_id)]:
            conn.execute(
                f"INSERT INTO {table} (rowid, embedding, doc_project, doc_type, doc_date) "
                "VALUES (?, ?, ?, ?, ?)",
                (rowid, embedding, "example", "memo", 20260315),
            )
        conn.commit()

    changed.write_text(changed.read_text().replace("obsolete phrase", "replacement phrase"))
    project = tmp_path / "projects" / "example" / "_project.md"
    project.write_text("---\ntitle: Project overview\n---\n\nA project record.")
    assert search.rebuild_index(tmp_path) == 3

    with closing(connect_index(tmp_path / "_index.sqlite")) as conn:
        assert load_vec_extension(conn)
        assert conn.execute("SELECT id, content FROM observations").fetchall() == [
            (42, "preserved observation")
        ]
        assert conn.execute("SELECT * FROM observation_topics").fetchall() == [(42, "example-topic")]
        assert conn.execute(
            "SELECT rowid FROM fts_observations WHERE fts_observations MATCH 'preserved'"
        ).fetchall() == [(42,)]
        assert conn.execute("SELECT rowid, embedding FROM vec_observations").fetchall() == [(42, embedding)]
        assert conn.execute("SELECT rowid, embedding FROM vec_chunks").fetchall() == [(chunk_id, embedding)]

    assert [row["title"] for row in search.search(tmp_path, "replacement")] == ["changed"]
    assert search.search(tmp_path, "obsolete") == []
    assert [row["title"] for row in search.search(tmp_path, "overview")] == ["Project overview"]


@pytest.mark.parametrize("query", ["temporal", "x"])
def test_fts_and_like_fallback_honor_date_range(dated_vault, query):
    # A single-letter query takes the real FTS-error -> LIKE path.
    results = search.search(
        dated_vault, query, since="2026-03-15", before="2026-03-16",
    )
    assert {row["title"] for row in results} == {"start", "end"}


@pytest.mark.parametrize(
    ("bounds", "expected"),
    [
        ({"since": "2026-03-16"}, {"end", "later"}),
        ({"before": "2026-03-15"}, {"older", "start"}),
    ],
)
def test_fts_date_bounds_work_independently(dated_vault, bounds, expected):
    assert {row["title"] for row in search.search(dated_vault, "temporal", **bounds)} == expected


@pytest.mark.parametrize(
    "date_args",
    [
        ["--since", "2026-03-15", "--before", "2026-03-16"],
        ["--between", "2026-03-15", "2026-03-16"],
        ["--since", "2026-03-15", "--until", "2026-03-16"],
    ],
)
def test_fts_cli_keeps_date_filters(dated_vault, monkeypatch, capsys, date_args):
    monkeypatch.setattr(search, "get_memex_path", lambda: dated_vault)
    monkeypatch.setattr("sys.argv", ["search.py", "temporal", "--mode=fts", *date_args])
    search._run()
    assert {row["title"] for row in json.loads(capsys.readouterr().out)} == {"start", "end"}


def test_first_fts_search_emits_valid_json(tmp_path, monkeypatch, capsys):
    _memo(tmp_path, "first", "2026-03-15")
    monkeypatch.setattr(search, "get_memex_path", lambda: tmp_path)
    monkeypatch.setattr("sys.argv", ["search.py", "temporal", "--mode=fts"])
    search._run()
    assert [row["title"] for row in json.loads(capsys.readouterr().out)] == ["first"]


@pytest.mark.parametrize("with_embeddings", [False, True])
@pytest.mark.parametrize("lock_mode", [fcntl.LOCK_SH, fcntl.LOCK_EX])
def test_legacy_rebuild_respects_running_writer(tmp_path, with_embeddings, lock_mode):
    lock_dir = Path.home() / ".memex" / "locks"
    lock_dir.mkdir(parents=True)
    with (lock_dir / "full-rebuild.lock").open("w") as running:
        fcntl.flock(running.fileno(), lock_mode | fcntl.LOCK_NB)
        with pytest.raises(SystemExit) as exc:
            if with_embeddings:
                search._rebuild_with_embeddings(tmp_path)
            else:
                search.rebuild_index(tmp_path)
        assert exc.value.code == 3
    assert not (tmp_path / "_index.sqlite").exists()


@pytest.mark.parametrize("query", ["", "  ", "?!", "---", "___"])
def test_empty_or_punctuation_query_does_not_create_index(tmp_path, query):
    assert search.search(tmp_path, query) == []
    assert not (tmp_path / "_index.sqlite").exists()


@pytest.mark.parametrize("query", ["AI ML", "AI ML optimization", "AI/ML"])
def test_short_acronyms_remain_searchable(tmp_path, query):
    _memo(tmp_path, "one", "2026-03-15", "AI planning")
    _memo(tmp_path, "two", "2026-03-16", "ML training")
    assert {row["title"] for row in search.search(tmp_path, query)} == {"one", "two"}
