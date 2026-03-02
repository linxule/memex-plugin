#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""
Graph Navigation Queries for the Memex Vault.

Provides functions to query the markdown graph structure:
- Backlinks and outlinks
- Broken link detection
- Task rollup
- Tag-based search
- Alias resolution
- Temporal activity queries

Usage:
    graph_queries.py stats                    # Show graph statistics
    graph_queries.py backlinks <path>         # Find backlinks to a document
    graph_queries.py tasks [--project] [--limit N]  # List open tasks
    graph_queries.py broken [--project]       # List broken links
    graph_queries.py tags <tag>               # Find docs by tag
    graph_queries.py recent [--days N]        # Recent activity
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


def get_memex_path() -> Path:
    """Get memex vault path with fallbacks.

    Resolution order:
    1. ~/.memex/config.json -> memex_path (user override)
    2. CLAUDE_PLUGIN_ROOT env var (set by plugin system)
    3. Script location fallback (assumes scripts are in memex/scripts/)
    """
    # Check user config first
    config_path = Path("~/.memex/config.json").expanduser()
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
            if config.get("memex_path"):
                path = Path(config["memex_path"]).expanduser().resolve()
                if path.exists():
                    return path
        except (json.JSONDecodeError, KeyError):
            pass

    # Check env var (set by plugin system)
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root:
        path = Path(plugin_root)
        if path.exists():
            return path

    # Fallback: script's parent directory (memex/scripts -> memex)
    return Path(__file__).parent.parent.resolve()


def get_backlinks(db_path: Path, doc_path: str) -> list[dict]:
    """
    Find all documents that link TO this document.

    Matches both:
    - target_path (resolved full path)
    - link_text (original text, for partial matches)
    """
    # Normalize doc_path for comparison
    doc_name = doc_path.replace('.md', '').split('/')[-1]

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        results = conn.execute("""
            SELECT DISTINCT source_path, link_text, display_text, line_number
            FROM wikilinks
            WHERE target_path = ?
               OR link_text = ?
               OR link_text = ?
            ORDER BY source_path, line_number
        """, (doc_path, doc_path, doc_name)).fetchall()

    return [dict(r) for r in results]


