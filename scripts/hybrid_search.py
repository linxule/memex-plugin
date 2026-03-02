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
Claude Memory Plugin - Hybrid Search

Combines FTS5 (BM25) keyword search with sqlite-vec vector similarity search.
Default weighting: 70% vector, 30% BM25 (configurable).

Usage:
    hybrid_search.py <query> [--mode=hybrid|fts|vector] [--weights=0.7,0.3]
"""

import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

# Import from sibling module
from embeddings import (
    EmbeddingPipeline,
    init_embedding_schema,
    get_embedding_config,
    serialize_f32,
)


# ============================================================================
# Configuration
# ============================================================================

DEFAULT_SEARCH_CONFIG = {
    "default_mode": "hybrid",
    "vector_weight": 0.7,
    "bm25_weight": 0.3,
}

# Stop words to filter from FTS queries (improves recall for natural language questions)
STOP_WORDS = {
    # Question words
    "why", "what", "how", "when", "where", "who", "which",
    # Common verbs
    "did", "do", "does", "is", "are", "was", "were", "have", "has", "had",
    "can", "could", "would", "should", "will", "shall", "be", "been", "being",
    "get", "got", "make", "made", "go", "went",
    # Pronouns
    "we", "you", "i", "they", "he", "she", "it", "me", "us", "them",
    # Articles and prepositions
    "the", "a", "an", "of", "to", "for", "with", "on", "at", "by", "from",
    "in", "out", "about", "into", "through", "during", "before", "after",
    # Common words
    "that", "this", "these", "those", "there", "here",
}


def get_search_config() -> dict:
    """Load search configuration."""
    config = DEFAULT_SEARCH_CONFIG.copy()

    config_path = Path("~/.memex/config.json").expanduser()
    if config_path.exists():
        try:
            full_config = json.loads(config_path.read_text())
            if "search" in full_config:
                config.update(full_config["search"])
        except json.JSONDecodeError:
            pass

    return config


# ============================================================================
# Data Types
# ============================================================================

@dataclass
class SearchResult:
    """A search result with combined scoring."""
    path: str
    title: str
    doc_type: str
    project: str
    snippet: str
    score: float
    match_type: str  # "hybrid", "fts_only", "vector_only"
    bm25_score: float = 0.0
    vector_score: float = 0.0


@dataclass
class FTSResult:
    """Raw FTS5 search result."""
    path: str
    title: str
    doc_type: str
    project: str
    snippet: str
    bm25_score: float  # BM25 returns negative scores (more negative = better match)


@dataclass
class VectorResult:
    """Raw vector search result."""
    chunk_id: int
    doc_path: str
    content: str
    distance: float  # Cosine distance (0 = identical, 2 = opposite)


# ============================================================================
# Query Processing
# ============================================================================

def extract_fts_keywords(query: str, use_or: bool = True) -> str:
    """
    Extract keywords from natural language query for FTS5.

    Removes stop words and short words, then joins with OR for better recall.
    "why did we choose hybrid search?" -> "hybrid OR search"

    Args:
        query: Natural language query
        use_or: If True, join with OR; if False, join with spaces (AND)

    Returns:
        FTS5-compatible query string
    """
    # Remove punctuation and lowercase
    cleaned = re.sub(r'[^\w\s]', ' ', query.lower())

    # Split and filter
    words = cleaned.split()
    keywords = [w for w in words if w not in STOP_WORDS and len(w) > 2]

    if not keywords:
        # Fallback: use original words longer than 2 chars
        keywords = [w for w in words if len(w) > 2][:5]

    if not keywords:
        return query  # Last resort

    # Limit to 7 keywords to avoid overly broad queries
    keywords = keywords[:7]

    if use_or:
        return " OR ".join(keywords)
    return " ".join(keywords)


# ============================================================================
# FTS5 Search (BM25)
# ============================================================================

def fts5_search(
    conn: sqlite3.Connection,
    query: str,
    file_type: str | None = None,
    project: str | None = None,
    since_cutoff: datetime | None = None,
    limit: int = 50
) -> list[FTSResult]:
    """
    BM25 keyword search via FTS5.

    Returns results sorted by relevance (lower BM25 score = better match).
    """
    sql = """
        SELECT path, title, type, project,
               snippet(fts_content, 2, '>>>', '<<<', '...', 50) as snippet,
               bm25(fts_content) as score
        FROM fts_content
        WHERE fts_content MATCH ?
    """
    params: list = [query]

    if file_type:
        sql += " AND type = ?"
        params.append(file_type)

    if project:
        sql += " AND project = ?"
        params.append(project)

    if since_cutoff:
        sql += " AND date >= ?"
        params.append(since_cutoff.strftime("%Y-%m-%d"))

    sql += " ORDER BY score LIMIT ?"
    params.append(limit)

    try:
        cursor = conn.execute(sql, params)
        return [
            FTSResult(
                path=row[0],
                title=row[1],
                doc_type=row[2],
                project=row[3],
                snippet=row[4],
                bm25_score=row[5]
            )
            for row in cursor
        ]
    except sqlite3.OperationalError as e:
        # FTS query syntax error - try fallback
        if "fts5" in str(e).lower() or "syntax" in str(e).lower():
            return fts5_search_fallback(conn, query, file_type, project, limit)
        raise


def escape_like_pattern(query: str) -> str:
    """Escape LIKE special characters to prevent pattern injection."""
    return query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def fts5_search_fallback(
    conn: sqlite3.Connection,
    query: str,
    file_type: str | None,
    project: str | None,
    limit: int
) -> list[FTSResult]:
    """Fallback search using LIKE when FTS query fails."""
    escaped_query = escape_like_pattern(query)
    sql = """
        SELECT path, title, type, project,
               substr(content, 1, 200) as snippet,
               -1.0 as score
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
    return [
        FTSResult(
            path=row[0],
            title=row[1],
            doc_type=row[2],
            project=row[3],
            snippet=row[4][:200] + "..." if len(row[4]) > 200 else row[4],
            bm25_score=row[5]
        )
        for row in cursor
    ]


