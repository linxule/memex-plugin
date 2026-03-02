#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""
Stress test for transcript_to_md.py pipeline.

Runs convert_transcript_file on a batch of real JSONL transcripts and
reports: crashes, empty output, noise leakage, unexpected patterns.

Usage:
    uv run scripts/stress_test_transcripts.py [--limit N] [--size SIZE] [--verbose]

    SIZE: tiny (<1KB), small (1-10KB), medium (10KB-1MB), large (1-10MB), huge (10MB+), all
"""

import argparse
import json
import os
import re
import sys
import tempfile
import time
import traceback
from pathlib import Path

# Add scripts dir to path
scripts_dir = Path(__file__).parent
sys.path.insert(0, str(scripts_dir))

from transcript_to_md import convert_transcript_file, parse_transcript_jsonl


# Noise patterns that should NOT appear in output
# Only check for system tags — JSON-level patterns cause false positives
# inside tool result content (truncated agent output contains raw JSON)
NOISE_PATTERNS = [
    (r'<system-reminder>',         "system-reminder tag leaked"),
    (r'<local-command-caveat>',    "local-command-caveat tag leaked"),
    (r'<local-command-stdout>',    "local-command-stdout tag leaked"),
    (r'<command-name>',            "command-name tag leaked"),
    (r'<command-message>',         "command-message tag leaked"),
    (r'<command-args>',            "command-args tag leaked"),
    (r'Loading updated app package', "Obsidian CLI loading line leaked"),
]

# Compiled noise regex
NOISE_RES = [(re.compile(p), desc) for p, desc in NOISE_PATTERNS]

# Skill expansion that should have been compressed
SKILL_LEAK_RE = re.compile(
    r'^## Instructions\n|^## Path Resolution|^ARGUMENTS:\s',
    re.MULTILINE
)


def categorize_size(path: Path) -> str:
    """Categorize transcript by file size."""
    size = path.stat().st_size
    if size < 1024:
        return "tiny"
    elif size < 10240:
        return "small"
    elif size < 1048576:
        return "medium"
    elif size < 10485760:
        return "large"
    else:
        return "huge"


def find_transcripts(base: Path, size_filter: str = "all", limit: int = 0) -> list[Path]:
    """Find JSONL transcripts matching size filter."""
    transcripts = sorted(base.rglob("projects/*/transcripts/*.jsonl"))

    if size_filter != "all":
        transcripts = [t for t in transcripts if categorize_size(t) == size_filter]

    if limit > 0:
        transcripts = transcripts[:limit]

    return transcripts


def check_noise(md_content: str) -> list[str]:
    """Check markdown output for noise that should have been filtered."""
    issues = []

    for pattern, desc in NOISE_RES:
        matches = pattern.findall(md_content)
        if matches:
            issues.append(f"  NOISE: {desc} ({len(matches)} occurrences)")

    # Check for uncompressed skill expansions
    if SKILL_LEAK_RE.search(md_content):
        # Only flag if it looks like a skill prompt, not user discussing skills
        # Look for the pattern within a User turn section
        lines = md_content.split('\n')
        in_user_section = False
        for i, line in enumerate(lines):
            if line.strip() == '### User':
                in_user_section = True
            elif line.startswith('### ') or line.startswith('## Turn'):
                in_user_section = False
            if in_user_section and ('## Instructions' in line or 'ARGUMENTS:' in line):
                issues.append(f"  NOISE: Uncompressed skill expansion at line {i+1}")
                break

    return issues


def check_structure(md_content: str, jsonl_path: Path) -> list[str]:
    """Check markdown output for structural issues."""
    issues = []

    if not md_content.strip():
        issues.append("  STRUCTURE: Empty output")
        return issues

    # Check frontmatter
    if not md_content.startswith('---'):
        issues.append("  STRUCTURE: Missing frontmatter")

    # Check for turns
    turn_count = len(re.findall(r'^## Turn \d+', md_content, re.MULTILINE))
    if turn_count == 0 and len(md_content) > 200:
        issues.append("  STRUCTURE: No turns found in non-trivial output")

    # Check for extremely long lines (possible untruncated output)
    for i, line in enumerate(md_content.split('\n'), 1):
        if len(line) > 5000:
            issues.append(f"  STRUCTURE: Very long line ({len(line)} chars) at line {i}")
            break  # Report first only

    return issues


def check_metadata(md_content: str, jsonl_path: Path) -> dict:
    """Extract metadata about new pipeline features for reporting.

    Returns a dict of feature counts (not issues — these are informational).
    """
    meta = {
        "has_start_time": False,
        "has_duration": False,
        "has_models": False,
        "has_subagents": False,
        "has_commits": False,
        "compaction_markers": 0,
        "subagent_markers": 0,
        "tool_durations": 0,
    }

    # Check frontmatter fields (between first and second ---)
    fm_match = re.match(r'^---\n(.*?)\n---', md_content, re.DOTALL)
    if fm_match:
        fm = fm_match.group(1)
        meta["has_start_time"] = "start_time:" in fm
        meta["has_duration"] = "duration_minutes:" in fm
        meta["has_models"] = "models:" in fm
        meta["has_subagents"] = "subagents:" in fm
        meta["has_commits"] = "commits:" in fm

    # Count markers in body
    meta["compaction_markers"] = len(re.findall(
        r'\[Session compacted', md_content))
    meta["subagent_markers"] = len(re.findall(
        r'\[Subagent: [a-f0-9]{7}\]', md_content))
    meta["tool_durations"] = len(re.findall(
        r'#### Tool: \w+ \(\d+\.\d+[sm]\)', md_content))

    return meta


def cross_validate_metadata(md_content: str, jsonl_path: Path) -> list[str]:
    """Cross-validate pipeline output against raw JSONL for consistency."""
    issues = []

    try:
        with open(jsonl_path) as f:
            lines = [l for l in f if l.strip()]

        has_compact_in_jsonl = any(
            '"isCompactSummary"' in l or
            '"This session is being continued from a previous conversation"' in l
            for l in lines
        )
        has_agents_in_jsonl = any('"agentId"' in l for l in lines)

        compact_markers = len(re.findall(r'\[Session compacted', md_content))
        agent_markers = len(re.findall(r'\[Subagent:', md_content))

        if has_compact_in_jsonl and compact_markers == 0:
            issues.append("  VALIDATE: JSONL has compaction but no [Session compacted] marker in output")
        if has_agents_in_jsonl and agent_markers == 0:
            # Not always an issue — agentId might only be on the main agent
            pass  # Informational only

    except Exception:
        pass

    return issues


def run_test(jsonl_path: Path, verbose: bool = False) -> dict:
    """Run pipeline on a single transcript, return results."""
    result = {
        "path": str(jsonl_path),
        "size": jsonl_path.stat().st_size,
        "size_category": categorize_size(jsonl_path),
        "lines": 0,
        "success": False,
        "issues": [],
        "output_size": 0,
        "turns": 0,
        "elapsed_ms": 0,
        "metadata": {},
    }

    # Count lines
    try:
        with open(jsonl_path) as f:
            result["lines"] = sum(1 for _ in f)
    except Exception:
        pass

    # Run conversion
    with tempfile.NamedTemporaryFile(suffix='.md', delete=False, mode='w') as tmp:
        tmp_path = Path(tmp.name)

    try:
        start = time.monotonic()
        convert_transcript_file(
            jsonl_path=jsonl_path,
            output_path=tmp_path,
            session_id=jsonl_path.stem,
            project=jsonl_path.parts[-3] if len(jsonl_path.parts) >= 3 else "unknown",
        )
        result["elapsed_ms"] = int((time.monotonic() - start) * 1000)
        result["success"] = True

        # Read output and check
        md_content = tmp_path.read_text(encoding='utf-8')
        result["output_size"] = len(md_content)
        result["turns"] = len(re.findall(r'^## Turn \d+', md_content, re.MULTILINE))

        # Check for noise
        result["issues"].extend(check_noise(md_content))

        # Check structure
        result["issues"].extend(check_structure(md_content, jsonl_path))

        # Cross-validate new features against JSONL
        result["issues"].extend(cross_validate_metadata(md_content, jsonl_path))

        # Collect metadata about new features (informational)
        result["metadata"] = check_metadata(md_content, jsonl_path)

    except Exception as e:
        result["issues"].append(f"  CRASH: {type(e).__name__}: {e}")
        if verbose:
            result["issues"].append(f"  TRACEBACK:\n{traceback.format_exc()}")
    finally:
        tmp_path.unlink(missing_ok=True)

    return result


def main():
    parser = argparse.ArgumentParser(description="Stress test transcript pipeline")
    parser.add_argument("--limit", type=int, default=0, help="Max transcripts to test (0=all)")
    parser.add_argument("--size", default="all", choices=["tiny", "small", "medium", "large", "huge", "all"])
    parser.add_argument("--verbose", action="store_true", help="Show full tracebacks")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("paths", nargs="*", help="Specific JSONL paths to test")
    args = parser.parse_args()

    memex = Path(__file__).parent.parent

    if args.paths:
        transcripts = [Path(p) for p in args.paths]
    else:
        transcripts = find_transcripts(memex, args.size, args.limit)

    if not transcripts:
        print("No transcripts found.", file=sys.stderr)
        sys.exit(1)

    print(f"Testing {len(transcripts)} transcripts (filter: {args.size})...\n")

    results = []
    issues_count = 0
    crashes = 0

    for i, t in enumerate(transcripts, 1):
        result = run_test(t, verbose=args.verbose)
        results.append(result)

        has_issues = len(result["issues"]) > 0
        is_crash = any("CRASH" in issue for issue in result["issues"])

        if is_crash:
            crashes += 1
        if has_issues:
            issues_count += 1

        # Progress line
        status = "CRASH" if is_crash else ("ISSUES" if has_issues else "OK")
        size_h = f"{result['size']/1024:.0f}KB" if result['size'] < 1048576 else f"{result['size']/1048576:.1f}MB"
        print(f"[{i}/{len(transcripts)}] {status:6s} {size_h:>8s} {result['lines']:>5d}L → {result['turns']:>3d} turns  {result['elapsed_ms']:>5d}ms  {t.name}")

        if has_issues:
            for issue in result["issues"]:
                print(issue)

    # Summary
    print(f"\n{'='*60}")
    print(f"SUMMARY: {len(transcripts)} tested, {issues_count} with issues, {crashes} crashes")

    if issues_count > 0:
        print(f"\nIssue breakdown:")
        issue_types = {}
        for r in results:
            for issue in r["issues"]:
                # Extract category (NOISE/STRUCTURE/CRASH)
                cat = issue.strip().split(":")[0]
                issue_types[cat] = issue_types.get(cat, 0) + 1
        for cat, count in sorted(issue_types.items(), key=lambda x: -x[1]):
            print(f"  {cat}: {count}")

    # Size distribution
    print(f"\nSize distribution:")
    for cat in ["tiny", "small", "medium", "large", "huge"]:
        cat_results = [r for r in results if r["size_category"] == cat]
        if cat_results:
            ok = sum(1 for r in cat_results if not r["issues"])
            avg_ms = sum(r["elapsed_ms"] for r in cat_results) // len(cat_results)
            print(f"  {cat:8s}: {len(cat_results):3d} tested, {ok:3d} OK, avg {avg_ms}ms")

    # Feature coverage (new pipeline features)
    successful = [r for r in results if r["success"] and r["output_size"] > 0]
    if successful:
        print(f"\nFeature coverage ({len(successful)} non-empty outputs):")
        fm_fields = {
            "has_start_time": "start_time",
            "has_duration": "duration_minutes",
            "has_models": "models",
            "has_subagents": "subagents",
            "has_commits": "commits",
        }
        for key, label in fm_fields.items():
            count = sum(1 for r in successful if r.get("metadata", {}).get(key))
            print(f"  {label:20s}: {count:3d}/{len(successful)} transcripts")
        markers = {
            "compaction_markers": "[Session compacted]",
            "subagent_markers": "[Subagent: ...]",
            "tool_durations": "Tool durations",
        }
        for key, label in markers.items():
            total = sum(r.get("metadata", {}).get(key, 0) for r in successful)
            files = sum(1 for r in successful if r.get("metadata", {}).get(key, 0) > 0)
            print(f"  {label:20s}: {total:3d} total in {files} files")

    if args.json:
        print(f"\n--- JSON RESULTS ---")
        print(json.dumps(results, indent=2))

    sys.exit(1 if crashes > 0 else 0)


if __name__ == "__main__":
    main()
