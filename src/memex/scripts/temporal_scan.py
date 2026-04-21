"""
Temporal scan — filesystem-based date query for memex transcripts and memos.

Bypasses the SQLite index for date-based queries. Faster (~3-6ms) and catches
unindexed sessions that haven't been rebuilt into the index yet.

Usage:
    temporal_scan.py <date-expr> [options]

Examples:
    temporal_scan.py yesterday
    temporal_scan.py "last week" --project=alcor
    temporal_scan.py "last 5 days" --type=memo --format=text
    temporal_scan.py "2026-03-15" --mode=detail
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from memex.paths import get_memex_path
from memex.scripts.date_utils import parse_temporal_expression


# Regex patterns for date extraction from filenames
_DATE_COMPACT = re.compile(r'^(\d{4})(\d{2})(\d{2})')       # 20260128-...
_DATE_ISO = re.compile(r'^(\d{4})-(\d{2})-(\d{2})')          # 2026-01-28-...


def extract_date_from_filename(filename: str) -> date | None:
    """Extract date from transcript/memo filename.

    Handles:
        20260128-135914-73c12fe2.md  → 2026-01-28
        2026-02-21-memex-synthesis.md → 2026-02-21
        af7379a6-uuid.md             → None (no date in filename)
    """
    stem = Path(filename).stem

    m = _DATE_COMPACT.match(stem)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    m = _DATE_ISO.match(stem)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    return None


def _parse_frontmatter_fast(path: Path) -> dict:
    """Read frontmatter from first ~20 lines of a file. Minimal parsing."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = []
            for i, line in enumerate(f):
                lines.append(line)
                if i > 25:
                    break

        content = "".join(lines)
        if not content.startswith("---"):
            return {}

        try:
            end = content.index("---", 3)
        except ValueError:
            return {}

        result = {}
        for line in content[3:end].strip().split("\n"):
            if ":" in line:
                key, _, value = line.partition(":")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if value.startswith("[") and value.endswith("]"):
                    value = [v.strip().strip('"').strip("'") for v in value[1:-1].split(",") if v.strip()]
                result[key] = value

        return result
    except (OSError, UnicodeDecodeError):
        return {}


def scan_temporal(
    memex: Path,
    start: datetime,
    end: datetime,
    project: str | None = None,
    doc_type: str | None = None,
    mode: str = "list",
    limit: int = 50,
) -> list[dict]:
    """Scan filesystem for documents in a date range.

    Args:
        memex: Path to memex vault root
        start: Start of date range (inclusive)
        end: End of date range (exclusive)
        project: Optional project filter
        doc_type: "memo", "transcript", or None (both)
        mode: "list" (filename only) or "detail" (read frontmatter)
        limit: Max results

    Returns:
        List of dicts sorted by date descending.
    """
    start_date = start.date() if isinstance(start, datetime) else start
    end_date = end.date() if isinstance(end, datetime) else end

    results: list[dict] = []

    # Determine which directories to scan
    if project:
        proj_path = (memex / "projects" / project).resolve()
        try:
            proj_path.relative_to((memex / "projects").resolve())
        except ValueError:
            return []  # path traversal attempt
        project_dirs = [proj_path]
    else:
        projects_root = memex / "projects"
        if not projects_root.exists():
            return []
        project_dirs = [d for d in projects_root.iterdir() if d.is_dir()]

    # Determine which types to scan
    scan_types = []
    if doc_type is None or doc_type == "transcript":
        scan_types.append(("transcript", "transcripts"))
    if doc_type is None or doc_type == "memo":
        scan_types.append(("memo", "memos"))

    for proj_dir in project_dirs:
        proj_name = proj_dir.name

        for type_name, subdir_name in scan_types:
            subdir = proj_dir / subdir_name
            if not subdir.exists():
                continue

            for filepath in subdir.glob("*.md"):
                file_date = extract_date_from_filename(filepath.name)

                # Fall back to mtime for files without date in filename
                if file_date is None:
                    mtime = datetime.fromtimestamp(filepath.stat().st_mtime)
                    file_date = mtime.date()

                # Date range filter
                if file_date < start_date or file_date >= end_date:
                    continue

                entry: dict = {
                    "path": str(filepath.relative_to(memex)),
                    "date": file_date.isoformat(),
                    "project": proj_name,
                    "type": type_name,
                }

                # In detail mode, read frontmatter for richer info
                if mode == "detail":
                    fm = _parse_frontmatter_fast(filepath)
                    if type_name == "transcript":
                        entry["title"] = fm.get("title", filepath.stem)
                        entry["duration_minutes"] = fm.get("duration_minutes", "")
                        entry["turns"] = fm.get("turns", fm.get("total_messages", ""))
                        entry["has_memo"] = fm.get("has_memo", "")
                    elif type_name == "memo":
                        entry["title"] = fm.get("title", filepath.stem)
                        entry["topics"] = fm.get("topics", [])
                        entry["status"] = fm.get("status", "active")
                else:
                    entry["title"] = filepath.stem

                results.append(entry)

    # Sort by date descending, then by type (memos first)
    results.sort(key=lambda r: (r["date"], 0 if r["type"] == "memo" else 1), reverse=True)

    return results[:limit]