def get_outlinks(db_path: Path, doc_path: str) -> list[dict]:
    """Find all documents this document links TO."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        results = conn.execute("""
            SELECT target_path, link_text, display_text, is_broken, line_number
            FROM wikilinks
            WHERE source_path = ?
            ORDER BY line_number
        """, (doc_path,)).fetchall()

    return [dict(r) for r in results]


def get_broken_links(db_path: Path, project: Optional[str] = None) -> list[dict]:
    """Find all broken wikilinks in the vault."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        sql = """
            SELECT source_path, link_text, line_number
            FROM wikilinks
            WHERE is_broken = 1
        """
        params: list = []
        if project:
            sql += " AND source_path LIKE ?"
            params.append(f"projects/{project}/%")
        sql += " ORDER BY source_path, line_number"
        results = conn.execute(sql, params).fetchall()

    return [dict(r) for r in results]


def get_open_tasks(
    db_path: Path,
    project: Optional[str] = None,
    include_transcripts: bool = False,
    only_open_threads: bool = True,
    days: Optional[int] = 14
) -> list[dict]:
    """Get all incomplete tasks, optionally filtered by project.

    Args:
        db_path: Path to the index database
        project: Filter to specific project
        include_transcripts: If False (default), exclude tasks from transcripts
        only_open_threads: If True (default), only include tasks under "Open Threads" sections
        days: Only include tasks from memos within this many days (default 14, None for all)
    """
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        # Join with fts_content to get memo dates for filtering
        sql = """
            SELECT t.doc_path, t.task_text, t.section, t.line_number, f.date, f.project as memo_project
            FROM tasks t
            LEFT JOIN fts_content f ON t.doc_path = f.path
            WHERE t.completed = 0
        """
        params: list = []

        # Exclude transcripts by default (they contain conversation artifacts, not real tasks)
        if not include_transcripts:
            sql += " AND t.doc_path NOT LIKE '%/transcripts/%'"

        # Only include tasks from "Open Threads" sections (most reliable source)
        if only_open_threads:
            sql += " AND t.section = 'Open Threads'"

        # Time-based filter (default: last 14 days)
        if days is not None:
            cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
            sql += " AND (f.date >= ? OR f.date IS NULL)"
            params.append(cutoff)

        if project:
            sql += " AND t.doc_path LIKE ?"
            params.append(f"projects/{project}/%")
        sql += " ORDER BY f.date DESC, t.doc_path, t.line_number"
        results = conn.execute(sql, params).fetchall()

    return [dict(r) for r in results]


def get_all_tasks(db_path: Path, project: Optional[str] = None) -> list[dict]:
    """Get all tasks (open and completed), optionally filtered by project."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        sql = """
            SELECT doc_path, task_text, completed, section, line_number
            FROM tasks
        """
        params: list = []
        if project:
            sql += " WHERE doc_path LIKE ?"
            params.append(f"projects/{project}/%")
        sql += " ORDER BY doc_path, line_number"
        results = conn.execute(sql, params).fetchall()

    return [dict(r) for r in results]


def get_docs_by_tag(db_path: Path, tag: str) -> list[str]:
    """Find all documents with a specific tag."""
    with sqlite3.connect(db_path) as conn:
        results = conn.execute("""
            SELECT doc_path FROM doc_tags WHERE tag = ?
            ORDER BY doc_path
        """, (tag,)).fetchall()

    return [r[0] for r in results]


def get_all_tags(db_path: Path) -> list[tuple[str, int]]:
    """Get all tags with their usage count."""
    with sqlite3.connect(db_path) as conn:
        results = conn.execute("""
            SELECT tag, COUNT(*) as count
            FROM doc_tags
            GROUP BY tag
            ORDER BY count DESC, tag ASC
        """).fetchall()

    return [(r[0], r[1]) for r in results]


def resolve_alias(db_path: Path, alias: str) -> Optional[str]:
    """Resolve an alias to its document path."""
    with sqlite3.connect(db_path) as conn:
        result = conn.execute("""
            SELECT doc_path FROM doc_aliases WHERE alias = ?
        """, (alias,)).fetchone()

    return result[0] if result else None


def get_doc_sections(db_path: Path, doc_path: str) -> list[dict]:
    """Get the section structure of a document."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        results = conn.execute("""
            SELECT heading, level, line_number
            FROM sections
            WHERE doc_path = ?
            ORDER BY line_number
        """, (doc_path,)).fetchall()

    return [dict(r) for r in results]


def get_topic_connections(db_path: Path, topic_path: str) -> dict:
    """Get full connection info for a topic."""
    backlinks = get_backlinks(db_path, topic_path)
    outlinks = get_outlinks(db_path, topic_path)

    # Get tasks from documents that link to this topic
    related_tasks = []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        for bl in backlinks:
            tasks = conn.execute("""
                SELECT doc_path, task_text, section, line_number
                FROM tasks
                WHERE doc_path = ? AND completed = 0
            """, (bl['source_path'],)).fetchall()
            related_tasks.extend([dict(t) for t in tasks])

    return {
        'backlinks': backlinks,
        'outlinks': outlinks,
        'related_open_tasks': related_tasks,
    }


def get_cross_project_links(db_path: Path) -> list[dict]:
    """Find links that cross project boundaries."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        results = conn.execute("""
            SELECT
                source_path,
                target_path,
                link_text
            FROM wikilinks
            WHERE target_path IS NOT NULL
              AND source_path LIKE 'projects/%'
              AND target_path LIKE 'projects/%'
              AND substr(source_path, 10, instr(substr(source_path, 10), '/') - 1)
                  != substr(target_path, 10, instr(substr(target_path, 10), '/') - 1)
            ORDER BY source_path
        """).fetchall()

    return [dict(r) for r in results]


def get_graph_stats(db_path: Path) -> dict:
    """Get statistics about the markdown graph."""
    with sqlite3.connect(db_path) as conn:
        stats = {}

        # Wikilinks
        stats['total_links'] = conn.execute("SELECT COUNT(*) FROM wikilinks").fetchone()[0]
        stats['broken_links'] = conn.execute("SELECT COUNT(*) FROM wikilinks WHERE is_broken = 1").fetchone()[0]
        stats['unique_targets'] = conn.execute("SELECT COUNT(DISTINCT target_path) FROM wikilinks WHERE target_path IS NOT NULL").fetchone()[0]

        # Tasks (all)
        stats['total_tasks'] = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        stats['open_tasks'] = conn.execute("SELECT COUNT(*) FROM tasks WHERE completed = 0").fetchone()[0]
        stats['completed_tasks'] = conn.execute("SELECT COUNT(*) FROM tasks WHERE completed = 1").fetchone()[0]

        # Tasks (actionable = memos only, Open Threads section, last 14 days)
        cutoff_14d = (datetime.now() - timedelta(days=14)).strftime('%Y-%m-%d')
        stats['actionable_tasks'] = conn.execute("""
            SELECT COUNT(*) FROM tasks t
            LEFT JOIN fts_content f ON t.doc_path = f.path
            WHERE t.completed = 0
              AND t.doc_path NOT LIKE '%/transcripts/%'
              AND t.section = 'Open Threads'
              AND (f.date >= ? OR f.date IS NULL)
        """, (cutoff_14d,)).fetchone()[0]

        # Tags
        stats['unique_tags'] = conn.execute("SELECT COUNT(DISTINCT tag) FROM doc_tags").fetchone()[0]
        stats['tagged_docs'] = conn.execute("SELECT COUNT(DISTINCT doc_path) FROM doc_tags").fetchone()[0]

        # Aliases
        stats['total_aliases'] = conn.execute("SELECT COUNT(*) FROM doc_aliases").fetchone()[0]
        stats['docs_with_aliases'] = conn.execute("SELECT COUNT(DISTINCT doc_path) FROM doc_aliases").fetchone()[0]

        # Sections
        stats['total_sections'] = conn.execute("SELECT COUNT(*) FROM sections").fetchone()[0]
        stats['docs_with_sections'] = conn.execute("SELECT COUNT(DISTINCT doc_path) FROM sections").fetchone()[0]

    return stats


def get_recent_activity(db_path: Path, days: int = 7, project: Optional[str] = None) -> dict:
    """Get documents modified/created in recent days (temporal query)."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        # Build query with optional project filter
        # Note: fts_content is the FTS5 table created by search.py/index_rebuild.py
        sql = """
            SELECT path, title, type, project, date
            FROM fts_content
            WHERE date >= ?
        """
        params: list = [cutoff]
        if project:
            sql += " AND project = ?"
            params.append(project)
        sql += " ORDER BY date DESC"

        recent_docs = conn.execute(sql, params).fetchall()

        # Tasks from recent docs
        doc_paths = [r['path'] for r in recent_docs]
        recent_tasks = []
        if doc_paths:
            placeholders = ','.join('?' * len(doc_paths))
            recent_tasks = conn.execute(f"""
                SELECT doc_path, task_text, completed, section
                FROM tasks
                WHERE doc_path IN ({placeholders})
                ORDER BY doc_path, line_number
            """, doc_paths).fetchall()

    return {
        'recent_docs': [dict(r) for r in recent_docs],
        'recent_tasks': [dict(r) for r in recent_tasks],
        'period_days': days,
        'cutoff_date': cutoff,
        'project_filter': project
    }


