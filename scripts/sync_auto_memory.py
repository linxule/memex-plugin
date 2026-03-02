#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "filelock>=3.0",
# ]
# ///
"""
Sync Claude Code auto-memory files into the memex vault.

Scans ~/.claude/projects/*/memory/*.md for auto-memory files and imports
them into projects/<name>/auto-memory/ with frontmatter and source tracking.
Re-syncs detect source changes via content hash while preserving hand-added
Vault Annotations (wikilinks, related notes).

Usage:
    uv run scripts/sync_auto_memory.py --discover          # list auto-memory files + coverage
    uv run scripts/sync_auto_memory.py --sync               # dry-run: show what would change
    uv run scripts/sync_auto_memory.py --sync --apply       # write files
    uv run scripts/sync_auto_memory.py --sync --apply -v    # verbose
    uv run scripts/sync_auto_memory.py --status             # fresh/stale/new/orphaned counts
    uv run scripts/sync_auto_memory.py --project=my-app      # filter to project
    uv run scripts/sync_auto_memory.py --json               # machine-readable output
"""

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

try:
    from .utils import (
        claude_dir_to_project_name, sanitize_project_name,
        get_memex_path, get_config, log_info, log_warning, log_error,
    )
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent))
    from utils import (
        claude_dir_to_project_name, sanitize_project_name,
        get_memex_path, get_config, log_info, log_warning, log_error,
    )


CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
ANNOTATION_MARKER = "## Vault Annotations"


# ============================================================================
# Discovery
# ============================================================================

def content_hash(content: str) -> str:
    """SHA-256 hash of content."""
    return hashlib.sha256(content.encode()).hexdigest()


def extract_title(content: str, filename: str) -> str:
    """Extract title from first # heading or filename."""
    for line in content.split("\n", 20):
        line = line.strip()
        if line.startswith("# ") and not line.startswith("# ///"):
            return line[2:].strip()
    return filename.replace("-", " ").replace("_", " ").removesuffix(".md").title()


def discover_auto_memory(project_filter: str | None = None) -> list[dict]:
    """Scan ~/.claude/projects/*/memory/*.md for auto-memory files.

    Returns list of dicts with source metadata.
    """
    if not PROJECTS_DIR.exists():
        return []

    config = get_config()
    am_config = config.get("auto_memory", {})
    if not am_config.get("enabled", True):
        return []

    exclude = set(am_config.get("exclude_projects", []))
    sync_volatile = am_config.get("sync_volatile", True)

    results = []
    for project_dir in sorted(PROJECTS_DIR.iterdir()):
        if not project_dir.is_dir():
            continue

        if project_dir.name in exclude:
            continue

        memory_dir = project_dir / "memory"
        if not memory_dir.exists() or not memory_dir.is_dir():
            continue

        display_name = claude_dir_to_project_name(project_dir.name)
        memex_name = sanitize_project_name(display_name)

        if project_filter:
            pf = project_filter.lower()
            candidates = (display_name.lower(), memex_name.lower())
            if pf not in candidates and not any(c.endswith(pf) for c in candidates):
                continue

        for md_file in sorted(memory_dir.glob("*.md")):
            is_memory_md = md_file.name == "MEMORY.md"
            if is_memory_md and not sync_volatile:
                continue

            try:
                raw = md_file.read_text()
            except (UnicodeDecodeError, FileNotFoundError):
                continue

            if not raw.strip():
                continue

            stat = md_file.stat()
            results.append({
                "source_path": str(md_file),
                "filename": md_file.name,
                "project_dir": project_dir.name,
                "project_display": display_name,
                "project_memex": memex_name,
                "size_bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "modified_date": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d"),
                "source_hash": content_hash(raw),
                "is_memory_md": is_memory_md,
                "title": extract_title(raw, md_file.name),
                "line_count": raw.count("\n") + 1,
            })

    return results


def discover_projects_without_memory(
    discovered: list[dict],
    project_filter: str | None = None,
) -> list[str]:
    """Find Claude projects that have sessions but no auto-memory.

    When a project filter is active, only reports on that project.
    """
    projects_with_memory = {d["project_dir"] for d in discovered}
    without = []

    for project_dir in sorted(PROJECTS_DIR.iterdir()):
        if not project_dir.is_dir():
            continue
        if project_dir.name in projects_with_memory:
            continue

        display = claude_dir_to_project_name(project_dir.name)
        memex_name = sanitize_project_name(display)

        if project_filter:
            pf = project_filter.lower()
            candidates = (display.lower(), memex_name.lower())
            if pf not in candidates and not any(c.endswith(pf) for c in candidates):
                continue

        sessions = list(project_dir.glob("*.jsonl"))
        if sessions:
            without.append(display)

    return without


