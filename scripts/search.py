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
Claude Memory Plugin - Search Implementation

Provides hybrid search combining FTS5 (BM25) with vector similarity.

Usage:
    search.py <query> [--mode=hybrid|fts|vector] [--weights=0.7,0.3]
    search.py <query> [--type=memo|transcript|concept] [--project=name] [--limit=10]
    search.py --rebuild                    # Rebuild FTS5 index only
    search.py --rebuild --with-embeddings  # Rebuild with embeddings
    search.py --status                     # Show index statistics

Output: JSON array of matching documents
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path


def get_memex_path() -> Path:
    """Get memex vault path with fallbacks.

    Resolution order:
    1. ~/.memex/config.json -> memex_path (user override)
    2. CLAUDE_PLUGIN_ROOT env var (set by plugin system)
    3. Script location fallback (assumes scripts are in memex/scripts/)
    """
    # Check user config first
    config_path = Path("~/.memex/config.json").expanduser()
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
            if config.get("memex_path"):
                path = Path(config["memex_path"]).expanduser().resolve()
                if path.exists():
                    return path
        except (json.JSONDecodeError, KeyError):
            pass

    # Check env var (set by plugin system)
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root:
        path = Path(plugin_root)
        if path.exists():
            return path

    # Fallback: script's parent directory (memex/scripts -> memex)
    return Path(__file__).parent.parent.resolve()


def get_index_path(memex: Path) -> Path:
    """Get path to SQLite index file."""
    return memex / "_index.sqlite"


def init_database(conn: sqlite3.Connection):
    """Initialize FTS5 tables if they don't exist."""
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


def index_file(conn: sqlite3.Connection, file_path: Path, memex: Path):
    """Index a single file."""
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


def rebuild_index(memex: Path) -> int:
    """Rebuild the entire search index."""
    index_path = get_index_path(memex)

    # Remove old index
    if index_path.exists():
        index_path.unlink()

    conn = sqlite3.connect(index_path)
    init_database(conn)

    count = 0

    # Index all markdown files
    for pattern in ["projects/**/*.md", "topics/*.md"]:
        for file_path in memex.glob(pattern):
            # Skip templates and special files
            if file_path.name.startswith("_"):
                continue
            if "/_templates/" in str(file_path):
                continue

            index_file(conn, file_path, memex)
            count += 1

    conn.commit()
    conn.close()

    return count


def search(
    memex: Path,
    query: str,
    file_type: str | None = None,
    project: str | None = None,
    limit: int = 20
) -> list[dict]:
    """Search the index and return matching documents."""
    index_path = get_index_path(memex)

    if not index_path.exists():
        # Build index if it doesn't exist
        rebuild_index(memex)

    conn = sqlite3.connect(index_path)
    init_database(conn)

    # Build query
    sql = """
        SELECT path, title, type, project,
               snippet(fts_content, 2, '>>>', '<<<', '...', 50) as snippet
        FROM fts_content
        WHERE fts_content MATCH ?
    """
    params = [query]

    if file_type:
        sql += " AND type = ?"
        params.append(file_type)

    if project:
        sql += " AND project = ?"
        params.append(project)

    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)

    try:
        cursor = conn.execute(sql, params)
        results = []

        for row in cursor:
            results.append({
                "path": row[0],
                "title": row[1],
                "type": row[2],
                "project": row[3],
                "snippet": row[4],
            })

        return results

    except sqlite3.OperationalError as e:
        # Handle FTS query errors gracefully
        if "fts5" in str(e).lower():
            # Try simpler LIKE query as fallback
            return search_fallback(conn, query, file_type, project, limit)
        raise

    finally:
        conn.close()


def escape_like_pattern(query: str) -> str:
    """Escape LIKE special characters to prevent pattern injection."""
    return query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_fallback(
    conn: sqlite3.Connection,
    query: str,
    file_type: str | None,
    project: str | None,
    limit: int
) -> list[dict]:
    """Fallback search using LIKE when FTS fails."""
    escaped_query = escape_like_pattern(query)
    sql = """
        SELECT path, title, type, project,
               substr(content, 1, 200) as snippet
        FROM fts_content
        WHERE content LIKE ? ESCAPE '\\'
    """
    params: list = [f"%{escaped_query}%"]

    if file_type:
        sql += " AND type = ?"
        params.append(file_type)

    if project:
        sql += " AND project = ?"
        params.append(project)

    sql += " LIMIT ?"
    params.append(limit)

    cursor = conn.execute(sql, params)
    results = []

    for row in cursor:
        results.append({
            "path": row[0],
            "title": row[1],
            "type": row[2],
            "project": row[3],
            "snippet": row[4][:200] + "..." if len(row[4]) > 200 else row[4],
        })

    return results


