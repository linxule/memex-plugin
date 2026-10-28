"""Tests for memex.observations.reassign_doc_path_prefix.

Promotes the manual SQL UPDATE pattern from the 2026-05-25 Apps-pi-proxy
migration to a tested first-class helper. The original pattern preserved
68 obs + 249 chunks across the rename; these tests pin that behaviour as a
regression guard.
"""

from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

import pytest

from memex.scripts.embeddings import init_document_schema
from memex.scripts.index_rebuild import init_fts_schema
from memex.observations import (
    _DOC_PATH_TABLES,
    reassign_doc_path_prefixes,
    reassign_doc_paths,
    reassign_invariant_violations,
)
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
        legacy = ("obs_matched", "chunks_matched", "obs_updated", "chunks_updated", "dry_run")
        assert {k: stats[k] for k in legacy} == {
            "obs_matched": 3,
            "chunks_matched": 5,
            "obs_updated": 0,
            "chunks_updated": 0,
            "dry_run": True,
        }
        # v0.22.0: per-table breakdown — only tables that exist are reported.
        assert stats["tables"] == {
            "chunks": {"matched": 5, "updated": 0},
            "observations": {"matched": 3, "updated": 0},
            "obs_sidecars": {"matched": 0, "updated": 0},
        }
        assert stats["paths"] == 3 and stats["collisions"] == []
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


# ── v0.22.0 (A5): the whole document moves ─────────────────────────────────

PATH_COLUMNS = {"path", "doc_path", "source_path"}


def _full_schema(conn: sqlite3.Connection) -> bool:
    """Every table a real index has. Returns whether sqlite-vec is present."""
    init_fts_schema(conn)
    init_document_schema(conn)
    vec = init_observation_schema(conn, 4)
    if vec:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0("
            "embedding float[4], doc_project text, doc_type text, doc_date integer)"
        )
    return vec


def test_doc_path_registry_covers_every_path_keyed_table() -> None:
    """A path-keyed table missing from `_DOC_PATH_TABLES` would be left at the
    old path by every move — the Deleted+New pair of 2026-10-28 again."""
    conn = sqlite3.connect(":memory:")
    _full_schema(conn)
    shadow = ("fts_content_", "fts_observations_", "vec_chunks_", "vec_observations_")
    found = set()
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        if table.startswith(shadow) or table.startswith("sqlite_"):
            continue
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        found.update((table, column) for column in columns & PATH_COLUMNS)
    assert len(found) >= 9, f"schema introspection found too little to be meaningful: {found}"
    assert found <= set(_DOC_PATH_TABLES), f"unregistered path-keyed table(s): {found - set(_DOC_PATH_TABLES)}"


def _seed_document(conn: sqlite3.Connection, path: str, project: str, word: str, vec: bool) -> None:
    conn.execute(
        "INSERT INTO fts_content (path, title, content, type, project, date) "
        "VALUES (?, ?, ?, 'memo', ?, '')",
        (path, word, f"about the {word}", project),
    )
    conn.execute("INSERT INTO doc_hashes (path, content_hash) VALUES (?, ?)", (path, f"h-{word}"))
    chunk_id = conn.execute(
        "INSERT INTO chunks (doc_path, chunk_index, content, content_hash, doc_project) "
        "VALUES (?, 0, ?, ?, ?)",
        (path, word, f"c-{word}", project),
    ).lastrowid
    obs_id = conn.execute(
        "INSERT INTO observations (doc_path, content, content_hash) VALUES (?, ?, ?)",
        (path, f"obs {word}", f"o-{word}"),
    ).lastrowid
    conn.execute("INSERT INTO obs_sidecars (doc_path, content_hash) VALUES (?, ?)", (path, f"s-{word}"))
    conn.execute("INSERT INTO tasks (doc_path, task_text, line_number) VALUES (?, 'todo', 1)", (path,))
    conn.execute(
        "INSERT INTO sections (doc_path, heading, level, line_number) VALUES (?, 'H', 1, 1)", (path,)
    )
    conn.execute("INSERT INTO doc_tags (doc_path, tag) VALUES (?, 'tag')", (path,))
    conn.execute("INSERT INTO doc_aliases (doc_path, alias) VALUES (?, ?)", (path, f"alias-{word}"))
    conn.execute(
        "INSERT INTO wikilinks (source_path, target_path, link_text, line_number) "
        "VALUES (?, NULL, 'x', 1)",
        (path,),
    )
    if vec:
        blob = struct.pack("4f", 1.0, 0.0, 0.0, 0.0)
        for table, rowid in (("vec_chunks", chunk_id), ("vec_observations", obs_id)):
            conn.execute(
                f"INSERT INTO {table} (rowid, embedding, doc_project, doc_type, doc_date) "
                "VALUES (?, ?, ?, 'memo', 0)",
                (rowid, blob, project),
            )


def _rows_at(conn: sqlite3.Connection, path: str) -> dict[str, int]:
    return {
        table: conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} = ?", (path,)).fetchone()[0]
        for table, col in _DOC_PATH_TABLES
    }