def format_timeline_text(results: list[dict], date_label: str = "") -> str:
    """Format scan results as human-readable timeline."""
    if not results:
        return f"No documents found for: {date_label}" if date_label else "No documents found."

    lines = []
    if date_label:
        lines.append(f"Timeline: {date_label}")
        lines.append("")

    # Group by type
    transcripts = [r for r in results if r["type"] == "transcript"]
    memos = [r for r in results if r["type"] == "memo"]

    if memos:
        lines.append(f"MEMOS ({len(memos)}):")
        for r in memos:
            title = r.get("title", r["path"].split("/")[-1])
            proj = r["project"]
            topics = r.get("topics", [])
            topic_str = f" [{', '.join(topics)}]" if topics and isinstance(topics, list) else ""
            lines.append(f"  [{r['date']}] {proj:<12s} | {title}{topic_str}")
        lines.append("")

    if transcripts:
        lines.append(f"TRANSCRIPTS ({len(transcripts)}):")
        for r in transcripts:
            proj = r["project"]
            dur = str(r.get("duration_minutes", ""))
            turns = str(r.get("turns", ""))
            title = r.get("title", r["path"].split("/")[-1])

            detail_parts = []
            if dur:
                detail_parts.append(f"{dur:>3s} min")
            if turns:
                detail_parts.append(f"{turns:>3s} turns")
            detail = ", ".join(detail_parts) if detail_parts else "—"

            lines.append(f"  [{r['date']}] {proj:<12s} | {detail:<18s} | {title}")
        lines.append("")

    lines.append(f"Total: {len(memos)} memo(s), {len(transcripts)} transcript(s)")
    return "\n".join(lines)


def _run() -> None:
    parser = argparse.ArgumentParser(
        description="Temporal scan — browse memex by date",
        epilog="Examples: temporal_scan.py yesterday, temporal_scan.py 'last week' --project=alcor",
    )
    parser.add_argument(
        "date_expr",
        nargs="+",
        help="Date expression: 'yesterday', 'last week', '3 days ago', '2026-03-15', etc.",
    )
    parser.add_argument("--project", type=str, help="Filter by project name")
    parser.add_argument(
        "--type",
        type=str,
        choices=["memo", "transcript"],
        dest="doc_type",
        help="Filter by document type",
    )
    parser.add_argument(
        "--mode",
        type=str,
        choices=["list", "detail"],
        default="detail",
        help="Output detail level (default: detail)",
    )
    parser.add_argument(
        "--format",
        type=str,
        choices=["json", "text", "paths"],
        default="text",
        dest="output_format",
        help="Output format (default: text)",
    )
    parser.add_argument("--limit", type=int, default=50, help="Max results (default: 50)")

    args = parser.parse_args()

    # Join multi-word date expressions
    date_expr = " ".join(args.date_expr)

    # Parse date expression
    parsed = parse_temporal_expression(date_expr)
    if parsed is None:
        print(f"Could not parse date expression: '{date_expr}'", file=sys.stderr)
        print("Try: yesterday, today, last week, 3 days ago, 2026-03-15", file=sys.stderr)
        sys.exit(1)

    start, end = parsed
    memex = get_memex_path()

    results = scan_temporal(
        memex=memex,
        start=start,
        end=end,
        project=args.project,
        doc_type=args.doc_type,
        mode=args.mode,
        limit=args.limit,
    )

    if args.output_format == "paths":
        for r in results:
            print(r["path"])
    elif args.output_format == "json":
        print(json.dumps(results, indent=2))
    else:
        print(format_timeline_text(results, date_label=date_expr))


def main():
    try:
        _run()
    except SystemExit:
        raise
    except FileNotFoundError as exc:
        print(f"Error: {exc}\nFix: Verify the referenced file exists and rerun the command.", file=sys.stderr)
        sys.exit(2)
    except sqlite3.OperationalError as exc:
        print(f"Error: {exc}\nFix: memex index rebuild --full", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
