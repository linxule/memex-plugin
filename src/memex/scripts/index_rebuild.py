"""
Claude Memory Plugin - Index Rebuild

Atomic index rebuild with embeddings support.

Features:
- Full rebuild: Complete rebuild of FTS5 + vector indexes
- Incremental: Only re-index changed documents
- Atomic swap: Uses temp DB to prevent corruption during rebuild

Usage:
    index_rebuild.py --full         # Full rebuild with embeddings
    index_rebuild.py --incremental  # Only changed documents
    index_rebuild.py --status       # Show index statistics
"""

import argparse
import fcntl
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


from memex.observations import init_observation_schema
from memex.observations import delete_observations_for_doc
from memex.paths import get_memex_path

# Import from sibling modules
from memex.scripts.embeddings import (
    EmbeddingPipeline,
    PartialEmbeddingFailure,
    init_embedding_schema,
    index_document,
    content_hash,
    serialize_f32,
)


# ============================================================================
# Observation Preservation Registry
# ============================================================================
#
# Tables/virtual-tables carried over from the old index into the freshly built
# atomic tmp DB on `rebuild_full(..., atomic=True)`. Observations are extracted
# by sonnet subagents (`memex backfill obs --stdin`), not derived from the
# documents at index time — so without explicit preservation, the atomic swap
# would silently install a DB with empty obs tables (the May 7, 2026 incident
# dropped ~3265 obs).
#
# CRITICAL invariant: this registry plus the special-case branches in
# `_preserve_obs_tables` (fts_observations, vec_observations) must cover EVERY
# table created by `init_observation_schema` in memex/observations.py. The test
# `test_preservation_registry_covers_init_schema` enforces this — if you add a
# new obs-related table to `init_observation_schema`, you MUST extend this
# registry (or the special-case branches) or CI will fail.
#
# Each entry: (table_name, column_list_sql, select_clause_sql).
# select_clause_sql joins/filters the rows from old.<table> down to those
# still relevant under the new index (dangling refs to deleted memos are
# intentionally dropped).
_OBS_PRESERVATION_TABLES = [
    (
        "observations",
        "id, doc_path, content, content_hash, obs_type, "
        "confidence, source_obs_ids, created_at",
        "SELECT id, doc_path, content, content_hash, obs_type, "
        "confidence, source_obs_ids, created_at "
        "FROM old.observations "
        "WHERE doc_path IN (SELECT DISTINCT path FROM main.fts_content)",
    ),
    (
        "observation_topics",
        "observation_id, topic_slug",
        "SELECT ot.observation_id, ot.topic_slug "
        "FROM old.observation_topics ot "
        "JOIN main.observations o ON o.id = ot.observation_id",
    ),
]
# Virtual tables handled by special-case branches in _preserve_obs_tables —
# named here so the registry-coverage test treats them as accounted for.
_OBS_VIRTUAL_TABLES = {"fts_observations", "vec_observations"}


# ============================================================================
# Database Initialization
# ============================================================================

