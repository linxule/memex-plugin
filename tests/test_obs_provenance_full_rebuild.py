"""A full rebuild drops observations of vanished docs by omission; their
citations must be detached like any other delete (v0.22.0 H1, wave 2)."""

from __future__ import annotations

import json
import sqlite3

from memex.observations import init_observation_schema
from memex.scripts.index_rebuild import _preserve_obs_tables


def _db(path, rows, docs):
    conn = sqlite3.connect(path)
    init_observation_schema(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS fts_content (path TEXT)")
    conn.executemany("INSERT INTO fts_content (path) VALUES (?)", [(d,) for d in docs])
    conn.executemany(
        "INSERT INTO observations (id, doc_path, content, content_hash, obs_type, confidence, source_obs_ids) "
        "VALUES (?, ?, ?, ?, ?, 'high', ?)", rows)
    conn.commit()
    return conn


def test_citation_of_a_dropped_observation_is_detached(tmp_path):
    old_rows = [
        (1, "projects/p/memos/kept.md", "kept claim", "h-kept", "explicit", None),
        (2, "projects/p/memos/gone.md", "gone claim", "h-gone", "explicit", None),
        (3, "projects/p/memos/kept.md", "deduction", "h-ded", "deductive", "[1, 2]"),
    ]
    _db(tmp_path / "old.sqlite", old_rows, ["projects/p/memos/kept.md", "projects/p/memos/gone.md"]).close()
    main = _db(tmp_path / "main.sqlite", [], ["projects/p/memos/kept.md"])  # gone.md left the vault
    main.execute(f"ATTACH DATABASE '{tmp_path / 'old.sqlite'}' AS old")
    main.execute("SAVEPOINT obs_preserve")
    _preserve_obs_tables(main, vec_available=False)
    main.execute("RELEASE SAVEPOINT obs_preserve")
    ids = {r[0] for r in main.execute("SELECT id FROM observations")}
    assert ids == {1, 3}
    cites = main.execute("SELECT source_obs_ids FROM observations WHERE id = 3").fetchone()[0]
    assert json.loads(cites) == [1], cites  # the dropped id 2 no longer cited
    pending = main.execute("SELECT * FROM obs_pending_sources").fetchall()
    assert any("h-gone" in map(str, row) for row in pending), pending
