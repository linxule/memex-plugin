"""Tests for memex.observations.reassign_doc_path_prefix.

Promotes the manual SQL UPDATE pattern from the 2026-05-25 Apps-pi-proxy
migration to a tested first-class helper. The original pattern preserved
68 obs + 249 chunks across the rename; these tests pin that behaviour as a
regression guard.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memex.observations import (
    init_observation_schema,
    reassign_doc_path_prefix,
)


def _init_index_with_chunks(index_path: Path) -> sqlite3.Connection:
    """Initialize an index with both observations and a stub chunks table."""
    conn = sqlite3.connect(index_path)
    init_observation_schema(conn, 8)
    # Minimal chunks table — real schema is in scripts/embeddings.py but
    # tests only need the columns reassign touches.
    conn.execute(
        """
        CREATE TABLE chunks (
            id INTEGER PRIMARY KEY,
            doc_path TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            UNIQUE(doc_path, chunk_index)
        )
        """
    )
    return conn


def _seed_rows(conn: sqlite3.Connection) -> None:
    """Seed 3 obs + 5 chunks under projects/Apps-X/ and 1 obs + 1 chunk under projects/Y/."""
    obs_rows = [
        ("projects/Apps-X/memos/a.md", "obs A", "ha"),
        ("projects/Apps-X/memos/b.md", "obs B", "hb"),
        ("projects/Apps-X/memos/c.md", "obs C", "hc"),
        ("projects/Y/memos/y.md", "obs Y", "hy"),
    ]
    conn.executemany(
        "INSERT INTO observations (doc_path, content, content_hash) VALUES (?, ?, ?)",
        obs_rows,
    )
    chunk_rows = [
        ("projects/Apps-X/memos/a.md", 0, "c1", "h1"),
        ("projects/Apps-X/memos/a.md", 1, "c2", "h2"),
        ("projects/Apps-X/memos/b.md", 0, "c3", "h3"),
        ("projects/Apps-X/memos/c.md", 0, "c4", "h4"),
        ("projects/Apps-X/memos/c.md", 1, "c5", "h5"),
        ("projects/Y/memos/y.md", 0, "cy", "hcy"),
    ]
    conn.executemany(
        "INSERT INTO chunks (doc_path, chunk_index, content, content_hash) VALUES (?, ?, ?, ?)",
        chunk_rows,
    )
    conn.commit()


def test_dry_run_counts_matches_without_changing_rows(tmp_path: Path) -> None:
    conn = _init_index_with_chunks(tmp_path / "_index.sqlite")
    try:
        _seed_rows(conn)
        stats = reassign_doc_path_prefix(
            conn,
            from_prefix="projects/Apps-X/",
            to_prefix="projects/X/",
            dry_run=True,
        )
        assert stats == {
            "obs_matched": 3,
            "chunks_matched": 5,
            "obs_updated": 0,
            "chunks_updated": 0,
            "dry_run": True,
        }
        # Verify nothing changed
        n_old_obs = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE doc_path LIKE 'projects/Apps-X/%'"
        ).fetchone()[0]
        assert n_old_obs == 3
    finally:
        conn.close()


def test_apply_updates_rows_atomically(tmp_path: Path) -> None:
    conn = _init_index_with_chunks(tmp_path / "_index.sqlite")
    try:
        _seed_rows(conn)
        stats = reassign_doc_path_prefix(
            conn,
            from_prefix="projects/Apps-X/",
            to_prefix="projects/X/",
            dry_run=False,
        )
        conn.commit()
        assert stats["obs_matched"] == 3
        assert stats["obs_updated"] == 3
        assert stats["chunks_matched"] == 5
        assert stats["chunks_updated"] == 5
        # Old prefix gone
        assert conn.execute(
            "SELECT COUNT(*) FROM observations WHERE doc_path LIKE 'projects/Apps-X/%'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE doc_path LIKE 'projects/Apps-X/%'"
        ).fetchone()[0] == 0
        # New prefix populated
        assert conn.execute(
            "SELECT COUNT(*) FROM observations WHERE doc_path LIKE 'projects/X/%'"
        ).fetchone()[0] == 3
        assert conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE doc_path LIKE 'projects/X/%'"
        ).fetchone()[0] == 5
        # Sibling project untouched
        assert conn.execute(
            "SELECT COUNT(*) FROM observations WHERE doc_path = 'projects/Y/memos/y.md'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_total_counts_preserved_across_reassign(tmp_path: Path) -> None:
    """The whole point of the SOP: cascade-delete loss is avoided."""
    conn = _init_index_with_chunks(tmp_path / "_index.sqlite")
    try:
        _seed_rows(conn)
        total_obs_before = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        total_chunks_before = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        reassign_doc_path_prefix(
            conn,
            from_prefix="projects/Apps-X/",
            to_prefix="projects/X/",
            dry_run=False,
        )
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == total_obs_before
        assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == total_chunks_before
    finally:
        conn.close()


def test_substr_replaces_only_prefix_not_internal_occurrences(tmp_path: Path) -> None:
    """Regression guard: a path with the substring later in the path must not be double-rewritten.

    Manual SOP used REPLACE() which would corrupt the second occurrence.
    The helper uses SUBSTR(LENGTH(prefix)+1) which is prefix-only.
    """
    conn = _init_index_with_chunks(tmp_path / "_index.sqlite")
    try:
        conn.execute(
            "INSERT INTO observations (doc_path, content, content_hash) VALUES (?, ?, ?)",
            ("projects/Apps-X/memos/Apps-X-backup.md", "edge", "edge"),
        )
        conn.commit()
        reassign_doc_path_prefix(
            conn,
            from_prefix="projects/Apps-X/",
            to_prefix="projects/X/",
            dry_run=False,
        )
        conn.commit()
        new_path = conn.execute(
            "SELECT doc_path FROM observations WHERE content = 'edge'"
        ).fetchone()[0]
        # Only the leading prefix swapped; "Apps-X-backup.md" portion unchanged.
        assert new_path == "projects/X/memos/Apps-X-backup.md"
    finally:
        conn.close()


def test_empty_from_prefix_rejected(tmp_path: Path) -> None:
    conn = _init_index_with_chunks(tmp_path / "_index.sqlite")
    try:
        with pytest.raises(ValueError, match="from_prefix must be non-empty"):
            reassign_doc_path_prefix(conn, from_prefix="", to_prefix="projects/X/")
    finally:
        conn.close()


def test_same_prefix_rejected_as_noop(tmp_path: Path) -> None:
    conn = _init_index_with_chunks(tmp_path / "_index.sqlite")
    try:
        with pytest.raises(ValueError, match="identical"):
            reassign_doc_path_prefix(
                conn,
                from_prefix="projects/Apps-X/",
                to_prefix="projects/Apps-X/",
            )
    finally:
        conn.close()


def test_no_match_returns_zero_counts(tmp_path: Path) -> None:
    """Reassigning a prefix that matches nothing is a safe no-op (not an error)."""
    conn = _init_index_with_chunks(tmp_path / "_index.sqlite")
    try:
        _seed_rows(conn)
        stats = reassign_doc_path_prefix(
            conn,
            from_prefix="projects/Apps-NOMATCH/",
            to_prefix="projects/NOMATCH/",
            dry_run=False,
        )
        conn.commit()
        assert stats["obs_matched"] == 0
        assert stats["chunks_matched"] == 0
        assert stats["obs_updated"] == 0
        assert stats["chunks_updated"] == 0
    finally:
        conn.close()


def test_unique_collision_raises_integrityerror(tmp_path: Path) -> None:
    """UNIQUE(doc_path, chunk_index) collision: data loss > failed migration."""
    conn = _init_index_with_chunks(tmp_path / "_index.sqlite")
    try:
        # Both old and new prefix have a chunk at index 0 for the same file slug
        conn.executemany(
            "INSERT INTO chunks (doc_path, chunk_index, content, content_hash) VALUES (?, ?, ?, ?)",
            [
                ("projects/Apps-X/memos/a.md", 0, "old", "h-old"),
                ("projects/X/memos/a.md", 0, "new", "h-new"),
            ],
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            reassign_doc_path_prefix(
                conn,
                from_prefix="projects/Apps-X/",
                to_prefix="projects/X/",
                dry_run=False,
            )
    finally:
        conn.close()


def test_to_prefix_starts_with_from_prefix_rejected(tmp_path: Path) -> None:
    """Re-run footgun guard: when to_prefix literally contains from_prefix,
    a second apply would match the already-rewritten rows and compound the
    prefix. Real triggering case: from=projects/X without trailing slash,
    to=projects/X-old (also without trailing slash) — to.startswith(from)
    is True. Rejected before any UPDATE runs.
    """
    conn = _init_index_with_chunks(tmp_path / "_index.sqlite")
    try:
        with pytest.raises(ValueError, match="compound"):
            reassign_doc_path_prefix(
                conn,
                from_prefix="projects/X",
                to_prefix="projects/X-old",
            )
        # Also: from=a/ to=a/b/ — second-run would re-match the moved rows
        with pytest.raises(ValueError, match="compound"):
            reassign_doc_path_prefix(
                conn,
                from_prefix="projects/a/",
                to_prefix="projects/a/b/",
            )
    finally:
        conn.close()


def test_sql_wildcards_in_prefix_treated_as_literals(tmp_path: Path) -> None:
    """`%` and `_` in from_prefix must NOT act as SQL wildcards.

    A `LIKE ? || '%'` formulation would match `projects/aXb/` when
    from_prefix is `projects/a_b/` (because `_` matches any single char).
    The SUBSTR(...) = ? formulation must treat them as literal characters.
    """
    conn = _init_index_with_chunks(tmp_path / "_index.sqlite")
    try:
        # Seed with two siblings that LIKE wildcards would conflate
        conn.executemany(
            "INSERT INTO observations (doc_path, content, content_hash) VALUES (?, ?, ?)",
            [
                ("projects/a_b/memos/m.md", "literal", "h-lit"),
                ("projects/aXb/memos/m.md", "looks-like-wildcard-match", "h-wild"),
                ("projects/a%b/memos/m.md", "literal-pct", "h-pct"),
            ],
        )
        conn.commit()
        # Reassign only the literal `a_b/` path; `aXb/` and `a%b/` must NOT match
        stats = reassign_doc_path_prefix(
            conn,
            from_prefix="projects/a_b/",
            to_prefix="projects/AB/",
            dry_run=False,
        )
        conn.commit()
        assert stats["obs_matched"] == 1
        assert stats["obs_updated"] == 1
        # Sibling with literal X in slot _ should be untouched
        survivor = conn.execute(
            "SELECT doc_path FROM observations WHERE content = 'looks-like-wildcard-match'"
        ).fetchone()[0]
        assert survivor == "projects/aXb/memos/m.md"
        # Sibling with literal % should also be untouched
        pct = conn.execute(
            "SELECT doc_path FROM observations WHERE content = 'literal-pct'"
        ).fetchone()[0]
        assert pct == "projects/a%b/memos/m.md"
    finally:
        conn.close()


def test_helper_does_not_commit(tmp_path: Path) -> None:
    """Caller owns transaction boundaries — matches index_document / embed_chunks convention."""
    conn = _init_index_with_chunks(tmp_path / "_index.sqlite")
    try:
        _seed_rows(conn)
        reassign_doc_path_prefix(
            conn,
            from_prefix="projects/Apps-X/",
            to_prefix="projects/X/",
            dry_run=False,
        )
        # Without commit, a fresh connection sees the OLD prefix
        # (proves the helper didn't commit on our behalf)
        conn.rollback()
        assert conn.execute(
            "SELECT COUNT(*) FROM observations WHERE doc_path LIKE 'projects/Apps-X/%'"
        ).fetchone()[0] == 3
    finally:
        conn.close()