def init_fts_schema(conn: sqlite3.Connection):
    """Initialize FTS5 table."""
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS fts_content
        USING fts5(
            path,
            title,
            content,
            type,
            project,
            date,
            tokenize='porter unicode61'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS index_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()


def parse_frontmatter(content: str) -> dict:
    """Extract YAML frontmatter from markdown."""
    if not content.startswith("---"):
        return {}

    try:
        end = content.index("---", 3)
        yaml_section = content[3:end].strip()

        result = {}
        for line in yaml_section.split("\n"):
            if ":" in line:
                key, _, value = line.partition(":")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                result[key] = value

        return result
    except ValueError:
        return {}


def index_file_fts(conn: sqlite3.Connection, file_path: Path, memex: Path):
    """Index a single file in FTS5 table."""
    try:
        content = file_path.read_text()
        meta = parse_frontmatter(content)

        # Determine type from path or frontmatter
        file_type = meta.get("type", "")
        if not file_type:
            if "/memos/" in str(file_path):
                file_type = "memo"
            elif "/transcripts/" in str(file_path):
                file_type = "transcript"
            elif "/auto-memory/" in str(file_path):
                file_type = "auto-memory"
            elif str(file_path).startswith(str(memex / "topics")):
                file_type = "concept"
            else:
                file_type = "note"

        # Determine project from path
        project = meta.get("project", "")
        if not project:
            rel_path = file_path.relative_to(memex)
            parts = rel_path.parts
            if len(parts) >= 2 and parts[0] == "projects":
                project = parts[1]

        # Get title
        title = meta.get("title", file_path.stem)

        # Get date (from frontmatter or filename)
        date_str = meta.get("date", "")
        if not date_str:
            # Try to extract from filename (e.g., 20260128-memo.md)
            import re
            match = re.match(r'^(\d{4})(\d{2})(\d{2})', file_path.stem)
            if match:
                date_str = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"

        # Relative path for storage
        rel_path_str = str(file_path.relative_to(memex))

        # Remove existing entry
        conn.execute("DELETE FROM fts_content WHERE path = ?", (rel_path_str,))

        # Insert new entry
        conn.execute(
            "INSERT INTO fts_content (path, title, content, type, project, date) VALUES (?, ?, ?, ?, ?, ?)",
            (rel_path_str, title, content, file_type, project, date_str)
        )

    except Exception as e:
        print(f"Warning: Failed to index {file_path}: {e}", file=sys.stderr)


# ============================================================================
# Index Operations
# ============================================================================

def find_documents(memex: Path) -> list[Path]:
    """Find all indexable markdown files."""
    documents = []

    for pattern in ["projects/**/*.md", "topics/*.md"]:
        for file_path in memex.glob(pattern):
            # Skip templates and special files
            if file_path.name.startswith("_") and file_path.name != "_project.md":
                continue
            if "/_templates/" in str(file_path):
                continue
            if "/_views/" in str(file_path):
                continue

            # Skip archived documents
            try:
                meta = parse_frontmatter(file_path.read_text())
                if meta.get("status") == "archived":
                    continue
            except (OSError, UnicodeDecodeError, ValueError) as e:
                print(f"Warning: Could not check archived status for {file_path}: {e}", file=sys.stderr)

            documents.append(file_path)

    return documents


def rebuild_full(
    memex: Path,
    with_embeddings: bool = True,
    atomic: bool = True
) -> dict:
    """
    Full rebuild of FTS5 and vector indexes.

    Args:
        memex: Path to memex vault
        with_embeddings: Whether to generate embeddings
        atomic: Use temp DB swap pattern

    Returns:
        Statistics dict
    """
    index_path = memex / "_index.sqlite"
    temp_path = memex / "_index.sqlite.tmp"
    backup_path = memex / "_index.sqlite.bak"

    # Clean up any stale temp files
    for p in [temp_path, backup_path]:
        if p.exists():
            p.unlink()

    target_path = temp_path if atomic else index_path

    # Remove existing if not atomic
    if not atomic and index_path.exists():
        index_path.unlink()

    # Create connection
    conn = _connect_index(target_path)
    try:
        # Initialize schemas
        init_fts_schema(conn)
        init_observation_schema(conn)

        vec_available = False
        if with_embeddings:
            vec_available = init_embedding_schema(conn)

        # Initialize pipeline
        pipeline = None
        if with_embeddings and vec_available:
            pipeline = EmbeddingPipeline()
            if not pipeline.enabled:
                print("Warning: Embeddings disabled (no API key)", file=sys.stderr)
                pipeline = None

        # Find and index documents
        documents = find_documents(memex)
        stats = {
            "total_docs": len(documents),
            "fts_indexed": 0,
            "chunks_indexed": 0,
            "embeddings_generated": 0,
            "observations_stored": 0,
            "errors": 0,
        }

        print(f"Found {len(documents)} documents to index...")

        # Per-doc atomicity via SAVEPOINT. `index_document` and `embed_chunks`
        # no longer commit internally (as of 2026-04-21), so savepoints actually
        # contain the writes. On mid-doc failure we ROLLBACK TO SAVEPOINT and
        # move on without polluting the batch; on success we RELEASE and
        # commit the whole batch at the end. Rebuild_full creates the DB
        # fresh, so no concurrent writers to worry about.
        for i, doc_path in enumerate(documents):
            conn.execute("SAVEPOINT doc")
            try:
                # FTS indexing
                index_file_fts(conn, doc_path, memex)
                stats["fts_indexed"] += 1

                # Embedding indexing
                if pipeline:
                    result = index_document(conn, doc_path, memex, pipeline)
                    stats["chunks_indexed"] += result["chunks"]
                    stats["embeddings_generated"] += result["embedded"]

                conn.execute("RELEASE SAVEPOINT doc")

                # Progress
                if (i + 1) % 10 == 0:
                    print(f"  Indexed {i + 1}/{len(documents)}...")

            except Exception as e:
                # Roll this doc's writes back but keep the loop alive so
                # subsequent docs can proceed. ROLLBACK must succeed — if
                # it raises, that's a real bug and we want it loud. RELEASE
                # is split into its own guard because SQLite does leave the
                # savepoint in place after ROLLBACK (per docs), but defensive
                # coding: if it's already gone, we don't want that to mask
                # the original exception `e`.
                _rollback_savepoint_or_die(conn, "doc")
                _release_savepoint_if_exists(conn, "doc")
                print(f"Error indexing {doc_path}: {e}", file=sys.stderr)
                stats["errors"] += 1

        # Preserve observations across the atomic rebuild. Observations are
        # extracted by sonnet subagents (`memex backfill obs --stdin`), not
        # derived from documents at index time — so a `--full` rebuild would
        # otherwise wipe them (the May 7, 2026 incident dropped ~3265 obs).
        # See `_OBS_PRESERVATION_TABLES` / `_preserve_obs_tables` for the
        # all-or-nothing SAVEPOINT semantics: partial preservation rolls back
        # and raises, so the atomic swap never installs a half-preserved DB.
        preservation_stats = {
            "observations": 0,
            "observation_topics": 0,
            "fts_observations": 0,
            "vec_observations": 0,
        }
        if atomic and index_path.exists():
            attached = False
            preservation_error: Exception | None = None
            try:
                # BEGIN IMMEDIATE on the old DB before ATTACH so a concurrent
                # `memex backfill obs --stdin` writer can't commit observations
                # mid-snapshot (WAL + busy_timeout otherwise allow that race;
                # those obs would land post-ATTACH-snapshot and silently die
                # on swap). The advisory file lock acquired in the CLI handler
                # is the primary defense; this is the secondary belt.
                old_conn = sqlite3.connect(str(index_path), timeout=10.0)
                try:
                    old_conn.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as e:
                    # Lock held by an active writer despite the advisory lock
                    # (or pre-lock-era backfill). Surface explicitly rather
                    # than silently snapshotting a moving target.
                    old_conn.close()
                    raise sqlite3.OperationalError(
                        f"cannot acquire write lock on old index for "
                        f"obs preservation snapshot: {e}"
                    )

                try:
                    conn.execute("ATTACH DATABASE ? AS old", (str(index_path),))
                    attached = True
                    # Detect whether the old DB even has the obs schema
                    # (pre-0.11 indexes don't). No-op when absent — not an error.
                    has_obs = conn.execute(
                        "SELECT 1 FROM old.sqlite_master "
                        "WHERE type='table' AND name='observations'"
                    ).fetchone()
                    if has_obs:
                        conn.execute("SAVEPOINT obs_preserve")
                        try:
                            preservation_stats = _preserve_obs_tables(
                                conn, vec_available
                            )
                            conn.execute("RELEASE SAVEPOINT obs_preserve")
                        except Exception as e:
                            # Roll back the entire preservation block. Better
                            # to install a DB with zero obs than one with
                            # half-preserved obs whose FTS/vec views silently
                            # disagree. Widened from OperationalError to
                            # Exception (R4-P1-C): sqlite-vec ValueError on
                            # dim mismatch, struct.error on binary unpack,
                            # decode errors etc. all count as "preservation
                            # failed; abort swap." Without the wider catch,
                            # those exceptions would escape with the savepoint
                            # still open and the tmp DB still ATTACHed.
                            _rollback_savepoint_or_die(conn, "obs_preserve")
                            _release_savepoint_if_exists(conn, "obs_preserve")
                            preservation_error = e
                            raise
                        total = sum(preservation_stats.values())
                        if total:
                            print(
                                f"  Preserved "
                                f"{preservation_stats['observations']} observations + "
                                f"{preservation_stats['observation_topics']} topic tags + "
                                f"{preservation_stats['fts_observations']} fts rows + "
                                f"{preservation_stats['vec_observations']} vec rows "
                                f"from old index"
                            )
                finally:
                    # Always release the BEGIN IMMEDIATE on the old DB.
                    try:
                        old_conn.execute("ROLLBACK")
                    except sqlite3.OperationalError:
                        pass
                    old_conn.close()
            except Exception as e:
                # All-or-nothing: a HAS-obs old DB whose preservation failed
                # mid-flight signals a real problem (disk-full, schema drift
                # on a column, sqlite-vec dim mismatch, etc.). The user opted
                # into `--full` expecting the obs to survive — silently
                # shipping a wiped DB defeats that. Record the error in stats
                # and re-raise; the atomic swap is in a `finally` further down
                # but we want it skipped, which the exception accomplishes.
                # Widened from OperationalError (R4-P1-C) so non-sqlite
                # failures inside _preserve_obs_tables still trigger rollback
                # rather than escaping with state half-mutated.
                stats["preservation_error"] = (
                    f"{type(preservation_error or e).__name__}: "
                    f"{preservation_error or e}"
                )
                print(
                    f"  Could not preserve observations from old index: {e}",
                    file=sys.stderr,
                )
                # Best-effort detach so the tmp DB isn't left with `old` bound.
                if attached:
                    try:
                        conn.execute("DETACH DATABASE old")
                    except sqlite3.OperationalError:
                        pass
                raise RuntimeError(
                    f"observation preservation failed; atomic swap aborted "
                    f"to avoid wiping the existing index: "
                    f"{type(e).__name__}: {e}"
                ) from e
            finally:
                if attached and "preservation_error" not in stats:
                    # Commit before DETACH — SQLite refuses to detach a DB
                    # that has writes pending in the current transaction.
                    try:
                        conn.commit()
                        conn.execute("DETACH DATABASE old")
                    except sqlite3.OperationalError as e:
                        print(
                            f"  DETACH after observation preservation failed: {e}",
                            file=sys.stderr,
                        )
        stats["observations_preserved"] = preservation_stats["observations"]
        stats["observation_topics_preserved"] = preservation_stats["observation_topics"]
        stats["fts_observations_preserved"] = preservation_stats["fts_observations"]
        stats["vec_observations_preserved"] = preservation_stats["vec_observations"]

        # Record metadata
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            ("last_full_rebuild", now)
        )
        conn.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            ("schema_version", "2")
        )
        if pipeline:
            conn.execute(
                "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
                ("embedding_model", pipeline.model)
            )

        conn.commit()
    finally:
        conn.close()

    # Atomic swap
    if atomic:
        try:
            if index_path.exists():
                index_path.rename(backup_path)
            temp_path.rename(index_path)
            if backup_path.exists():
                backup_path.unlink()
        except Exception as e:
            # Restore backup on failure
            if backup_path.exists():
                if index_path.exists():
                    index_path.unlink()
                backup_path.rename(index_path)
            if temp_path.exists():
                temp_path.unlink()
            raise RuntimeError(f"Atomic swap failed: {e}")

    stats["completed_at"] = now
    stats["embedding_gaps"] = count_embedding_gaps(memex)
    return stats


