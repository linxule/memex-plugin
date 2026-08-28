from __future__ import annotations

import json
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from memex.paths import get_index_path

from memex.observations import (
    fetch_observations,
    init_observation_schema,
    search_observations_fts,
    vector_search_observations,
)
from memex.scripts.wikilink_filters import strip_code_spans

STOP_WORDS = {
    "why", "what", "how", "when", "where", "who", "which",
    "did", "do", "does", "is", "was", "were", "are", "been",
    "we", "you", "they", "our", "the", "a", "an",
    "about", "with", "from", "for", "of", "in", "on", "to", "at",
    "this", "that", "these", "those",
}

_TOKENIZER = None


def _get_tokenizer():
    global _TOKENIZER
    if _TOKENIZER is None:
        import tiktoken
        _TOKENIZER = tiktoken.get_encoding("cl100k_base")
    return _TOKENIZER


@dataclass(slots=True)
class AskResult:
    path: str
    title: str
    project: str
    date: str
    score: float
    match_type: str
    content: str
    outgoing_links: list[str] = field(default_factory=list)
    backlink_count: int = 0


@dataclass(slots=True)
class ObservationResult:
    content: str
    obs_type: str
    source_memo: str
    confidence: str


@dataclass(slots=True)
class AskResponse:
    results: list[AskResult]
    observations: list[ObservationResult]
    query_info: dict[str, Any]


def extract_keywords(question: str) -> str:
    words = re.findall(r"\b\w+\b", question.lower())
    keywords = [word for word in words if word not in STOP_WORDS and len(word) > 2]
    return " OR ".join(keywords[:6]) or question


def ask(
    question: str,
    index_path: Path,
    vault_path: Path,
    project: str | None = None,
    depth: str = "quick",
    max_content_tokens: int = 800,
) -> AskResponse:
    limit = 10 if depth == "quick" else 20
    return_count = 5 if depth == "quick" else 10
    fts_query = extract_keywords(question)
    vector_query = question

    # Pre-compute query embedding once for thorough mode (used by both doc + obs vector search)
    query_embedding = None
    if depth == "thorough":
        pipeline = _build_pipeline()
        if pipeline is not None and getattr(pipeline, "enabled", False):
            query_embedding = pipeline.embed_query(question)

    conn = sqlite3.connect(index_path)
    try:
        init_observation_schema(conn)
        fts_rows = _fts_candidates(conn, fts_query, project=project, limit=limit)
        if depth == "thorough" and query_embedding:
            vector_rows = _vector_candidates(conn, query_embedding, project=project, limit=limit)
            merged = _merge_candidates(fts_rows, vector_rows)
        else:
            merged = fts_rows
        top_results = merged[:return_count]

        results: list[AskResult] = []
        for row in top_results:
            rel_path = str(row["path"])
            content = _read_and_truncate(vault_path, rel_path, max_content_tokens)
            results.append(
                AskResult(
                    path=rel_path,
                    title=str(row["title"]),
                    project=str(row["project"] or ""),
                    date=str(row["date"] or ""),
                    score=float(row["score"]),
                    match_type=str(row["match_type"]),
                    content=content,
                    outgoing_links=_extract_wikilinks(content),
                    backlink_count=_get_backlink_count(conn, rel_path),
                )
            )

        observations = _observation_results(
            conn,
            fts_query=fts_query,
            query_embedding=query_embedding,
            project=project,
        )

        query_info = {
            "fts_query": fts_query,
            "vector_query": vector_query,
            "total_candidates": len(merged),
            "results_returned": len(results),
            "gaps": _infer_gaps(results, observations),
        }
        return AskResponse(results=results, observations=observations, query_info=query_info)
    finally:
        conn.close()


