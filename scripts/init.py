#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "memex",
# ]
# ///
"""
Claude Memory Plugin - Project Initialization

Initialize a new project structure in the memex vault.

Usage:
    init.py <project-name> [--cwd=/path/to/project]
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from memex.paths import get_memex_path


def sanitize_name(name: str) -> str:
    """Sanitize project name for filesystem."""
    import re
    name = re.sub(r'[^\w\-]', '_', name)
    name = re.sub(r'_+', '_', name).strip('_')[:50]
    return name or '_uncategorized'


def init_project(name: str, cwd: str | None = None) -> Path:
    """Initialize a new project structure."""
    memex = get_memex_path()
    safe_name = sanitize_name(name)

    project_path = memex / "projects" / safe_name

    if project_path.exists():
        print(f"Project already exists: {project_path}")
        return project_path

    # Create directories
    (project_path / "memos").mkdir(parents=True)
    (project_path / "transcripts").mkdir(parents=True)

    # Create project metadata
    meta_content = f"""---
type: project
name: {safe_name}
created: {datetime.now().strftime("%Y-%m-%d")}
---

# {name}

## Overview

{f"Project workspace: `{cwd}`" if cwd else ""}

## Key Decisions

<!-- Important decisions made in this project -->

## Patterns

<!-- Recurring patterns or approaches used -->

## Open Questions

<!-- Things to investigate or resolve -->
"""

    (project_path / "_project.md").write_text(meta_content)

    print(f"Initialized project: {project_path}")
    return project_path


def main():
    parser = argparse.ArgumentParser(description="Initialize a memex project")
    parser.add_argument("name", help="Project name")
    parser.add_argument("--cwd", help="Working directory path")

    args = parser.parse_args()

    init_project(args.name, args.cwd)


if __name__ == "__main__":
    main()