# ============================================================================
# Vector Search
# ============================================================================

def vector_search(
    conn: sqlite3.Connection,
    query_embedding: bytes,
    limit: int = 50
) -> list[VectorResult]:
    """
    KNN vector search via sqlite-vec.

    Returns results sorted by distance (lower = more similar).
    """
    try:
        # Load sqlite-vec if not already loaded
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except Exception as e:
        print(f"Warning: sqlite-vec not available, vector search disabled: {e}", file=sys.stderr)
        return []

    try:
        # sqlite-vec requires k=N syntax for KNN queries
        cursor = conn.execute("""
            SELECT v.rowid, c.doc_path, c.content, v.distance
            FROM vec_chunks v
            JOIN chunks c ON c.id = v.rowid
            WHERE v.embedding MATCH ?
            AND k = ?
        """, (query_embedding, limit))

        return [
            VectorResult(
                chunk_id=row[0],
                doc_path=row[1],
                content=row[2],
                distance=row[3]
            )
            for row in cursor
        ]
    except sqlite3.OperationalError as e:
        # vec_chunks table doesn't exist or other error
        print(f"Vector search error: {e}", file=sys.stderr)
        return []


# ============================================================================
# Hybrid Search
# ============================================================================

def normalize_bm25_scores(results: list[FTSResult]) -> dict[str, float]:
    """
    Normalize BM25 scores to [0, 1] range.

    BM25 returns negative scores where more negative = better match.
    We invert and scale to get 1.0 = best match, 0.0 = worst match.
    """
    if not results:
        return {}

    scores = [r.bm25_score for r in results]
    min_score = min(scores)  # Most negative = best
    max_score = max(scores)  # Least negative = worst

    range_score = max_score - min_score

    # Fix: If scores are too similar (edge case), return neutral score
    # Otherwise all results get 1.0 which doesn't help ranking
    if range_score < 0.1:
        return {r.path: 0.5 for r in results}

    # Invert: best match (most negative) becomes 1.0
    return {
        r.path: (max_score - r.bm25_score) / range_score
        for r in results
    }