def rebuild_incremental(memex: Path) -> dict:
    """
    Incremental update - only re-index changed documents.

    Uses SHA-256 hashes to detect changes.
    """
    index_path = memex / "_index.sqlite"

    if not index_path.exists():
        print("No existing index found. Running full rebuild...")
        return rebuild_full(memex, with_embeddings=True, atomic=True)

    conn = _connect_index(index_path)
    try:
        # Ensure schemas exist
        init_fts_schema(conn)
        init_observation_schema(conn)
        vec_available = init_embedding_schema(conn)

        # Initialize pipeline
        pipeline = None
        if vec_available:
            pipeline = EmbeddingPipeline()
            if not pipeline.enabled:
                pipeline = None

        # Get existing document hashes
        existing_hashes = {}
        try:
            cursor = conn.execute("SELECT path, content_hash FROM doc_hashes")
            existing_hashes = {row[0]: row[1] for row in cursor}
        except sqlite3.OperationalError:
            pass  # doc_hashes table doesn't exist yet

        # Find documents and check for changes
        documents = find_documents(memex)
        stats = {
            "total_docs": len(documents),
            "unchanged": 0,
            "updated": 0,
            "new": 0,
            "deleted": 0,
            "observations_stored": 0,
            "errors": 0,
        }

        indexed_paths = set()

        for doc_path in documents:
            rel_path = str(doc_path.relative_to(memex))
            indexed_paths.add(rel_path)

            try:
                content = doc_path.read_text()
                current_hash = content_hash(content)

                if rel_path in existing_hashes:
                    if existing_hashes[rel_path] == current_hash:
                        stats["unchanged"] += 1
                        continue
                    stats["updated"] += 1
                else:
                    stats["new"] += 1
            except Exception as e:
                print(f"Error reading {doc_path}: {e}", file=sys.stderr)
                stats["errors"] += 1
                continue

            # Per-doc atomicity via SAVEPOINT (see rebuild_full for the
            # full rationale). index_document no longer commits internally
            # as of 2026-04-21, so the savepoint actually contains its writes.
            conn.execute("SAVEPOINT doc")
            try:
                index_file_fts(conn, doc_path, memex)
                if pipeline:
                    index_document(conn, doc_path, memex, pipeline)
                conn.execute("RELEASE SAVEPOINT doc")
            except Exception as e:
                try:
                    conn.execute("ROLLBACK TO SAVEPOINT doc")
                    conn.execute("RELEASE SAVEPOINT doc")
                except sqlite3.OperationalError:
                    pass
                print(f"Error indexing {doc_path}: {e}", file=sys.stderr)
                stats["errors"] += 1

        # Remove deleted documents from index
        for old_path in existing_hashes.keys():
            if old_path not in indexed_paths:
                # vec_chunks cleanup FIRST — delete by rowid (= chunks.id)
                # before chunks rows are gone. Mirrors index_document's
                # pattern. Otherwise orphan embeddings persist in
                # vec_chunks and inflate gap counters / waste space.
                old_chunk_ids = [
                    row[0] for row in conn.execute(
                        "SELECT id FROM chunks WHERE doc_path = ?", (old_path,)
                    )
                ]
                if old_chunk_ids:
                    placeholders = ','.join('?' * len(old_chunk_ids))
                    try:
                        conn.execute(
                            f"DELETE FROM vec_chunks WHERE rowid IN ({placeholders})",
                            old_chunk_ids,
                        )
                    except sqlite3.OperationalError:
                        pass  # vec_chunks may not exist or sqlite-vec not loaded

                conn.execute("DELETE FROM fts_content WHERE path = ?", (old_path,))
                conn.execute("DELETE FROM chunks WHERE doc_path = ?", (old_path,))
                conn.execute("DELETE FROM doc_hashes WHERE path = ?", (old_path,))
                # Clean up graph metadata
                conn.execute("DELETE FROM wikilinks WHERE source_path = ?", (old_path,))
                conn.execute("DELETE FROM tasks WHERE doc_path = ?", (old_path,))
                conn.execute("DELETE FROM sections WHERE doc_path = ?", (old_path,))
                conn.execute("DELETE FROM doc_tags WHERE doc_path = ?", (old_path,))
                conn.execute("DELETE FROM doc_aliases WHERE doc_path = ?", (old_path,))
                delete_observations_for_doc(conn, old_path)
                stats["deleted"] += 1

        # Update metadata
        conn.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            ("last_incremental_update", datetime.now().isoformat())
        )

        conn.commit()
    finally:
        conn.close()

    stats["embedding_gaps"] = count_embedding_gaps(memex)
    return stats


# WAL + busy_timeout helpers moved to memex.db_utils so every module that
# touches the index (writers AND readers) uses the same connection pattern.
# These module-level names remain for backward compatibility — callers and
# monkeypatches in the test suite import them from here.
from memex.db_utils import (
    connect_index as _connect_index,
    load_vec_extension as _load_vec_extension,
)


def _rollback_savepoint_or_die(conn: sqlite3.Connection, name: str) -> None:
    """ROLLBACK TO SAVEPOINT with a clear failure mode.

    Per SQLite docs, ROLLBACK TO SAVEPOINT never fails if the savepoint
    exists. A failure here means either (a) the savepoint was never
    established or (b) something released it prematurely — both are bugs
    we want surfaced, not swallowed. Letting the exception propagate kills
    the rebuild loop, but that's preferable to silently discarding partial
    writes on EVERY subsequent doc.
    """
    conn.execute(f"ROLLBACK TO SAVEPOINT {name}")


