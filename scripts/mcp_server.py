#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "mcp>=1.0.0",
#     "google-genai>=1.0.0",
#     "sqlite-vec>=0.1.6",
#     "tiktoken>=0.5",
#     "filelock>=3.0",
# ]
# ///
"""
Memex MCP Server

Exposes memex knowledge base search to other Claude instances via MCP.

Tools:
- memex_search: Search memos, transcripts, concepts
- memex_status: Vault stats and index info
- memex_read: Read specific document by path
- memex_graph: Navigate knowledge graph (backlinks, tasks, etc.)
"""

import asyncio
import json
import logging
import sqlite3
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.server.models import InitializationOptions
from mcp.types import (
    ServerCapabilities,
    TextContent,
    Tool,
    ToolsCapability,
)

# Add scripts dir to path for sibling imports
sys.path.insert(0, str(Path(__file__).parent))

from hybrid_search import hybrid_search as do_hybrid_search
from embeddings import EmbeddingPipeline
from graph_queries import (
    get_backlinks,
    get_outlinks,
    get_broken_links,
    get_open_tasks,
    get_all_tags,
    get_docs_by_tag,
    get_recent_activity,
    get_orphan_docs,
    get_graph_stats,
)
from index_rebuild import get_index_status
from utils import get_memex_path

logger = logging.getLogger("memex-mcp")

# ============================================================================
# State Management
# ============================================================================

_memex_path: Path | None = None
_conn: sqlite3.Connection | None = None
_pipeline: EmbeddingPipeline | None = None


def get_memex() -> Path:
    """Resolve memex vault path (cached)."""
    global _memex_path
    if _memex_path is None:
        _memex_path = get_memex_path()
    return _memex_path


def get_conn() -> sqlite3.Connection:
    """Get or create SQLite connection (cached)."""
    global _conn
    if _conn is None:
        index_path = get_memex() / "_index.sqlite"
        if not index_path.exists():
            raise FileNotFoundError(
                f"Index not found at {index_path}. "
                f"Run: uv run scripts/index_rebuild.py --incremental"
            )

        _conn = sqlite3.connect(str(index_path))

        # Load sqlite-vec extension
        try:
            import sqlite_vec
            _conn.enable_load_extension(True)
            sqlite_vec.load(_conn)
            _conn.enable_load_extension(False)
        except Exception as e:
            logger.warning(f"sqlite-vec not loaded: {e}")

    return _conn


def get_pipeline() -> EmbeddingPipeline | None:
    """Get or create embedding pipeline (cached)."""
    global _pipeline
    if _pipeline is None:
        _pipeline = EmbeddingPipeline()
        if not _pipeline.enabled:
            _pipeline = None
    return _pipeline


# ============================================================================
# MCP Server
# ============================================================================

server = Server("memex")


