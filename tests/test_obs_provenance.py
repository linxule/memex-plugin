"""H1 (v0.22.0): a deleted observation's id never passes its provenance on.

`observations.id` is an INTEGER PRIMARY KEY without AUTOINCREMENT, so SQLite
gives a freed id (when it was the largest) to the next insert. Before the fix
`delete_observation_ids` left a deduction's `source_obs_ids` citing the freed
id, and the deduction then claimed the unrelated newcomer as its source — in
the index and, rendered by hash, in the vault sidecar. The deleter now turns
each such citation into a pending source hash, so the citation can only ever
resolve to the SAME text again.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from memex.extract import Observation, store_observations
from memex.observations import (
    content_hash,
    decode_source_ids,
    delete_observation_ids,
    delete_observations_for_doc,
    detach_source_refs,
    encode_source_ids,
    init_observation_schema,
)
from memex.sidecars import render_sidecar, resolve_pending_sources

PROJECT_DOC = "projects/p/_project.md"
MEMO = "projects/p/memos/m.md"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    init_observation_schema(conn, 8)
    return conn


def _insert(conn, doc: str, content: str, *, obs_type: str = "explicit", sources=None) -> int:
    cur = conn.execute(
        "INSERT INTO observations (doc_path, content, content_hash, obs_type, source_obs_ids) "
        "VALUES (?, ?, ?, ?, ?)",
        (doc, content, content_hash(content), obs_type, encode_source_ids(sources)),
    )
    obs_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO fts_observations (rowid, content, obs_type) VALUES (?, ?, ?)",
        (obs_id, content, obs_type),
    )
    return obs_id


def _sources(conn, obs_id: int) -> list[int]:
    return decode_source_ids(
        conn.execute("SELECT source_obs_ids FROM observations WHERE id = ?", (obs_id,)).fetchone()[0]
    )


def _pending(conn, obs_id: int) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT source_hash FROM obs_pending_sources WHERE observation_id = ?", (obs_id,)
        )
    }


def _deduction_citing_later_sources(conn) -> tuple[int, int, int]:
    """A deduction whose sources got HIGHER ids than it — the order a vault
    sidecar ingest produces (`_project.obs.jsonl` sorts before memos/, and
    `resolve_pending_sources` links the sources once they arrive). Only a
    deleted max id is reused, so this is the shape where reuse can bite."""
    deduction = _insert(conn, PROJECT_DOC, "Deduction: the team prefers small releases", obs_type="deductive")
    kept = _insert(conn, MEMO, "Release 0.20 shipped in two days")
    doomed = _insert(conn, MEMO, "Release 0.21 shipped in one day")
    conn.execute(
        "UPDATE observations SET source_obs_ids = ? WHERE id = ?",
        (encode_source_ids([kept, doomed]), deduction),
    )
    return deduction, kept, doomed


def test_deleted_then_reused_id_never_inherits_provenance() -> None:
    conn = _conn()
    deduction, kept, doomed = _deduction_citing_later_sources(conn)

    assert delete_observation_ids(conn, [doomed]) == 1
    newcomer = _insert(conn, "projects/q/memos/other.md", "An unrelated fact about lunch")
    # Precondition, so the test is not vacuous: SQLite really did reuse the id.
    assert newcomer == doomed

    assert _sources(conn, deduction) == [kept]
    # The citation is remembered by content, not dropped: the reason the
    # newcomer cannot inherit it.
    assert _pending(conn, deduction) == {content_hash("Release 0.21 shipped in one day")}

    rendered = render_sidecar(conn, PROJECT_DOC)
    assert content_hash("An unrelated fact about lunch") not in rendered
    assert content_hash("Release 0.21 shipped in one day") in rendered
    assert content_hash("Release 0.20 shipped in two days") in rendered


def test_reused_id_is_not_resolved_back_by_a_pending_sweep() -> None:
    conn = _conn()
    deduction, kept, doomed = _deduction_citing_later_sources(conn)
    delete_observation_ids(conn, [doomed])
    _insert(conn, "projects/q/memos/other.md", "An unrelated fact about lunch")  # takes `doomed`'s id

    resolve_pending_sources(conn)

    # Still unresolved: the hash belongs to text that no longer exists.
    assert _sources(conn, deduction) == [kept]
    assert _pending(conn, deduction) == {content_hash("Release 0.21 shipped in one day")}


def test_same_text_returning_restores_the_citation() -> None:
    """Re-extraction (`backfill obs --replace`) deletes and re-inserts the
    same text under a new id; the provenance must follow the text."""
    conn = _conn()
    deduction, kept, doomed = _deduction_citing_later_sources(conn)
    delete_observation_ids(conn, [doomed])
    _insert(conn, "projects/q/memos/other.md", "An unrelated fact about lunch")
    returned = _insert(conn, MEMO, "Release 0.21 shipped in one day")
    assert returned != doomed

    resolve_pending_sources(conn)

    assert _sources(conn, deduction) == [kept, returned]
    assert _pending(conn, deduction) == set()


def test_replace_extraction_keeps_provenance(tmp_path: Path) -> None:
    """The common real path: a memo is re-extracted with --replace."""
    index = tmp_path / "_index.sqlite"
    conn = sqlite3.connect(index)
    init_observation_schema(conn, 8)
    deduction = _insert(conn, PROJECT_DOC, "Deduction: reviews catch remediation bugs", obs_type="deductive")
    source = _insert(conn, MEMO, "Round two found a bug inside the fix")
    conn.execute(
        "UPDATE observations SET source_obs_ids = ? WHERE id = ?",
        (encode_source_ids([source]), deduction),
    )
    conn.commit()
    conn.close()

    store_observations(
        index,
        MEMO,
        [
            Observation(content="A new observation from the second pass", obs_type="explicit", confidence="high"),
            Observation(content="Round two found a bug inside the fix", obs_type="explicit", confidence="high"),
        ],
        None,
        mode="replace",
        vault=None,
    )

    conn = sqlite3.connect(index)
    try:
        init_observation_schema(conn, 8)
        reinserted = conn.execute(
            "SELECT id FROM observations WHERE content_hash = ?",
            (content_hash("Round two found a bug inside the fix"),),
        ).fetchone()[0]
        newcomer = conn.execute(
            "SELECT id FROM observations WHERE content_hash = ?",
            (content_hash("A new observation from the second pass"),),
        ).fetchone()[0]
        # The first re-inserted row took the deleted id; the deduction must
        # not be pointing at it.
        assert newcomer == source
        assert _sources(conn, deduction) == []
        resolve_pending_sources(conn)
        assert _sources(conn, deduction) == [reinserted]
    finally:
        conn.close()


def test_deleting_a_deduction_with_its_sources_leaves_no_pending_rows() -> None:
    conn = _conn()
    deduction, kept, doomed = _deduction_citing_later_sources(conn)

    delete_observation_ids(conn, [deduction, kept, doomed])

    assert conn.execute("SELECT COUNT(*) FROM obs_pending_sources").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0


def test_unrelated_citations_are_untouched() -> None:
    conn = _conn()
    deduction, kept, doomed = _deduction_citing_later_sources(conn)
    other = _insert(conn, PROJECT_DOC, "Deduction: two-day releases are normal", obs_type="deductive", sources=[kept])

    delete_observations_for_doc(conn, "projects/none/memos/nothing.md")  # deletes nothing
    delete_observation_ids(conn, [doomed])

    assert _sources(conn, other) == [kept]
    assert _pending(conn, other) == set()
    assert _sources(conn, deduction) == [kept]


def test_merge_redirect_moves_the_citation_to_the_survivor() -> None:
    """The dreamer's duplicate merge keeps one row per group: a citation of a
    dropped duplicate belongs to the survivor, not to a hash that is gone."""
    conn = _conn()
    deduction, kept, doomed = _deduction_citing_later_sources(conn)
    survivor = _insert(conn, MEMO, "release 0.21 shipped in one day.")  # near-duplicate of `doomed`

    delete_observation_ids(conn, [doomed], redirect={doomed: survivor})

    assert _sources(conn, deduction) == [kept, survivor]
    assert _pending(conn, deduction) == set()


def test_citation_lookup_uses_the_partial_index() -> None:
    conn = _conn()
    plan = " ".join(
        row[-1]
        for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT id, source_obs_ids FROM observations "
            "WHERE source_obs_ids IS NOT NULL AND source_obs_ids != ''"
        )
    )
    assert "idx_obs_cites" in plan, plan


def test_detach_without_pending_table_still_prunes() -> None:
    """A pre-v0.17 schema has no obs_pending_sources: the dangling id must
    still go (dropping provenance beats inventing it)."""
    conn = _conn()
    conn.execute("DROP TABLE obs_pending_sources")
    deduction, kept, doomed = _deduction_citing_later_sources(conn)

    rewritten = detach_source_refs(conn, {doomed: content_hash("Release 0.21 shipped in one day")})

    assert rewritten == 1
    assert _sources(conn, deduction) == [kept]
