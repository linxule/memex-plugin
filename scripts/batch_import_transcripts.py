#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "filelock>=3.0",
#     "tiktoken>=0.5",
# ]
# ///
"""
Batch import transcripts from ~/.claude/projects/ to memex vault.

Reuses existing infrastructure from session-end hook:
- detect_project() for project mapping
- ensure_project_structure() for folder creation
- convert_transcript_file() for JSONL → markdown

Usage:
    uv run scripts/batch_import_transcripts.py [--dry-run] [--limit=N]
"""

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

# Add scripts directory for imports
sys.path.insert(0, str(Path(__file__).parent))

from utils import (
    get_memex_path,
    detect_project,
    ensure_project_structure,
    get_session_memo_saved,
    log_info,
    log_error,
)
from transcript_to_md import convert_transcript_file


def extract_cwd_from_dirname(dirname: str) -> str:
    """Convert .claude/projects dir name back to cwd path."""
    # Dir names like: -Users-yourname-Documents-Apps-memex
    # Convert back to: /Users/yourname/Documents/Apps/memex
    return "/" + dirname.replace("-", "/")


def get_existing_sessions(memex: Path) -> set:
    """Get session IDs already in vault."""
    ids = set()
    for jsonl in memex.glob("projects/*/transcripts/*.jsonl"):
        # Filename: YYYYMMDD-HHMMSS-sessionid.jsonl
        parts = jsonl.stem.split("-")
        if len(parts) >= 3:
            ids.add(parts[-1])
    return ids


def main():
    parser = argparse.ArgumentParser(description="Batch import transcripts")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of imports")
    args = parser.parse_args()

    memex = get_memex_path()
    claude_projects = Path.home() / ".claude" / "projects"

    if not claude_projects.exists():
        print("No .claude/projects directory found")
        return

    print(f"Memex vault: {memex}")
    print(f"Claude projects: {claude_projects}")

    existing = get_existing_sessions(memex)
    print(f"Already archived: {len(existing)} sessions")

    imported = 0
    skipped = 0
    errors = 0

    for project_dir in sorted(claude_projects.iterdir()):
        if not project_dir.is_dir():
            continue

        # Reconstruct cwd from directory name
        cwd = extract_cwd_from_dirname(project_dir.name)

        for jsonl_path in sorted(project_dir.glob("*.jsonl")):
            session_id = jsonl_path.stem
            session_short = session_id[:8]

            if session_short in existing:
                skipped += 1
                continue

            if args.limit and imported >= args.limit:
                print(f"\nReached limit of {args.limit}")
                break

            # Use existing detect_project (same as session-end hook)
            project = detect_project(cwd)

            if args.dry_run:
                print(f"  [DRY-RUN] {project}/{session_short}")
                imported += 1
                continue

            try:
                # Use existing ensure_project_structure
                project_path = ensure_project_structure(project, memex)

                # Generate filenames (same as session-end hook)
                mtime = datetime.fromtimestamp(jsonl_path.stat().st_mtime)
                timestamp = mtime.strftime("%Y%m%d-%H%M%S")
                base_name = f"{timestamp}-{session_short}"

                jsonl_dest = project_path / "transcripts" / f"{base_name}.jsonl"
                md_dest = project_path / "transcripts" / f"{base_name}.md"

                # Copy JSONL
                if not jsonl_dest.exists():
                    shutil.copy2(jsonl_path, jsonl_dest)

                # Convert to markdown (same as session-end hook)
                if not md_dest.exists():
                    has_memo = get_session_memo_saved(session_id)
                    result = convert_transcript_file(
                        jsonl_path=jsonl_dest,
                        output_path=md_dest,
                        session_id=session_id,
                        project=project,
                        has_memo=has_memo,
                    )
                    if result is None:
                        # Conversion failed (empty session)
                        jsonl_dest.unlink(missing_ok=True)
                        errors += 1
                        continue

                imported += 1
                existing.add(session_short)

                if imported % 50 == 0:
                    print(f"  Imported {imported}...")

            except Exception as e:
                errors += 1
                log_error(f"{jsonl_path.name}: {e}")

        if args.limit and imported >= args.limit:
            break

    print(f"\n=== Import Complete ===")
    print(f"Imported: {imported}")
    print(f"Skipped: {skipped}")
    print(f"Errors (empty sessions): {errors}")

    if imported > 0 and not args.dry_run:
        print(f"\nRun 'uv run scripts/index_rebuild.py --incremental' to update search index")


if __name__ == "__main__":
    main()
