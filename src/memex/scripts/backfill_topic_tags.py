"""
Backfill observation_topics — propagate topic tags from memo frontmatter to the
observation_topics junction table.

Each observation has a doc_path pointing to its source memo. Memos have topics:
in their YAML frontmatter. This script reads the frontmatter, filters to valid
topic slugs (from topics/*.md), and inserts rows into observation_topics.

No LLM calls needed — purely structural propagation.

Usage:
  uv run scripts/backfill_topic_tags.py                # dry-run (default)
  uv run scripts/backfill_topic_tags.py --apply         # actually write changes
  uv run scripts/backfill_topic_tags.py --verbose        # show each tagging
  uv run scripts/backfill_topic_tags.py --apply -v       # apply + verbose
"""

import argparse
import re
import sqlite3
import sys
from pathlib import Path

from memex.observations import init_observation_schema
from memex.paths import get_memex_path


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

def parse_topics_from_frontmatter(content: str) -> list[str]:
    """Extract the topics list from YAML frontmatter.

    Handles both inline format: topics: [a, b, c]
    and multi-line format:
      topics:
        - a
        - b
    """
    if not content.startswith("---"):
        return []

    try:
        end = content.index("---", 3)
    except ValueError:
        return []

    fm_block = content[3:end]

    # Try inline format first: topics: [a, b, c]
    inline_match = re.search(r"^topics:\s*\[([^\]]*)\]", fm_block, re.MULTILINE)
    if inline_match:
        raw = inline_match.group(1)
        if not raw.strip():
            return []
        return [t.strip().strip('"').strip("'") for t in raw.split(",") if t.strip()]

    # Try multi-line format: topics:\n  - a\n  - b
    ml_match = re.search(r"^topics:\s*$", fm_block, re.MULTILINE)
    if ml_match:
        topics = []
        # Walk lines after "topics:" until we hit a non-list-item line
        lines_after = fm_block[ml_match.end():].split("\n")
        for line in lines_after:
            stripped = line.strip()
            if stripped.startswith("- "):
                topics.append(stripped[2:].strip().strip('"').strip("'"))
            elif stripped == "" or stripped == "-":
                continue
            else:
                break
        return topics

    return []


def is_archived(content: str) -> bool:
    """Check if frontmatter contains status: archived."""
    if not content.startswith("---"):
        return False
    try:
        end = content.index("---", 3)
    except ValueError:
        return False
    fm_block = content[3:end]
    return bool(re.search(r"^status:\s*archived\s*$", fm_block, re.MULTILINE))


# ---------------------------------------------------------------------------
# Valid topic collection
# ---------------------------------------------------------------------------

def collect_valid_topics(vault: Path) -> set[str]:
    """List valid topic slugs from topics/*.md, excluding archived ones."""
    valid = set()
    topics_dir = vault / "topics"
    if not topics_dir.is_dir():
        return valid

    for topic_file in topics_dir.glob("*.md"):
        slug = topic_file.stem
        try:
            content = topic_file.read_text()
        except OSError:
            continue
        if is_archived(content):
            continue
        valid.add(slug)

    return valid


# ---------------------------------------------------------------------------
# Database connection
# ---------------------------------------------------------------------------

def _connect(index_path: Path) -> sqlite3.Connection:
    """Open the index with WAL + busy_timeout."""
    conn = sqlite3.connect(index_path, timeout=10.0)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _run() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill observation_topics from memo frontmatter topics",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write changes (default is dry-run)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print each observation tagging",
    )
    args = parser.parse_args()

    dry_run = not args.apply
    vault = get_memex_path()
    index_path = vault / "_index.sqlite"

    if not index_path.exists():
        print(f"Error: index not found at {index_path}", file=sys.stderr)
        sys.exit(2)

    # 1. Collect valid topic slugs
    valid_topics = collect_valid_topics(vault)
    print(f"Valid topics: {len(valid_topics)}")

    # 2. Connect and ensure schema
    conn = _connect(index_path)
    try:
        init_observation_schema(conn)

        # 3. Fetch all observations
        rows = conn.execute(
            "SELECT id, doc_path FROM observations ORDER BY id"
        ).fetchall()
        total = len(rows)
        print(f"Total observations: {total}")

        # Cache: doc_path -> parsed topics (avoid re-reading same file)
        frontmatter_cache: dict[str, list[str]] = {}
        tagged_count = 0
        topics_used: set[str] = set()
        no_match_count = 0
        inserted_total = 0
        batch_count = 0

        for i, (obs_id, doc_path) in enumerate(rows):
            # Parse frontmatter topics (cached per doc_path)
            if doc_path not in frontmatter_cache:
                file_path = vault / doc_path
                if file_path.exists():
                    try:
                        content = file_path.read_text()
                        raw_topics = parse_topics_from_frontmatter(content)
                        # Filter to valid slugs
                        frontmatter_cache[doc_path] = [
                            t for t in raw_topics if t in valid_topics
                        ]
                    except OSError:
                        frontmatter_cache[doc_path] = []
                else:
                    frontmatter_cache[doc_path] = []

            matched_topics = frontmatter_cache[doc_path]

            if not matched_topics:
                no_match_count += 1
                continue

            tagged_count += 1
            topics_used.update(matched_topics)

            if args.verbose and (i + 1) % 200 == 0:
                print(f"  Progress: {i + 1}/{total}")

            if args.verbose:
                print(f"  TAG obs={obs_id}  topics={matched_topics}  doc={doc_path}")

            if not dry_run:
                for slug in matched_topics:
                    conn.execute(
                        "INSERT OR IGNORE INTO observation_topics "
                        "(observation_id, topic_slug) VALUES (?, ?)",
                        (obs_id, slug),
                    )
                    inserted_total += 1
                batch_count += 1
                if batch_count >= 100:
                    conn.commit()
                    batch_count = 0

        if not dry_run and batch_count > 0:
            conn.commit()

        # Summary
        action = "Would tag" if dry_run else "Tagged"
        print(f"\n{action}: {tagged_count} observations")
        print(f"No matching topics: {no_match_count}")
        print(f"Distinct topics used: {len(topics_used)}")
        if not dry_run:
            print(f"Rows inserted (inc. ignored dupes): {inserted_total}")

        if dry_run and tagged_count > 0:
            print("\nRun with --apply to write changes.")

    finally:
        conn.close()


def main():
    try:
        _run()
    except SystemExit:
        raise
    except sqlite3.OperationalError as exc:
        print(f"Error: {exc}\nFix: memex index rebuild --full", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
