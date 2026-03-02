#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Strip wikilink syntax from frontmatter topics fields.

Changes:
  topics: ["[[topic-name]]"]  →  topics: [topic-name]
  topics: ["[[?suggested]]"]  →  topics: [suggested]

Plain string topics are left unchanged.
Frontmatter `tags:` field is not touched.

Usage:
  uv run scripts/fix_frontmatter_topics.py              # dry run
  uv run scripts/fix_frontmatter_topics.py --apply       # apply changes
"""
import json
import os
import re
import sys
from pathlib import Path


def _get_vault() -> Path:
    """Resolve vault path: config.json → env var → script location fallback."""
    config_path = Path.home() / ".memex" / "config.json"
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
            if "memex_path" in config:
                return Path(config["memex_path"]).resolve()
        except (json.JSONDecodeError, KeyError):
            pass
    env_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env_root:
        return Path(env_root).resolve()
    return Path(__file__).parent.parent.resolve()


VAULT = _get_vault()

def fix_frontmatter_topics(content: str) -> tuple[str, list[str]]:
    """Fix wikilink syntax in frontmatter topics. Returns (new_content, changes)."""
    # Split frontmatter from body
    if not content.startswith('---'):
        return content, []

    parts = content.split('---', 2)
    if len(parts) < 3:
        return content, []

    frontmatter = parts[1]
    body = parts[2]
    changes = []

    # Find and fix topic lines with wikilinks
    lines = frontmatter.split('\n')
    new_lines = []
    in_topics = False

    for line in lines:
        if line.strip() == 'topics:':
            in_topics = True
            new_lines.append(line)
            continue

        if in_topics:
            # Check if still in topics list (indented with -)
            if re.match(r'\s+-\s', line):
                # Strip [[...]] and [[?...]] from topic values
                original = line
                # Handle: - "[[?topic]]" or - "[[topic]]"
                line = re.sub(r'"?\[\[\??(.*?)\]\]"?', r'\1', line)
                # Handle: - '[[?topic]]' or - '[[topic]]'
                line = re.sub(r"'?\[\[\??(.*?)\]\]'?", r'\1', line)
                if line != original:
                    old_val = re.search(r'-\s+(.+)', original)
                    new_val = re.search(r'-\s+(.+)', line)
                    if old_val and new_val:
                        changes.append(f"  {old_val.group(1).strip()} → {new_val.group(1).strip()}")
                new_lines.append(line)
            else:
                in_topics = False
                new_lines.append(line)
        else:
            new_lines.append(line)

    new_frontmatter = '\n'.join(new_lines)
    return f'---{new_frontmatter}---{body}', changes


def main():
    apply = '--apply' in sys.argv

    # Find all markdown files in projects/
    memo_files = sorted(VAULT.glob('projects/*/memos/*.md'))
    topic_files = sorted(VAULT.glob('topics/*.md'))
    overview_files = sorted(VAULT.glob('projects/*/_project.md'))

    all_files = memo_files + topic_files + overview_files

    total_changes = 0
    files_changed = 0

    for fpath in all_files:
        content = fpath.read_text()
        new_content, changes = fix_frontmatter_topics(content)

        if changes:
            files_changed += 1
            total_changes += len(changes)
            rel = fpath.relative_to(VAULT)
            print(f"\n{rel}:")
            for c in changes:
                print(f"  {c}")

            if apply:
                fpath.write_text(new_content)

    print(f"\n{'=' * 40}")
    print(f"Files: {files_changed} changed out of {len(all_files)} scanned")
    print(f"Topics fixed: {total_changes}")
    if not apply:
        print("\nDry run. Use --apply to write changes.")
    else:
        print("\nChanges applied.")


if __name__ == '__main__':
    main()
