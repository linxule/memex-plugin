#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""
Obsidian CLI wrapper for memex vault operations.

Provides Python API around Obsidian CLI (1.12+), with automatic
loading-line filtering and fallback detection.

Usage as module:
    from obsidian_cli import ObsidianCLI
    cli = ObsidianCLI(vault="memex")
    if cli.is_available():
        orphans = cli.orphans()
        backlinks = cli.backlinks("claude-code-hooks")

Usage as script:
    obsidian_cli.py backlinks claude-code-hooks
    obsidian_cli.py orphans [--total]
    obsidian_cli.py tasks --path=<file>
    obsidian_cli.py eval "app.vault.getFiles().length"
    obsidian_cli.py status
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional


# Loading line pattern from Obsidian CLI stdout
_LOADING_RE = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} Loading updated app package')
# Installer update nag (Obsidian post-1.12.2)
_INSTALLER_NAG_RE = re.compile(r'^Your Obsidian installer is out of date')

# Default binary location on macOS
_MACOS_BINARY = "/Applications/Obsidian.app/Contents/MacOS/obsidian"


class ObsidianCLI:
    """Wrapper around Obsidian CLI with loading-line filtering.

    Updated for Obsidian 1.12.2 — uses native commands where possible,
    falls back to eval for features not yet exposed via CLI.
    """

    def __init__(self, vault: str = "memex", timeout: int = 15):
        self.vault = vault
        self.timeout = timeout
        self._binary = self._find_binary()

    def _find_binary(self) -> Optional[str]:
        """Find the obsidian binary."""
        # Check PATH first
        binary = shutil.which("obsidian")
        if binary:
            return binary
        # macOS default location
        if Path(_MACOS_BINARY).exists():
            return _MACOS_BINARY
        return None

    def is_available(self) -> bool:
        """Check if Obsidian CLI is available and enabled."""
        if not self._binary:
            return False
        try:
            result = self._run_raw(["version"])
            # If CLI is disabled, output contains "not enabled"
            return "not enabled" not in result and len(result.strip()) > 0
        except (subprocess.TimeoutExpired, subprocess.SubprocessError):
            return False

    def ensure_running(self, wait: int = 8) -> bool:
        """Launch Obsidian if not running, wait for CLI to become available.

        Returns True if CLI is available (was already running or launched).
        Returns False if launch failed or timed out.
        """
        if self.is_available():
            return True
        # Try to launch Obsidian (macOS)
        app_path = "/Applications/Obsidian.app"
        if not Path(app_path).exists():
            return False
        import time
        subprocess.Popen(["open", "-a", app_path], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        # Wait for CLI to become responsive
        for _ in range(wait):
            time.sleep(1)
            if self.is_available():
                return True
        return False

    def _run_raw(self, args: list[str]) -> str:
        """Run a CLI command and return raw stdout.

        Returns empty string on timeout or subprocess error.
        Logs stderr when exit code is non-zero.
        """
        if not self._binary:
            return ""
        cmd = [self._binary, f"vault={self.vault}"] + args
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout
            )
            if result.returncode != 0 and result.stderr.strip():
                print(f"obsidian CLI error (exit {result.returncode}): {result.stderr.strip()}", file=sys.stderr)
            return result.stdout
        except (subprocess.TimeoutExpired, subprocess.SubprocessError) as e:
            print(f"obsidian CLI subprocess error: {e}", file=sys.stderr)
            return ""

    def run(self, args: list[str]) -> list[str]:
        """Run a CLI command and return filtered output lines."""
        raw = self._run_raw(args)
        lines = []
        for line in raw.splitlines():
            if not _LOADING_RE.match(line) and not _INSTALLER_NAG_RE.match(line):
                lines.append(line)
        return lines

    def run_text(self, args: list[str]) -> str:
        """Run a CLI command and return filtered output as text."""
        return "\n".join(self.run(args)).strip()

    # ================================================================
    # High-level commands
    # ================================================================

    def backlinks(self, file: str, counts: bool = False, total: bool = False) -> list[str]:
        """List backlinks to a file. Uses wikilink resolution."""
        args = ["backlinks", f"file={file}"]
        if counts:
            args.append("counts")
        if total:
            args.append("total")
        return self.run(args)

    def orphans(self, total: bool = False) -> list[str]:
        """List files with no incoming links."""
        args = ["orphans"]
        if total:
            args.append("total")
        return self.run(args)

    def deadends(self, total: bool = False) -> list[str]:
        """List files with no outgoing links."""
        args = ["deadends"]
        if total:
            args.append("total")
        return self.run(args)

    def unresolved(self, total: bool = False, counts: bool = False, verbose: bool = False) -> list[str]:
        """List unresolved links in vault."""
        args = ["unresolved"]
        if total:
            args.append("total")
        if counts:
            args.append("counts")
        if verbose:
            args.append("verbose")
        return self.run(args)

    def tags(self, all_vault: bool = True, counts: bool = True, sort: str = "count") -> list[str]:
        """List tags."""
        args = ["tags"]
        if all_vault:
            args.append("all")
        if counts:
            args.append("counts")
        if sort:
            args.append(f"sort={sort}")
        return self.run(args)

    def tasks(self, path: Optional[str] = None, file: Optional[str] = None,
              todo: bool = True, total: bool = False) -> list[str]:
        """List tasks. File-specific works; vault-wide listing may be empty (1.12.1 bug)."""
        args = ["tasks"]
        if path:
            args.append(f"path={path}")
        if file:
            args.append(f"file={file}")
        if todo:
            args.append("todo")
        if total:
            args.append("total")
        return self.run(args)

    def properties(self, counts: bool = True, sort: str = "count",
                   file: Optional[str] = None, path: Optional[str] = None,
                   fmt: str = "yaml") -> list[str]:
        """List properties (vault-wide or per-file).

        fmt: yaml (default), json, tsv. Use json for per-file to get
        full frontmatter as structured data.
        """
        args = ["properties"]
        if file:
            args.append(f"file={file}")
        if path:
            args.append(f"path={path}")
        if not file and not path:
            # Vault-wide listing
            if counts:
                args.append("counts")
            if sort:
                args.append(f"sort={sort}")
        args.append(f"format={fmt}")
        return self.run(args)

    def aliases(self, file: Optional[str] = None, verbose: bool = False,
                total: bool = False) -> list[str]:
        """List aliases in vault (or for a specific file).

        With verbose=True, returns 'alias\\tfile_path' lines.
        With total=True, returns count.
        """
        args = ["aliases"]
        if file:
            args.append(f"file={file}")
        if verbose:
            args.append("verbose")
        if total:
            args.append("total")
        return self.run(args)

    def alias_map(self) -> dict[str, str]:
        """Build {alias_lowercase: file_path} map from native aliases command.

        Returns frontmatter aliases only. Filename stems are NOT included
        because Obsidian resolves those natively — they won't appear in
        unresolvedLinks so they don't need filtering.
        """
        lines = self.aliases(verbose=True)
        mapping: dict[str, str] = {}
        for line in lines:
            parts = line.split("\t", 1)
            if len(parts) == 2:
                alias, path = parts
                mapping[alias.lower()] = path
        return mapping

    def links(self, file: Optional[str] = None, path: Optional[str] = None,
              total: bool = False) -> list[str]:
        """List outgoing links from a file."""
        args = ["links"]
        if file:
            args.append(f"file={file}")
        if path:
            args.append(f"path={path}")
        if total:
            args.append("total")
        return self.run(args)

    def property_read(self, name: str, file: Optional[str] = None,
                      path: Optional[str] = None) -> str:
        """Read a single property value from a file.

        Only works for scalar properties (text, number, date, checkbox).
        Errors on list properties (topics, aliases) — use properties(format=json) instead.
        """
        args = ["property:read", f"name={name}"]
        if file:
            args.append(f"file={file}")
        if path:
            args.append(f"path={path}")
        return self.run_text(args)

    def property_set(self, name: str, value: str, file: Optional[str] = None,
                     path: Optional[str] = None,
                     prop_type: Optional[str] = None) -> list[str]:
        """Set a property on a file.

        prop_type: text, list, number, checkbox, date, datetime
        """
        args = ["property:set", f"name={name}", f"value={value}"]
        if file:
            args.append(f"file={file}")
        if path:
            args.append(f"path={path}")
        if prop_type:
            args.append(f"type={prop_type}")
        return self.run(args)

    def wordcount(self, file: Optional[str] = None,
                  path: Optional[str] = None) -> dict[str, int]:
        """Get word and character counts for a file.

        Returns dict with 'words' and 'characters' keys.
        """
        args = ["wordcount"]
        if file:
            args.append(f"file={file}")
        if path:
            args.append(f"path={path}")
        result = {}
        for line in self.run(args):
            if ":" in line:
                key, val = line.split(":", 1)
                try:
                    result[key.strip()] = int(val.strip())
                except ValueError:
                    pass
        return result

    def file_info(self, file: Optional[str] = None,
                  path: Optional[str] = None) -> dict[str, str]:
        """Get file metadata (path, name, extension, size, created, modified)."""
        args = ["file"]
        if file:
            args.append(f"file={file}")
        if path:
            args.append(f"path={path}")
        result = {}
        for line in self.run(args):
            if "\t" in line:
                key, val = line.split("\t", 1)
                result[key.strip()] = val.strip()
        return result

    def files(self, folder: Optional[str] = None, ext: Optional[str] = None,
              total: bool = False) -> list[str]:
        """List files in vault, optionally filtered by folder/extension."""
        args = ["files"]
        if folder:
            args.append(f"folder={folder}")
        if ext:
            args.append(f"ext={ext}")
        if total:
            args.append("total")
        return self.run(args)

    def vault_info(self) -> dict[str, str]:
        """Get vault summary (name, path, files, folders, size)."""
        args = ["vault"]
        result = {}
        for line in self.run(args):
            if "\t" in line:
                key, val = line.split("\t", 1)
                result[key.strip()] = val.strip()
        return result

    def search(self, query: str, path: Optional[str] = None,
               limit: Optional[int] = None, total: bool = False,
               fmt: str = "text") -> list[str]:
        """Search vault for text.

        Note: As of 1.12.2, search output may still be empty due to
        output buffering. Use search.py for reliable text search.
        """
        args = ["search", f"query={query}"]
        if path:
            args.append(f"path={path}")
        if limit:
            args.append(f"limit={limit}")
        if total:
            args.append("total")
        args.append(f"format={fmt}")
        return self.run(args)

    def search_context(self, query: str, path: Optional[str] = None,
                       limit: Optional[int] = None,
                       fmt: str = "text") -> list[str]:
        """Search with matching line context.

        Note: As of 1.12.2, may still return empty output.
        """
        args = ["search:context", f"query={query}"]
        if path:
            args.append(f"path={path}")
        if limit:
            args.append(f"limit={limit}")
        args.append(f"format={fmt}")
        return self.run(args)

    def read_file(self, file: Optional[str] = None, path: Optional[str] = None) -> str:
        """Read file contents. Uses wikilink resolution for file= param."""
        args = ["read"]
        if file:
            args.append(f"file={file}")
        if path:
            args.append(f"path={path}")
        return self.run_text(args)

    def outline(self, file: Optional[str] = None, path: Optional[str] = None,
                fmt: str = "tree") -> list[str]:
        """Show headings for a file."""
        args = ["outline"]
        if file:
            args.append(f"file={file}")
        if path:
            args.append(f"path={path}")
        args.append(f"format={fmt}")
        return self.run(args)

    def eval_js(self, code: str) -> str:
        """Execute JavaScript in Obsidian and return result."""
        args = ["eval", f"code={code}"]
        output = self.run_text(args)
        # eval output starts with "=> " prefix (or just "=>" when result is empty)
        if output.startswith("=> "):
            output = output[3:]
        elif output == "=>":
            output = ""
        return output

    # ================================================================
    # Compound queries via eval
    # ================================================================

    @staticmethod
    def _safe_path(path: str) -> str:
        """Escape path for safe interpolation into JavaScript strings."""
        return path.replace("\\", "\\\\").replace("'", "\\'")

    def vault_file_count(self) -> int:
        """Get total markdown file count.

        Uses native `files ext=md total` (1.12.2+), falls back to eval.
        """
        result = self.files(ext="md", total=True)
        if result and result[0].isdigit():
            return int(result[0])
        # Fallback to eval
        result = self.eval_js("app.vault.getMarkdownFiles().length")
        return int(result) if result and result.isdigit() else 0

    def resolved_backlinks(self, doc_path: str) -> list[str]:
        """Get backlinks using Obsidian's resolved link graph."""
        safe = self._safe_path(doc_path)
        code = (
            f"Object.entries(app.metadataCache.resolvedLinks)"
            f".filter(([k,v]) => Object.keys(v).includes('{safe}'))"
            f".map(([k]) => k).join('\\n')"
        )
        result = self.eval_js(code)
        return [l for l in result.splitlines() if l.strip()] if result else []

    def resolved_outlinks(self, doc_path: str) -> list[str]:
        """Get outgoing links using native `links` command (1.12.2+).

        Falls back to eval-based resolvedLinks query.
        """
        # Try native command first
        result = self.links(path=doc_path)
        if result:
            return result
        # Fallback to eval
        safe = self._safe_path(doc_path)
        code = (
            f"JSON.stringify("
            f"Object.keys(app.metadataCache.resolvedLinks['{safe}'] || {{}})"
            f")"
        )
        result = self.eval_js(code)
        try:
            return json.loads(result) if result else []
        except json.JSONDecodeError:
            return []

    def unresolved_links_for(self, doc_path: str) -> list[str]:
        """Get unresolved links from a specific file."""
        safe = self._safe_path(doc_path)
        code = (
            f"Object.keys(app.metadataCache.unresolvedLinks['{safe}'] || {{}})"
            f".join('\\n')"
        )
        result = self.eval_js(code)
        return [l for l in result.splitlines() if l.strip()] if result else []

    def frontmatter(self, doc_path: str) -> dict:
        """Get frontmatter for a file.

        Uses native `properties format=json` (1.12.2+), which returns
        the full frontmatter including list properties. Falls back to
        eval-based metadataCache query if native command returns empty.
        """
        # Try native command first (more reliable, no injection risk)
        args = ["properties", f"path={doc_path}", "format=json"]
        result = self.run_text(args)
        if result:
            try:
                return json.loads(result)
            except json.JSONDecodeError:
                pass
        # Fallback to eval
        safe = self._safe_path(doc_path)
        code = (
            f"JSON.stringify("
            f"app.metadataCache.getCache('{safe}')?.frontmatter || {{}}"
            f")"
        )
        result = self.eval_js(code)
        try:
            return json.loads(result) if result else {}
        except json.JSONDecodeError:
            return {}

    # ================================================================
    # Base view commands
    # ================================================================

    def bases(self) -> list[str]:
        """List all base files in vault."""
        return self.run(["bases"])

    def base_views(self, file: Optional[str] = None, path: Optional[str] = None) -> list[str]:
        """List views in a base file."""
        args = ["base:views"]
        if file:
            args.append(f"file={file}")
        if path:
            args.append(f"path={path}")
        return self.run(args)

    def base_query(self, file: Optional[str] = None, path: Optional[str] = None,
                   view: Optional[str] = None, fmt: str = "json") -> str:
        """Query a base view and return results.

        Args:
            file: Base file name (wikilink resolution)
            path: Base file path
            view: View name within the base file
            fmt: Output format (json, csv, tsv, md, paths)
        """
        args = ["base:query"]
        if file:
            args.append(f"file={file}")
        if path:
            args.append(f"path={path}")
        if view:
            args.append(f"view={view}")
        args.append(f"format={fmt}")
        return self.run_text(args)

    def base_create(self, name: str, content: Optional[str] = None,
                    silent: bool = False, newtab: bool = False) -> list[str]:
        """Create a new item in the current base view."""
        args = ["base:create", f"name={name}"]
        if content:
            args.append(f"content={content}")
        if silent:
            args.append("silent")
        if newtab:
            args.append("newtab")
        return self.run(args)


