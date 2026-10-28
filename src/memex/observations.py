from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from memex import db_utils
from memex.config import get_settings


@dataclass(slots=True)
class StoredObservation:
    id: int
    doc_path: str
    content: str
    content_hash: str
    obs_type: str
    confidence: str
    source_obs_ids: list[int]
    created_at: str


def serialize_f32(vector: Sequence[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def normalize_text(text: str) -> str:
    return " ".join(text.lower().split())


def encode_source_ids(source_obs_ids: Sequence[int] | None) -> str | None:
    if not source_obs_ids:
        return None
    return json.dumps(list(source_obs_ids))


def decode_source_ids(raw: str | None) -> list[int]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [int(item) for item in data if isinstance(item, int)]


def load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """Compatibility entry point for the shared, exception-safe loader."""
    return db_utils.load_vec_extension(conn)


def init_observation_schema(
    conn: sqlite3.Connection,
    dimensions: int | None = None,
) -> bool:
    """Create the observation tables (observations, fts_observations,
    observation_topics, vec_observations) if they don't already exist.

    Returns True if sqlite-vec loaded (so `vec_observations` was created),
    False otherwise.

    NOTE: Callers must commit. This function executes DDL (CREATE TABLE /
    CREATE VIRTUAL TABLE / CREATE INDEX) but does NOT call `conn.commit()`,
    matching the v0.11.1 "callers own transactions" convention re-asserted
    in `.claude/rules/python-patterns.md`. Most SQLite builds auto-commit
    DDL anyway, but explicit commits at the caller keep transaction
    semantics legible — and matter when this is invoked from inside a
    SAVEPOINT (e.g., during `rebuild_full`).
    """
    settings = get_settings()
    dims = dimensions if dimensions is not None else settings.embeddings.effective_index_dimensions
    try:
        dims = int(dims)
    except (TypeError, ValueError):
        dims = settings.embeddings.effective_index_dimensions

    vec_available = load_sqlite_vec(conn)

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY,
            doc_path TEXT NOT NULL,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            obs_type TEXT NOT NULL DEFAULT 'explicit',
            confidence TEXT DEFAULT 'high',
            source_obs_ids TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(content_hash)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_obs_doc ON observations(doc_path)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_obs_type ON observations(obs_type)"
    )
    # Partial index over the rows that cite sources (deductions,
    # contradictions): `detach_source_refs`, run by every
    # `delete_observation_ids`, scans only these instead of the table.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_obs_cites ON observations(id) "
        "WHERE source_obs_ids IS NOT NULL"
    )
    # Invariant: fts_observations.rowid == observations.id. The writer in
    # src/memex/extract.py uses `lastrowid` from the parent observations
    # INSERT as the explicit rowid for the FTS row. rebuild_full's
    # preservation path (_preserve_obs_tables in
    # src/memex/scripts/index_rebuild.py) joins on this equality to drop
    # orphaned FTS rows (those whose parent obs was filtered out). If you
    # change the schema or the insert path, you must update both the
    # writer and the preservation path together.
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS fts_observations
        USING fts5(content, obs_type, tokenize='porter unicode61')
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS observation_topics (
            observation_id INTEGER NOT NULL REFERENCES observations(id),
            topic_slug TEXT NOT NULL,
            PRIMARY KEY (observation_id, topic_slug)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ot_topic ON observation_topics(topic_slug)"
    )
    # Tracks the vault-backed `.obs.jsonl` sidecar last written/ingested for
    # each doc_path, keyed by the sidecar FILE's content hash (not any single
    # observation's). `write_sidecar`/`ingest_sidecar` in memex.sidecars use
    # this to skip unchanged sidecars on a full rebuild and to detect drift
    # (`memex obs sidecars` health report). See docs/2026-09-13-obs-sidecar-spec.md.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS obs_sidecars (
            doc_path TEXT PRIMARY KEY,
            content_hash TEXT NOT NULL,
            ingested_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # Addendum A3 (2026-09-13 adversarial review of the sidecar spec):
    # unresolved `source_obs` hashes referenced by a sidecar record — e.g. a
    # deduction in `_project.obs.jsonl` whose source memo's sidecar hasn't
    # synced in yet — are remembered here so `resolve_pending_sources` can
    # retry them on every future `ingest_all_sidecars` call, including runs
    # where the referencing sidecar itself is unchanged (and so never
    # re-parsed). Keyed by observation id like `observation_topics`, so it
    # is a `delete_observation_ids` mirror, not a doc_path-keyed table like
    # `obs_sidecars`.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS obs_pending_sources (
            observation_id INTEGER NOT NULL REFERENCES observations(id),
            source_hash TEXT NOT NULL,
            PRIMARY KEY (observation_id, source_hash)
        )
        """
    )
    if vec_available:
        conn.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_observations
            USING vec0(
                embedding float[{dims}],
                doc_project text,
                doc_type text,
                doc_date integer
            )
            """
        )
    # Intentionally no commit — callers own transaction boundaries. See
    # the docstring for the rationale.
    return vec_available