@server.list_tools()
async def handle_list_tools() -> list[Tool]:
    """List available memex tools."""
    return [
        Tool(
            name="memex_search",
            description=(
                "Search the memex knowledge base using hybrid search (BM25 + semantic). "
                "Use keywords joined by OR for best results (e.g., 'JWT OR authentication'). "
                "Returns memos, transcripts, concepts, and notes with relevance scores."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query. Use keywords joined by OR (e.g., 'JWT OR auth'), not full questions."
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["hybrid", "fts", "vector"],
                        "default": "hybrid",
                        "description": "Search mode: 'hybrid' (keyword+semantic), 'fts' (keyword only), 'vector' (semantic only)"
                    },
                    "type": {
                        "type": "string",
                        "enum": ["memo", "transcript", "concept", "note"],
                        "description": "Filter by document type"
                    },
                    "project": {
                        "type": "string",
                        "description": "Filter by project name"
                    },
                    "since": {
                        "type": "string",
                        "description": "Recency filter: '7d' (7 days), '2w' (2 weeks), '3m' (3 months)"
                    },
                    "limit": {
                        "type": "integer",
                        "default": 10,
                        "description": "Maximum number of results to return"
                    }
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="memex_status",
            description="Get memex vault statistics, index info, and graph stats.",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        Tool(
            name="memex_read",
            description="Read a specific document from the vault by its relative path.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path within vault (e.g., 'topics/hooks.md' or 'projects/memex/memos/file.md')"
                    }
                },
                "required": ["path"]
            }
        ),
        Tool(
            name="memex_graph",
            description="Navigate the knowledge graph: backlinks, tasks, broken links, tags, recent activity, orphans.",
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["backlinks", "outlinks", "tasks", "broken", "tags", "recent", "orphans", "stats"],
                        "description": "Graph query type"
                    },
                    "path": {
                        "type": "string",
                        "description": "Document path (required for 'backlinks' and 'outlinks')"
                    },
                    "project": {
                        "type": "string",
                        "description": "Filter by project (for 'tasks', 'broken', 'recent')"
                    },
                    "tag": {
                        "type": "string",
                        "description": "Tag to search (for 'tags' action)"
                    },
                    "days": {
                        "type": "integer",
                        "default": 7,
                        "description": "Lookback period for 'recent' and 'tasks'"
                    }
                },
                "required": ["action"]
            }
        ),
    ]


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Handle tool invocation."""
    try:
        if name == "memex_search":
            return await handle_search(arguments)
        elif name == "memex_status":
            return await handle_status(arguments)
        elif name == "memex_read":
            return await handle_read(arguments)
        elif name == "memex_graph":
            return await handle_graph(arguments)
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    except Exception as e:
        error_msg = f"Error in {name}: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return [TextContent(type="text", text=error_msg)]


# ============================================================================
# Tool Handlers
# ============================================================================

async def handle_search(args: dict) -> list[TextContent]:
    """Handle memex_search tool."""
    conn = get_conn()
    pipeline = get_pipeline()

    mode = args.get("mode", "hybrid")
    if mode in ("hybrid", "vector") and pipeline is None:
        mode = "fts"  # Graceful fallback

    results = do_hybrid_search(
        conn=conn,
        query=args["query"],
        pipeline=pipeline,
        mode=mode,
        file_type=args.get("type"),
        project=args.get("project"),
        since=args.get("since"),
        limit=args.get("limit", 10)
    )

    # Format as JSON
    output = [
        {
            "path": r.path,
            "title": r.title,
            "type": r.doc_type,
            "project": r.project,
            "snippet": r.snippet,
            "score": round(r.score, 4),
            "match_type": r.match_type,
        }
        for r in results
    ]

    return [TextContent(type="text", text=json.dumps(output, indent=2))]


async def handle_status(args: dict) -> list[TextContent]:
    """Handle memex_status tool."""
    memex = get_memex()
    index_path = memex / "_index.sqlite"

    # Get index stats
    stats = get_index_status(memex)
    stats["vault_path"] = str(memex)

    # Add graph stats if index exists
    if index_path.exists():
        stats["graph"] = get_graph_stats(index_path)

    return [TextContent(type="text", text=json.dumps(stats, indent=2))]


async def handle_read(args: dict) -> list[TextContent]:
    """Handle memex_read tool."""
    memex = get_memex()
    rel_path = args["path"]

    # Validate path (prevent traversal)
    try:
        full_path = (memex / rel_path).resolve()
        full_path.relative_to(memex.resolve())
    except ValueError:
        return [TextContent(type="text", text=f"Invalid path (traversal detected): {rel_path}")]

    if not full_path.exists():
        return [TextContent(type="text", text=f"File not found: {rel_path}")]

    if not full_path.is_file():
        return [TextContent(type="text", text=f"Not a file: {rel_path}")]

    content = full_path.read_text()
    return [TextContent(type="text", text=content)]


async def handle_graph(args: dict) -> list[TextContent]:
    """Handle memex_graph tool."""
    db_path = get_memex() / "_index.sqlite"
    action = args["action"]

    if action == "backlinks":
        if "path" not in args:
            return [TextContent(type="text", text="Error: 'backlinks' requires 'path' parameter")]
        links = get_backlinks(db_path, args["path"])
        output = {"action": "backlinks", "path": args["path"], "links": links}

    elif action == "outlinks":
        if "path" not in args:
            return [TextContent(type="text", text="Error: 'outlinks' requires 'path' parameter")]
        links = get_outlinks(db_path, args["path"])
        output = {"action": "outlinks", "path": args["path"], "links": links}

    elif action == "tasks":
        project = args.get("project")
        days = args.get("days", 14)
        tasks = get_open_tasks(db_path, project_filter=project, days_back=days)
        output = {"action": "tasks", "project": project, "days": days, "tasks": tasks}

    elif action == "broken":
        project = args.get("project")
        broken = get_broken_links(db_path, project_filter=project)
        output = {"action": "broken", "project": project, "broken_links": broken}

    elif action == "tags":
        if "tag" in args:
            docs = get_docs_by_tag(db_path, args["tag"])
            output = {"action": "tags", "tag": args["tag"], "documents": docs}
        else:
            tags = get_all_tags(db_path)
            output = {"action": "tags", "all_tags": tags}

    elif action == "recent":
        days = args.get("days", 7)
        recent = get_recent_activity(db_path, days=days)
        output = {"action": "recent", "days": days, "recent": recent}

    elif action == "orphans":
        orphans = get_orphan_docs(db_path)
        output = {"action": "orphans", "orphans": orphans}

    elif action == "stats":
        stats = get_graph_stats(db_path)
        output = {"action": "stats", "graph_stats": stats}

    else:
        return [TextContent(type="text", text=f"Unknown graph action: {action}")]

    return [TextContent(type="text", text=json.dumps(output, indent=2))]


# ============================================================================
# Entry Point
# ============================================================================

async def main():
    """Run the MCP server."""
    logging.basicConfig(level=logging.INFO)

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="memex",
                server_version="0.3.0",
                instructions=(
                    "A wisdom garden — knowledge cultivated by AI across sessions. "
                    "Use memex_search with keywords joined by OR (not full questions). "
                    "Use memex_read to read documents. "
                    "Use memex_graph to navigate connections (backlinks, outlinks, orphans). "
                    "Project overviews live at projects/<name>/_project.md. "
                    "Cross-project concepts live in topics/."
                ),
                capabilities=ServerCapabilities(tools=ToolsCapability()),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
