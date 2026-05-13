from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

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
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception:
        return False


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
    dims = dimensions or settings.embeddings.dimensions
    try:
        dims = int(dims)
    except (TypeError, ValueError):
        dims = settings.embeddings.dimensions

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
    if vec_available:
        conn.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_observations
            USING vec0(embedding float[{dims}])
            """
        )
    # Intentionally no commit — callers own transaction boundaries. See
    # the docstring for the rationale.
    return vec_available


def delete_observations_for_doc(conn: sqlite3.Connection, doc_path: str) -> int:
    rows = conn.execute(
        "SELECT id FROM observations WHERE doc_path = ?",
        (doc_path,),
    ).fetchall()
    if not rows:
        return 0

    obs_ids = [row[0] for row in rows]
    placeholders = ",".join("?" for _ in obs_ids)
    try:
        conn.execute(
            f"DELETE FROM vec_observations WHERE rowid IN ({placeholders})",
            obs_ids,
        )
    except sqlite3.OperationalError:
        pass
    conn.execute(
        f"DELETE FROM fts_observations WHERE rowid IN ({placeholders})",
        obs_ids,
    )
    conn.execute(
        f"DELETE FROM observation_topics WHERE observation_id IN ({placeholders})",
        obs_ids,
    )
    conn.execute("DELETE FROM observations WHERE doc_path = ?", (doc_path,))
    return len(obs_ids)


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


def retag_topic(
    conn: sqlite3.Connection,
    old_slug: str,
    new_slug: str,
) -> int:
    count = conn.execute(
        "SELECT COUNT(*) FROM observation_topics WHERE topic_slug = ?",
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
    rows = conn.execute(
        """
        SELECT topic_slug, COUNT(*) as cnt
        FROM observation_topics
        GROUP BY topic_slug
        ORDER BY cnt DESC
        """
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


def open_index(index_path: Path) -> sqlite3.Connection:
    # WAL + busy_timeout via shared helper — callers of this writer race
    # with `memex backfill obs --stdin` and the rebuild loop. Without WAL
    # the concurrent writes hit "database is locked" intermittently.
    from memex.db_utils import connect_index

    conn = connect_index(index_path)
    init_observation_schema(conn)
    return conn
