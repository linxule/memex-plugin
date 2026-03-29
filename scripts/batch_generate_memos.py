#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "anthropic>=0.39.0",
#     "filelock>=3.0",
#     "tiktoken>=0.5",
# ]
# ///
"""
Batch generate memos from transcripts.

Generates memos for all transcripts that don't already have corresponding memos.
Reuses existing infrastructure from pre-compact hook.

Usage:
    uv run scripts/batch_generate_memos.py my-app research-project theory-lab
    uv run scripts/batch_generate_memos.py --all
    uv run scripts/batch_generate_memos.py my-app --dry-run
    uv run scripts/batch_generate_memos.py my-app --limit=5
"""

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

# Add scripts directory for imports
sys.path.insert(0, str(Path(__file__).parent))

from utils import (
    get_memex_path,
    get_config,
    safe_write,
    format_frontmatter,
    log_info,
    log_error,
    log_warning,
)
from transcript_to_md import extract_for_memo, parse_transcript_jsonl

# Lazy anthropic import
anthropic = None


def get_anthropic_client():
    """Get or create Anthropic client."""
    global anthropic
    if anthropic is None:
        import anthropic as anthropic_module
        anthropic = anthropic_module
    return anthropic.Anthropic()


# Default memo generation prompt (same as pre-compact hook)
DEFAULT_MEMO_PROMPT = """You are a knowledge management assistant helping to create session memos.

Your task is to distill a Claude Code session transcript into a concise, useful memo that captures:

1. **Key Decisions**: Important choices made during the session
2. **Learnings**: New knowledge, patterns, or insights discovered
3. **Solutions**: Problems solved and how they were solved
4. **Open Threads**: Unfinished work or questions for future sessions

## Output Format

Generate a memo in this format:

```markdown
---
type: memo
title: [Brief descriptive title]
topics: [topic1, topic2]
---

# [Title]

## Summary
[2-3 sentence overview of what was accomplished]

## Key Points

### Decisions
- [Decision 1 and rationale]
- [Decision 2 and rationale]

### Learnings
- [Learning 1]
- [Learning 2]

### Solutions
- **[Problem]**: [How it was solved]

## Open Threads
- [ ] [Unfinished item 1]
- [ ] [Unfinished item 2]

## Related
- [[related-topic]]
```

## Guidelines

1. Be concise - the memo should be scannable
2. Focus on decisions and learnings, not step-by-step actions
3. Use wikilinks [[topic-name]] for cross-references
4. Include open threads for continuity between sessions
5. Title should capture the main theme of the session
6. Topics should be 2-4 relevant keywords"""


def get_projects(memex: Path) -> list[str]:
    """Get all project names."""
    projects_dir = memex / "projects"
    return [d.name for d in projects_dir.iterdir() if d.is_dir()]


def get_transcripts(memex: Path, project: str) -> list[Path]:
    """Get all transcripts for a project."""
    transcripts_dir = memex / "projects" / project / "transcripts"
    if not transcripts_dir.exists():
        return []

    # Return JSONL files (source for memo generation)
    return sorted(transcripts_dir.glob("*.jsonl"))


def get_existing_memos(memex: Path, project: str) -> set[str]:
    """Get session IDs that already have memos."""
    memos_dir = memex / "projects" / project / "memos"
    if not memos_dir.exists():
        return set()

    # Extract session IDs from memo filenames (YYYYMMDD-HHMMSS-sessionid.md)
    session_ids = set()
    for memo in memos_dir.glob("*.md"):
        parts = memo.stem.split("-")
        if len(parts) >= 3:
            # Session ID is everything after timestamp
            session_ids.add(parts[-1])

    return session_ids


def extract_session_id(transcript_path: Path) -> str:
    """Extract session ID from transcript filename."""
    # Format: YYYYMMDD-HHMMSS-sessionid.jsonl
    parts = transcript_path.stem.split("-")
    if len(parts) >= 3:
        return parts[-1]
    return transcript_path.stem


def generate_memo(transcript_path: Path, project: str) -> str | None:
    """Generate memo from transcript using Anthropic API."""
    # Extract curated content from transcript
    transcript_text = extract_for_memo(transcript_path)

    if not transcript_text.strip():
        log_warning(f"Empty transcript content: {transcript_path.name}")
        return None

    # Build API request
    session_id = extract_session_id(transcript_path)
    user_message = f"""Please generate a memo from this session transcript.

Project: {project}
Session ID: {session_id}

## Transcript

{transcript_text}

---

Generate a concise, useful memo following the format in your instructions."""

    # Call API with retry
    return call_api_with_retry(DEFAULT_MEMO_PROMPT, user_message)


