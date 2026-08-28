"""
Topic Similarity Detection

Identifies near-duplicate or overlapping topics by comparing their
vector embeddings pairwise. Reports pairs above a cosine similarity
threshold for potential merge during garden tending.

Usage:
    similarity_detection.py [--threshold=0.80] [--json] [-v]
"""

import argparse
import json
import math
import sqlite3
import struct
import sys
from collections import defaultdict
from pathlib import Path

from memex.paths import get_index_path, get_memex_path


def deserialize_f32(blob: bytes) -> list[float]:
    count = len(blob) // 4
    return list(struct.unpack(f"{count}f", blob))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def average_vectors(vectors: list[list[float]]) -> list[float]:
    if len(vectors) == 1:
        return vectors[0]
    n = len(vectors)
    dim = len(vectors[0])
    return [sum(vectors[j][i] for j in range(n)) / n for i in range(dim)]


def load_topic_embeddings(conn: sqlite3.Connection) -> dict[str, list[float]]:
    """Load one representative embedding per topic from the index."""
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except Exception as e:
        print(f"Error: sqlite-vec not available: {e}", file=sys.stderr)
        return {}

    try:
        rows = conn.execute("""
            SELECT c.doc_path, c.chunk_index, c.chunk_type, v.embedding
            FROM chunks c
            JOIN vec_chunks v ON v.rowid = c.id
            WHERE c.doc_path LIKE 'topics/%'
            AND c.chunk_type != 'frontmatter'
        """).fetchall()
    except sqlite3.OperationalError as e:
        print(f"Error querying embeddings: {e}", file=sys.stderr)
        return {}

    if not rows:
        print("No topic embeddings found. Run: memex index rebuild --full", file=sys.stderr)
        return {}

    # Group by doc_path
    by_doc: dict[str, list[list[float]]] = defaultdict(list)
    for doc_path, _chunk_idx, _chunk_type, embedding_blob in rows:
        by_doc[doc_path].append(deserialize_f32(embedding_blob))

    # Average vectors per topic
    return {doc_path: average_vectors(vecs) for doc_path, vecs in by_doc.items()}


def load_aliases(conn: sqlite3.Connection) -> set[frozenset[str]]:
    """Load alias pairs to filter already-linked topics."""
    try:
        rows = conn.execute("SELECT alias, doc_path FROM doc_aliases").fetchall()
        pairs = set()
        for alias, doc_path in rows:
            # Alias might be a topic name — resolve to path
            alias_path = f"topics/{alias}.md"
            pairs.add(frozenset([alias_path, doc_path]))
        return pairs
    except sqlite3.OperationalError:
        return set()


def get_topic_metadata(vault: Path) -> dict[str, dict]:
    """Read frontmatter and line count for topics."""
    topics_dir = vault / "topics"
    if not topics_dir.exists():
        return {}

    metadata = {}
    for f in topics_dir.glob("*.md"):
        rel_path = f"topics/{f.name}"
        try:
            content = f.read_text()
            line_count = content.count("\n")

            # Quick frontmatter parse
            status = "active"
            title = f.stem
            if content.startswith("---"):
                try:
                    end = content.index("---", 3)
                    fm = content[3:end]
                    for line in fm.split("\n"):
                        if line.startswith("status:"):
                            status = line.split(":", 1)[1].strip().strip('"')
                        elif line.startswith("title:"):
                            title = line.split(":", 1)[1].strip().strip('"')
                except ValueError:
                    pass

            metadata[rel_path] = {
                "title": title,
                "status": status,
                "lines": line_count,
            }
        except (OSError, UnicodeDecodeError):
            continue

    return metadata


def find_similar_topics(
    vault: Path,
    index_path: Path,
    threshold: float = 0.85,
) -> list[dict]:
    conn = sqlite3.connect(str(index_path))
    try:
        embeddings = load_topic_embeddings(conn)
        if not embeddings:
            return []

        aliases = load_aliases(conn)
        metadata = get_topic_metadata(vault)

        # Filter: skip archived and stubs (<50 lines)
        active_topics = {}
        for path, vec in embeddings.items():
            meta = metadata.get(path, {})
            if meta.get("status") == "archived":
                continue
            if meta.get("lines", 0) < 50:
                continue
            active_topics[path] = vec

        if len(active_topics) < 2:
            return []

        # Pairwise comparison
        paths = list(active_topics.keys())
        pairs = []

        for i in range(len(paths)):
            for j in range(i + 1, len(paths)):
                path_a, path_b = paths[i], paths[j]

                # Skip aliased pairs
                if frozenset([path_a, path_b]) in aliases:
                    continue

                sim = cosine_similarity(active_topics[path_a], active_topics[path_b])
                if sim >= threshold:
                    meta_a = metadata.get(path_a, {})
                    meta_b = metadata.get(path_b, {})
                    pairs.append({
                        "topic_a": path_a,
                        "topic_b": path_b,
                        "title_a": meta_a.get("title", Path(path_a).stem),
                        "title_b": meta_b.get("title", Path(path_b).stem),
                        "similarity": round(sim, 4),
                    })

        pairs.sort(key=lambda p: p["similarity"], reverse=True)
        return pairs

    finally:
        conn.close()


def format_similarity_results(pairs: list[dict], output_format: str = "text", verbose: bool = False) -> str:
    if output_format == "json":
        return json.dumps(pairs, indent=2)

    if not pairs:
        return "No similar topics found above threshold."

    lines = [
        "Topic Similarity Report",
        "=" * 40,
        f"Found {len(pairs)} topic pair(s):\n",
    ]

    for i, p in enumerate(pairs, 1):
        sim = p["similarity"]
        badge = "MERGE?" if sim >= 0.90 else "REVIEW"
        lines.append(f"{i}. {sim:.2f}  [[{p['title_a']}]] <-> [[{p['title_b']}]]  [{badge}]")
        if verbose:
            lines.append(f"       {p['topic_a']}")
            lines.append(f"       {p['topic_b']}")

    lines.append("")
    lines.append("Guidance:")
    lines.append("  > 0.90 — likely duplicates, merge or alias")
    lines.append("  0.85-0.90 — related, review manually")

    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description="Detect near-duplicate or overlapping topics",
        epilog=(
            "Examples:\n"
            "  memex similarity                    # default threshold 0.80\n"
            "  memex similarity --threshold 0.90   # strict — near-duplicates only\n"
            "  memex similarity --json             # programmatic output\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def main():
    parser = _build_parser()
    parser.add_argument("--threshold", type=float, default=0.85,
                        help="Cosine similarity threshold (0-1, default: 0.85)")
    parser.add_argument("--format", choices=["json", "text"], default="text",
                        help="Output format")
    parser.add_argument("--json", action="store_true", help="JSON output (shorthand)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Show file paths alongside titles")

    args = parser.parse_args()

    if args.json:
        args.format = "json"

    vault = get_memex_path()
    index_path = get_index_path(vault)

    if not index_path.exists():
        print("Error: No index found. Run: memex index rebuild --full", file=sys.stderr)
        sys.exit(1)

    pairs = find_similar_topics(vault, index_path, threshold=args.threshold)
    print(format_similarity_results(pairs, output_format=args.format, verbose=args.verbose))


if __name__ == "__main__":
    main()