# ============================================================================
# CLI
# ============================================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Obsidian CLI wrapper")
    parser.add_argument("--vault", default="memex", help="Vault name")
    subparsers = parser.add_subparsers(dest="command")

    # Status
    subparsers.add_parser("status", help="Check CLI availability and vault stats")

    # Backlinks
    bl = subparsers.add_parser("backlinks", help="List backlinks to a file")
    bl.add_argument("file", help="File name (wikilink resolution)")
    bl.add_argument("--counts", action="store_true")

    # Orphans
    orph = subparsers.add_parser("orphans", help="List orphan files")
    orph.add_argument("--total", action="store_true")

    # Deadends
    de = subparsers.add_parser("deadends", help="List dead-end files")
    de.add_argument("--total", action="store_true")

    # Unresolved
    ur = subparsers.add_parser("unresolved", help="List unresolved links")
    ur.add_argument("--total", action="store_true")
    ur.add_argument("--verbose", action="store_true")

    # Tags
    subparsers.add_parser("tags", help="List all tags with counts")

    # Tasks
    tk = subparsers.add_parser("tasks", help="List tasks")
    tk.add_argument("--path", help="File path to get tasks from")
    tk.add_argument("--file", help="File name (wikilink resolution)")
    tk.add_argument("--total", action="store_true")

    # Properties
    pp = subparsers.add_parser("properties", help="List all properties with counts")
    pp.add_argument("--file", help="File name (wikilink resolution)")
    pp.add_argument("--path", help="File path in vault")
    pp.add_argument("--format", dest="fmt", choices=["yaml", "json"], default="yaml",
                    help="Output format (default: yaml)")

    # Aliases
    al = subparsers.add_parser("aliases", help="List aliases in vault")
    al.add_argument("--file", help="File name (wikilink resolution)")
    al.add_argument("--verbose", action="store_true", help="Include file paths")
    al.add_argument("--total", action="store_true")

    # Links
    lk = subparsers.add_parser("links", help="List outgoing links from a file")
    lk.add_argument("file", help="File name (wikilink resolution)")
    lk.add_argument("--total", action="store_true")

    # Wordcount
    wc = subparsers.add_parser("wordcount", help="Word and character counts")
    wc.add_argument("file", help="File name (wikilink resolution)")

    # File info
    fi = subparsers.add_parser("file-info", help="File metadata")
    fi.add_argument("file", help="File name (wikilink resolution)")

    # Files
    fl = subparsers.add_parser("files", help="List files in vault")
    fl.add_argument("--folder", help="Filter by folder")
    fl.add_argument("--ext", help="Filter by extension")
    fl.add_argument("--total", action="store_true")

    # Vault info
    subparsers.add_parser("vault-info", help="Vault summary")

    # Search
    sr = subparsers.add_parser("search", help="Search vault (may return empty — use search.py)")
    sr.add_argument("query", help="Search query")
    sr.add_argument("--limit", type=int)
    sr.add_argument("--total", action="store_true")

    # Check links
    cl = subparsers.add_parser("check-links", help="Validate wikilinks in a file")
    cl.add_argument("file", nargs="?", help="File name (wikilink resolution)")
    cl.add_argument("--path", help="File path in vault")

    # Eval
    ev = subparsers.add_parser("eval", help="Execute JavaScript in Obsidian")
    ev.add_argument("code", help="JavaScript code to execute")

    # Bases
    subparsers.add_parser("bases", help="List all base files in vault")

    # Base views
    bv = subparsers.add_parser("base-views", help="List views in a base file")
    bv.add_argument("--file", help="Base file name (wikilink resolution)")
    bv.add_argument("--path", help="Base file path")

    # Base query
    bq = subparsers.add_parser("base-query", help="Query a base view")
    bq.add_argument("--file", help="Base file name (wikilink resolution)")
    bq.add_argument("--path", help="Base file path")
    bq.add_argument("--view", help="View name within the base file")
    bq.add_argument("--format", default="md", choices=["json", "csv", "tsv", "md", "paths"],
                    help="Output format (default: md)")

    parser.add_argument("--launch", action="store_true",
                        help="Launch Obsidian if not running (waits up to 8s)")
    args = parser.parse_args()
    cli = ObsidianCLI(vault=args.vault)

    if not cli.is_available():
        if args.launch:
            print("Obsidian not running, launching...", file=sys.stderr)
            if not cli.ensure_running():
                print("Failed to launch Obsidian or CLI not enabled.", file=sys.stderr)
                sys.exit(1)
            print("Obsidian ready.", file=sys.stderr)
        else:
            print("Obsidian CLI not available. Use --launch to start Obsidian, or start it manually.", file=sys.stderr)
            sys.exit(1)

    if args.command == "status":
        print(f"Obsidian CLI: available")
        vi = cli.vault_info()
        print(f"Vault: {vi.get('name', args.vault)} ({vi.get('path', 'unknown')})")
        print(f"Files: {vi.get('files', '?')} | Folders: {vi.get('folders', '?')}")
        count = cli.vault_file_count()
        print(f"Markdown files: {count}")
        alias_count = cli.aliases(total=True)
        print(f"Aliases: {alias_count[0] if alias_count else '?'}")
        orphan_count = cli.orphans(total=True)
        print(f"Orphans: {orphan_count[0] if orphan_count else '?'}")
        deadend_count = cli.deadends(total=True)
        print(f"Dead-ends: {deadend_count[0] if deadend_count else '?'}")
        unresolved_count = cli.unresolved(total=True)
        print(f"Unresolved links: {unresolved_count[0] if unresolved_count else '?'}")

    elif args.command == "backlinks":
        for line in cli.backlinks(args.file, counts=args.counts):
            print(line)

    elif args.command == "orphans":
        for line in cli.orphans(total=args.total):
            print(line)

    elif args.command == "deadends":
        for line in cli.deadends(total=args.total):
            print(line)

    elif args.command == "unresolved":
        for line in cli.unresolved(total=args.total, verbose=args.verbose):
            print(line)

    elif args.command == "tags":
        for line in cli.tags():
            print(line)

    elif args.command == "tasks":
        for line in cli.tasks(path=args.path, file=args.file, total=args.total):
            print(line)

    elif args.command == "properties":
        for line in cli.properties(file=args.file, path=args.path, fmt=args.fmt):
            print(line)

    elif args.command == "aliases":
        for line in cli.aliases(file=args.file, verbose=args.verbose, total=args.total):
            print(line)

    elif args.command == "links":
        for line in cli.links(file=args.file, total=args.total):
            print(line)

    elif args.command == "wordcount":
        counts = cli.wordcount(file=args.file)
        for k, v in counts.items():
            print(f"{k}: {v}")

    elif args.command == "file-info":
        info = cli.file_info(file=args.file)
        for k, v in info.items():
            print(f"{k}: {v}")

    elif args.command == "files":
        for line in cli.files(folder=args.folder, ext=args.ext, total=args.total):
            print(line)

    elif args.command == "vault-info":
        info = cli.vault_info()
        for k, v in info.items():
            print(f"{k}: {v}")

    elif args.command == "search":
        for line in cli.search(args.query, limit=args.limit, total=args.total):
            print(line)

    elif args.command == "check-links":
        file_arg = args.file
        path_arg = getattr(args, "path", None)
        # Resolve doc_path for unresolved_links_for
        if path_arg:
            doc_path = path_arg
        elif file_arg:
            # Try to resolve via links command to get the actual path
            outgoing = cli.links(file=file_arg)
            # Use eval to get the actual file path
            result = cli.eval_js(
                f"app.metadataCache.getFirstLinkpathDest('{file_arg}', '')?.path || ''"
            )
            doc_path = result.strip() if result else f"{file_arg}.md"
        else:
            print("Error: provide a file name or --path", file=sys.stderr)
            sys.exit(1)

        unresolved = cli.unresolved_links_for(doc_path)
        outgoing = cli.links(path=doc_path)
        resolved_count = len(outgoing)
        total = resolved_count + len(unresolved)

        if not unresolved:
            print(f"All {total} links in {doc_path} resolve.")
        else:
            print(f"{doc_path}: {len(unresolved)}/{total} links unresolved:")
            for link in sorted(unresolved):
                print(f"  - [[{link}]]")

    elif args.command == "eval":
        print(cli.eval_js(args.code))

    elif args.command == "bases":
        for line in cli.bases():
            print(line)

    elif args.command == "base-views":
        for line in cli.base_views(file=args.file, path=args.path):
            print(line)

    elif args.command == "base-query":
        print(cli.base_query(file=args.file, path=args.path,
                             view=args.view, fmt=args.format))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