def _release_savepoint_if_exists(conn: sqlite3.Connection, name: str) -> None:
    """RELEASE SAVEPOINT, tolerating the 'no such savepoint' case.

    After ROLLBACK, SQLite normally leaves the savepoint in place, but we
    keep this tolerant because (a) we already decided to abort this doc,
    (b) the outer transaction is still healthy, and (c) we don't want a
    defensive release to mask the original caller's exception.
    """
    try:
        conn.execute(f"RELEASE SAVEPOINT {name}")
    except sqlite3.OperationalError as e:
        msg = str(e).lower()
        if "no such savepoint" in msg:
            return  # benign — already gone
        raise  # anything else is unexpected; re-raise


def _preserve_obs_tables(
    conn: sqlite3.Connection, vec_available: bool
) -> dict[str, int]:
    """Copy obs-related tables from `old` (ATTACHed) to `main`.

    Must be called inside a SAVEPOINT — partial failures must roll back
    rather than leave the new DB with half the obs data. Caller owns the
    savepoint lifecycle (`SAVEPOINT obs_preserve` / `RELEASE` / `ROLLBACK TO`).

    The registry `_OBS_PRESERVATION_TABLES` plus the FTS5/vec0 branches
    below MUST cover every table created by `init_observation_schema`;
    `test_preservation_registry_covers_init_schema` enforces this invariant.

    Raises sqlite3.OperationalError on any failure (disk full, schema drift,
    missing column on the old DB) — caller decides whether to ROLLBACK TO
    obs_preserve and abort the rebuild, or fall through.

    Returns counts keyed by canonical table name, e.g.
    {"observations": 2, "observation_topics": 3,
     "fts_observations": 2, "vec_observations": 0}.
    """
    counts: dict[str, int] = {
        "observations": 0,
        "observation_topics": 0,
        "fts_observations": 0,
        "vec_observations": 0,
    }
    # Regular tables driven by the registry.
    for table_name, cols, select_sql in _OBS_PRESERVATION_TABLES:
        conn.execute(
            f"INSERT INTO main.{table_name} ({cols}) {select_sql}"
        )
        counts[table_name] = conn.execute(
            f"SELECT COUNT(*) FROM main.{table_name}"
        ).fetchone()[0]

    # FTS5 virtual table: copy only rows whose parent observation survived
    # the preservation filter. Created empty by init_observation_schema;
    # without this carry-over, `memex ask` and search_observations_fts
    # return zero hits for preserved obs until re-extraction.
    has_fts_obs = conn.execute(
        "SELECT 1 FROM old.sqlite_master "
        "WHERE type='table' AND name='fts_observations'"
    ).fetchone()
    if has_fts_obs:
        conn.execute(
            "INSERT INTO main.fts_observations (rowid, content, obs_type) "
            "SELECT fo.rowid, fo.content, fo.obs_type "
            "FROM old.fts_observations fo "
            "JOIN main.observations o ON o.id = fo.rowid"
        )
        counts["fts_observations"] = conn.execute(
            "SELECT COUNT(*) FROM main.fts_observations"
        ).fetchone()[0]

    # vec0 virtual table: only present when sqlite-vec loaded and
    # vec_observations was initialized in main. Carried over in Python so we
    # can (a) populate the v0.15.0 metadata columns — sqlite-vec rejects NULL
    # TEXT metadata — and (b) Matryoshka-truncate to main's index dimension
    # when it differs from old's. Handles both old-with-metadata and
    # old-bare-schema indexes.
    if vec_available:
        has_vec_obs = conn.execute(
            "SELECT 1 FROM old.sqlite_master "
            "WHERE type='table' AND name='vec_observations'"
        ).fetchone()
        if has_vec_obs:
            from memex.scripts.embeddings import (
                truncate_unit_vector,
                project_from_path,
                date_to_yyyymmdd,
                get_vector_dimensions,
            )

            new_dim = get_vector_dimensions()
            old_cols = {
                r[1] for r in conn.execute("PRAGMA old.table_info(vec_observations)")
            }
            old_has_meta = {"doc_project", "doc_type", "doc_date"}.issubset(old_cols)

            if old_has_meta:
                rows = conn.execute(
                    "SELECT vo.rowid, vo.embedding, vo.doc_project, vo.doc_type, vo.doc_date "
                    "FROM old.vec_observations vo "
                    "JOIN main.observations o ON o.id = vo.rowid"
                ).fetchall()
                staged = [
                    (rid, truncate_unit_vector(emb, new_dim), p or "", t or "", d or 0)
                    for (rid, emb, p, t, d) in rows
                ]
            else:
                rows = conn.execute(
                    "SELECT vo.rowid, vo.embedding, o.doc_path "
                    "FROM old.vec_observations vo "
                    "JOIN main.observations o ON o.id = vo.rowid"
                ).fetchall()
                staged = [
                    (rid, truncate_unit_vector(emb, new_dim),
                     project_from_path(dp or ""), "memo", date_to_yyyymmdd(dp or ""))
                    for (rid, emb, dp) in rows
                ]

            conn.executemany(
                "INSERT INTO main.vec_observations "
                "(rowid, embedding, doc_project, doc_type, doc_date) VALUES (?, ?, ?, ?, ?)",
                staged,
            )
            counts["vec_observations"] = conn.execute(
                "SELECT COUNT(*) FROM main.vec_observations"
            ).fetchone()[0]

    return counts