def test_reassign_moves_document_rows_and_project() -> None:
    conn = sqlite3.connect(":memory:")
    vec = _full_schema(conn)
    _seed_document(conn, "projects/A/memos/x.md", "A", "zebra", vec)
    _seed_document(conn, "projects/A/memos/y.md", "A", "quokka", vec)
    _seed_document(conn, "projects/B/memos/z.md", "B", "ibis", vec)
    conn.execute(
        "INSERT INTO wikilinks (source_path, target_path, link_text, line_number) "
        "VALUES ('projects/B/memos/z.md', 'projects/A/memos/x.md', 'projects/A/memos/x', 9)"
    )

    stats = reassign_doc_path_prefix(conn, "projects/A/memos/", "projects/C/memos/", dry_run=False)

    assert reassign_invariant_violations(stats) == []
    assert (stats["paths"], stats["project_changed"], stats["inbound_links"]) == (2, 2, 1)
    for name in ("x", "y"):
        assert set(_rows_at(conn, f"projects/A/memos/{name}.md").values()) == {0}
        assert set(_rows_at(conn, f"projects/C/memos/{name}.md").values()) == {1}
    sibling = _rows_at(conn, "projects/B/memos/z.md")
    assert sibling.pop("wikilinks") == 2  # its own row + the inbound link
    assert set(sibling.values()) == {1}, "sibling project untouched"
    assert conn.execute(
        "SELECT DISTINCT project FROM fts_content WHERE path LIKE 'projects/C/%'"
    ).fetchall() == [("C",)]
    assert conn.execute(
        "SELECT DISTINCT doc_project FROM chunks WHERE doc_path LIKE 'projects/C/%'"
    ).fetchall() == [("C",)]
    # The inbound link keeps its resolved target: rewriting it would claim the
    # path-style link text still resolves.
    assert conn.execute(
        "SELECT target_path FROM wikilinks WHERE line_number = 9"
    ).fetchone() == ("projects/A/memos/x.md",)
    if vec:
        assert stats["tables"]["vec_chunks"] == {"matched": 2, "updated": 2}
        assert stats["tables"]["vec_observations"] == {"matched": 2, "updated": 2}
        for table in ("vec_chunks", "vec_observations"):
            assert sorted(conn.execute(f"SELECT doc_project FROM {table}").fetchall()) == [
                ("B",), ("C",), ("C",)
            ]


def test_reassign_within_one_project_keeps_the_written_project() -> None:
    conn = sqlite3.connect(":memory:")
    vec = _full_schema(conn)
    # The indexed project came from frontmatter and differs from the folder.
    _seed_document(conn, "projects/A/memos/x.md", "A-frontmatter", "zebra", vec)

    stats = reassign_doc_paths(
        conn, {"projects/A/memos/x.md": "projects/A/memos/archive/x.md"}, dry_run=False
    )

    assert stats["project_changed"] == 0 and reassign_invariant_violations(stats) == []
    assert conn.execute("SELECT path, project FROM fts_content").fetchall() == [
        ("projects/A/memos/archive/x.md", "A-frontmatter")
    ]


def test_fts_only_collision_raises_before_writing() -> None:
    """fts_content has no UNIQUE constraint; the explicit check is all that
    stops one path from getting two document rows."""
    conn = sqlite3.connect(":memory:")
    _full_schema(conn)
    _seed_document(conn, "projects/A/memos/x.md", "A", "zebra", False)
    conn.execute(
        "INSERT INTO fts_content (path, title, content, type, project, date) "
        "VALUES ('projects/C/memos/x.md', 'ghost', 'ghost', 'memo', 'C', '')"
    )

    dry = reassign_doc_path_prefix(conn, "projects/A/memos/", "projects/C/memos/", dry_run=True)
    assert dry["collisions"] == ["projects/C/memos/x.md"]
    with pytest.raises(sqlite3.IntegrityError, match="already have index rows"):
        reassign_doc_path_prefix(conn, "projects/A/memos/", "projects/C/memos/", dry_run=False)
    assert set(_rows_at(conn, "projects/A/memos/x.md").values()) == {1}


def test_overlapping_prefixes_are_rejected() -> None:
    conn = sqlite3.connect(":memory:")
    _full_schema(conn)
    with pytest.raises(ValueError, match="overlapping"):
        reassign_doc_path_prefixes(
            conn, [("projects/A/", "projects/C/"), ("projects/A/memos/", "projects/D/")]
        )


def test_two_sources_onto_one_target_is_a_collision() -> None:
    """{A: C, B: C} would merge two documents into one while every matched ==
    updated invariant still holds (gauntlet round 1, Kimi lane)."""
    conn = sqlite3.connect(":memory:")
    _full_schema(conn)
    _seed_document(conn, "projects/A/memos/x.md", "A", "zebra", False)
    _seed_document(conn, "projects/B/memos/y.md", "B", "yak", False)
    target = "projects/C/memos/x.md"
    mapping = {"projects/A/memos/x.md": target, "projects/B/memos/y.md": target}
    dry = reassign_doc_paths(conn, mapping, dry_run=True)
    assert dry["collisions"] == [target]
    with pytest.raises(sqlite3.IntegrityError):
        reassign_doc_paths(conn, mapping, dry_run=False)
    assert set(_rows_at(conn, "projects/A/memos/x.md").values()) == {1}
    assert set(_rows_at(conn, "projects/B/memos/y.md").values()) == {1}
