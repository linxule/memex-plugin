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

    assert stored == {"inserted": 2, "embedded": 0, "embed_failed": 0}
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

    assert stored == {"inserted": 1, "embedded": 0, "embed_failed": 0}
    assert len(stored_rows) == 2
    assert contradictions
    assert contradictions[0].obs_type == "contradiction"