def count_embedding_gaps(memex: Path) -> dict:
    """
    Count chunks and observations that exist in the index but lack
    corresponding vec_chunks / vec_observations entries.

    This is the actionable signal for 'something went wrong during
    embedding' — e.g., expired API key, rate-limit exhaustion.
    """
    index_path = memex / "_index.sqlite"
    result = {"chunks": 0, "observations": 0, "docs": 0, "available": False}

    if not index_path.exists():
        return result

    conn = _connect_index(index_path)
    try:
        if not _load_vec_extension(conn):
            return result
        result["available"] = True
        try:
            # Fast path: LEFT JOIN against the sqlite-vec virtual table cannot
            # use an index, so it probes per-row (~107s on 136K chunks). The
            # rebuild + reembed paths only insert into vec_chunks after chunks,
            # and chunk deletion is paired, so the COUNT delta is the right
            # gap signal at ~2000× the speed. Per-doc breakdown is only
            # interesting when a gap actually exists; gate it accordingly.
            total_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            embedded_chunks = conn.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0]
            gap = max(0, total_chunks - embedded_chunks)
            result["chunks"] = gap
            if gap > 0:
                result["docs"] = conn.execute(
                    "SELECT COUNT(DISTINCT c.doc_path) FROM chunks c "
                    "LEFT JOIN vec_chunks v ON v.rowid = c.id "
                    "WHERE v.rowid IS NULL"
                ).fetchone()[0]
        except sqlite3.OperationalError:
            pass
        try:
            # Observations are small (low thousands at most); LEFT JOIN is fine here.
            result["observations"] = conn.execute(
                "SELECT COUNT(*) FROM observations o "
                "LEFT JOIN vec_observations v ON v.rowid = o.id "
                "WHERE v.rowid IS NULL"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()

    return result


def _new_vec_ddl(table: str, dim: int) -> str:
    """DDL for a v0.15.0 vec0 table: embedding + metadata filter columns."""
    return (
        f"CREATE VIRTUAL TABLE {table} USING vec0("
        f"embedding float[{int(dim)}], "
        f"doc_project text, doc_type text, doc_date integer)"
    )


def _migrate_one_vec_table(
    conn: sqlite3.Connection,
    *,
    vec_table: str,
    select_sql_range: str,
    transform,
    idx_dim: int,
    batch_size: int,
) -> tuple[str, int]:
    """Rebuild one vec0 table at ``idx_dim`` with metadata columns, sourcing
    vectors from the existing table (Matryoshka-truncated, no re-embed).

    ``select_sql_range`` must be a query taking two params (rowid_lo, rowid_hi)
    selecting ``WHERE v.rowid >= ? AND v.rowid < ?`` so reads are batched by
    rowid window — independent queries, no long-open cursor competing with the
    staging writes.

    Returns (status, rows). status ∈ {absent, skip, recreated-empty, migrated}.
    Idempotent: a table already at idx_dim WITH metadata columns is skipped.
    """
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if vec_table not in names:
        return ("absent", 0)

    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({vec_table})")}
    has_meta = {"doc_project", "doc_type", "doc_date"}.issubset(cols)
    row = conn.execute(f"SELECT embedding FROM {vec_table} LIMIT 1").fetchone()
    cur_dim = (len(row[0]) // 4) if row and row[0] else None

    if cur_dim is None:
        # Empty table — just ensure the schema is current.
        if not has_meta:
            conn.execute(f"DROP TABLE {vec_table}")
            conn.execute(_new_vec_ddl(vec_table, idx_dim))
            conn.commit()
            return ("recreated-empty", 0)
        return ("skip", 0)

    if has_meta and cur_dim == idx_dim:
        return ("skip", 0)

    bounds = conn.execute(f"SELECT min(rowid), max(rowid) FROM {vec_table}").fetchone()
    lo_bound, hi_bound = bounds if bounds else (None, None)

    # Stage truncated vectors + metadata in a plain table (on disk, batched by
    # rowid window — avoids holding ~GB of float data in memory and avoids a
    # concurrent scan+write on the vec0 shadow tables).
    conn.execute("DROP TABLE IF EXISTS _vstage")
    conn.execute(
        "CREATE TABLE _vstage (rid INTEGER PRIMARY KEY, emb BLOB, "
        "p TEXT, t TEXT, d INTEGER)"
    )
    n = 0
    lo = lo_bound
    batch_no = 0
    while lo is not None and lo <= hi_bound:
        hi = lo + batch_size
        rows = conn.execute(select_sql_range, (lo, hi)).fetchall()
        if rows:
            staged = [transform(r, idx_dim) for r in rows]
            conn.executemany(
                "INSERT INTO _vstage (rid, emb, p, t, d) VALUES (?, ?, ?, ?, ?)",
                staged,
            )
            n += len(staged)
        lo = hi
        batch_no += 1
        if batch_no % 20 == 0:
            print(f"  {vec_table}: staged {n} vectors...", file=sys.stderr, flush=True)
    conn.commit()
    print(f"  {vec_table}: staged {n} vectors; swapping table in place...",
          file=sys.stderr, flush=True)

    # Atomic swap: DROP + CREATE + INSERT…SELECT in one transaction. Transactional
    # DDL means a mid-swap failure rolls back to the original table; the
    # pre-migration index backup is the outer safety net.
    conn.execute("BEGIN")
    try:
        conn.execute(f"DROP TABLE {vec_table}")
        conn.execute(_new_vec_ddl(vec_table, idx_dim))
        conn.execute(
            f"INSERT INTO {vec_table} (rowid, embedding, doc_project, doc_type, doc_date) "
            f"SELECT rid, emb, p, t, d FROM _vstage"
        )
        conn.execute("DROP TABLE _vstage")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return ("migrated", n)


def migrate_vec_metadata(memex: Path, batch_size: int = 2000) -> dict:
    """Migrate vec_chunks/vec_observations to the v0.15.0 schema in place:
    Matryoshka-truncate embeddings to the configured ``index_dimensions`` and
    add metadata columns (doc_project/doc_type/doc_date) for KNN filter
    pushdown.

    No API calls — vectors are sourced from the existing vec tables (full
    fidelity is also retained in embedding_cache). Idempotent: tables already
    at the target dimension with metadata are skipped.
    """
    from memex.scripts.embeddings import (
        get_vector_dimensions,
        truncate_unit_vector,
        date_to_yyyymmdd,
        project_from_path,
    )

    index_path = memex / "_index.sqlite"
    stats: dict = {
        "target_dim": None,
        "vec_chunks": "skip",
        "vec_observations": "skip",
        "chunks_migrated": 0,
        "observations_migrated": 0,
        "error": None,
    }
    if not index_path.exists():
        stats["error"] = f"no index at {index_path}"
        return stats

    idx_dim = get_vector_dimensions()
    stats["target_dim"] = idx_dim

    conn = _connect_index(index_path)
    try:
        if not _load_vec_extension(conn):
            stats["error"] = "sqlite-vec extension not available"
            return stats

        # Precompute path → doc type ONCE. fts_content is an FTS5 table with no
        # index on `path`, so a per-row JOIN full-scans it for every vector
        # (~32ms each → O(n*m), ~96 min on a 178k-chunk index). The whole map
        # builds in ~30ms.
        path_type: dict = {}
        try:
            path_type = {p: t for (p, t) in conn.execute("SELECT path, type FROM fts_content")}
        except sqlite3.OperationalError:
            pass  # minimal/legacy index without fts_content

        def _chunk_transform(r, dim):
            rowid, emb, doc_project, doc_path, doc_date = r
            return (
                rowid,
                truncate_unit_vector(emb, dim),
                doc_project or "",
                path_type.get(doc_path, ""),
                date_to_yyyymmdd(doc_date),
            )

        c_status, c_n = _migrate_one_vec_table(
            conn,
            vec_table="vec_chunks",
            select_sql_range=(
                "SELECT v.rowid, v.embedding, c.doc_project, c.doc_path, c.doc_date "
                "FROM vec_chunks v JOIN chunks c ON c.id = v.rowid "
                "WHERE v.rowid >= ? AND v.rowid < ?"
            ),
            transform=_chunk_transform,
            idx_dim=idx_dim,
            batch_size=batch_size,
        )
        stats["vec_chunks"] = c_status
        stats["chunks_migrated"] = c_n

        def _obs_transform(r, dim):
            rowid, emb, doc_path = r
            return (
                rowid,
                truncate_unit_vector(emb, dim),
                project_from_path(doc_path or ""),
                path_type.get(doc_path, "memo"),
                date_to_yyyymmdd(doc_path or ""),
            )

        o_status, o_n = _migrate_one_vec_table(
            conn,
            vec_table="vec_observations",
            select_sql_range=(
                "SELECT v.rowid, v.embedding, o.doc_path "
                "FROM vec_observations v JOIN observations o ON o.id = v.rowid "
                "WHERE v.rowid >= ? AND v.rowid < ?"
            ),
            transform=_obs_transform,
            idx_dim=idx_dim,
            batch_size=batch_size,
        )
        stats["vec_observations"] = o_status
        stats["observations_migrated"] = o_n

        return stats
    except Exception as exc:  # noqa: BLE001 — surface as stats, exit non-zero
        stats["error"] = f"{type(exc).__name__}: {exc}"
        return stats
    finally:
        conn.close()


def format_migrate_stats(stats: dict) -> str:
    """Human-readable summary of migrate_vec_metadata."""
    if stats.get("error"):
        return f"vec migration failed: {stats['error']}"
    return (
        f"vec migration → {stats.get('target_dim')}d + metadata columns\n"
        f"  vec_chunks:       {stats.get('vec_chunks')} "
        f"({stats.get('chunks_migrated', 0)} rows)\n"
        f"  vec_observations: {stats.get('vec_observations')} "
        f"({stats.get('observations_migrated', 0)} rows)"
    )


def reembed_missing(memex: Path, batch_size: int = 50) -> dict:
    """
    Find chunks and observations currently missing from the vec_* tables
    and embed them using the configured pipeline.

    Idempotent: safe to run repeatedly. Use after a rebuild that reported
    embedding gaps (e.g., expired API key, rate-limit exhaustion) once
    the root cause is fixed.
    """
    index_path = memex / "_index.sqlite"
    stats = {
        "chunks_pending": 0,
        "chunks_embedded": 0,
        "chunks_failed": 0,
        "observations_pending": 0,
        "observations_embedded": 0,
        "observations_failed": 0,
        "error": None,
    }

    if not index_path.exists():
        stats["error"] = "no index at {}".format(index_path)
        return stats

    conn = _connect_index(index_path)
    try:
        if not _load_vec_extension(conn):
            stats["error"] = "sqlite-vec extension not available"
            return stats

        pipeline = EmbeddingPipeline()
        if not pipeline.enabled or not pipeline._provider_impl:
            stats["error"] = (
                "embedding pipeline disabled — check API key or config"
            )
            return stats

        from memex.scripts.embeddings import (
            get_vector_dimensions,
            truncate_unit_vector,
            table_has_metadata,
            vec_stored_dim,
        )

        def _embed_and_insert(rows, vec_table, make_meta):
            stats_key_prefix = "chunks" if vec_table == "vec_chunks" else "observations"
            stats[f"{stats_key_prefix}_pending"] = len(rows)
            has_meta = table_has_metadata(conn, vec_table)
            # Align to the table's stored dim (fallback to config when empty) so
            # backfill never mismatches a not-yet-migrated table.
            dim = vec_stored_dim(conn, vec_table) or get_vector_dimensions()
            for i in range(0, len(rows), batch_size):
                window = rows[i:i + batch_size]
                texts = [r[1] for r in window]
                try:
                    vecs = pipeline._provider_impl.embed_texts(
                        texts, task_type="document"
                    )
                except PartialEmbeddingFailure as exc:
                    vecs = exc.results
                for row, vec in zip(window, vecs):
                    rowid = row[0]
                    if vec is None:
                        stats[f"{stats_key_prefix}_failed"] += 1
                        continue
                    try:
                        blob = truncate_unit_vector(serialize_f32(vec), dim)
                        if has_meta:
                            proj, typ, datev = make_meta(row)
                            conn.execute(
                                f"INSERT INTO {vec_table}(rowid, embedding, doc_project, doc_type, doc_date) "
                                f"VALUES (?, ?, ?, ?, ?)",
                                (rowid, blob, proj, typ, datev),
                            )
                        else:
                            conn.execute(
                                f"INSERT INTO {vec_table}(rowid, embedding) VALUES (?, ?)",
                                (rowid, blob),
                            )
                        stats[f"{stats_key_prefix}_embedded"] += 1
                    except sqlite3.IntegrityError:
                        # Row already has a vec entry (concurrent writer);
                        # not a failure, just skip.
                        pass
                conn.commit()

        from memex.scripts.embeddings import date_to_yyyymmdd, project_from_path

        # Chunks
        try:
            try:
                missing_chunks = conn.execute(
                    "SELECT c.id, c.content, c.doc_project, COALESCE(ft.type, ''), c.doc_date "
                    "FROM chunks c "
                    "LEFT JOIN vec_chunks v ON v.rowid = c.id "
                    "LEFT JOIN fts_content ft ON ft.path = c.doc_path "
                    "WHERE v.rowid IS NULL"
                ).fetchall()
            except sqlite3.OperationalError:
                # Minimal index without fts_content — fall back to chunk-only
                # metadata (doc_type unavailable → '').
                missing_chunks = conn.execute(
                    "SELECT c.id, c.content, c.doc_project, '', c.doc_date "
                    "FROM chunks c "
                    "LEFT JOIN vec_chunks v ON v.rowid = c.id "
                    "WHERE v.rowid IS NULL"
                ).fetchall()
            _embed_and_insert(
                missing_chunks,
                "vec_chunks",
                lambda r: (r[2] or "", r[3] or "", date_to_yyyymmdd(r[4])),
            )
        except sqlite3.OperationalError as exc:
            stats["error"] = f"chunks query failed: {exc}"

        # Observations
        try:
            missing_obs = conn.execute(
                "SELECT o.id, o.content, o.doc_path FROM observations o "
                "LEFT JOIN vec_observations v ON v.rowid = o.id "
                "WHERE v.rowid IS NULL"
            ).fetchall()
            _embed_and_insert(
                missing_obs,
                "vec_observations",
                lambda r: (project_from_path(r[2] or ""), "memo", date_to_yyyymmdd(r[2] or "")),
            )
        except sqlite3.OperationalError as exc:
            if stats["error"] is None:
                stats["error"] = f"observations query failed: {exc}"
    finally:
        conn.close()

    return stats


def format_reembed_stats(stats: dict) -> str:
    """Format reembed_missing statistics for human output."""
    lines = ["Embed-Missing Complete", "=" * 40]
    if stats.get("error"):
        lines.append(f"Error: {stats['error']}")
        return "\n".join(lines)

    lines.extend([
        f"Chunks: {stats['chunks_embedded']}/{stats['chunks_pending']} embedded"
        + (f", {stats['chunks_failed']} failed" if stats['chunks_failed'] else ""),
        f"Observations: {stats['observations_embedded']}/{stats['observations_pending']} embedded"
        + (f", {stats['observations_failed']} failed" if stats['observations_failed'] else ""),
    ])

    total_failed = stats["chunks_failed"] + stats["observations_failed"]
    if total_failed:
        lines.append("")
        lines.append(
            f"⚠️  {total_failed} item(s) still unembedded. "
            "Check API key, rate limits, or provider availability."
        )
    return "\n".join(lines)


def get_index_status(memex: Path) -> dict:
    """Get index statistics."""
    index_path = memex / "_index.sqlite"

    if not index_path.exists():
        return {"exists": False}

    conn = _connect_index(index_path)
    try:
        vec_loaded = _load_vec_extension(conn)

        stats = {
            "exists": True,
            "size_bytes": index_path.stat().st_size,
            "size_kb": round(index_path.stat().st_size / 1024, 1),
        }

        # FTS stats
        try:
            cursor = conn.execute("SELECT COUNT(*) FROM fts_content")
            stats["fts_documents"] = cursor.fetchone()[0]

            cursor = conn.execute("SELECT type, COUNT(*) FROM fts_content GROUP BY type")
            stats["fts_by_type"] = {row[0]: row[1] for row in cursor}
        except sqlite3.OperationalError:
            stats["fts_documents"] = 0
            stats["fts_by_type"] = {}

        # Vector stats
        try:
            cursor = conn.execute("SELECT COUNT(*) FROM chunks")
            stats["total_chunks"] = cursor.fetchone()[0]

            # vec_chunks requires sqlite-vec extension
            if vec_loaded:
                cursor = conn.execute("SELECT COUNT(*) FROM vec_chunks")
                stats["embedded_chunks"] = cursor.fetchone()[0]
            else:
                # Fallback: use chunks count as proxy
                stats["embedded_chunks"] = stats["total_chunks"]

            cursor = conn.execute("SELECT COUNT(*) FROM embedding_cache")
            stats["cached_embeddings"] = cursor.fetchone()[0]

            cursor = conn.execute("SELECT COUNT(DISTINCT doc_path) FROM chunks")
            stats["embedded_documents"] = cursor.fetchone()[0]
        except sqlite3.OperationalError:
            stats["total_chunks"] = 0
            stats["embedded_chunks"] = 0
            stats["cached_embeddings"] = 0
            stats["embedded_documents"] = 0

        try:
            cursor = conn.execute("SELECT COUNT(*) FROM observations")
            stats["observations"] = cursor.fetchone()[0]
        except sqlite3.OperationalError:
            stats["observations"] = 0

        # Embedding gaps — actionable signal when > 0
        if vec_loaded:
            stats["embedding_gaps"] = count_embedding_gaps(memex)

        # Metadata
        try:
            cursor = conn.execute("SELECT key, value FROM index_meta")
            stats["metadata"] = {row[0]: row[1] for row in cursor}
        except sqlite3.OperationalError:
            stats["metadata"] = {}

        # Graph stats
        try:
            stats["graph"] = {}
            cursor = conn.execute("SELECT COUNT(*) FROM wikilinks")
            stats["graph"]["total_links"] = cursor.fetchone()[0]
            cursor = conn.execute("SELECT COUNT(*) FROM wikilinks WHERE is_broken = 1")
            stats["graph"]["broken_links"] = cursor.fetchone()[0]
            cursor = conn.execute("SELECT COUNT(*) FROM tasks")
            stats["graph"]["total_tasks"] = cursor.fetchone()[0]
            cursor = conn.execute("SELECT COUNT(*) FROM tasks WHERE completed = 0")
            stats["graph"]["open_tasks"] = cursor.fetchone()[0]
            cursor = conn.execute("SELECT COUNT(DISTINCT tag) FROM doc_tags")
            stats["graph"]["unique_tags"] = cursor.fetchone()[0]
            cursor = conn.execute("SELECT COUNT(*) FROM doc_aliases")
            stats["graph"]["total_aliases"] = cursor.fetchone()[0]
            cursor = conn.execute("SELECT COUNT(*) FROM sections")
            stats["graph"]["total_sections"] = cursor.fetchone()[0]
        except sqlite3.OperationalError:
            stats["graph"] = {}

        return stats
    finally:
        conn.close()


# ============================================================================
# CLI
# ============================================================================

def format_status(stats: dict) -> str:
    """Format status for display."""
    if not stats.get("exists"):
        return "Index does not exist. Run --full to create."

    lines = [
        "Index Status",
        "=" * 40,
        f"Size: {stats['size_kb']} KB",
        "",
        "FTS5 Index:",
        f"  Documents: {stats['fts_documents']}",
    ]

    if stats.get("fts_by_type"):
        for doc_type, count in stats["fts_by_type"].items():
            lines.append(f"    - {doc_type}: {count}")

    lines.extend([
        "",
        "Vector Index:",
        f"  Embedded documents: {stats.get('embedded_documents', 0)}",
        f"  Total chunks: {stats.get('total_chunks', 0)}",
        f"  Embedded chunks: {stats.get('embedded_chunks', 0)}",
        f"  Cached embeddings: {stats.get('cached_embeddings', 0)}",
        f"  Observations: {stats.get('observations', 0)}",
    ])

    gaps = stats.get("embedding_gaps") or {}
    if gaps and not gaps.get("available", True):
        lines.append("")
        lines.append(
            "⚠️  sqlite-vec extension not loaded — embedding gaps cannot be checked"
        )
    else:
        gap_chunks = gaps.get("chunks", 0)
        gap_obs = gaps.get("observations", 0)
        if gap_chunks or gap_obs:
            lines.append("")
            lines.append("⚠️  Embedding gaps detected:")
            if gap_chunks:
                lines.append(
                    f"  {gap_chunks} chunk(s) across {gaps.get('docs', '?')} doc(s)"
                )
            if gap_obs:
                lines.append(f"  {gap_obs} observation(s)")
            lines.append("  Fix: memex index embed-missing")

    if stats.get("graph"):
        lines.extend([
            "",
            "Graph Index:",
            f"  Wikilinks: {stats['graph'].get('total_links', 0)} ({stats['graph'].get('broken_links', 0)} broken)",
            f"  Tasks: {stats['graph'].get('total_tasks', 0)} ({stats['graph'].get('open_tasks', 0)} open)",
            f"  Tags: {stats['graph'].get('unique_tags', 0)} unique",
            f"  Aliases: {stats['graph'].get('total_aliases', 0)}",
            f"  Sections: {stats['graph'].get('total_sections', 0)}",
        ])

    if stats.get("metadata"):
        lines.extend(["", "Metadata:"])
        for key, value in stats["metadata"].items():
            lines.append(f"  {key}: {value}")

    return "\n".join(lines)


def format_rebuild_stats(stats: dict) -> str:
    """Format rebuild statistics."""
    lines = [
        "Rebuild Complete",
        "=" * 40,
    ]

    if "fts_indexed" in stats:
        # Full rebuild
        lines.extend([
            f"Documents indexed: {stats['fts_indexed']}/{stats['total_docs']}",
            f"Chunks created: {stats['chunks_indexed']}",
            f"Embeddings generated: {stats['embeddings_generated']}",
            f"Observations stored: {stats.get('observations_stored', 0)}",
        ])
    else:
        # Incremental
        lines.extend([
            f"Total documents: {stats['total_docs']}",
            f"New: {stats['new']}",
            f"Updated: {stats['updated']}",
            f"Unchanged: {stats['unchanged']}",
            f"Deleted: {stats['deleted']}",
            f"Observations stored: {stats.get('observations_stored', 0)}",
        ])

    if stats.get("errors", 0) > 0:
        lines.append(f"Errors: {stats['errors']}")

    # Observation preservation across atomic --full rebuild (0.11.3+).
    # Surface in the summary so the operator sees that the carry-over
    # happened (and how much) rather than relying on the mid-rebuild
    # print to scroll past. Always emit a preservation line — operators
    # need to distinguish "ran-and-empty" (clean run on an obs-less vault)
    # from "ran-into-exception" (preservation failed mid-way).
    obs_pres = stats.get("observations_preserved", 0)
    ot_pres = stats.get("observation_topics_preserved", 0)
    fts_pres = stats.get("fts_observations_preserved", 0)
    vec_pres = stats.get("vec_observations_preserved", 0)
    preservation_error = stats.get("preservation_error")
    if preservation_error:
        # Distinct line — partial counts above may be misleading on failure.
        lines.append(
            f"Preservation FAILED: {preservation_error}. Run may be incomplete."
        )
    else:
        suffix = (
            " (no prior observations)"
            if not (obs_pres or ot_pres or fts_pres or vec_pres)
            else ""
        )
        lines.append(
            f"Preserved across atomic swap: "
            f"{obs_pres}/{ot_pres}/{fts_pres}/{vec_pres} "
            f"obs/topic-tags/fts/vec rows{suffix}"
        )

    gaps = stats.get("embedding_gaps") or {}
    if gaps and not gaps.get("available", True):
        lines.append("")
        lines.append(
            "⚠️  sqlite-vec extension not loaded — embedding gaps cannot be "
            "checked. Semantic search is likely broken; install sqlite-vec."
        )
    else:
        gap_chunks = gaps.get("chunks", 0)
        gap_obs = gaps.get("observations", 0)
        if gap_chunks or gap_obs:
            lines.append("")
            lines.append("⚠️  Embedding gaps detected:")
            if gap_chunks:
                lines.append(
                    f"   {gap_chunks} chunk(s) across {gaps.get('docs', '?')} doc(s) "
                    "missing embeddings"
                )
            if gap_obs:
                lines.append(f"   {gap_obs} observation(s) missing embeddings")
            lines.append("   Fix root cause (API key, rate limits), then run:")
            lines.append("     memex index embed-missing")

    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description="Index rebuild utilities",
        epilog=(
            "Examples:\n"
            "  memex index status\n"
            "  memex index rebuild\n"
            "  memex index rebuild --full\n"
            "  memex index rebuild --full --no-embeddings\n"
            "  memex index embed-missing   # Retry chunks/obs missing from vec tables"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def _run() -> None:
    parser = _build_parser()
    parser.add_argument("--full", action="store_true",
                        help="Full rebuild with embeddings")
    parser.add_argument("--incremental", action="store_true",
                        help="Incremental update (changed docs only)")
    parser.add_argument("--status", action="store_true",
                        help="Show index statistics")
    parser.add_argument("--embed-missing", action="store_true", dest="embed_missing",
                        help="Embed chunks/observations currently missing from vec tables")
    parser.add_argument("--migrate-vec", action="store_true", dest="migrate_vec",
                        help="Migrate vec tables to index_dimensions + metadata columns (no re-embed)")
    parser.add_argument("--no-embeddings", action="store_true",
                        help="Skip embedding generation (FTS only)")
    parser.add_argument("--no-atomic", action="store_true",
                        help="Don't use atomic swap (faster but riskier)")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON")

    args = parser.parse_args()

    memex = get_memex_path()

    if not memex.exists():
        print(f"Memex path not found: {memex}", file=sys.stderr)
        sys.exit(1)

    if args.status:
        stats = get_index_status(memex)
        if args.json:
            print(json.dumps(stats, indent=2))
        else:
            print(format_status(stats))

    elif args.embed_missing:
        print(f"Scanning {memex} for missing embeddings...")
        stats = reembed_missing(memex)
        if args.json:
            print(json.dumps(stats, indent=2))
        else:
            print(format_reembed_stats(stats))
        if stats.get("error"):
            sys.exit(1)
        if stats.get("chunks_failed", 0) + stats.get("observations_failed", 0) > 0:
            sys.exit(2)

    elif args.migrate_vec:
        print(f"Migrating vec tables at {memex} (Matryoshka truncate + metadata, no re-embed)...")
        stats = migrate_vec_metadata(memex)
        if args.json:
            print(json.dumps(stats, indent=2))
        else:
            print(format_migrate_stats(stats))
        if stats.get("error"):
            sys.exit(1)

    elif args.full:
        print(f"Starting full rebuild at {memex}...")
        # Advisory file lock. Blocks concurrent `--full` rebuilds and any
        # `memex backfill obs --stdin` that has acquired LOCK_SH on the
        # same path. Without this, ATTACH-old snapshot races with backfill
        # writes that commit between ATTACH-time and DETACH-time, silently
        # dropping the post-snapshot obs on swap.
        lock_dir = Path.home() / ".memex" / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / "full-rebuild.lock"
        lock_f = lock_path.open("w")
        try:
            try:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print(
                    "Error: another `memex index rebuild --full` is already "
                    "running, or a `memex backfill obs` is holding the lock. "
                    "Wait for it to finish or remove the lock file at "
                    f"{lock_path} if you know it's stale.",
                    file=sys.stderr,
                )
                sys.exit(3)
            try:
                stats = rebuild_full(
                    memex,
                    with_embeddings=not args.no_embeddings,
                    atomic=not args.no_atomic
                )
            except RuntimeError as e:
                # R4-P1-A: `rebuild_full` re-raises RuntimeError when the
                # observation-preservation savepoint failed, before the atomic
                # swap could run. Distinguish this from generic RuntimeErrors
                # (e.g., atomic-swap rename failure) by message prefix and
                # exit 4 so chained scripts can detect "rebuild aborted to
                # protect existing data" specifically. The previous design
                # relied on a `stats["preservation_error"]` sentinel that was
                # never returned because the raise propagated past the
                # assignment — making the exit-4 branch unreachable.
                if "observation preservation failed" in str(e):
                    print(f"Error: {e}", file=sys.stderr)
                    print(
                        "  The atomic swap was aborted to protect existing "
                        "data. Your original index is intact.",
                        file=sys.stderr,
                    )
                    sys.exit(4)
                raise
        finally:
            try:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
            finally:
                lock_f.close()
        # Defense in depth: even if rebuild_full returned (didn't raise),
        # check for preservation_error sentinel before printing success.
        # This path is unreachable in current code (rebuild_full always
        # raises when preservation_error is set) but kept as a belt against
        # future refactors that decide to return-not-raise.
        if stats.get("preservation_error"):
            print(
                f"Error: observation preservation failed during atomic "
                f"rebuild: {stats['preservation_error']}",
                file=sys.stderr,
            )
            sys.exit(4)
        if args.json:
            print(json.dumps(stats, indent=2))
        else:
            print(format_rebuild_stats(stats))

    elif args.incremental:
        print(f"Starting incremental update at {memex}...")
        stats = rebuild_incremental(memex)
        if args.json:
            print(json.dumps(stats, indent=2))
        else:
            print(format_rebuild_stats(stats))

    else:
        parser.print_help()
        sys.exit(1)


def main():
    try:
        _run()
    except SystemExit:
        raise
    except FileNotFoundError as exc:
        print(f"Error: {exc}\nFix: Verify the memex path exists and rerun the command.", file=sys.stderr)
        sys.exit(2)
    except sqlite3.OperationalError as exc:
        print(f"Error: {exc}\nFix: memex index rebuild --full", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