# ============================================================================
# Vault State
# ============================================================================

def parse_frontmatter_simple(content: str) -> dict:
    """Extract YAML frontmatter key-value pairs."""
    if not content.startswith("---"):
        return {}
    try:
        end = content.index("---", 3)
        result = {}
        for line in content[3:end].strip().split("\n"):
            if ":" in line:
                key, _, value = line.partition(":")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                result[key] = value
        return result
    except ValueError:
        return {}


def get_vault_sync_state(memex: Path) -> dict[str, dict]:
    """Read sync state from existing auto-memory files in the vault.

    Returns dict keyed by vault relative path.
    """
    state = {}
    for vault_file in memex.glob("projects/*/auto-memory/*.md"):
        try:
            content = vault_file.read_text()
        except (UnicodeDecodeError, FileNotFoundError):
            continue

        meta = parse_frontmatter_simple(content)
        rel_path = str(vault_file.relative_to(memex))
        state[rel_path] = {
            "source_hash": meta.get("source_hash", ""),
            "synced": meta.get("synced", ""),
            "source": meta.get("source", ""),
            "volatile": meta.get("volatile", "false") == "true",
        }

    return state


# ============================================================================
# Sync Planning
# ============================================================================

def vault_path_for(item: dict) -> str:
    """Compute vault-relative path for a discovered auto-memory file."""
    return f"projects/{item['project_memex']}/auto-memory/{item['filename']}"


def compute_sync_plan(
    discovered: list[dict],
    vault_state: dict[str, dict],
    memex: Path,
    project_filter: str | None = None,
) -> list[dict]:
    """Compare discovered files against vault state.

    Returns list with action: new | update | unchanged for each discovered file,
    plus action: orphaned for vault files whose source no longer exists.
    Orphan detection is skipped when a project filter is active (filtered
    discovery would falsely mark all other projects' files as orphaned).
    """
    plan = []
    seen_vault_paths = set()

    for item in discovered:
        vp = vault_path_for(item)
        seen_vault_paths.add(vp)

        if vp not in vault_state:
            plan.append({**item, "action": "new", "vault_path": vp})
        elif vault_state[vp]["source_hash"] != item["source_hash"]:
            plan.append({
                **item,
                "action": "update",
                "vault_path": vp,
                "old_hash": vault_state[vp]["source_hash"],
            })
        else:
            plan.append({**item, "action": "unchanged", "vault_path": vp})

    # Detect orphans (only when not filtering by project)
    if not project_filter:
        for vp, state in vault_state.items():
            if vp not in seen_vault_paths:
                plan.append({
                    "action": "orphaned",
                    "vault_path": vp,
                    "source": state.get("source", ""),
                })

    return plan


# ============================================================================
# Content Assembly
# ============================================================================

def extract_annotations(vault_content: str) -> str | None:
    """Extract Vault Annotations section from existing vault file.

    Returns everything at and after the ANNOTATION_MARKER,
    or None if no annotations exist or the section is empty.
    """
    idx = vault_content.rfind(ANNOTATION_MARKER)
    if idx < 0:
        return None

    annotations = vault_content[idx:].strip()
    # Check if it's just the marker with no content
    lines = annotations.split("\n")
    content_lines = [l for l in lines[1:] if l.strip()]
    if not content_lines:
        return None

    return annotations


def strip_source_frontmatter(content: str) -> str:
    """Remove YAML frontmatter from source content if present."""
    if not content.startswith("---"):
        return content
    try:
        end = content.index("---", 3)
        return content[end + 3:].lstrip("\n")
    except ValueError:
        return content


def build_vault_content(
    source_content: str,
    meta: dict,
    existing_annotations: str | None = None,
) -> str:
    """Assemble vault file: frontmatter + source body + Vault Annotations."""
    # Build frontmatter
    source_path = meta["source_path"]
    # Use ~ prefix for portability
    home = str(Path.home())
    if source_path.startswith(home):
        source_path = "~" + source_path[len(home):]

    fm_lines = [
        "---",
        "type: auto-memory",
        'title: "{}"'.format(meta["title"].replace('"', '\\"')),
        f"project: {meta['project_memex']}",
        f"date: {meta['modified_date']}",
        f"source: {source_path}",
        f"source_hash: {meta['source_hash']}",
        f"synced: {datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}",
        f"volatile: {'true' if meta['is_memory_md'] else 'false'}",
        "topics: []",
        "status: active",
        "---",
    ]
    frontmatter = "\n".join(fm_lines)

    # Strip any existing frontmatter from source
    body = strip_source_frontmatter(source_content)

    # Assemble
    parts = [frontmatter, "", body.rstrip()]

    if existing_annotations:
        parts.extend(["", "", existing_annotations])
    else:
        parts.extend(["", "", ANNOTATION_MARKER, ""])

    return "\n".join(parts) + "\n"


