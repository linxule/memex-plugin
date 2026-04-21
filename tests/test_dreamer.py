from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

from memex.dreamer import run_dreamer
from memex.observations import init_observation_schema


def _seed_index(index_path: Path) -> None:
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
        conn.execute(
            """
            CREATE TABLE tasks (
                doc_path TEXT,
                task_text TEXT,
                completed INTEGER,
                line_number INTEGER,
                section TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE doc_aliases (
                doc_path TEXT,
                alias TEXT
            )
            """
        )
        init_observation_schema(conn, 8)
        conn.executemany(
            "INSERT INTO fts_content (path, title, content, type, project, date) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    "projects/memex/memos/2026-03-16-one.md",
                    "One",
                    "Typed config decision",
                    "memo",
                    "memex",
                    "2026-03-16",
                ),
                (
                    "projects/memex/memos/2025-01-01-old.md",
                    "Old",
                    "Old memo",
                    "memo",
                    "memex",
                    "2025-01-01",
                ),
                (
                    "projects/alpha/memos/2026-03-16-two.md",
                    "Two",
                    "Typed config rollout",
                    "memo",
                    "alpha",
                    "2026-03-16",
                ),
            ],
        )
        conn.executemany(
            """
            INSERT INTO observations (doc_path, content, content_hash, obs_type, confidence, source_obs_ids)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "projects/memex/memos/2026-03-16-one.md",
                    "Decision: typed-config remained enabled for memex.",
                    "hash-1",
                    "explicit",
                    "high",
                    None,
                ),
                (
                    "projects/memex/memos/2026-03-16-one.md",
                    "Decision: typed-config kept configuration typed in memex.",
                    "hash-2",
                    "explicit",
                    "high",
                    None,
                ),
                (
                    "projects/alpha/memos/2026-03-16-two.md",
                    "Decision: typed-config rollout stayed enabled in alpha.",
                    "hash-3",
                    "explicit",
                    "high",
                    None,
                ),
            ],
        )
        rows = conn.execute("SELECT id, content, obs_type FROM observations ORDER BY id").fetchall()
        conn.executemany(
            "INSERT INTO fts_observations(rowid, content, obs_type) VALUES (?, ?, ?)",
            rows,
        )
        conn.execute(
            "INSERT INTO doc_aliases (doc_path, alias) VALUES (?, ?)",
            ("topics/typed-config.md", "typed-config-alias"),
        )
        conn.execute(
            "INSERT INTO wikilinks VALUES (?, ?, ?, ?, ?, ?)",
            (
                "projects/memex/_project.md",
                None,
                "typed-config-alias",
                None,
                1,
                1,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _make_vault(tmp_path: Path) -> tuple[Path, Path]:
    vault = tmp_path / "vault"
    (vault / "topics").mkdir(parents=True)
    (vault / "projects" / "memex" / "memos").mkdir(parents=True)
    (vault / "projects" / "alpha" / "memos").mkdir(parents=True)
    project_file = vault / "projects" / "memex" / "_project.md"
    project_file.write_text("# Memex\n\nCurrent state.\n\n[[typed-config-alias]]\n")
    (vault / "projects" / "alpha" / "_project.md").write_text("# Alpha\n")
    (vault / "projects" / "memex" / "memos" / "2026-03-16-one.md").write_text("memo one")
    (vault / "projects" / "alpha" / "memos" / "2026-03-16-two.md").write_text("memo two")
    (vault / "projects" / "memex" / "memos" / "2025-01-01-old.md").write_text("old memo")
    index_path = vault / "_index.sqlite"
    _seed_index(index_path)
    return vault, index_path


def test_run_dreamer_creates_patterns_and_updates_project(tmp_path: Path) -> None:
    """Original test — heuristic path, no LLM needed."""
    vault, index_path = _make_vault(tmp_path)
    project_file = vault / "projects" / "memex" / "_project.md"

    with patch("memex.dreamer.claude_available", return_value=False):
        result = asyncio.run(
            run_dreamer(
                vault_path=vault,
                index_path=index_path,
                scope="all",
                dry_run=False,
            )
        )

    assert result.deductions_created >= 1
    assert result.patterns_found >= 1
    assert result.links_fixed >= 1
    assert "projects/memex/memos/2025-01-01-old.md" in result.archives_suggested
    assert (vault / "topics" / "typed-config.md").exists()
    assert "Dreamer Update" in project_file.read_text()
    assert "[[typed-config]]" in project_file.read_text()


def test_dreamer_dry_run_skips_llm(tmp_path: Path) -> None:
    """Dry-run should never call the LLM, even if claude is available."""
    vault, index_path = _make_vault(tmp_path)

    with patch("memex.dreamer.call_claude_json") as mock_call:
        result = asyncio.run(
            run_dreamer(
                vault_path=vault,
                index_path=index_path,
                scope="all",
                dry_run=True,
            )
        )

    mock_call.assert_not_called()
    assert result.deductions_created >= 1


def test_dreamer_llm_analysis(tmp_path: Path) -> None:
    """LLM path returns structured deductions and contradictions."""
    vault, index_path = _make_vault(tmp_path)

    llm_response = {
        "deductions": [
            {
                "content": "Both memex and alpha adopted typed-config via pydantic-settings, suggesting a vault-wide convention.",
                "source_ids": [1, 3],
                "confidence": "high",
            }
        ],
        "contradictions": [],
    }
    pattern_response = {
        "patterns": [
            {
                "title": "typed-config-convention",
                "description": "Multiple projects adopted typed config via pydantic-settings as a shared architectural pattern.",
                "observation_ids": [1, 2, 3],
                "projects": ["memex", "alpha"],
            }
        ],
    }

    def mock_call(prompt, schema, model="sonnet", timeout=120):
        if "deductions and contradictions" in prompt:
            return llm_response
        if "cross-project PATTERN" in prompt:
            return pattern_response
        return None

    with patch("memex.dreamer.claude_available", return_value=True), \
         patch("memex.dreamer.call_claude_json", side_effect=mock_call):
        result = asyncio.run(
            run_dreamer(
                vault_path=vault,
                index_path=index_path,
                scope="all",
                dry_run=False,
            )
        )

    assert result.deductions_created >= 1
    assert result.patterns_found >= 1
    topic_file = vault / "topics" / "typed-config-convention.md"
    assert topic_file.exists()
    assert "pydantic-settings" in topic_file.read_text()


def test_llm_envelope_unwrapping() -> None:
    """call_claude_json extracts structured_output from CLI wrapper envelope."""
    from memex.llm import call_claude_json

    envelope = json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "raw text",
        "structured_output": {"answer": "42"},
        "session_id": "test",
    })
    with patch("memex.llm.subprocess.run") as mock_run:
        mock_run.return_value = type("R", (), {"returncode": 0, "stdout": envelope})()
        result = call_claude_json("test", {"type": "object"})
    assert result == {"answer": "42"}


def test_llm_envelope_error_returns_none() -> None:
    """call_claude_json returns None when CLI reports is_error."""
    from memex.llm import call_claude_json

    envelope = json.dumps({
        "type": "result",
        "is_error": True,
        "result": "Credit balance is too low",
    })
    with patch("memex.llm.subprocess.run") as mock_run:
        mock_run.return_value = type("R", (), {"returncode": 0, "stdout": envelope})()
        result = call_claude_json("test", {"type": "object"})
    assert result is None


def test_dreamer_llm_error_falls_back(tmp_path: Path) -> None:
    """When LLM returns None (error), fall back to heuristics."""
    vault, index_path = _make_vault(tmp_path)

    with patch("memex.dreamer.claude_available", return_value=True), \
         patch("memex.dreamer.call_claude_json", return_value=None):
        result = asyncio.run(
            run_dreamer(
                vault_path=vault,
                index_path=index_path,
                scope="all",
                dry_run=False,
            )
        )

    # Should still produce heuristic results
    assert result.deductions_created >= 1
    assert result.patterns_found >= 1
