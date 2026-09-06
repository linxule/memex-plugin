from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import sqlite_vec

from memex.ask import _merge_candidates, _vector_candidates, ask
from memex.observations import init_observation_schema


@pytest.fixture(autouse=True)
def _disable_embedding_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("memex.ask._build_pipeline", lambda: None)


def _init_index(index_path: Path) -> None:
    conn = sqlite3.connect(index_path)
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE fts_content
            USING fts5(path, title, content, type, project, date, tokenize='porter unicode61')
            """
        )
        conn.execute(
            """
            CREATE TABLE wikilinks (
                source_path TEXT,
                target_path TEXT,
                link_text TEXT,
                display_text TEXT,
                is_broken INTEGER,
                line_number INTEGER
            )
            """
        )
        init_observation_schema(conn, 8)
        conn.execute(
            "INSERT INTO fts_content (path, title, content, type, project, date) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "projects/memex/memos/2026-03-16-evolution.md",
                "Memex Evolution",
                "We chose typed config with pydantic-settings because it replaced raw JSON loading.",
                "memo",
                "memex",
                "2026-03-16",
            ),
        )
        conn.execute(
            "INSERT INTO wikilinks VALUES (?, ?, ?, ?, ?, ?)",
            (
                "topics/pydantic-settings.md",
                "projects/memex/memos/2026-03-16-evolution.md",
                "2026-03-16-evolution",
                None,
                0,
                1,
            ),
        )
        cursor = conn.execute(
            """
            INSERT INTO observations (doc_path, content, content_hash, obs_type, confidence, source_obs_ids)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "projects/memex/memos/2026-03-16-evolution.md",
                "Decision: typed config with pydantic-settings replaced raw JSON loading.",
                "obs-hash-1",
                "explicit",
                "high",
                None,
            ),
        )
        conn.execute(
            "INSERT INTO fts_observations(rowid, content, obs_type) VALUES (?, ?, ?)",
            (
                cursor.lastrowid,
                "Decision: typed config with pydantic-settings replaced raw JSON loading.",
                "explicit",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_ask_returns_content_and_observations(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    memo_path = vault / "projects" / "memex" / "memos"
    memo_path.mkdir(parents=True)
    (vault / "topics").mkdir()
    (memo_path / "2026-03-16-evolution.md").write_text(
        """---
type: memo
title: Memex Evolution
date: 2026-03-16
---

We chose typed config with pydantic-settings because it replaced raw JSON loading.

Related: [[pydantic-settings]]
"""
    )
    index_path = vault / "_index.sqlite"
    _init_index(index_path)

    response = ask(
        question="why did we choose pydantic settings",
        index_path=index_path,
        vault_path=vault,
        project="memex",
        depth="thorough",
    )

    assert response.results
    assert response.results[0].path == "projects/memex/memos/2026-03-16-evolution.md"
    assert "typed config" in response.results[0].content
    assert response.results[0].backlink_count == 1
    assert response.observations
    assert "pydantic-settings" in response.observations[0].content
    assert response.query_info["results_returned"] == 1


@pytest.mark.parametrize("depth", ["quick", "thorough"])
@pytest.mark.parametrize("question", ["", " \n\t", "???", "***", '\"\"()', "___"])
def test_ask_empty_queries_skip_database_and_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, question: str, depth: str,
) -> None:
    def unexpected_access(*args, **kwargs):
        pytest.fail("An empty query must not access a database or embedding provider")

    monkeypatch.setattr("memex.ask.connect_index", unexpected_access)
    monkeypatch.setattr("memex.ask._build_pipeline", unexpected_access)
    index_path = tmp_path / "_index.sqlite"

    response = ask(question, index_path, tmp_path, depth=depth)

    assert response.results == []
    assert response.observations == []
    assert response.query_info == {
        "fts_query": "",
        "vector_query": question,
        "total_candidates": 0,
        "results_returned": 0,
        "gaps": ["No matching memos or notes were found."],
    }
    assert not index_path.exists()


@pytest.mark.parametrize(
    ("question", "expected_paths"),
    [
        ("why AI?", {"ai.md"}),
        ("why ML?", {"ml.md"}),
        ("AI/ML", {"ai.md", "ml.md"}),
        ("C++?", {"c.md"}),
    ],
)
def test_ask_preserves_short_terms_and_sanitizes_punctuation(
    tmp_path: Path, question: str, expected_paths: set[str],
) -> None:
    index_path = tmp_path / "_index.sqlite"
    _init_index(index_path)
    with sqlite3.connect(index_path) as conn:
        for term in ("AI", "ML", "C"):
            path = f"{term.lower()}.md"
            (tmp_path / path).write_text(f"{term} research notes")
            conn.execute(
                "INSERT INTO fts_content VALUES (?, ?, ?, ?, ?, ?)",
                (path, term, f"{term} research notes", "memo", "memex", "2026-03-16"),
            )

    response = ask(question, index_path, tmp_path, depth="thorough")

    assert {result.path for result in response.results} == expected_paths


@pytest.mark.parametrize("project", [None, "memex"])
def test_vector_candidates_rank_by_distance_when_scores_are_clamped(
    tmp_path: Path, project: str | None,
) -> None:
    conn = sqlite3.connect(tmp_path / "_index.sqlite")
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute(
            "CREATE VIRTUAL TABLE fts_content USING fts5(path, title, project, date)"
        )
        conn.execute("CREATE TABLE chunks (id INTEGER PRIMARY KEY, doc_path TEXT)")
        conn.execute(
            "CREATE VIRTUAL TABLE vec_chunks USING vec0(embedding float[2], doc_project text)"
        )
        # FTS metadata order opposes KNN order. Every unit vector lies more than
        # distance 1 from the query, so the displayed similarities all become 0.
        for path in ("far.md", "middle.md", "near.md"):
            conn.execute(
                "INSERT INTO fts_content VALUES (?, ?, ?, ?)",
                (path, path, "memex", "2026-03-16"),
            )
        for chunk_id, (path, vector) in enumerate(
            [
                ("far.md", [-1.0, 0.0]),
                ("middle.md", [-0.6, 0.8]),
                ("near.md", [-0.8, -0.6]),
                ("near.md", [0.0, 1.0]),
            ],
            start=1,
        ):
            conn.execute("INSERT INTO chunks VALUES (?, ?)", (chunk_id, path))
            conn.execute(
                "INSERT INTO vec_chunks(rowid, embedding, doc_project) VALUES (?, ?, ?)",
                (chunk_id, sqlite_vec.serialize_float32(vector), "memex"),
            )

        rows = _vector_candidates(
            conn, sqlite_vec.serialize_float32([1.0, 0.0]), project=project, limit=4,
        )

        assert [row["path"] for row in rows] == ["near.md", "middle.md", "far.md"]
        assert [row["rank"] for row in rows] == [1, 2, 3]
        assert [row["score"] for row in rows] == [0.0, 0.0, 0.0]
        merged = _merge_candidates([], rows)
        assert [row["path"] for row in merged] == ["near.md", "middle.md", "far.md"]
        assert merged[0]["score"] > merged[1]["score"] > merged[2]["score"]
    finally:
        conn.close()