def reciprocal_rank_fusion(
    fts_results: list[FTSResult],
    vec_results: list[VectorResult],
    k: int = 60
) -> dict[str, dict]:
    """
    Compute RRF scores for results from both retrieval systems.

    RRF formula: score = sum(1 / (k + rank)) across all systems
    k=60 is the industry standard (used by Azure AI Search, OpenSearch).
    No weight tuning needed - handles edge cases gracefully.

    Args:
        fts_results: BM25 search results
        vec_results: Vector similarity results
        k: Ranking constant (default 60)

    Returns:
        Dict mapping doc path -> {rrf_score, bm25_present, vector_present, fts_data, vec_snippet}
    """
    doc_scores: dict[str, dict] = {}

    # FTS ranks (lower BM25 = better, so sort ascending by score)
    fts_ranked = sorted(fts_results, key=lambda r: r.bm25_score)
    for rank, result in enumerate(fts_ranked):
        if result.path not in doc_scores:
            doc_scores[result.path] = {
                "rrf_score": 0.0,
                "bm25_present": False,
                "vector_present": False,
                "fts_data": None,
                "vec_snippet": None
            }
        doc_scores[result.path]["rrf_score"] += 1.0 / (k + rank + 1)
        doc_scores[result.path]["bm25_present"] = True
        doc_scores[result.path]["fts_data"] = result

    # Vector ranks - dedupe by document first (keep best chunk per doc)
    vec_by_doc: dict[str, VectorResult] = {}
    for vr in vec_results:
        if vr.doc_path not in vec_by_doc or vr.distance < vec_by_doc[vr.doc_path].distance:
            vec_by_doc[vr.doc_path] = vr

    vec_ranked = sorted(vec_by_doc.values(), key=lambda r: r.distance)
    for rank, result in enumerate(vec_ranked):
        if result.doc_path not in doc_scores:
            doc_scores[result.doc_path] = {
                "rrf_score": 0.0,
                "bm25_present": False,
                "vector_present": False,
                "fts_data": None,
                "vec_snippet": None,
            }
        doc_scores[result.doc_path]["rrf_score"] += 1.0 / (k + rank + 1)
        doc_scores[result.doc_path]["vector_present"] = True
        doc_scores[result.doc_path]["vec_snippet"] = extract_snippet(result.content)

    return doc_scores


def apply_result_diversity(
    results: list["SearchResult"],
    max_per_doc: int = 3,
    limit: int = 20
) -> list["SearchResult"]:
    """
    Limit results per document to ensure diversity.

    Prevents a single long session from dominating results with many similar chunks.

    Args:
        results: Sorted search results
        max_per_doc: Maximum chunks per document
        limit: Total results to return

    Returns:
        Filtered list with diversity applied
    """
    doc_counts: dict[str, int] = {}
    diverse_results = []

    for result in results:
        # Extract document path (remove chunk suffix if any)
        doc_path = result.path.split("#")[0]

        count = doc_counts.get(doc_path, 0)
        if count < max_per_doc:
            diverse_results.append(result)
            doc_counts[doc_path] = count + 1

        if len(diverse_results) >= limit:
            break

    return diverse_results


def parse_since_duration(since: str) -> datetime | None:
    """
    Parse duration string to cutoff datetime.

    Supports: 7d (days), 2w (weeks), 3m (months)

    Args:
        since: Duration string like "30d", "2w", "6m"

    Returns:
        Cutoff datetime or None if invalid
    """
    if not since:
        return None

    match = re.match(r'^(\d+)([dwm])$', since.lower())
    if not match:
        return None

    num, unit = int(match.group(1)), match.group(2)
    days = {"d": 1, "w": 7, "m": 30}[unit] * num
    return datetime.now() - timedelta(days=days)


