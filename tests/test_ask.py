from __future__ import annotations

import sqlite3
from pathlib import Path

from memex.ask import ask
from memex.observations import init_observation_schema


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