# ============================================================================
# Sync Execution
# ============================================================================

def sync_file(
    plan_item: dict,
    memex: Path,
    dry_run: bool = True,
) -> dict:
    """Sync a single auto-memory file into the vault."""
    action = plan_item["action"]
    vault_path = memex / plan_item["vault_path"]

    if action == "unchanged":
        return {"status": "unchanged", "vault_path": plan_item["vault_path"]}

    if action == "orphaned":
        return {"status": "orphaned", "vault_path": plan_item["vault_path"]}

    if dry_run:
        return {"status": f"would_{action}", "vault_path": plan_item["vault_path"]}

    try:
        source_content = Path(plan_item["source_path"]).read_text()
    except (FileNotFoundError, UnicodeDecodeError) as e:
        return {"status": "error", "vault_path": plan_item["vault_path"], "error": str(e)}

    # Recompute hash from actual content being written (source may have changed since discovery)
    plan_item["source_hash"] = content_hash(source_content)

    # Preserve existing annotations on update
    existing_annotations = None
    if action == "update" and vault_path.exists():
        try:
            existing_annotations = extract_annotations(vault_path.read_text())
        except (FileNotFoundError, UnicodeDecodeError):
            pass

    content = build_vault_content(source_content, plan_item, existing_annotations)

    try:
        vault_path.parent.mkdir(parents=True, exist_ok=True)
        vault_path.write_text(content)
    except OSError as e:
        return {"status": "error", "vault_path": plan_item["vault_path"], "error": str(e)}

    status = "created" if action == "new" else "updated"
    return {"status": status, "vault_path": plan_item["vault_path"]}


def sync_all(
    plan: list[dict],
    memex: Path,
    dry_run: bool = True,
) -> list[dict]:
    """Execute full sync plan."""
    return [sync_file(item, memex, dry_run) for item in plan]


# ============================================================================
# Related Note Suggestions
# ============================================================================

def suggest_related(
    results: list[dict],
    memex: Path,
    max_per_file: int = 5,
) -> dict[str, list[str]]:
    """For newly synced files, find related vault notes via FTS.

    Returns dict keyed by vault_path -> list of related paths.
    """
    new_files = [r for r in results if r["status"] in ("created", "updated")]
    if not new_files:
        return {}

    db_path = memex / "_index.sqlite"
    if not db_path.exists():
        return {}

    suggestions: dict[str, list[str]] = {}
    try:
        conn = sqlite3.connect(str(db_path))
        for r in new_files:
            vp = r["vault_path"]
            # Read the synced file to extract search terms from title
            full_path = memex / vp
            if not full_path.exists():
                continue

            content = full_path.read_text()
            meta = parse_frontmatter_simple(content)
            title = meta.get("title", "")
            if not title:
                continue

            # Build FTS query from title words (skip short/reserved)
            FTS5_RESERVED = {"AND", "OR", "NOT", "NEAR"}
            words = [w for w in re.findall(r'[a-zA-Z]{3,}', title)
                     if w.upper() not in FTS5_RESERVED]
            if not words:
                continue
            query = " OR ".join(words[:8])

            try:
                rows = conn.execute(
                    """SELECT path, title, type FROM fts_content
                       WHERE fts_content MATCH ? AND path != ?
                       ORDER BY bm25(fts_content) LIMIT ?""",
                    (query, vp, max_per_file + 2)
                ).fetchall()
            except sqlite3.OperationalError:
                continue

            related = []
            for path, doc_title, doc_type in rows:
                if path == vp:
                    continue
                # Skip transcripts — too noisy
                if doc_type == "transcript":
                    continue
                related.append(f"{path}  ({doc_type}: {doc_title})")
                if len(related) >= max_per_file:
                    break

            if related:
                suggestions[vp] = related

        conn.close()
    except sqlite3.Error:
        pass

    return suggestions


# ============================================================================
# Display
# ============================================================================

def print_discover(discovered: list[dict], verbose: bool = False, project_filter: str | None = None):
    """Print discovery results with coverage report."""
    if not discovered:
        print("No auto-memory files found in ~/.claude/projects/*/memory/")
        return

    # Group by project
    by_project: dict[str, list[dict]] = {}
    for item in discovered:
        by_project.setdefault(item["project_display"], []).append(item)

    print("Auto-memory found:")
    for project, files in sorted(by_project.items()):
        names = ", ".join(f["filename"] for f in files)
        total_lines = sum(f["line_count"] for f in files)
        label = "file" if len(files) == 1 else "files"
        print(f"  {project:30s} {len(files)} {label:5s} {total_lines:4d} lines  ({names})")

        if verbose:
            for f in files:
                vol = " [volatile]" if f["is_memory_md"] else ""
                print(f"    {f['filename']:40s} {f['line_count']:4d} lines  {f['modified_date']}{vol}")

    # Coverage report
    without = discover_projects_without_memory(discovered, project_filter=project_filter)
    if without:
        print(f"\nNo auto-memory (sessions exist):")
        print(f"  {', '.join(without)}")