def format_results(results: list[dict], output_format: str = "json") -> str:
    """Format search results for output."""
    if output_format == "json":
        return json.dumps(results, indent=2)

    # Human-readable format
    if not results:
        return "No results found."

    lines = [f"Found {len(results)} result(s):\n"]

    # Group by type
    by_type = {}
    for r in results:
        t = r["type"]
        by_type.setdefault(t, []).append(r)

    type_emoji = {
        "memo": "📝",
        "transcript": "📜",
        "concept": "💡",
        "note": "📄",
    }

    for file_type, items in by_type.items():
        emoji = type_emoji.get(file_type, "📄")
        lines.append(f"{emoji} {file_type.title()}s:")

        for i, item in enumerate(items, 1):
            lines.append(f"  {i}. **{item['title']}**")
            if item["project"]:
                lines.append(f"     Project: {item['project']}")
            if item["snippet"]:
                # Clean up snippet
                snippet = item["snippet"].replace(">>>", "**").replace("<<<", "**")
                snippet = re.sub(r'\s+', ' ', snippet).strip()
                lines.append(f"     {snippet[:150]}...")
            lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Search the memex vault")
    parser.add_argument("query", nargs="?", help="Search query")
    parser.add_argument("--type", dest="file_type", help="Filter by type (memo, transcript, concept)")
    parser.add_argument("--project", help="Filter by project")
    parser.add_argument("--limit", type=int, default=20, help="Max results")
    parser.add_argument("--rebuild", action="store_true", help="Rebuild the search index")
    parser.add_argument("--with-embeddings", action="store_true", help="Include embeddings in rebuild")
    parser.add_argument("--status", action="store_true", help="Show index statistics")
    parser.add_argument("--format", choices=["json", "text"], default="json", help="Output format")

    # Hybrid search options
    parser.add_argument("--mode", choices=["hybrid", "fts", "vector"], default=None,
                        help="Search mode (default: hybrid if embeddings available, else fts)")
    parser.add_argument("--scoring", choices=["rrf", "linear"], default="rrf",
                        help="Scoring algorithm (default: rrf)")
    parser.add_argument("--weights", type=str, help="Vector,BM25 weights for linear scoring (e.g., 0.7,0.3)")
    parser.add_argument("--since", type=str, help="Filter to docs newer than (e.g., 7d, 2w, 3m)")

    args = parser.parse_args()

    memex = get_memex_path()

    # Status command
    if args.status:
        try:
            from index_rebuild import get_index_status, format_status
            stats = get_index_status(memex)
            if args.format == "json":
                print(json.dumps(stats, indent=2))
            else:
                print(format_status(stats))
        except ImportError:
            # Fallback: basic status
            index_path = get_index_path(memex)
            if index_path.exists():
                size_kb = index_path.stat().st_size / 1024
                print(f"Index exists: {index_path} ({size_kb:.1f} KB)")
            else:
                print("Index does not exist")
        sys.exit(0)

    # Rebuild command
    if args.rebuild:
        if args.with_embeddings:
            try:
                from index_rebuild import rebuild_full, format_rebuild_stats
                print("Rebuilding with embeddings...")
                stats = rebuild_full(memex, with_embeddings=True, atomic=True)
                print(format_rebuild_stats(stats))
            except ImportError as e:
                print(f"Embedding rebuild requires additional dependencies: {e}", file=sys.stderr)
                print("Falling back to FTS-only rebuild...")
                count = rebuild_index(memex)
                print(f"Rebuilt FTS index with {count} documents")
        else:
            count = rebuild_index(memex)
            print(f"Rebuilt FTS index with {count} documents")
        sys.exit(0)

    if not args.query:
        parser.print_help()
        sys.exit(1)

    # Parse weights
    vector_weight = None
    bm25_weight = None
    if args.weights:
        try:
            parts = args.weights.split(",")
            vector_weight = float(parts[0])
            bm25_weight = float(parts[1])
        except (ValueError, IndexError):
            print("Invalid weights format. Use: 0.7,0.3", file=sys.stderr)
            sys.exit(1)

    # Determine search mode
    mode = args.mode

    # Try hybrid search if mode is hybrid or vector, or if no mode specified
    if mode in ("hybrid", "vector") or (mode is None and args.weights):
        try:
            from hybrid_search import hybrid_search as do_hybrid_search, format_results as format_hybrid_results
            from embeddings import EmbeddingPipeline

            index_path = get_index_path(memex)
            if not index_path.exists():
                rebuild_index(memex)

            conn = sqlite3.connect(index_path)
            try:
                pipeline = EmbeddingPipeline()

                if mode == "vector" and not pipeline.enabled:
                    print("Vector search requires embeddings. Set GEMINI_API_KEY.", file=sys.stderr)
                    sys.exit(1)

                # Use hybrid if embeddings available, else fallback
                actual_mode = mode or ("hybrid" if pipeline.enabled else "fts")

                results = do_hybrid_search(
                    conn=conn,
                    query=args.query,
                    pipeline=pipeline if pipeline.enabled else None,
                    mode=actual_mode,
                    scoring=args.scoring,
                    vector_weight=vector_weight,
                    bm25_weight=bm25_weight,
                    file_type=args.file_type,
                    project=args.project,
                    since=args.since,
                    limit=args.limit
                )

                print(format_hybrid_results(results, args.format))
                sys.exit(0)
            finally:
                conn.close()

        except ImportError:
            # Hybrid modules not available, fall back to FTS
            if mode == "vector":
                print("Vector search not available", file=sys.stderr)
                sys.exit(1)
            mode = "fts"

    # Standard FTS search
    results = search(
        memex,
        args.query,
        file_type=args.file_type,
        project=args.project,
        limit=args.limit
    )

    print(format_results(results, args.format))


if __name__ == "__main__":
    main()