def distance_to_similarity(distance: float) -> float:
    """
    Convert cosine distance to similarity score.

    Cosine distance range: [0, 2]
    - 0 = identical vectors
    - 1 = orthogonal
    - 2 = opposite vectors

    Returns: similarity in [0, 1] where 1 = most similar
    """
    return 1.0 - (distance / 2.0)


def extract_snippet(content: str, max_length: int = 200) -> str:
    """Extract a snippet from chunk content."""
    # Skip frontmatter if present
    if content.startswith("---"):
        try:
            end = content.index("---", 3) + 3
            content = content[end:].strip()
        except ValueError:
            pass

    # Clean and truncate
    snippet = " ".join(content.split())[:max_length]
    if len(content) > max_length:
        snippet += "..."
    return snippet


def get_doc_metadata(conn: sqlite3.Connection, path: str) -> dict:
    """Get document metadata from FTS content table."""
    cursor = conn.execute(
        "SELECT title, type, project FROM fts_content WHERE path = ?",
        (path,)
    )
    row = cursor.fetchone()
    if row:
        return {"title": row[0], "type": row[1], "project": row[2]}
    return {"title": Path(path).stem, "type": "unknown", "project": ""}


def get_doc_date(conn: sqlite3.Connection, path: str) -> datetime | None:
    """Get document date from FTS content table."""
    cursor = conn.execute(
        "SELECT date FROM fts_content WHERE path = ?",
        (path,)
    )
    row = cursor.fetchone()
    if row and row[0]:
        try:
            return datetime.strptime(row[0], "%Y-%m-%d")
        except ValueError:
            return None
    return None


def _linear_score_fusion(
    fts_results: list[FTSResult],
    vec_results: list[VectorResult]
) -> dict[str, dict]:
    """
    Linear weighted score fusion (original approach).

    Used when scoring="linear" is explicitly requested.
    """
    doc_scores: dict[str, dict] = {}
    normalized_bm25 = normalize_bm25_scores(fts_results)

    for r in fts_results:
        if r.path not in doc_scores:
            doc_scores[r.path] = {
                "bm25": 0.0,
                "vector": 0.0,
                "bm25_present": False,
                "vector_present": False,
                "fts_data": None,
                "vec_snippet": None
            }
        doc_scores[r.path]["bm25"] = normalized_bm25.get(r.path, 0.0)
        doc_scores[r.path]["bm25_present"] = True
        doc_scores[r.path]["fts_data"] = r

    # Deduplicate vector results by doc
    doc_best_vectors: dict[str, VectorResult] = {}
    for vr in vec_results:
        if vr.doc_path not in doc_best_vectors:
            doc_best_vectors[vr.doc_path] = vr
        elif vr.distance < doc_best_vectors[vr.doc_path].distance:
            doc_best_vectors[vr.doc_path] = vr

    for doc_path, vr in doc_best_vectors.items():
        if doc_path not in doc_scores:
            doc_scores[doc_path] = {
                "bm25": 0.0,
                "vector": 0.0,
                "bm25_present": False,
                "vector_present": False,
                "fts_data": None,
                "vec_snippet": None
            }
        doc_scores[doc_path]["vector"] = distance_to_similarity(vr.distance)
        doc_scores[doc_path]["vector_present"] = True
        doc_scores[doc_path]["vec_snippet"] = extract_snippet(vr.content)

    return doc_scores