def _fts_candidates(
    conn: sqlite3.Connection,
    query: str,
    *,
    project: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    sql = """
        SELECT path, title, project, date, bm25(fts_content) AS score
        FROM fts_content
        WHERE fts_content MATCH ?
    """
    params: list[object] = [query]
    if project:
        sql += " AND project = ?"
        params.append(project)
    sql += " ORDER BY score LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    results: list[dict[str, Any]] = []
    for rank, row in enumerate(rows, start=1):
        results.append(
            {
                "path": row[0],
                "title": row[1],
                "project": row[2],
                "date": row[3],
                "score": float(row[4]),
                "rank": rank,
                "match_type": "fts",
                "source": "fts",
            }
        )
    return results


def _vector_candidates(
    conn: sqlite3.Connection,
    query_embedding,
    *,
    project: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    # Align the native-dim query vector to vec_chunks' stored dimension so
    # search still works during a config-flip-before-migrate window (and at
    # any index_dimensions setting). Lazy import avoids a circular dependency.
    from memex.scripts.embeddings import match_query_dim
    query_embedding = match_query_dim(conn, "vec_chunks", query_embedding)

    # Run the KNN FIRST (vec_chunks → chunks by rowid only), then enrich with
    # fts_content metadata in a single batched lookup. Joining fts_content
    # INSIDE the KNN query makes SQLite scan fts_content as the OUTER loop and
    # re-run the vec match once per doc — O(docs × KNN), which hangs for minutes
    # on a real-size index. fts_content has no path index (FTS5), so it must
    # never drive the join. (Mirrors hybrid_search.vector_search.)
    knn_sql = (
        "SELECT c.doc_path, v.distance "
        "FROM vec_chunks v JOIN chunks c ON c.id = v.rowid "
        "WHERE v.embedding MATCH ? AND k = ?"
    )
    knn: list | None = None
    if project:
        # Push the project filter into the KNN via the v0.15.0 metadata column.
        try:
            knn = conn.execute(
                knn_sql + " AND v.doc_project = ?",
                [query_embedding, limit, project],
            ).fetchall()
        except sqlite3.OperationalError:
            knn = None  # pre-metadata index → bare KNN + post-filter below
    if knn is None:
        try:
            knn = conn.execute(knn_sql, [query_embedding, limit]).fetchall()
        except sqlite3.OperationalError:
            return []
    if not knn:
        return []

    # Best (smallest) distance per doc.
    dist_by_path: dict[str, float] = {}
    for doc_path, distance in knn:
        if doc_path not in dist_by_path or distance < dist_by_path[doc_path]:
            dist_by_path[doc_path] = distance

    placeholders = ",".join("?" for _ in dist_by_path)
    meta_rows = conn.execute(
        f"SELECT path, title, project, date FROM fts_content WHERE path IN ({placeholders})",
        list(dist_by_path.keys()),
    ).fetchall()

    results: list[dict[str, Any]] = []
    for path, title, proj, date in meta_rows:
        if project and proj != project:
            continue  # safety net for the bare-KNN fallback path
        results.append({
            "path": path,
            "title": title,
            "project": proj,
            "date": date,
            "score": max(0.0, 1.0 - float(dist_by_path[path])),
        })
    results.sort(key=lambda item: item["score"], reverse=True)
    return [
        {**row, "rank": rank, "source": "vector"}
        for rank, row in enumerate(results[:limit], start=1)
    ]


def _merge_candidates(
    fts_rows: list[dict[str, Any]],
    vector_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    scores: dict[str, dict[str, Any]] = {}

    for row in fts_rows:
        entry = scores.setdefault(
            row["path"],
            {
                "path": row["path"],
                "title": row["title"],
                "project": row["project"],
                "date": row["date"],
                "score": 0.0,
                "sources": set(),
            },
        )
        entry["score"] += 1.0 / (60 + row["rank"])
        entry["sources"].add("fts")

    for row in vector_rows:
        entry = scores.setdefault(
            row["path"],
            {
                "path": row["path"],
                "title": row["title"],
                "project": row["project"],
                "date": row["date"],
                "score": 0.0,
                "sources": set(),
            },
        )
        entry["score"] += 1.0 / (60 + row["rank"])
        entry["sources"].add("vector")

    merged = []
    for entry in scores.values():
        sources = entry.pop("sources")
        if sources == {"fts", "vector"}:
            entry["match_type"] = "hybrid"
        elif sources == {"vector"}:
            entry["match_type"] = "vector"
        else:
            entry["match_type"] = "fts"
        merged.append(entry)
    merged.sort(key=lambda item: item["score"], reverse=True)
    return merged


def _observation_results(
    conn: sqlite3.Connection,
    *,
    fts_query: str,
    query_embedding=None,
    project: str | None,
) -> list[ObservationResult]:
    fts_matches = search_observations_fts(conn, fts_query, project=project, limit=10)
    ranked: dict[int, tuple[float, ObservationResult]] = {}

    for rank, match in enumerate(fts_matches, start=1):
        ranked[match.id] = (
            1.0 / (60 + rank),
            ObservationResult(
                content=match.content,
                obs_type=match.obs_type,
                source_memo=match.doc_path,
                confidence=match.confidence,
            ),
        )

    if query_embedding is not None:
        for rank, (match, distance) in enumerate(
            vector_search_observations(conn, query_embedding, project=project, limit=10),
            start=1,
        ):
            score = 1.0 / (60 + rank) + max(0.0, 1.0 - float(distance))
            existing = ranked.get(match.id)
            result = ObservationResult(
                content=match.content,
                obs_type=match.obs_type,
                source_memo=match.doc_path,
                confidence=match.confidence,
            )
            if existing is None or score > existing[0]:
                ranked[match.id] = (score, result)

    ordered = [item[1] for item in sorted(ranked.values(), key=lambda value: value[0], reverse=True)]
    return ordered[:10]


def _read_and_truncate(vault_path: Path, rel_path: str, max_tokens: int) -> str:
    try:
        content = (vault_path / rel_path).read_text()
    except (FileNotFoundError, OSError):
        return "[File not found]"
    if content.startswith("---"):
        end = content.find("\n---", 3)
        if end != -1:
            content = content[end + 4 :].strip()
    tokens = _get_tokenizer().encode(content)
    if len(tokens) <= max_tokens:
        return content
    return _get_tokenizer().decode(tokens[:max_tokens]) + "\n\n[Truncated]"


def _extract_wikilinks(content: str) -> list[str]:
    # Strip code spans so fenced examples / inline code don't seed graph-hop
    # expansion with phantom links (shared with the indexer + crystallization
    # checker via wikilink_filters).
    return re.findall(r"\[\[([^\]]+)\]\]", strip_code_spans(content))


def _get_backlink_count(conn: sqlite3.Connection, path: str) -> int:
    try:
        row = conn.execute(
            "SELECT count(*) FROM wikilinks WHERE target_path = ?",
            (path,),
        ).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.OperationalError:
        return 0


def _infer_gaps(
    results: list[AskResult],
    observations: list[ObservationResult],
) -> list[str]:
    gaps: list[str] = []
    if not results:
        gaps.append("No matching memos or notes were found.")
    if results and not observations:
        gaps.append("Relevant documents were found, but no atomic observations matched directly.")
    return gaps


def _build_pipeline():
    try:
        from memex.scripts.embeddings import EmbeddingPipeline

        pipeline = EmbeddingPipeline()
        if pipeline.enabled:
            return pipeline
    except Exception:
        return None
    return None


def response_to_dict(response: AskResponse) -> dict[str, Any]:
    return {
        "results": [asdict(item) for item in response.results],
        "observations": [asdict(item) for item in response.observations],
        "query_info": response.query_info,
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Ask a deep question of the memex index")
    parser.add_argument("question")
    parser.add_argument("--index", type=Path, help="Path to index (auto-detected)")
    parser.add_argument("--vault", type=Path, help="Path to vault (auto-detected)")
    parser.add_argument("--project")
    parser.add_argument("--depth", choices=["quick", "thorough"], default="quick")
    args = parser.parse_args()

    # Auto-detect paths if not provided
    if args.vault is None:
        from memex.paths import get_memex_path
        args.vault = get_memex_path()
    if args.index is None:
        args.index = get_index_path(args.vault)

    response = ask(
        question=args.question,
        index_path=args.index,
        vault_path=args.vault,
        project=args.project,
        depth=args.depth,
    )
    print(json.dumps(response_to_dict(response), indent=2))


if __name__ == "__main__":
    main()
