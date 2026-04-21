from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from memex.observations import (
    content_hash,
    delete_observations_for_doc,
    encode_source_ids,
    fetch_observations,
    init_observation_schema,
    normalize_text,
    serialize_f32,
    store_observation_topics,
    vector_search_observations,
)
from memex.paths import get_index_path, get_memex_path
from memex.scripts.embeddings import PartialEmbeddingFailure

EXTRACTION_PROMPT = """You are extracting atomic observations from a session memo.
An observation is a single, self-contained fact that makes sense without
surrounding context.

Rules:
- Each observation must be independently understandable
- Use absolute dates (e.g., "2026-03-16" not "today" or "yesterday")
- Attribute correctly: if the memo says "we decided X", the observation
  is "Decision: X was chosen over Y because Z"
- Capture decisions, facts, preferences, constraints, and open questions
- Do NOT extract meta-observations about the memo itself
- Do NOT extract obvious/trivial facts
- Target 5-15 observations per memo

Types:
- explicit: directly stated in the memo
- deductive: logically follows from combining multiple explicit facts
- contradiction: conflicts with a known existing observation (flag these)

Topic tagging:
- For each observation, include a "topics" field listing 0-3 topic slugs it relates to
- Only use slugs from the provided topic list (if available)
- If no topics fit, use an empty list
- Topic slugs are kebab-case (e.g., "agent-architecture", "memory-management")
"""

STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "from", "with", "that", "this",
    "into", "onto", "because", "about", "have", "has", "had", "were", "was",
    "been", "being", "their", "there", "them", "they", "then", "than", "while",
}

POSITIVE_TOKENS = {
    "enable", "enabled", "keep", "kept", "use", "used", "works", "working",
    "chosen", "preferred", "allow", "allowed", "include", "included", "true",
}
NEGATIVE_TOKENS = {
    "disable", "disabled", "avoid", "avoided", "remove", "removed", "reject",
    "rejected", "exclude", "excluded", "skip", "skipped", "false", "not",
    "never",
}


@dataclass(slots=True)
class Observation:
    content: str
    obs_type: str
    confidence: str
    source_obs_ids: list[int] | None = None
    topics: list[str] = field(default_factory=list)


# WAL + busy_timeout is shared with index_rebuild.py via memex.db_utils
# so writers and readers use identical connection configuration.
from memex.db_utils import connect_index as _connect


