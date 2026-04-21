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
            result["chunks"] = conn.execute(
                "SELECT COUNT(*) FROM chunks c "
                "LEFT JOIN vec_chunks v ON v.rowid = c.id "
                "WHERE v.rowid IS NULL"
            ).fetchone()[0]
            result["docs"] = conn.execute(
                "SELECT COUNT(DISTINCT c.doc_path) FROM chunks c "
                "LEFT JOIN vec_chunks v ON v.rowid = c.id "
                "WHERE v.rowid IS NULL"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            pass
        try:
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

        def _embed_and_insert(rows, vec_table):
            stats_key_prefix = "chunks" if vec_table == "vec_chunks" else "observations"
            stats[f"{stats_key_prefix}_pending"] = len(rows)
            for i in range(0, len(rows), batch_size):
                window = rows[i:i + batch_size]
                ids = [r[0] for r in window]
                texts = [r[1] for r in window]
                try:
                    vecs = pipeline._provider_impl.embed_texts(
                        texts, task_type="document"
                    )
                except PartialEmbeddingFailure as exc:
                    vecs = exc.results
                for rowid, vec in zip(ids, vecs):
                    if vec is None:
                        stats[f"{stats_key_prefix}_failed"] += 1
                        continue
                    try:
                        conn.execute(
                            f"INSERT INTO {vec_table}(rowid, embedding) VALUES (?, ?)",
                            (rowid, serialize_f32(vec)),
                        )
                        stats[f"{stats_key_prefix}_embedded"] += 1
                    except sqlite3.IntegrityError:
                        # Row already has a vec entry (concurrent writer);
                        # not a failure, just skip.
                        pass
                conn.commit()

        # Chunks
        try:
            missing_chunks = conn.execute(
                "SELECT c.id, c.content FROM chunks c "
                "LEFT JOIN vec_chunks v ON v.rowid = c.id "
                "WHERE v.rowid IS NULL"
            ).fetchall()
            _embed_and_insert(missing_chunks, "vec_chunks")
        except sqlite3.OperationalError as exc:
            stats["error"] = f"chunks query failed: {exc}"

        # Observations
        try:
            missing_obs = conn.execute(
                "SELECT o.id, o.content FROM observations o "
                "LEFT JOIN vec_observations v ON v.rowid = o.id "
                "WHERE v.rowid IS NULL"
            ).fetchall()
            _embed_and_insert(missing_obs, "vec_observations")
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

    elif args.full:
        print(f"Starting full rebuild at {memex}...")
        stats = rebuild_full(
            memex,
            with_embeddings=not args.no_embeddings,
            atomic=not args.no_atomic
        )
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
