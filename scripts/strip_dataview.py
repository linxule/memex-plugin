#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///

"""
Strip Dataview blocks from topic files.

Removes standard template sections that contain ONLY Dataview blocks:
- "## Memos Referencing This Topic" (always remove)
- "## Open Threads" (only if contains ONLY dataview TASK block)
- "## Connected Topics" (only if contains ONLY dataview block)
- "## Recent Transcripts" (always remove if found)

Also cleans up double blank lines left by removal.
"""

import argparse
import re
from pathlib import Path
from typing import List, Tuple


def find_section_bounds(lines: List[str], section_heading: str) -> Tuple[int, int]:
    """
    Find start and end line indices for a section.
    Returns (start_idx, end_idx) or (-1, -1) if not found.
    End index is exclusive (points to next section or EOF).
    """
    start_idx = -1

    # Find section start
    for i, line in enumerate(lines):
        if line.strip() == section_heading:
            start_idx = i
            break

    if start_idx == -1:
        return (-1, -1)

    # Find section end (next ## heading or EOF)
    end_idx = len(lines)
    for i in range(start_idx + 1, len(lines)):
        if lines[i].startswith('## '):
            end_idx = i
            break

    return (start_idx, end_idx)


def section_contains_only_dataview(lines: List[str], start_idx: int, end_idx: int) -> bool:
    """
    Check if section contains ONLY:
    - The heading
    - Blank lines
    - A ```dataview block

    Returns True if section should be removed.
    """
    in_dataview = False
    has_dataview = False
    has_other_content = False

    for i in range(start_idx + 1, end_idx):
        line = lines[i].strip()

        if line.startswith('```dataview'):
            in_dataview = True
            has_dataview = True
        elif line == '```' and in_dataview:
            in_dataview = False
        elif line == '':
            continue
        elif not in_dataview:
            # Non-blank, non-dataview content
            has_other_content = True
            break

    return has_dataview and not has_other_content


def section_contains_only_dataview_task(lines: List[str], start_idx: int, end_idx: int) -> bool:
    """
    Check if "## Open Threads" section contains ONLY a dataview TASK block.
    If it has real task items (- [ ] ...), return False (preserve section).
    """
    in_dataview = False
    has_dataview_task = False
    has_real_tasks = False

    for i in range(start_idx + 1, end_idx):
        line = lines[i].strip()

        if line.startswith('```dataview'):
            in_dataview = True
            # Check if it's a TASK query
            for j in range(i, min(i + 3, end_idx)):
                if 'TASK' in lines[j]:
                    has_dataview_task = True
                    break
        elif line == '```' and in_dataview:
            in_dataview = False
        elif line.startswith('- [ ]') or line.startswith('- [x]'):
            # Real task item found
            has_real_tasks = True
            break
        elif line == '':
            continue
        elif not in_dataview:
            # Other non-blank content (could be explanatory text)
            # Don't count as "real tasks" but also don't auto-remove
            if not line.startswith('```'):
                has_real_tasks = True
                break

    # Remove section only if it has ONLY dataview TASK and no real tasks
    return has_dataview_task and not has_real_tasks


def clean_double_blank_lines(content: str) -> str:
    """Replace 3+ consecutive blank lines with just 2."""
    # Replace 3+ newlines with exactly 2
    return re.sub(r'\n{3,}', '\n\n', content)


def strip_dataview_from_file(filepath: Path, dry_run: bool = False) -> Tuple[bool, List[str]]:
    """
    Strip Dataview blocks from a single file.

    Returns (changed, messages) where:
    - changed: True if file would be/was modified
    - messages: List of change descriptions
    """
    try:
        content = filepath.read_text()
        lines = content.split('\n')
        original_lines = lines.copy()
        messages = []

        # Track sections to remove (in reverse order to avoid index shifts)
        sections_to_remove = []

        # Check "## Memos Referencing This Topic"
        start, end = find_section_bounds(lines, "## Memos Referencing This Topic")
        if start != -1 and section_contains_only_dataview(lines, start, end):
            sections_to_remove.append((start, end, "Memos Referencing This Topic"))

        # Check "## Recent Transcripts" (if present, always remove)
        start, end = find_section_bounds(lines, "## Recent Transcripts (Last 7 Days)")
        if start != -1 and section_contains_only_dataview(lines, start, end):
            sections_to_remove.append((start, end, "Recent Transcripts"))

        # Check "## Open Threads" (only if ONLY dataview TASK)
        start, end = find_section_bounds(lines, "## Open Threads")
        if start != -1 and section_contains_only_dataview_task(lines, start, end):
            sections_to_remove.append((start, end, "Open Threads"))

        # Check "## Connected Topics"
        start, end = find_section_bounds(lines, "## Connected Topics")
        if start != -1 and section_contains_only_dataview(lines, start, end):
            sections_to_remove.append((start, end, "Connected Topics"))

        if not sections_to_remove:
            return (False, [])

        # Remove sections in reverse order (to avoid index shifts)
        sections_to_remove.sort(reverse=True)
        for start, end, name in sections_to_remove:
            del lines[start:end]
            messages.append(f"  - Removed '## {name}' section")

        # Reconstruct content
        new_content = '\n'.join(lines)
        new_content = clean_double_blank_lines(new_content)

        # Check if actually changed
        changed = new_content != content

        if changed and not dry_run:
            filepath.write_text(new_content)

        return (changed, messages)

    except Exception as e:
        return (False, [f"  ERROR: {e}"])


def main():
    parser = argparse.ArgumentParser(
        description="Strip Dataview blocks from topic files"
    )
    parser.add_argument(
        'directory',
        type=Path,
        help='Directory to process (e.g., /path/to/memex/topics)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Report changes without modifying files'
    )

    args = parser.parse_args()

    if not args.directory.exists():
        print(f"ERROR: Directory does not exist: {args.directory}")
        return 1

    if not args.directory.is_dir():
        print(f"ERROR: Not a directory: {args.directory}")
        return 1

    print(f"{'DRY RUN: ' if args.dry_run else ''}Processing {args.directory}")
    print()

    # Find all .md files
    md_files = sorted(args.directory.glob('*.md'))

    if not md_files:
        print("No .md files found")
        return 0

    total_changed = 0

    for filepath in md_files:
        changed, messages = strip_dataview_from_file(filepath, dry_run=args.dry_run)

        if changed:
            total_changed += 1
            print(f"{filepath.name}:")
            for msg in messages:
                print(msg)
            print()

    print(f"\n{'Would change' if args.dry_run else 'Changed'} {total_changed}/{len(md_files)} files")

    return 0


if __name__ == '__main__':
    exit(main())