def call_api_with_retry(system_prompt: str, user_message: str, max_retries: int = 3) -> str | None:
    """Call Anthropic API with retry logic."""
    for attempt in range(max_retries):
        try:
            client = get_anthropic_client()
            config = get_config()
            model = config.get("model", "claude-sonnet-4-20250514")

            response = client.messages.create(
                model=model,
                max_tokens=2000,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
                timeout=60,
            )

            return response.content[0].text

        except Exception as e:
            error_type = type(e).__name__

            if "RateLimitError" in error_type or "rate" in str(e).lower():
                wait = (2 ** attempt) + random.uniform(0, 1)
                log_warning(f"Rate limited, waiting {wait:.1f}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(wait)

            elif "overloaded" in str(e).lower():
                wait = 5 + random.uniform(0, 2)
                log_warning(f"API overloaded, waiting {wait:.1f}s")
                time.sleep(wait)

            else:
                log_error(f"API error: {e}")
                if attempt == max_retries - 1:
                    return None
                time.sleep(1)

    return None


def save_memo(content: str, memex: Path, project: str, session_id: str, transcript_path: Path) -> Path | None:
    """Save memo to project memos folder."""
    try:
        memos_dir = memex / "projects" / project / "memos"
        memos_dir.mkdir(parents=True, exist_ok=True)

        # Use transcript timestamp for memo filename
        # Format: YYYYMMDD-HHMMSS-sessionid.jsonl -> YYYYMMDD-HHMMSS-sessionid.md
        base_name = transcript_path.stem  # e.g., 20260121-141858-415a8b95
        memo_path = memos_dir / f"{base_name}.md"

        # Extract source_cwd from transcript if available (not critical)
        source_cwd = ""

        # Add/update frontmatter if not present
        if not content.startswith("---"):
            # Get timestamp from filename
            parts = base_name.split("-")
            date_str = f"{parts[0][:4]}-{parts[0][4:6]}-{parts[0][6:8]}" if len(parts) >= 2 else datetime.now().strftime("%Y-%m-%d")

            frontmatter = format_frontmatter({
                "type": "memo",
                "session_id": session_id,
                "project": project,
                "date": date_str,
                "created_at": datetime.now().isoformat(),
                "source_cwd": source_cwd,
            })
            content = f"{frontmatter}\n\n{content}"

        safe_write(memo_path, content)
        return memo_path

    except Exception as e:
        log_error(f"Failed to save memo: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(description="Batch generate memos from transcripts")
    parser.add_argument("projects", nargs="*", help="Project names (or use --all)")
    parser.add_argument("--all", action="store_true", help="Process all projects")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    parser.add_argument("--limit", type=int, default=0, help="Limit memos per project")
    parser.add_argument("--min-turns", type=int, default=5, help="Minimum turns for memo (default: 5)")
    args = parser.parse_args()

    memex = get_memex_path()

    # Determine projects to process
    if args.all:
        projects = get_projects(memex)
    elif args.projects:
        projects = args.projects
    else:
        print("Specify project names or use --all")
        print(f"Available: {', '.join(get_projects(memex))}")
        return

    print(f"Memex vault: {memex}")
    print(f"Projects: {', '.join(projects)}")
    if args.dry_run:
        print("DRY RUN - no memos will be generated")
    print()

    total_generated = 0
    total_skipped = 0
    total_errors = 0

    for project in projects:
        transcripts = get_transcripts(memex, project)
        existing = get_existing_memos(memex, project)

        print(f"\n=== {project} ===")
        print(f"Transcripts: {len(transcripts)}, Existing memos: {len(existing)}")

        project_generated = 0

        for transcript_path in transcripts:
            session_id = extract_session_id(transcript_path)

            # Skip if memo exists
            if session_id in existing:
                total_skipped += 1
                continue

            # Check transcript size (skip tiny sessions)
            messages, _ = parse_transcript_jsonl(transcript_path)
            if len(messages) < args.min_turns:
                print(f"  Skip {session_id[:8]}... ({len(messages)} turns, min {args.min_turns})")
                total_skipped += 1
                continue

            # Check limit
            if args.limit and project_generated >= args.limit:
                print(f"  Reached limit of {args.limit} for {project}")
                break

            if args.dry_run:
                print(f"  [DRY-RUN] Would generate memo for {session_id[:8]}... ({len(messages)} turns)")
                project_generated += 1
                total_generated += 1
                continue

            # Generate memo
            print(f"  Generating memo for {session_id[:8]}... ({len(messages)} turns)")

            memo_content = generate_memo(transcript_path, project)

            if not memo_content:
                print(f"    ERROR: Failed to generate")
                total_errors += 1
                continue

            # Save memo
            memo_path = save_memo(memo_content, memex, project, session_id, transcript_path)

            if memo_path:
                print(f"    Saved: {memo_path.name}")
                project_generated += 1
                total_generated += 1
            else:
                print(f"    ERROR: Failed to save")
                total_errors += 1

            # Small delay to avoid rate limiting
            time.sleep(0.5)

        print(f"  Generated: {project_generated}")

    print(f"\n=== Summary ===")
    print(f"Generated: {total_generated}")
    print(f"Skipped: {total_skipped}")
    print(f"Errors: {total_errors}")

    if total_generated > 0 and not args.dry_run:
        print(f"\nRun 'uv run scripts/index_rebuild.py --incremental' to update search index")


if __name__ == "__main__":
    main()
