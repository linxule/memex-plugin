# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "filelock>=3.0",
#     "tiktoken>=0.5",
# ]
# ///
"""
Backfill token usage stats into existing transcript frontmatter.

Reads the JSONL source to extract input_tokens, output_tokens, and
cache_read_input_tokens from assistant messages, then patches the
markdown frontmatter without touching the body content.

Usage:
    uv run scripts/backfill_tokens.py              # dry-run (default)
    uv run scripts/backfill_tokens.py --apply      # actually patch files
    uv run scripts/backfill_tokens.py --apply -v   # verbose with details
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from utils import get_memex_path, log_info


def extract_token_usage(jsonl_path: Path) -> dict | None:
    """Extract token usage from JSONL without full parse.

    Only parses assistant messages for the usage block.
    Returns dict with input_tokens, output_tokens, cache_read_tokens
    or None if no usage data found.
    """
    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0

    try:
        with open(jsonl_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                # Quick string check before JSON parse
                if '"usage"' not in line:
                    continue
                if '"type":"assistant"' not in line and '"type": "assistant"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("type") != "assistant":
                        continue
                    msg = entry.get("message", {})
                    if not isinstance(msg, dict):
                        continue
                    usage = msg.get("usage", {})
                    if not isinstance(usage, dict):
                        continue
                    input_tokens += usage.get("input_tokens", 0)
                    output_tokens += usage.get("output_tokens", 0)
                    cache_read_tokens += usage.get("cache_read_input_tokens", 0)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return None

    if not input_tokens and not output_tokens:
        return None

    return {
        "input_tokens": input_tokens + cache_read_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
    }


def patch_frontmatter(md_path: Path, token_data: dict, dry_run: bool = True) -> bool:
    """Inject token fields into existing markdown frontmatter.

    Inserts after the last existing frontmatter field, before the closing ---.
    Does NOT modify body content.

    Returns True if file was modified (or would be in dry-run).
    """
    content = md_path.read_text()

    # Already has token data?
    if "input_tokens:" in content[:1000]:
        return False

    # Find frontmatter boundaries
    if not content.startswith("---"):
        return False
    end_idx = content.index("---", 3)
    frontmatter = content[3:end_idx].strip()
    body = content[end_idx:]  # includes closing ---

    # Build new frontmatter lines
    new_lines = []
    for key in ("input_tokens", "output_tokens", "cache_read_tokens"):
        if key in token_data and token_data[key]:
            new_lines.append(f"{key}: {token_data[key]}")

    if not new_lines:
        return False

    # Insert token lines at end of frontmatter
    new_frontmatter = frontmatter + "\n" + "\n".join(new_lines)
    new_content = "---\n" + new_frontmatter + "\n" + body

    if not dry_run:
        md_path.write_text(new_content)

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Backfill token usage into transcript frontmatter"
    )
    parser.add_argument("--apply", action="store_true",
                        help="Actually modify files (default: dry-run)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show per-file details")
    args = parser.parse_args()

    memex = get_memex_path()
    dry_run = not args.apply

    patched = 0
    skipped_no_jsonl = 0
    skipped_no_usage = 0
    skipped_already = 0
    errors = 0

    for md_path in sorted(memex.glob("projects/*/transcripts/*.md")):
        jsonl_path = md_path.with_suffix(".jsonl")

        if not jsonl_path.exists():
            skipped_no_jsonl += 1
            continue

        # Already has token data?
        with open(md_path) as f:
            head = f.read(1000)
        if "input_tokens:" in head:
            skipped_already += 1
            continue

        # Extract tokens from JSONL
        token_data = extract_token_usage(jsonl_path)
        if not token_data:
            skipped_no_usage += 1
            if args.verbose:
                print(f"  no usage: {md_path.name}")
            continue

        # Patch frontmatter
        try:
            modified = patch_frontmatter(md_path, token_data, dry_run=dry_run)
            if modified:
                patched += 1
                if args.verbose:
                    total = token_data["input_tokens"] + token_data["output_tokens"]
                    print(f"  {'would patch' if dry_run else 'patched':>12}: "
                          f"{md_path.name}  ({total:,} tokens)")
        except Exception as e:
            errors += 1
            print(f"  ERROR: {md_path.name}: {e}", file=sys.stderr)

    # Summary
    action = "Would patch" if dry_run else "Patched"
    print(f"\n{action}: {patched}")
    print(f"Already had tokens: {skipped_already}")
    print(f"No JSONL source: {skipped_no_jsonl}")
    print(f"No usage in JSONL: {skipped_no_usage}")
    if errors:
        print(f"Errors: {errors}")

    if dry_run and patched > 0:
        print(f"\nRun with --apply to write changes.")


if __name__ == "__main__":
    main()