def store_observations(
    index_path: Path,
    memo_path: str,
    observations: list[Observation],
    pipeline,
) -> dict:
    """Store observations. Returns {inserted, embedded, embed_failed}."""
    # Pre-compute embeddings BEFORE opening the write transaction.
    # Network round-trips inside the transaction would hold the SQLite
    # write lock for seconds and cause "database is locked" races under
    # parallel backfill.
    embed_enabled = bool(pipeline and getattr(pipeline, "enabled", False))
    precomputed_vectors: list[bytes | None] = [None] * len(observations)
    if embed_enabled and observations:
        texts = [observation.content for observation in observations]
        try:
            raw_vectors = pipeline._provider_impl.embed_texts(
                texts,
                task_type="document",
            )
        except PartialEmbeddingFailure as exc:
            raw_vectors = exc.results
            missing = sum(1 for vector in raw_vectors if vector is None)
            # exc.last_error distinguishes rate-limit (retryable) from
            # API_KEY_INVALID / 4xx (non-retryable) — report the actual type
            # so operators can act. Silent "rate limited" was misleading
            # during the 2026-04-21 key-rotation incident.
            err = getattr(exc, "last_error", None)
            cause = (
                f"{type(err).__name__}: {err}"
                if err is not None else "unknown error"
            )
            print(
                f"⚠️  {missing}/{len(observations)} observations stored without "
                f"embeddings ({cause}). Semantic search will miss them. "
                f"Run `memex index embed-missing` after fixing the cause.",
                file=sys.stderr,
            )
        precomputed_vectors = [
            serialize_f32(vector) if vector else None for vector in raw_vectors
        ]

    conn = _connect(index_path)
    try:
        init_observation_schema(conn, getattr(pipeline, "dimensions", None))
        delete_observations_for_doc(conn, memo_path)

        inserted = 0
        embedded = 0
        embed_failed = 0
        for observation, vector_blob in zip(observations, precomputed_vectors):
            obs_hash = content_hash(observation.content)
            existing = conn.execute(
                "SELECT id FROM observations WHERE content_hash = ?",
                (obs_hash,),
            ).fetchone()
            if existing:
                continue

            cursor = conn.execute(
                """
                INSERT INTO observations
                (doc_path, content, content_hash, obs_type, confidence, source_obs_ids)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    memo_path,
                    observation.content,
                    obs_hash,
                    observation.obs_type,
                    observation.confidence,
                    encode_source_ids(observation.source_obs_ids),
                ),
            )
            obs_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO fts_observations(rowid, content, obs_type) VALUES (?, ?, ?)",
                (obs_id, observation.content, observation.obs_type),
            )

            if vector_blob is not None:
                try:
                    conn.execute(
                        "INSERT INTO vec_observations(rowid, embedding) VALUES (?, ?)",
                        (obs_id, vector_blob),
                    )
                    embedded += 1
                except sqlite3.OperationalError:
                    embed_failed += 1
            elif embed_enabled:
                # Pipeline enabled but this observation didn't come back with
                # a vector — counts as a failure (e.g., partial API error).
                embed_failed += 1

            if observation.topics:
                store_observation_topics(conn, obs_id, observation.topics)

            inserted += 1

        conn.commit()
        return {"inserted": inserted, "embedded": embedded, "embed_failed": embed_failed}
    finally:
        conn.close()


def detect_contradictions(
    index_path: Path,
    new_observations: list[Observation],
    pipeline,
    threshold: float = 0.85,
) -> list[Observation]:
    conn = _connect(index_path)
    try:
        init_observation_schema(conn, getattr(pipeline, "dimensions", None))
        contradictions: list[Observation] = []

        for observation in new_observations:
            obs_hash = content_hash(observation.content)
            row = conn.execute(
                "SELECT id, doc_path FROM observations WHERE content_hash = ?",
                (obs_hash,),
            ).fetchone()
            if not row:
                continue

            obs_id = int(row[0])
            doc_path = str(row[1])
            similar = _similar_existing_observations(
                conn,
                observation.content,
                obs_id,
                doc_path,
                pipeline,
                threshold,
            )
            for existing_id, existing_content in similar:
                contradictions.append(
                    Observation(
                        content=(
                            f"Contradiction: '{observation.content}' conflicts with "
                            f"existing observation '{existing_content}'"
                        ),
                        obs_type="contradiction",
                        confidence="medium",
                        source_obs_ids=[existing_id, obs_id],
                    )
                )

        return contradictions
    finally:
        conn.close()


def append_contradictions_to_memo(
    memo_file: Path,
    contradictions: Sequence[Observation],
) -> None:
    if not contradictions or not memo_file.exists():
        return

    text = memo_file.read_text()
    if not text.startswith("---"):
        return

    end = text.find("\n---", 3)
    if end == -1:
        return

    frontmatter = text[: end + 4]
    body = text[end + 4 :]
    entries = [
        " vs ".join(str(source_id) for source_id in contradiction.source_obs_ids or [])
        + f": {contradiction.content}"
        for contradiction in contradictions
    ]
    rendered = "[" + ", ".join(json.dumps(entry) for entry in entries) + "]"

    if "\ncontradictions:" in frontmatter:
        frontmatter = re.sub(
            r"\ncontradictions:\s*\[[^\n]*\]",
            f"\ncontradictions: {rendered}",
            frontmatter,
        )
    else:
        frontmatter = frontmatter[:-4] + f"\ncontradictions: {rendered}\n---"

    memo_file.write_text(frontmatter + body)


def _similar_existing_observations(
    conn: sqlite3.Connection,
    content: str,
    obs_id: int,
    doc_path: str,
    pipeline,
    threshold: float,
) -> list[tuple[int, str]]:
    candidates: list[tuple[int, str]] = []
    if pipeline and getattr(pipeline, "enabled", False):
        embedding = pipeline.embed_text(content, task_type="RETRIEVAL_QUERY")
        if embedding:
            for existing, distance in vector_search_observations(
                conn,
                serialize_f32(embedding),
                limit=10,
            ):
                if existing.id == obs_id or existing.doc_path == doc_path:
                    continue
                similarity = max(0.0, 1.0 - distance)
                if similarity < threshold:
                    continue
                if _is_contradiction(content, existing.content):
                    candidates.append((existing.id, existing.content))

    if candidates:
        return candidates

    for existing in fetch_observations(conn, limit=200):
        if existing.id == obs_id or existing.doc_path == doc_path:
            continue
        if _token_overlap(content, existing.content) < 0.3:
            continue
        if _is_contradiction(content, existing.content):
            candidates.append((existing.id, existing.content))
    return candidates


def _token_overlap(left: str, right: str) -> float:
    left_tokens = _meaningful_tokens(left)
    right_tokens = _meaningful_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = left_tokens & right_tokens
    union = left_tokens | right_tokens
    return len(intersection) / len(union)


def _meaningful_tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"\b[a-z0-9][a-z0-9_-]+\b", text.lower())
        if len(token) > 2 and token not in STOPWORDS
    }


def _polarity(text: str) -> int:
    tokens = _meaningful_tokens(text)
    positive = len(tokens & POSITIVE_TOKENS)
    negative = len(tokens & NEGATIVE_TOKENS)
    if positive > negative:
        return 1
    if negative > positive:
        return -1
    return 0


def _is_contradiction(left: str, right: str) -> bool:
    if _token_overlap(left, right) < 0.3:
        return False
    left_polarity = _polarity(left)
    right_polarity = _polarity(right)
    return left_polarity != 0 and right_polarity != 0 and left_polarity != right_polarity


def _init_pipeline():
    """Initialize embedding pipeline for write-time embedding."""
    try:
        from memex.scripts.embeddings import EmbeddingPipeline

        pipeline = EmbeddingPipeline()
        return pipeline if pipeline.enabled else None
    except Exception:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Store Claude-generated observations")
    parser.add_argument("--store-json", type=Path,
                        help="JSON file with observations (Claude-generated)")
    parser.add_argument("--stdin", action="store_true",
                        help="Read observations JSON from stdin")
    parser.add_argument("--doc-path", type=str, required=True,
                        help="Memo path relative to vault")
    parser.add_argument("--index", type=Path, help="Override index path")
    parser.add_argument("--no-embed", action="store_true",
                        help="Skip embedding (store text + FTS only)")
    args = parser.parse_args()

    if args.stdin:
        obs_data = json.loads(sys.stdin.read())
    elif args.store_json:
        if not args.store_json.exists():
            print(f"Error: observations file not found: {args.store_json}", file=sys.stderr)
            sys.exit(1)
        obs_data = json.loads(args.store_json.read_text())
    else:
        print("Error: either --store-json or --stdin is required", file=sys.stderr)
        sys.exit(1)
    observations = [Observation(**o) for o in obs_data]
    active_index = args.index or get_index_path()
    pipeline = None if args.no_embed else _init_pipeline()
    result = store_observations(active_index, args.doc_path, observations, pipeline=pipeline)
    pipeline_enabled = pipeline is not None and pipeline.enabled
    output = {
        "stored": result["inserted"],
        "total": len(observations),
        "embedded": result["embedded"],
        "embed_failed": result["embed_failed"],
        "pipeline_enabled": pipeline_enabled,
    }
    print(json.dumps(output))
    # Exit non-zero if embeddings were attempted but any failed, so chained
    # scripts (hooks, backfills) can detect silent-partial embed failures.
    if result["embed_failed"] > 0:
        sys.exit(2)


if __name__ == "__main__":
    main()