def get_activity_by_project(db_path: Path, project: str, days: int = 30) -> dict:
    """Get project activity over time."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        docs = conn.execute("""
            SELECT path, title, type, date
            FROM fts_content
            WHERE project = ? AND date >= ?
            ORDER BY date DESC
        """, (project, cutoff)).fetchall()

        all_tasks = conn.execute("""
            SELECT t.task_text, t.completed, t.section, t.doc_path
            FROM tasks t
            WHERE t.doc_path LIKE ?
            ORDER BY t.doc_path, t.line_number
        """, (f"projects/{project}/%",)).fetchall()

    open_tasks = [dict(t) for t in all_tasks if not t['completed']]
    completed_tasks = [dict(t) for t in all_tasks if t['completed']]

    return {
        'project': project,
        'period_days': days,
        'docs': [dict(r) for r in docs],
        'total_tasks': len(all_tasks),
        'open_tasks': open_tasks,
        'completed_tasks': completed_tasks,
    }


def get_project_status(db_path: Path) -> list[dict]:
    """Get status overview for all projects."""
    cutoff_7d = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    cutoff_30d = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        # Get all projects
        projects = conn.execute("""
            SELECT DISTINCT project FROM fts_content
            WHERE project IS NOT NULL AND project != ''
            ORDER BY project
        """).fetchall()

        results = []
        for (project,) in projects:
            # Recent docs (7d)
            recent_7d = conn.execute("""
                SELECT COUNT(*) FROM fts_content
                WHERE project = ? AND date >= ?
            """, (project, cutoff_7d)).fetchone()[0]

            # Recent docs (30d)
            recent_30d = conn.execute("""
                SELECT COUNT(*) FROM fts_content
                WHERE project = ? AND date >= ?
            """, (project, cutoff_30d)).fetchone()[0]

            # Total memos
            memos = conn.execute("""
                SELECT COUNT(*) FROM fts_content
                WHERE project = ? AND type = 'memo'
            """, (project,)).fetchone()[0]

            # Total transcripts
            transcripts = conn.execute("""
                SELECT COUNT(*) FROM fts_content
                WHERE project = ? AND type = 'transcript'
            """, (project,)).fetchone()[0]

            # Open tasks (last 30d, memos only, Open Threads)
            tasks = conn.execute("""
                SELECT COUNT(*) FROM tasks t
                LEFT JOIN fts_content f ON t.doc_path = f.path
                WHERE f.project = ?
                  AND t.completed = 0
                  AND t.doc_path NOT LIKE '%/transcripts/%'
                  AND t.section = 'Open Threads'
                  AND f.date >= ?
            """, (project, cutoff_30d)).fetchone()[0]

            # Determine status
            if recent_7d > 0:
                status = 'active'
            elif recent_30d > 0:
                status = 'recent'
            elif memos > 0:
                status = 'dormant'
            else:
                status = 'empty'

            results.append({
                'project': project,
                'recent_7d': recent_7d,
                'recent_30d': recent_30d,
                'memos': memos,
                'transcripts': transcripts,
                'open_tasks': tasks,
                'status': status
            })

    return results


def get_orphan_docs(db_path: Path) -> list[str]:
    """Find documents with no inbound links (orphans)."""
    with sqlite3.connect(db_path) as conn:
        # Get all document paths
        all_docs = set(r[0] for r in conn.execute(
            "SELECT DISTINCT path FROM fts_content"
        ).fetchall())

        # Get all link targets
        linked_docs = set(r[0] for r in conn.execute(
            "SELECT DISTINCT target_path FROM wikilinks WHERE target_path IS NOT NULL"
        ).fetchall())

        # Orphans = all docs - linked docs
        orphans = all_docs - linked_docs

    return sorted(list(orphans))


# ============================================================================
# CLI
# ============================================================================

def main():
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Graph navigation queries")
    subparsers = parser.add_subparsers(dest='command', help='Commands')

    # Stats
    subparsers.add_parser('stats', help='Show graph statistics')

    # Backlinks
    backlinks_parser = subparsers.add_parser('backlinks', help='Show backlinks to a doc')
    backlinks_parser.add_argument('path', help='Document path')

    # Outlinks
    outlinks_parser = subparsers.add_parser('outlinks', help='Show outlinks from a doc')
    outlinks_parser.add_argument('path', help='Document path')

    # Tasks
    tasks_parser = subparsers.add_parser('tasks', help='List open tasks')
    tasks_parser.add_argument('--project', help='Filter by project')
    tasks_parser.add_argument('--all', action='store_true', help='Show completed tasks too')
    tasks_parser.add_argument('--limit', type=int, default=0, help='Limit number of tasks shown (0 = no limit)')
    tasks_parser.add_argument('--days', type=int, default=14,
                              help='Only show tasks from last N days (default: 14, use 0 for all time)')
    tasks_parser.add_argument('--include-transcripts', action='store_true',
                              help='Include tasks from transcripts (excluded by default)')
    tasks_parser.add_argument('--all-sections', action='store_true',
                              help='Include tasks from all sections (default: only "Open Threads")')

    # Broken links
    broken_parser = subparsers.add_parser('broken', help='List broken links')
    broken_parser.add_argument('--project', help='Filter by project')

    # Tags
    tags_parser = subparsers.add_parser('tags', help='Find docs by tag or list all tags')
    tags_parser.add_argument('tag', nargs='?', help='Tag to search for (omit to list all)')

    # Recent activity
    recent_parser = subparsers.add_parser('recent', help='Recent activity')
    recent_parser.add_argument('--days', type=int, default=7, help='Days to look back')
    recent_parser.add_argument('--project', help='Filter by project')

    # Orphans
    subparsers.add_parser('orphans', help='Find orphan documents')

    # Cross-project
    subparsers.add_parser('cross-project', help='Find cross-project links')

    # Sections
    sections_parser = subparsers.add_parser('sections', help='Show document sections')
    sections_parser.add_argument('path', help='Document path')

    # Project status
    status_parser = subparsers.add_parser('status', help='Show project status overview')
    status_parser.add_argument('--active-only', action='store_true',
                               help='Only show active projects (activity in last 7 days)')

    args = parser.parse_args()

    # Find database using consistent path resolution
    memex = get_memex_path()
    db_path = memex / "_index.sqlite"
    if not db_path.exists():
        print(f"Error: {db_path} not found. Run index rebuild first.", file=sys.stderr)
        sys.exit(1)

    if args.command == 'stats':
        stats = get_graph_stats(db_path)
        print("Graph Statistics:")
        print(f"  Wikilinks: {stats['total_links']} ({stats['broken_links']} broken)")
        print(f"  Unique targets: {stats['unique_targets']}")
        print(f"  Tasks: {stats['total_tasks']} ({stats['actionable_tasks']} actionable, {stats['open_tasks']} raw open, {stats['completed_tasks']} done)")
        print(f"  Tags: {stats['unique_tags']} unique across {stats['tagged_docs']} docs")
        print(f"  Aliases: {stats['total_aliases']} across {stats['docs_with_aliases']} docs")
        print(f"  Sections: {stats['total_sections']} across {stats['docs_with_sections']} docs")

    elif args.command == 'backlinks':
        links = get_backlinks(db_path, args.path)
        print(f"Backlinks to {args.path}:")
        for link in links:
            display = f" ({link['display_text']})" if link['display_text'] else ""
            print(f"  {link['source_path']}:{link['line_number']} [[{link['link_text']}]]{display}")
        if not links:
            print("  (no backlinks found)")

    elif args.command == 'outlinks':
        links = get_outlinks(db_path, args.path)
        print(f"Outlinks from {args.path}:")
        for link in links:
            status = " [BROKEN]" if link['is_broken'] else ""
            target = link['target_path'] or link['link_text']
            print(f"  :{link['line_number']} -> {target}{status}")
        if not links:
            print("  (no outlinks found)")

    elif args.command == 'tasks':
        if args.all:
            tasks = get_all_tasks(db_path, args.project)
        else:
            include_transcripts = getattr(args, 'include_transcripts', False)
            all_sections = getattr(args, 'all_sections', False)
            days = getattr(args, 'days', 14)
            days = None if days == 0 else days  # 0 means all time
            tasks = get_open_tasks(
                db_path,
                args.project,
                include_transcripts=include_transcripts,
                only_open_threads=not all_sections,
                days=days
            )

        title = "All tasks" if args.all else "Open tasks"
        if not args.all:
            days_val = getattr(args, 'days', 14)
            title += f" (last {days_val}d)" if days_val > 0 else " (all time)"
        if args.project:
            title += f" in {args.project}"

        total_tasks = len(tasks)
        if args.limit > 0:
            tasks = tasks[:args.limit]
            title += f" (showing {len(tasks)} of {total_tasks})"

        print(f"{title}:")

        current_doc = None
        for task in tasks:
            if task['doc_path'] != current_doc:
                current_doc = task['doc_path']
                print(f"\n  {current_doc}:")
            marker = 'x' if task.get('completed') else ' '
            section = f" [{task['section']}]" if task['section'] else ""
            print(f"    [{marker}] {task['task_text']}{section}")

        if not tasks:
            print("  (no tasks found)")

    elif args.command == 'broken':
        links = get_broken_links(db_path, args.project)
        title = "Broken links"
        if args.project:
            title += f" in {args.project}"
        print(f"{title}:")

        for link in links:
            print(f"  {link['source_path']}:{link['line_number']} -> [[{link['link_text']}]]")

        if not links:
            print("  (no broken links found)")

    elif args.command == 'tags':
        if args.tag:
            docs = get_docs_by_tag(db_path, args.tag)
            print(f"Documents tagged with '{args.tag}':")
            for doc in docs:
                print(f"  {doc}")
            if not docs:
                print("  (no documents found)")
        else:
            tags = get_all_tags(db_path)
            print("All tags:")
            for tag, count in tags:
                print(f"  {tag}: {count}")
            if not tags:
                print("  (no tags found)")

    elif args.command == 'recent':
        activity = get_recent_activity(db_path, args.days, args.project)
        title = f"Activity in last {args.days} days"
        if args.project:
            title += f" for {args.project}"
        print(f"{title}:")

        print(f"\n  Documents ({len(activity['recent_docs'])}):")
        for doc in activity['recent_docs']:
            print(f"    [{doc['date']}] {doc['path']}")

        open_tasks = [t for t in activity['recent_tasks'] if not t['completed']]
        print(f"\n  Open tasks ({len(open_tasks)}):")
        for task in open_tasks[:10]:  # Limit output
            print(f"    - {task['task_text'][:60]}...")
        if len(open_tasks) > 10:
            print(f"    ... and {len(open_tasks) - 10} more")

    elif args.command == 'orphans':
        orphans = get_orphan_docs(db_path)
        print("Orphan documents (no inbound links):")
        for doc in orphans:
            print(f"  {doc}")
        if not orphans:
            print("  (no orphans found)")

    elif args.command == 'cross-project':
        links = get_cross_project_links(db_path)
        print("Cross-project links:")
        for link in links:
            print(f"  {link['source_path']} -> {link['target_path']}")
        if not links:
            print("  (no cross-project links found)")

    elif args.command == 'sections':
        sections = get_doc_sections(db_path, args.path)
        print(f"Sections in {args.path}:")
        for section in sections:
            indent = "  " * section['level']
            print(f"  {indent}{'#' * section['level']} {section['heading']} (line {section['line_number']})")
        if not sections:
            print("  (no sections found)")

    elif args.command == 'status':
        projects = get_project_status(db_path)

        if args.active_only:
            projects = [p for p in projects if p['status'] == 'active']

        print("PROJECT STATUS OVERVIEW")
        print("=" * 75)
        print(f"{'Project':<25} {'7d':>5} {'30d':>5} {'Memos':>6} {'Trans':>6} {'Tasks':>6}  {'Status'}")
        print("-" * 75)

        for p in projects:
            print(f"{p['project']:<25} {p['recent_7d']:>5} {p['recent_30d']:>5} "
                  f"{p['memos']:>6} {p['transcripts']:>6} {p['open_tasks']:>6}  {p['status'].capitalize()}")

        print("-" * 75)
        print("7d/30d = docs in last 7/30 days | Tasks = open threads (last 30d)")

        # Summary
        active = len([p for p in projects if p['status'] == 'active'])
        total_tasks = sum(p['open_tasks'] for p in projects)
        print(f"\nSummary: {active} active projects, {total_tasks} open tasks")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