# Every table that mirrors `observations` keyed by observation id. ALL
# deletion paths route through `delete_observation_ids` so the list is
# maintained in exactly one place.
#
# This registry exists because it was previously maintained in two places and
# they disagreed: `delete_observations_for_doc` cleaned all three mirrors,
# while `dreamer._merge_duplicate_observations` cleaned only two and left
# `observation_topics` rows pointing at deleted observations. Those orphans
# are invisible to every JOIN-ing read path, so nothing surfaced them — they
# only showed up as inflated counts in the two counters that did NOT join.
#
# Each entry: (table_name, id_column).
_OBS_MIRROR_TABLES = [
    ("vec_observations", "rowid"),
    ("fts_observations", "rowid"),
    ("observation_topics", "observation_id"),
    # obs_pending_sources (Addendum A3, v0.17.0): unresolved source_obs
    # hashes for an observation. Id-keyed like observation_topics, so it
    # must be cleared on delete the same way.
    ("obs_pending_sources", "observation_id"),
]


def _mirror_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Whether a mirror table exists in this database at all.

    Existence is settled by `sqlite_master`, NOT by catching the
    `OperationalError` from a failed query against it. Those are different
    questions with the same exception type: `no such table` means the table
    was never created (sqlite-vec absent at init), while `no such module:
    vec0` means the table EXISTS and holds rows this connection cannot reach,
    and `database is locked` means it exists and is momentarily unavailable.

    Treating the last two as "absent" would skip their DELETE and then delete
    the parent anyway — manufacturing exactly the orphans this module exists
    to prevent, silently, inside the function that promises not to. So only a
    genuine absence is tolerated here; every other failure propagates.
    """
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


# SQLite's host-parameter ceiling is 32766 since 3.32 but 999 before it, and
# memex runs against whatever SQLite the interpreter was built with. Chunking
# below the old limit keeps a large prune from failing with "too many SQL
# variables" on an older runtime — a limit that is invisible on a modern one,
# which is how an environment-dependent pass hides a defect.
_SQL_VAR_CHUNK = 900


def _chunked(items: Sequence, size: int = _SQL_VAR_CHUNK):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def detach_source_refs(
    conn: sqlite3.Connection,
    id_hashes: Mapping[int, str],
    *,
    redirect: Mapping[int, int] | None = None,
) -> int:
    """Stop every surviving observation from citing the ids in `id_hashes` by id.

    `id_hashes` maps each observation id that is about to disappear (or
    already has) to its content_hash. Every OTHER observation whose
    `source_obs_ids` lists one of those ids gets the id removed, and the
    cited content is remembered by hash in `obs_pending_sources` instead —
    the same table that holds a sidecar's not-yet-resolved `source_obs`.
    `redirect` maps a disappearing id to the surviving id that replaces it
    (the dreamer's duplicate merge keeps one row of each group): such a
    citation is rewritten to the survivor instead of going pending.

    Why: `observations.id` is an INTEGER PRIMARY KEY without AUTOINCREMENT,
    so SQLite hands a deleted id (when it was the largest) to the next
    insert. A deduction still citing that id would then claim the NEW,
    unrelated observation as its source — wrong provenance, rendered into
    the vault sidecar as the newcomer's hash. A hash cannot be reused that
    way. Keeping the reference as a pending hash (rather than dropping it)
    means that the same text coming back (`backfill obs --replace` re-sending
    a set) restores the link on the next `resolve_pending_sources`, and that
    the DB keeps agreeing with the vault: the citing doc's sidecar on disk
    already lists the hash, and `render_sidecar` writes pending hashes too
    (after the resolved ones, so a later re-render may reorder the list).
    A source deleted for good stays pending — "cites text that no longer
    exists here" is the true state, and it is what the sidecar says.

    Ids in `id_hashes` that cite each other are skipped — they are going
    away together. Returns the number of observations rewritten. Does not
    commit.
    """
    if not id_hashes:
        return 0
    gone = set(id_hashes)
    redirect = {
        old: new for old, new in (redirect or {}).items() if old in gone and new not in gone
    }
    record_pending = _mirror_table_exists(conn, "obs_pending_sources")
    rewritten = 0
    # `idx_obs_cites` (partial, source_obs_ids IS NOT NULL) keeps this scan
    # proportional to the rows that cite anything, not to the table.
    rows = conn.execute(
        "SELECT id, source_obs_ids FROM observations "
        "WHERE source_obs_ids IS NOT NULL AND source_obs_ids != ''"
    ).fetchall()
    for obs_id, raw in rows:
        if obs_id in gone:
            continue
        refs = decode_source_ids(raw)
        if not any(ref in gone for ref in refs):
            continue
        kept: list[int] = []
        pending: list[int] = []
        for ref in refs:
            if ref in redirect:
                ref = redirect[ref]
            elif ref in gone:
                pending.append(ref)
                continue
            if ref != obs_id and ref not in kept:
                kept.append(ref)
        conn.execute(
            "UPDATE observations SET source_obs_ids = ? WHERE id = ?",
            (encode_source_ids(kept), obs_id),
        )
        if record_pending:
            for ref in pending:
                conn.execute(
                    "INSERT OR IGNORE INTO obs_pending_sources "
                    "(observation_id, source_hash) VALUES (?, ?)",
                    (obs_id, id_hashes[ref]),
                )
        rewritten += 1
    return rewritten


def delete_observation_ids(
    conn: sqlite3.Connection,
    obs_ids: Sequence[int],
    *,
    redirect: Mapping[int, int] | None = None,
) -> int:
    """Delete observations by id, plus every table that mirrors them.

    Returns the number of parent `observations` rows actually deleted, from
    the DELETE's `rowcount` — a measurement, not the length of the input list.

    This is the ONLY supported way to remove observation rows. Deleting from
    `observations` directly leaves orphaned mirror rows that no JOIN-ing query
    will ever surface, which means the damage is silent.

    Mirrors are deleted BEFORE the parent, deliberately. A failure partway
    through leaves a parent with some mirrors already gone — an observation
    that search cannot find — which is recoverable by re-extraction and
    detectable by comparing counts. The reverse order would leave orphans,
    which are neither. Callers wanting all-or-nothing must supply a
    transaction; this function does not commit.

    Provenance (v0.22.0, H1): before the parents go, every surviving
    observation that cites one of them in `source_obs_ids` is detached via
    `detach_source_refs` — otherwise SQLite's reuse of the freed id would
    make that citation point at whatever row is inserted next. Pass
    `redirect={deleted_id: surviving_id}` when a deleted row is merged into a
    survivor, so its citations move to the survivor instead.
    """
    if not obs_ids:
        return 0

    ids = list(obs_ids)
    for table, id_column in _OBS_MIRROR_TABLES:
        if not _mirror_table_exists(conn, table):
            continue
        try:
            for batch in _chunked(ids):
                conn.execute(
                    f"DELETE FROM {table} WHERE {id_column} IN "
                    f"({','.join('?' for _ in batch)})",
                    batch,
                )
        except sqlite3.OperationalError as exc:
            # The table is real and holds rows we cannot reach — most often a
            # vec0 table on a connection that never loaded sqlite-vec. Deleting
            # the parent anyway is what produced the orphans this function
            # exists to prevent, so refuse, and say what to do about it.
            raise sqlite3.OperationalError(
                f"cannot delete observation mirror rows from {table!r}: {exc}. "
                "Call init_observation_schema() (or db_utils.load_vec_extension) "
                "on this connection before deleting observations — deleting the "
                "parent rows without clearing this table would orphan them."
            ) from exc

    # Hashes must be read while the parent rows still exist.
    id_hashes: dict[int, str] = {}
    for batch in _chunked(ids):
        id_hashes.update(
            conn.execute(
                f"SELECT id, content_hash FROM observations WHERE id IN "
                f"({','.join('?' for _ in batch)})",
                batch,
            ).fetchall()
        )
    detach_source_refs(conn, id_hashes, redirect=redirect)

    deleted = 0
    for batch in _chunked(ids):
        cursor = conn.execute(
            f"DELETE FROM observations WHERE id IN "
            f"({','.join('?' for _ in batch)})",
            batch,
        )
        # rowcount is -1 only for statements SQLite cannot count; a DELETE with
        # a WHERE clause always reports. Fall back to the batch length rather
        # than inventing a number, and never accumulate a negative.
        deleted += cursor.rowcount if cursor.rowcount >= 0 else len(batch)
    return deleted


def count_orphaned_observation_rows(conn: sqlite3.Connection) -> dict[str, int]:
    """Count mirror rows whose parent observation no longer exists.

    Read-only. A healthy index answers 0 for every mirror table. Non-zero
    means some deletion path bypassed `delete_observation_ids`.
    """
    counts: dict[str, int] = {}
    for table, id_column in _OBS_MIRROR_TABLES:
        if not _mirror_table_exists(conn, table):
            # Absent key, NOT a zero. Callers must not render "could not ask"
            # as "asked and found none" — see `unchecked_mirror_tables`.
            continue
        counts[table] = conn.execute(
            f"SELECT COUNT(*) FROM {table} m "
            f"LEFT JOIN observations o ON o.id = m.{id_column} "
            "WHERE o.id IS NULL"
        ).fetchone()[0]
    return counts


def unchecked_mirror_tables(
    conn: sqlite3.Connection, measured: dict[str, int]
) -> list[str]:
    """Mirror tables absent from a count/prune result — questions not asked.

    Exists so every consumer, human-readable or machine-readable, can report
    the gap. A report that lists only what it measured reads as complete.
    """
    return [t for t, _ in _OBS_MIRROR_TABLES if t not in measured]


def delete_orphaned_observation_rows(conn: sqlite3.Connection) -> dict[str, int]:
    """Remove mirror rows whose parent observation is gone. Returns counts deleted.

    Caller owns the transaction. Counts come from the same query that drives
    the deletion, so the reported number is what was actually removed.

    Orphan identification and deletion happen in ONE statement. Collecting ids
    with a SELECT and deleting them afterwards is not safe here: `observations.id`
    is a plain INTEGER PRIMARY KEY with no AUTOINCREMENT, so SQLite reuses ids
    freed by a delete. A concurrent writer granted a reused id between the two
    statements would have its own topic tag silently dropped by INSERT OR IGNORE
    (the orphan row already occupies that primary key), and the prune would then
    delete the row — leaving a live observation missing a tag it did insert.
    """
    removed: dict[str, int] = {}
    for table, id_column in _OBS_MIRROR_TABLES:
        if not _mirror_table_exists(conn, table):
            continue
        try:
            cursor = conn.execute(
                f"DELETE FROM {table} WHERE {id_column} IN ("
                f"  SELECT m.{id_column} FROM {table} m"
                f"  LEFT JOIN observations o ON o.id = m.{id_column}"
                "   WHERE o.id IS NULL"
                ")"
            )
        except sqlite3.OperationalError as exc:
            # Same distinction as `delete_observation_ids`: the table exists,
            # so this is not absence. Reporting a table we could not prune as
            # pruned-clean is the defect this command exists to surface.
            raise sqlite3.OperationalError(
                f"cannot prune orphaned rows from {table!r}: {exc}. "
                "Call init_observation_schema() (or db_utils.load_vec_extension) "
                "on this connection first."
            ) from exc
        # No host parameters here, so no chunking is needed and no id list can
        # go stale between statements.
        removed[table] = cursor.rowcount if cursor.rowcount >= 0 else 0
    return removed


def delete_observations_for_doc(conn: sqlite3.Connection, doc_path: str) -> int:
    """Remove a document's observations and their index mirrors.

    Returns the number of parent `observations` rows actually deleted.

    The count comes from the DELETE's `rowcount`, NOT from the preceding
    SELECT — callers surface this number to operators as a measured fact, and a
    guess presented as a measurement is the defect class v0.15.11 was spent
    removing.

    Deletion is BY ID (via `delete_observation_ids`), not by doc_path. The
    distinction matters because `backfill obs` takes a SHARED advisory lock
    (see `db_utils.writer_lock`), so a concurrent writer for the same doc_path
    can insert between the SELECT and the DELETE. A `WHERE doc_path = ?` DELETE
    would remove that row too — but its mirror rows were never in `obs_ids`, so
    they would survive as orphans that no JOIN-ing read path can see. Deleting
    the ids we actually collected mirrors keeps parent and mirrors in lockstep;
    the concurrent writer's row simply survives intact, which is the correct
    outcome for a row this call never observed.
    """
    rows = conn.execute(
        "SELECT id FROM observations WHERE doc_path = ?",
        (doc_path,),
    ).fetchall()
    if not rows:
        # Nothing to delete AND nothing was deleted — a true measured zero.
        return 0

    obs_ids = [row[0] for row in rows]
    return delete_observation_ids(conn, obs_ids)


def fetch_observations(
    conn: sqlite3.Connection,
    *,
    project: str | None = None,
    obs_type: str | None = None,
    doc_path: str | None = None,
    limit: int | None = None,
) -> list[StoredObservation]:
    sql = """
        SELECT o.id, o.doc_path, o.content, o.content_hash, o.obs_type,
               o.confidence, o.source_obs_ids, o.created_at
        FROM observations o
    """
    params: list[object] = []
    clauses: list[str] = []

    if project:
        sql += " JOIN fts_content f ON f.path = o.doc_path"
        clauses.append("f.project = ?")
        params.append(project)
    if obs_type:
        clauses.append("o.obs_type = ?")
        params.append(obs_type)
    if doc_path:
        clauses.append("o.doc_path = ?")
        params.append(doc_path)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY o.id ASC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    return [
        StoredObservation(
            id=row[0],
            doc_path=row[1],
            content=row[2],
            content_hash=row[3],
            obs_type=row[4],
            confidence=row[5],
            source_obs_ids=decode_source_ids(row[6]),
            created_at=row[7],
        )
        for row in rows
    ]


def search_observations_fts(
    conn: sqlite3.Connection,
    query: str,
    *,
    project: str | None = None,
    limit: int = 10,
) -> list[StoredObservation]:
    sql = """
        SELECT o.id, o.doc_path, o.content, o.content_hash, o.obs_type,
               o.confidence, o.source_obs_ids, o.created_at
        FROM fts_observations fts
        JOIN observations o ON o.id = fts.rowid
    """
    params: list[object] = []
    clauses: list[str] = ["fts_observations MATCH ?"]
    params.append(query)

    if project:
        sql += " JOIN fts_content docs ON docs.path = o.doc_path"
        clauses.append("docs.project = ?")
        params.append(project)

    sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY bm25(fts_observations) LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    return [
        StoredObservation(
            id=row[0],
            doc_path=row[1],
            content=row[2],
            content_hash=row[3],
            obs_type=row[4],
            confidence=row[5],
            source_obs_ids=decode_source_ids(row[6]),
            created_at=row[7],
        )
        for row in rows
    ]


def vector_search_observations(
    conn: sqlite3.Connection,
    query_embedding: bytes,
    *,
    project: str | None = None,
    limit: int = 10,
) -> list[tuple[StoredObservation, float]]:
    if not load_sqlite_vec(conn):
        return []

    # Keep the query vector aligned with the stored (possibly Matryoshka-
    # truncated) dimension regardless of how the caller produced it.
    from memex.scripts.embeddings import match_query_dim
    query_embedding = match_query_dim(conn, "vec_observations", query_embedding)

    sql = """
        SELECT o.id, o.doc_path, o.content, o.content_hash, o.obs_type,
               o.confidence, o.source_obs_ids, o.created_at, v.distance
        FROM vec_observations v
        JOIN observations o ON o.id = v.rowid
    """
    params: list[object] = []
    clauses: list[str] = ["v.embedding MATCH ?", "k = ?"]
    params.extend([query_embedding, limit])

    if project:
        sql += " JOIN fts_content docs ON docs.path = o.doc_path"
        clauses.insert(0, "docs.project = ?")
        params.insert(0, project)

    sql += " WHERE " + " AND ".join(clauses)
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        (
            StoredObservation(
                id=row[0],
                doc_path=row[1],
                content=row[2],
                content_hash=row[3],
                obs_type=row[4],
                confidence=row[5],
                source_obs_ids=decode_source_ids(row[6]),
                created_at=row[7],
            ),
            float(row[8]),
        )
        for row in rows
    ]


def project_for_doc(conn: sqlite3.Connection, doc_path: str) -> str | None:
    row = conn.execute(
        "SELECT project FROM fts_content WHERE path = ?",
        (doc_path,),
    ).fetchone()
    return row[0] if row else None


def observation_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM observations").fetchone()
    return int(row[0]) if row else 0


def dedupe_by_content(
    observations: Iterable[StoredObservation],
) -> list[StoredObservation]:
    seen: set[str] = set()
    deduped: list[StoredObservation] = []
    for observation in observations:
        key = normalize_text(observation.content)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(observation)
    return deduped


def store_observation_topics(
    conn: sqlite3.Connection,
    obs_id: int,
    topics: Iterable[str],
) -> int:
    inserted = 0
    for slug in topics:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO observation_topics (observation_id, topic_slug) VALUES (?, ?)",
            (obs_id, slug),
        )
        if cursor.rowcount > 0:
            inserted += 1
    return inserted


def fetch_observations_by_topic(
    conn: sqlite3.Connection,
    topic_slug: str,
    *,
    limit: int | None = None,
) -> list[StoredObservation]:
    sql = """
        SELECT o.id, o.doc_path, o.content, o.content_hash, o.obs_type,
               o.confidence, o.source_obs_ids, o.created_at
        FROM observation_topics ot
        JOIN observations o ON o.id = ot.observation_id
        WHERE ot.topic_slug = ?
        ORDER BY o.created_at ASC
    """
    params: list[object] = [topic_slug]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [
        StoredObservation(
            id=row[0],
            doc_path=row[1],
            content=row[2],
            content_hash=row[3],
            obs_type=row[4],
            confidence=row[5],
            source_obs_ids=decode_source_ids(row[6]),
            created_at=row[7],
        )
        for row in rows
    ]


def doc_paths_for_topic(conn: sqlite3.Connection, slug: str) -> list[str]:
    """Distinct doc_paths of observations currently tagged with `slug`.

    Used by `memex obs retag` to know which sidecars need rewriting after a
    retag — the topic tags on a doc's observations changed, so its rendered
    `.obs.jsonl` is now stale. JOINs against `observations` so a tag row
    orphaned by a deletion path doesn't yield a doc_path that no longer holds
    any observations.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT o.doc_path
        FROM observation_topics ot
        JOIN observations o ON o.id = ot.observation_id
        WHERE ot.topic_slug = ?
        """,
        (slug,),
    ).fetchall()
    return [row[0] for row in rows]


def retag_topic(
    conn: sqlite3.Connection,
    old_slug: str,
    new_slug: str,
) -> int:
    # JOIN, so orphaned tag rows (parent observation deleted) are not counted
    # as retagged observations. The caller prints this as "Retagged N
    # observations"; a row pointing at a deleted observation is not one.
    count = conn.execute(
        """
        SELECT COUNT(*)
        FROM observation_topics ot
        JOIN observations o ON o.id = ot.observation_id
        WHERE ot.topic_slug = ?
        """,
        (old_slug,),
    ).fetchone()[0]
    conn.execute(
        """
        UPDATE OR IGNORE observation_topics
        SET topic_slug = ?
        WHERE topic_slug = ?
        """,
        (new_slug, old_slug),
    )
    conn.execute(
        "DELETE FROM observation_topics WHERE topic_slug = ?",
        (old_slug,),
    )
    return count


def topic_observation_counts(
    conn: sqlite3.Connection,
) -> list[tuple[str, int]]:
    # JOIN so a topic's count reflects observations that exist. Counting
    # `observation_topics` rows directly inflates every affected topic by its
    # orphaned tags — the count claims retrievable observations that
    # `fetch_observations_by_topic` (which does join) will never return.
    rows = conn.execute(
        """
        SELECT ot.topic_slug, COUNT(*) as cnt
        FROM observation_topics ot
        JOIN observations o ON o.id = ot.observation_id
        GROUP BY ot.topic_slug
        ORDER BY cnt DESC
        """
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


# ── Moving documents (A5, v0.22.0) ─────────────────────────────────────────

# Every table holding rows keyed by a DOCUMENT's vault-relative path, with the
# column that holds it. A move is a move only when all of them follow the
# document: a row left at the old path is what the next incremental rebuild
# reports as Deleted (and its twin at the new path as New) — 105/167 after the
# 2026-10-28 consolidation — and a stale `fts_content` row is a search hit
# under the old path and project. `delete_document_rows` drives off the same
# list. `test_doc_path_registry_covers_every_path_keyed_table` builds the full
# index schema and fails when a path-keyed table is missing here.
#
# Deliberately absent: `wikilinks.target_path`. It is a REFERENCE resolved
# from another document's link text, not this document's key; rewriting it
# would claim a path-style link still resolves after its target moved. A
# reassign counts those links (`inbound_links`) instead of guessing.
_DOC_PATH_TABLES = [
    ("fts_content", "path"),
    ("doc_hashes", "path"),
    ("chunks", "doc_path"),
    ("observations", "doc_path"),
    ("obs_sidecars", "doc_path"),
    ("wikilinks", "source_path"),
    ("tasks", "doc_path"),
    ("sections", "doc_path"),
    ("doc_tags", "doc_path"),
    ("doc_aliases", "doc_path"),
]

# vec0 tables whose `doc_project` metadata (KNN filter pushdown) is keyed by a
# parent row's id: (vec table, parent table, parent path column).
_VEC_PROJECT_COLUMNS = [
    ("vec_chunks", "chunks", "doc_path"),
    ("vec_observations", "observations", "doc_path"),
]


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _project_of(doc_path: str) -> str:
    """The folder project of a vault-relative path (`projects/<p>/…` -> `p`).

    Same rule as `memex.scripts.embeddings.project_from_path`, which cannot be
    imported here (embeddings imports this module).
    """
    if doc_path.startswith("projects/"):
        parts = doc_path.split("/")
        if len(parts) >= 2:
            return parts[1]
    return ""


def _validate_prefix_pair(from_prefix: str, to_prefix: str) -> None:
    if not from_prefix:
        raise ValueError("from_prefix must be non-empty")
    if from_prefix == to_prefix:
        raise ValueError("from_prefix and to_prefix are identical (no-op)")
    if to_prefix.startswith(from_prefix):
        raise ValueError(
            "to_prefix starts with from_prefix; re-runs would compound the "
            f"rewrite (from={from_prefix!r} → to={to_prefix!r})"
        )


def _fts_rows(conn: sqlite3.Connection) -> list[tuple[int, str]] | None:
    """(rowid, path) of every `fts_content` row, or None when the table is absent.

    One scan per reassign: FTS5 cannot index a column for equality, so every
    path lookup against it is a full scan anyway.
    """
    if not _mirror_table_exists(conn, "fts_content"):
        return None
    return [
        (int(rowid), path)
        for rowid, path in conn.execute("SELECT rowid, path FROM fts_content")
        if path is not None
    ]


def _paths_with_prefix(
    conn: sqlite3.Connection, prefix: str, fts_rows: list[tuple[int, str]] | None
) -> set[str]:
    # Literal `SUBSTR(col, 1, n) = ?`, never `LIKE`: `%` and `_` in a folder
    # name must not act as wildcards.
    n = len(prefix)
    paths: set[str] = set()
    for table, col in _DOC_PATH_TABLES:
        if table == "fts_content" or not _mirror_table_exists(conn, table):
            continue
        paths.update(
            row[0]
            for row in conn.execute(
                f"SELECT DISTINCT {col} FROM {table} WHERE SUBSTR({col}, 1, ?) = ?",
                (n, prefix),
            )
        )
    if fts_rows:
        paths.update(path for _, path in fts_rows if path[:n] == prefix)
    return paths


def reassign_doc_path_prefix(
    conn: sqlite3.Connection,
    from_prefix: str,
    to_prefix: str,
    *,
    dry_run: bool = True,
) -> dict:
    """Rewrite the ``doc_path`` prefix of every index row a document owns.

    Promotes the manual SQL UPDATE pattern used during the 2026-05-25
    Apps-pi-proxy → pi-proxy folder migration to a first-class helper. Since
    v0.22.0 (A5) it moves the WHOLE document, not just its observations and
    chunks: every table in `_DOC_PATH_TABLES` (the FTS document row,
    `doc_hashes`, graph rows, the sidecar record) follows, and when the path
    crosses project folders the per-document project (`fts_content.project`,
    `chunks.doc_project`, the vec0 `doc_project` metadata) becomes the new
    folder's — so search shows the new project immediately and the next
    incremental rebuild sees no Deleted+New pair. Mirror tables keyed by
    observation / chunk id (fts_observations, vec_*, observation_topics)
    follow their parent row automatically.

    Pre-conditions / safety:

    * ``from_prefix`` must be non-empty (refuses to "match everything").
    * ``from_prefix`` and ``to_prefix`` should usually end in ``/`` to avoid
      partial-segment matches (e.g., ``Apps-pi`` would also rewrite
      ``Apps-pi-proxy-old/``). Caller's responsibility — the helper doesn't
      enforce because legitimate uses (file moves, not just folder moves)
      exist. `reassign_doc_paths` is the exact-path form.
    * ``from_prefix == to_prefix`` is a no-op (rejected).
    * ``dry_run=True`` returns counts without touching rows. The caller
      decides commit/rollback; this helper does not commit.

    Returns a dict with:
        - ``obs_matched`` / ``obs_updated``, ``chunks_matched`` /
          ``chunks_updated``: as before v0.22.0 (updated is 0 on a dry run)
        - ``tables``: ``{table: {"matched": n, "updated": m}}`` for every
          table present, plus the vec0 project metadata (apply only). An
          absent table is an absent key, never a zero.
        - ``paths``: distinct document paths moved
        - ``project_changed``: paths whose project folder changes
        - ``collisions``: target paths that already have index rows
        - ``inbound_links``: wikilinks whose resolved target is a moved path
          (left as they are — see `_DOC_PATH_TABLES`)
        - ``dry_run``

    Invariant: ``matched == updated`` for every entry of ``tables`` after a
    non-dry-run call (`reassign_invariant_violations` lists breaches). If
    they ever diverge, investigate before committing.

    Collisions: a target path that already has rows in any of the tables
    raises ``sqlite3.IntegrityError`` before anything is written — merging
    two documents' rows is data loss, and ``fts_content`` has no UNIQUE
    constraint that would catch it. A dry run reports them instead.

    Re-run footgun guard: if ``to_prefix`` starts with ``from_prefix``
    (e.g., ``from=projects/X`` ``to=projects/X-old``), re-running the
    same command would compound the rewrite on already-migrated rows.
    Rejected with ValueError.

    Wildcard safety: literal `SUBSTR(doc_path, 1, ?) = ?` comparison is
    used instead of `LIKE ? || '%'` so any SQL wildcards (``%``, ``_``)
    inside ``from_prefix`` are treated as literal characters.
    """
    return reassign_doc_path_prefixes(conn, [(from_prefix, to_prefix)], dry_run=dry_run)


def reassign_doc_path_prefixes(
    conn: sqlite3.Connection,
    pairs: Sequence[tuple[str, str]],
    *,
    dry_run: bool = True,
) -> dict:
    """`reassign_doc_path_prefix` for several (from, to) pairs in one pass.

    One collision check and one invariant cover all of them (`memex project
    move` reassigns memos/, transcripts/ and auto-memory/ together). The
    from-prefixes must not overlap — one path would be mapped twice.
    """
    pairs = list(pairs)
    if not pairs:
        raise ValueError("no prefixes given")
    for from_prefix, to_prefix in pairs:
        _validate_prefix_pair(from_prefix, to_prefix)
    froms = [from_prefix for from_prefix, _ in pairs]
    for i, a in enumerate(froms):
        for b in froms[i + 1 :]:
            if a.startswith(b) or b.startswith(a):
                raise ValueError(f"overlapping from-prefixes: {a!r} and {b!r}")

    fts_rows = _fts_rows(conn)
    mapping: dict[str, str] = {}
    for from_prefix, to_prefix in pairs:
        for path in _paths_with_prefix(conn, from_prefix, fts_rows):
            mapping[path] = to_prefix + path[len(from_prefix) :]
    return _reassign(conn, mapping, fts_rows, dry_run=dry_run)


def reassign_doc_paths(
    conn: sqlite3.Connection,
    mapping: Mapping[str, str],
    *,
    dry_run: bool = True,
) -> dict:
    """Exact-path form of `reassign_doc_path_prefix`: ``{old_path: new_path}``.

    For moving single files — a prefix equal to a file's path would also
    match any longer path that starts with it. Same return shape, same
    collision and invariant rules. Does not commit.
    """
    mapping = dict(mapping)
    for old, new in mapping.items():
        if not old or not new:
            raise ValueError("document paths must be non-empty")
        if old == new:
            raise ValueError(f"source and target are identical (no-op): {old!r}")
    return _reassign(conn, mapping, _fts_rows(conn), dry_run=dry_run)


def reassign_invariant_violations(stats: dict) -> list[str]:
    """Tables whose reassign UPDATE changed a different number of rows than
    it matched — `[]` when the move is whole."""
    return [
        f"{table}: matched={counts['matched']} updated={counts['updated']}"
        for table, counts in stats.get("tables", {}).items()
        if counts["matched"] != counts["updated"]
    ]


def _reassign(
    conn: sqlite3.Connection,
    mapping: dict[str, str],
    fts_rows: list[tuple[int, str]] | None,
    *,
    dry_run: bool,
) -> dict:
    present = [(t, c) for t, c in _DOC_PATH_TABLES if _mirror_table_exists(conn, t)]
    old_paths = sorted(mapping)
    targets = sorted(set(mapping.values()))
    tables: dict[str, dict[str, int]] = {}
    collisions: set[str] = set()
    # Two sources onto one target would merge two documents into one, with every
    # matched == updated invariant still holding (gauntlet round 1): a collision.
    sources_per_target: dict[str, int] = {}
    for dst in mapping.values():
        sources_per_target[dst] = sources_per_target.get(dst, 0) + 1
    collisions.update(dst for dst, n in sources_per_target.items() if n > 1)

    fts_match: list[tuple[int, str]] = []
    for table, col in present:
        if table == "fts_content":
            fts_match = [(rowid, path) for rowid, path in (fts_rows or []) if path in mapping]
            existing = {path for _, path in (fts_rows or [])}
            collisions.update(t for t in targets if t in existing)
            tables[table] = {"matched": len(fts_match), "updated": 0}
            continue
        matched = 0
        for batch in _chunked(old_paths):
            matched += conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {col} IN ({','.join('?' for _ in batch)})",
                batch,
            ).fetchone()[0]
        for batch in _chunked(targets):
            collisions.update(
                row[0]
                for row in conn.execute(
                    f"SELECT DISTINCT {col} FROM {table} "
                    f"WHERE {col} IN ({','.join('?' for _ in batch)})",
                    batch,
                )
            )
        tables[table] = {"matched": int(matched), "updated": 0}

    inbound = 0
    if _mirror_table_exists(conn, "wikilinks"):
        for batch in _chunked(old_paths):
            inbound += conn.execute(
                "SELECT COUNT(*) FROM wikilinks "
                f"WHERE target_path IN ({','.join('?' for _ in batch)})",
                batch,
            ).fetchone()[0]

    # Only a path that crosses project folders gets a new project: within one
    # folder (memos/ -> memos/archive/) the indexed project stays as written.
    new_project = {
        old: _project_of(new)
        for old, new in mapping.items()
        if _project_of(old) != _project_of(new)
    }

    stats: dict = {
        "obs_matched": tables.get("observations", {}).get("matched", 0),
        "chunks_matched": tables.get("chunks", {}).get("matched", 0),
        "obs_updated": 0,
        "chunks_updated": 0,
        "dry_run": dry_run,
        "tables": tables,
        "paths": len(mapping),
        "project_changed": len(new_project),
        "collisions": sorted(collisions),
        "inbound_links": int(inbound),
    }
    if dry_run or not mapping:
        return stats
    if collisions:
        sample = ", ".join(sorted(collisions)[:3])
        raise sqlite3.IntegrityError(
            f"{len(collisions)} target path(s) already have index rows (e.g. {sample}); "
            "refusing to merge two documents' rows"
        )

    for table, col in present:
        updated = 0
        if table == "fts_content":
            for rowid, old in fts_match:
                if old in new_project:
                    cur = conn.execute(
                        "UPDATE fts_content SET path = ?, project = ? WHERE rowid = ?",
                        (mapping[old], new_project[old], rowid),
                    )
                else:
                    cur = conn.execute(
                        "UPDATE fts_content SET path = ? WHERE rowid = ?", (mapping[old], rowid)
                    )
                updated += max(cur.rowcount, 0)
        else:
            set_project = table == "chunks" and "doc_project" in _table_columns(conn, "chunks")
            for old in old_paths:
                if set_project and old in new_project:
                    cur = conn.execute(
                        "UPDATE chunks SET doc_path = ?, doc_project = ? WHERE doc_path = ?",
                        (mapping[old], new_project[old], old),
                    )
                else:
                    cur = conn.execute(
                        f"UPDATE {table} SET {col} = ? WHERE {col} = ?", (mapping[old], old)
                    )
                updated += max(cur.rowcount, 0)
        tables[table]["updated"] = updated

    if new_project:
        _update_vec_projects(conn, mapping, new_project, tables)

    stats["obs_updated"] = tables.get("observations", {}).get("updated", 0)
    stats["chunks_updated"] = tables.get("chunks", {}).get("updated", 0)
    return stats


def _update_vec_projects(
    conn: sqlite3.Connection,
    mapping: dict[str, str],
    new_project: dict[str, str],
    tables: dict[str, dict[str, int]],
) -> None:
    """Retag the vec0 `doc_project` metadata of moved rows (parents already moved).

    Without this a project-filtered vector search keeps finding moved
    observations under the old project indefinitely — an incremental
    rebuild never rewrites an observation's vector row.
    """
    for vec_table, parent, path_col in _VEC_PROJECT_COLUMNS:
        if not (_mirror_table_exists(conn, vec_table) and _mirror_table_exists(conn, parent)):
            continue
        db_utils.load_vec_extension(conn)
        try:
            if "doc_project" not in _table_columns(conn, vec_table):
                continue  # pre-v0.15.0 vec table without metadata columns
            matched = updated = 0
            for old, project in new_project.items():
                parent_ids = [
                    row[0]
                    for row in conn.execute(
                        f"SELECT id FROM {parent} WHERE {path_col} = ?", (mapping[old],)
                    )
                ]
                # Point statements by rowid: vec0 cannot use a
                # `rowid IN (subquery)` constraint and scans the whole vector
                # table for each one (measured ~60x slower at 100K rows).
                # A parent without a vector (an embedding gap) is not a match.
                for rowid in parent_ids:
                    if conn.execute(
                        f"SELECT 1 FROM {vec_table} WHERE rowid = ?", (rowid,)
                    ).fetchone() is None:
                        continue
                    matched += 1
                    cur = conn.execute(
                        f"UPDATE {vec_table} SET doc_project = ? WHERE rowid = ?",
                        (project, rowid),
                    )
                    updated += max(cur.rowcount, 0)
        except sqlite3.OperationalError as exc:
            # Same refusal as delete_observation_ids: the table exists and
            # holds rows this connection cannot reach. Leaving their project
            # stale would be silent; failing is not.
            raise sqlite3.OperationalError(
                f"cannot update {vec_table!r} project metadata: {exc}. Load "
                "sqlite-vec on this connection (db_utils.load_vec_extension) "
                "before reassigning documents across projects."
            ) from exc
        tables[vec_table] = {"matched": int(matched), "updated": updated}


def delete_document_rows(conn: sqlite3.Connection, doc_path: str) -> dict[str, int]:
    """Remove one document from the index: every `_DOC_PATH_TABLES` row, its
    chunk vectors, and its observations (through `delete_observations_for_doc`,
    so mirrors and provenance are handled).

    The same rows `rebuild_incremental` removes for a document gone from
    disk; for a tool that deletes the file itself (`memex project move`
    removing a template-only `_project.md`), so the next rebuild has nothing
    left to report. Returns rows removed per table. Does not commit.
    """
    removed: dict[str, int] = {}
    if _mirror_table_exists(conn, "vec_chunks") and _mirror_table_exists(conn, "chunks"):
        db_utils.load_vec_extension(conn)
        chunk_ids = [
            row[0] for row in conn.execute("SELECT id FROM chunks WHERE doc_path = ?", (doc_path,))
        ]
        removed["vec_chunks"] = 0
        try:
            for rowid in chunk_ids:  # by rowid — see _update_vec_projects
                cur = conn.execute("DELETE FROM vec_chunks WHERE rowid = ?", (rowid,))
                removed["vec_chunks"] += max(cur.rowcount, 0)
        except sqlite3.OperationalError as exc:
            raise sqlite3.OperationalError(
                f"cannot delete chunk vectors for {doc_path!r}: {exc}"
            ) from exc
    for table, col in _DOC_PATH_TABLES:
        if not _mirror_table_exists(conn, table):
            continue
        if table == "observations":
            removed[table] = delete_observations_for_doc(conn, doc_path)
            continue
        cur = conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (doc_path,))
        removed[table] = max(cur.rowcount, 0)
    return removed