def hybrid_search(
    conn: sqlite3.Connection,
    query: str,
    pipeline: EmbeddingPipeline | None = None,
    mode: Literal["hybrid", "fts", "vector"] = "hybrid",
    scoring: Literal["rrf", "linear"] = "rrf",
    vector_weight: float | None = None,
    bm25_weight: float | None = None,
    file_type: str | None = None,
    project: str | None = None,
    since: str | None = None,
    limit: int = 20
) -> list[SearchResult]:
    """
    Hybrid search combining FTS5 (BM25) and vector similarity.

    Args:
        conn: SQLite connection with FTS5 and vec tables
        query: Search query string
        pipeline: EmbeddingPipeline for vector search (optional)
        mode: Search mode - "hybrid", "fts", or "vector"
        scoring: Scoring algorithm - "rrf" (default, industry standard) or "linear"
        vector_weight: Weight for vector scores (linear mode only)
        bm25_weight: Weight for BM25 scores (linear mode only)
        file_type: Filter by document type
        project: Filter by project
        since: Recency filter - "7d", "2w", "3m" etc.
        limit: Maximum results to return

    Returns:
        List of SearchResult objects sorted by combined score
    """
    config = get_search_config()
    vector_weight = vector_weight if vector_weight is not None else config["vector_weight"]
    bm25_weight = bm25_weight if bm25_weight is not None else config["bm25_weight"]

    # Parse since filter
    since_cutoff = parse_since_duration(since) if since else None

    # Candidate multiplier - fetch more than limit for merging
    candidate_limit = limit * 3

    # Gather raw results from both systems
    fts_results: list[FTSResult] = []
    vec_results: list[VectorResult] = []

    # 1. FTS5 search (BM25)
    if mode in ("hybrid", "fts"):
        fts_query = extract_fts_keywords(query, use_or=True)
        fts_results = fts5_search(conn, fts_query, file_type, project, since_cutoff, candidate_limit)

    # 2. Vector search (if enabled)
    embeddings_available = pipeline and pipeline.enabled and mode in ("hybrid", "vector")

    if embeddings_available:
        query_embedding = pipeline.embed_query(query)

        if query_embedding:
            vec_results = vector_search(conn, query_embedding, candidate_limit)

            # Apply filters to vector results (FTS already filtered)
            if file_type or project or since_cutoff:
                filtered_vec = []
                for vr in vec_results:
                    meta = get_doc_metadata(conn, vr.doc_path)
                    if file_type and meta.get("type") != file_type:
                        continue
                    if project and meta.get("project") != project:
                        continue
                    # Date filter for vector results (exclude docs with unknown dates)
                    if since_cutoff:
                        doc_date = get_doc_date(conn, vr.doc_path)
                        if doc_date is None or doc_date < since_cutoff:
                            continue
                    filtered_vec.append(vr)
                vec_results = filtered_vec

    # 3. Score fusion
    if scoring == "rrf":
        # RRF scoring - industry standard (Azure, OpenSearch)
        doc_scores = reciprocal_rank_fusion(fts_results, vec_results, k=60)
    else:
        # Linear weighted scoring (original approach)
        doc_scores = _linear_score_fusion(fts_results, vec_results)

    # 4. Build results from scored documents
    results = []

    for path, scores in doc_scores.items():
        # Determine match type using presence flags
        has_bm25 = scores.get("bm25_present", False)
        has_vector = scores.get("vector_present", False)

        if has_bm25 and has_vector:
            match_type = "hybrid"
        elif has_vector:
            match_type = "vector_only"
        else:
            match_type = "fts_only"

        # Calculate final score based on scoring method
        if scoring == "rrf":
            final_score = scores.get("rrf_score", 0.0)
            # For display purposes, also track component scores
            bm25_display = scores.get("bm25", 0.0) if "bm25" in scores else (0.5 if has_bm25 else 0.0)
            vector_display = scores.get("vector", 0.0) if "vector" in scores else (0.5 if has_vector else 0.0)
        else:
            # Linear weighted scoring
            if has_bm25 and has_vector:
                final_score = (vector_weight * scores["vector"]) + (bm25_weight * scores["bm25"])
            elif has_vector:
                final_score = scores["vector"]
            else:
                final_score = scores["bm25"]
            bm25_display = scores.get("bm25", 0.0)
            vector_display = scores.get("vector", 0.0)

        # Get metadata
        if scores.get("fts_data"):
            fts = scores["fts_data"]
            title = fts.title
            doc_type = fts.doc_type
            proj = fts.project
            snippet = fts.snippet
        else:
            meta = get_doc_metadata(conn, path)
            title = meta["title"]
            doc_type = meta["type"]
            proj = meta["project"]
            snippet = scores.get("vec_snippet", "")

        results.append(SearchResult(
            path=path,
            title=title,
            doc_type=doc_type,
            project=proj,
            snippet=snippet,
            score=final_score,
            match_type=match_type,
            bm25_score=bm25_display,
            vector_score=vector_display
        ))

    # 5. Sort by score and apply diversity
    results.sort(key=lambda r: r.score, reverse=True)
    results = apply_result_diversity(results, max_per_doc=3, limit=limit)

    return results