def print_status(plan: list[dict]):
    """Print sync status summary."""
    counts = {"new": 0, "update": 0, "unchanged": 0, "orphaned": 0}
    for item in plan:
        counts[item["action"]] = counts.get(item["action"], 0) + 1

    total = sum(counts.values())
    print(f"Auto-memory sync status ({total} files):")
    if counts["unchanged"]:
        print(f"  Fresh:    {counts['unchanged']}")
    if counts["new"]:
        print(f"  New:      {counts['new']}  (not yet in vault)")
    if counts["update"]:
        print(f"  Stale:    {counts['update']}  (source changed)")
    if counts["orphaned"]:
        print(f"  Orphaned: {counts['orphaned']}  (source removed)")

    actionable = counts["new"] + counts["update"]
    if actionable:
        print(f"\nRun with --sync --apply to sync {actionable} file(s)")
    else:
        print("\nAll synced.")


def print_sync_results(
    results: list[dict],
    suggestions: dict[str, list[str]] | None = None,
    verbose: bool = False,
):
    """Print sync operation results with optional related-note suggestions."""
    for r in results:
        status = r["status"]
        if status == "unchanged" and not verbose:
            continue
        prefix = {
            "would_new": "+",
            "would_update": "~",
            "created": "+",
            "updated": "~",
            "unchanged": "=",
            "orphaned": "?",
            "error": "!",
        }.get(status, " ")
        print(f"  {prefix} {r['vault_path']}  [{status}]")
        if "error" in r:
            print(f"    Error: {r['error']}")

    actionable = [r for r in results if r["status"].startswith("would_")]
    synced = [r for r in results if r["status"] in ("created", "updated")]

    if actionable:
        print(f"\nDry run: {len(actionable)} file(s) would be synced. Use --apply to write.")
    elif synced:
        print(f"\nSynced {len(synced)} file(s).")

    # Print related-note suggestions for enrichment
    if suggestions:
        print(f"\nRelated notes (for Vault Annotations):")
        for vp, related in suggestions.items():
            print(f"\n  {vp}:")
            for note in related:
                print(f"    - {note}")


# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Sync Claude Code auto-memory into the memex vault"
    )

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--discover", action="store_true",
                      help="List auto-memory files and coverage")
    mode.add_argument("--sync", action="store_true",
                      help="Sync auto-memory into vault (dry-run by default)")
    mode.add_argument("--status", action="store_true",
                      help="Show sync state (fresh/stale/new/orphaned)")

    parser.add_argument("--apply", action="store_true",
                        help="Actually write files (with --sync)")
    parser.add_argument("--project", type=str, default=None,
                        help="Filter to specific project")
    parser.add_argument("--json", action="store_true",
                        help="Machine-readable JSON output")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Show per-file details")

    args = parser.parse_args()

    memex = get_memex_path()
    discovered = discover_auto_memory(project_filter=args.project)

    if args.discover:
        if args.json:
            print(json.dumps(discovered, indent=2))
        else:
            print_discover(discovered, verbose=args.verbose, project_filter=args.project)
        return

    vault_state = get_vault_sync_state(memex)
    plan = compute_sync_plan(discovered, vault_state, memex, project_filter=args.project)

    if args.status:
        if args.json:
            summary = {}
            for item in plan:
                summary[item["action"]] = summary.get(item["action"], 0) + 1
            print(json.dumps({"summary": summary, "files": plan}, indent=2, default=str))
        else:
            print_status(plan)

            if args.verbose:
                for item in plan:
                    action = item["action"]
                    vp = item.get("vault_path", "?")
                    print(f"  [{action:9s}] {vp}")
        return

    if args.sync:
        results = sync_all(plan, memex, dry_run=not args.apply)

        # Suggest related notes for newly synced files
        suggestions = {}
        if args.apply:
            suggestions = suggest_related(results, memex)

        if args.json:
            output = {"results": results}
            if suggestions:
                output["suggestions"] = suggestions
            print(json.dumps(output, indent=2, default=str))
        else:
            print_sync_results(results, suggestions=suggestions, verbose=args.verbose)
        return


if __name__ == "__main__":
    main()
