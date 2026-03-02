#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "google-genai>=1.0.0",
#     "sqlite-vec>=0.1.6",
#     "tiktoken>=0.5",
# ]
# ///
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
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Import from sibling modules
from embeddings import (
    EmbeddingPipeline,
    init_embedding_schema,
    index_document,
    chunk_markdown,
    content_hash,
    get_embedding_config,
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
            except (UnicodeDecodeError, FileNotFoundError):
                pass
            except Exception as e:
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
    conn = sqlite3.connect(target_path)
    try:
        # Initialize schemas
        init_fts_schema(conn)

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
            "errors": 0,
        }

        print(f"Found {len(documents)} documents to index...")

        for i, doc_path in enumerate(documents):
            try:
                # FTS indexing
                index_file_fts(conn, doc_path, memex)
                stats["fts_indexed"] += 1

                # Embedding indexing
                if pipeline:
                    chunk_count = index_document(conn, doc_path, memex, pipeline)
                    stats["chunks_indexed"] += chunk_count
                    if chunk_count > 0:
                        stats["embeddings_generated"] += chunk_count

                # Progress
                if (i + 1) % 10 == 0:
                    print(f"  Indexed {i + 1}/{len(documents)}...")

            except Exception as e:
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

    conn = sqlite3.connect(index_path)
    try:
        # Ensure schemas exist
        init_fts_schema(conn)
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

                # Re-index this document
                index_file_fts(conn, doc_path, memex)

                if pipeline:
                    index_document(conn, doc_path, memex, pipeline)

            except Exception as e:
                print(f"Error indexing {doc_path}: {e}", file=sys.stderr)
                stats["errors"] += 1

        # Remove deleted documents from index
        for old_path in existing_hashes.keys():
            if old_path not in indexed_paths:
                conn.execute("DELETE FROM fts_content WHERE path = ?", (old_path,))
                conn.execute("DELETE FROM chunks WHERE doc_path = ?", (old_path,))
                conn.execute("DELETE FROM doc_hashes WHERE path = ?", (old_path,))
                # Clean up graph metadata
                conn.execute("DELETE FROM wikilinks WHERE source_path = ?", (old_path,))
                conn.execute("DELETE FROM tasks WHERE doc_path = ?", (old_path,))
                conn.execute("DELETE FROM sections WHERE doc_path = ?", (old_path,))
                conn.execute("DELETE FROM doc_tags WHERE doc_path = ?", (old_path,))
                conn.execute("DELETE FROM doc_aliases WHERE doc_path = ?", (old_path,))
                stats["deleted"] += 1

        # Update metadata
        conn.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            ("last_incremental_update", datetime.now().isoformat())
        )

        conn.commit()
    finally:
        conn.close()

    return stats


def get_index_status(memex: Path) -> dict:
    """Get index statistics."""
    index_path = memex / "_index.sqlite"

    if not index_path.exists():
        return {"exists": False}

    conn = sqlite3.connect(index_path)
    try:
        # Load sqlite-vec extension for vec_chunks queries
        try:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            vec_loaded = True
        except (ImportError, Exception):
            vec_loaded = False

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
    ])

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
        ])
    else:
        # Incremental
        lines.extend([
            f"Total documents: {stats['total_docs']}",
            f"New: {stats['new']}",
            f"Updated: {stats['updated']}",
            f"Unchanged: {stats['unchanged']}",
            f"Deleted: {stats['deleted']}",
        ])

    if stats.get("errors", 0) > 0:
        lines.append(f"Errors: {stats['errors']}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Index rebuild utilities")
    parser.add_argument("--full", action="store_true",
                        help="Full rebuild with embeddings")
    parser.add_argument("--incremental", action="store_true",
                        help="Incremental update (changed docs only)")
    parser.add_argument("--status", action="store_true",
                        help="Show index statistics")
    parser.add_argument("--no-embeddings", action="store_true",
                        help="Skip embedding generation (FTS only)")
    parser.add_argument("--no-atomic", action="store_true",
                        help="Don't use atomic swap (faster but riskier)")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON")

    args = parser.parse_args()

    # Get memex path (check config first, then env var, then script location)
    config_path = Path.home() / ".memex" / "config.json"
    memex = None
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
            if "memex_path" in config:
                memex = Path(config["memex_path"]).expanduser()
        except (json.JSONDecodeError, KeyError):
            pass
    if not memex:
        memex = Path(os.environ.get("CLAUDE_PLUGIN_ROOT", Path(__file__).parent.parent))

    if not memex.exists():
        print(f"Memex path not found: {memex}", file=sys.stderr)
        sys.exit(1)

    if args.status:
        stats = get_index_status(memex)
        if args.json:
            print(json.dumps(stats, indent=2))
        else:
            print(format_status(stats))

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


if __name__ == "__main__":
    main()