# ============================================================================
# CLI
# ============================================================================

def format_results(results: list[SearchResult], output_format: str = "json") -> str:
    """Format search results for output."""
    if output_format == "json":
        return json.dumps([
            {
                "path": r.path,
                "title": r.title,
                "type": r.doc_type,
                "project": r.project,
                "snippet": r.snippet,
                "score": round(r.score, 4),
                "match_type": r.match_type,
                "bm25_score": round(r.bm25_score, 4),
                "vector_score": round(r.vector_score, 4),
            }
            for r in results
        ], indent=2)

    # Human-readable format
    if not results:
        return "No results found."

    lines = [f"Found {len(results)} result(s):\n"]

    type_emoji = {
        "memo": "\U0001F4DD",  # memo
        "transcript": "\U0001F4DC",  # scroll
        "concept": "\U0001F4A1",  # lightbulb
        "note": "\U0001F4C4",  # page
    }

    for i, r in enumerate(results, 1):
        emoji = type_emoji.get(r.doc_type, "\U0001F4C4")
        match_badge = f"[{r.match_type}]"

        lines.append(f"{i}. {emoji} **{r.title}** {match_badge}")
        lines.append(f"   Score: {r.score:.3f} (BM25: {r.bm25_score:.3f}, Vec: {r.vector_score:.3f})")

        if r.project:
            lines.append(f"   Project: {r.project}")

        if r.snippet:
            snippet = r.snippet.replace(">>>", "**").replace("<<<", "**")
            snippet = " ".join(snippet.split())[:150]
            lines.append(f"   {snippet}...")

        lines.append("")

    return "\n".join(lines)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Hybrid search (FTS5 + vector)")
    parser.add_argument("query", nargs="?", help="Search query")
    parser.add_argument("--mode", choices=["hybrid", "fts", "vector"], default="hybrid",
                        help="Search mode")
    parser.add_argument("--scoring", choices=["rrf", "linear"], default="rrf",
                        help="Scoring algorithm (default: rrf)")
    parser.add_argument("--weights", type=str, help="Vector,BM25 weights for linear scoring (e.g., 0.7,0.3)")
    parser.add_argument("--type", dest="file_type", help="Filter by type")
    parser.add_argument("--project", help="Filter by project")
    parser.add_argument("--since", type=str, help="Filter to docs newer than (e.g., 7d, 2w, 3m)")
    parser.add_argument("--limit", type=int, default=20, help="Max results")
    parser.add_argument("--format", choices=["json", "text"], default="json")

    args = parser.parse_args()

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

    # Get paths (check config first, then env var, then script location)
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
    index_path = memex / "_index.sqlite"

    if not index_path.exists():
        print(f"Index not found at {index_path}. Run search.py --rebuild first.", file=sys.stderr)
        sys.exit(1)

    # Connect and search
    conn = sqlite3.connect(index_path)
    try:
        # Initialize pipeline for vector search
        pipeline = None
        if args.mode in ("hybrid", "vector"):
            pipeline = EmbeddingPipeline()
            if not pipeline.enabled:
                if args.mode == "vector":
                    print("Vector search requires embeddings.", file=sys.stderr)
                    sys.exit(1)
                # For hybrid mode, fall back to FTS-only
                pipeline = None

        results = hybrid_search(
            conn=conn,
            query=args.query,
            pipeline=pipeline,
            mode=args.mode,
            scoring=args.scoring,
            vector_weight=vector_weight,
            bm25_weight=bm25_weight,
            file_type=args.file_type,
            project=args.project,
            since=args.since,
            limit=args.limit
        )

        print(format_results(results, args.format))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
