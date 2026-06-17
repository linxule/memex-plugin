#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["filelock>=3.0"]
# ///
"""
Obsidian CLI wrapper for memex vault operations.

Provides Python API around Obsidian CLI (1.12.5+), with automatic
loading-line filtering and fallback detection.

Usage as module:
    from obsidian_cli import ObsidianCLI
    cli = ObsidianCLI(vault="memex")
    if cli.is_available():
        orphans = cli.orphans()
        backlinks = cli.backlinks("claude-code-hooks")
        cli.move("old-note", file="old-note")  # link-safe rename
        cli.append("new content", file="my-note")

Usage as script:
    obsidian_cli.py status
    obsidian_cli.py backlinks claude-code-hooks
    obsidian_cli.py orphans [--total]
    obsidian_cli.py tasks --path=<file> [--verbose]
    obsidian_cli.py create --name=new-note --template=memo
    obsidian_cli.py move my-note topics/
    obsidian_cli.py rename my-note better-name
    obsidian_cli.py append my-note "new content"
    obsidian_cli.py tag memo --verbose
    obsidian_cli.py task-done --ref="projects/x/_project.md:42"
    obsidian_cli.py recents
    obsidian_cli.py folders --folder=projects
    obsidian_cli.py eval "app.vault.getFiles().length"
"""

import contextlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

# Best-effort cross-process serialization (degrades to no-op if unavailable).
try:
    from filelock import FileLock
except Exception:  # pragma: no cover - filelock is a declared dep
    FileLock = None


# Loading line pattern from Obsidian CLI stdout
_LOADING_RE = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} Loading updated app package')
# Installer update nag (Obsidian post-1.12.2)
_INSTALLER_NAG_RE = re.compile(r'^Your Obsidian installer is out of date')

# Default binary location on macOS
_MACOS_BINARY = "/Applications/Obsidian.app/Contents/MacOS/obsidian"

# Cross-process serialization for CLI calls. A rapid burst of concurrent calls
# (e.g. parallel garden-tending agents) can wedge the single Obsidian renderer
# at ~99-140% CPU, after which every command returns empty until a manual
# restart (observed 2026-06-16). Serializing calls through one lock prevents the
# dogpile; on lock timeout we proceed unserialized rather than fail.
_CLI_LOCK_PATH = Path.home() / ".memex" / "locks" / "obsidian-cli.lock"
_CLI_LOCK_TIMEOUT = 30  # seconds


@contextlib.contextmanager
def _cli_serialize():
    """Best-effort cross-process mutex around an Obsidian CLI call.

    No-op when filelock is unavailable or the lock can't be acquired in time —
    serialization is a reliability aid, not a correctness requirement.
    """
    if FileLock is None:
        yield
        return
    lock = None
    try:
        _CLI_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(_CLI_LOCK_PATH), timeout=_CLI_LOCK_TIMEOUT)
        lock.acquire()
    except Exception:
        lock = None  # contended/unavailable — proceed without serialization
    try:
        yield
    finally:
        if lock is not None:
            try:
                lock.release()
            except Exception:
                pass


class ObsidianCLI:
    """Wrapper around Obsidian CLI with loading-line filtering.

    Updated for Obsidian 1.12.5 — uses native commands where possible,
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

    def is_available(self, deep: bool = True) -> bool:
        """Check if Obsidian CLI is available, enabled, AND responsive.

        A wedged renderer can keep accepting connections (so ``version`` still
        returns) while returning EMPTY for every real query — silently starving
        callers that would otherwise fall back to the SQLite graph queries. By
        default this runs a cheap *real* query (vault info) after the version
        check and treats an empty result as unavailable, so callers degrade
        gracefully. Pass ``deep=False`` for the legacy version-only check.
        """
        if not self._binary:
            return False
        try:
            result = self._run_raw(["version"])
            # If CLI is disabled, output contains "not enabled"
            if "not enabled" in result or len(result.strip()) == 0:
                return False
            if not deep:
                return True
            # Liveness probe: a wedged instance returns empty here even though
            # `version` succeeded. `vault` is cheap and exercises the data path.
            probe = self._run_raw(["vault"])
            return len(probe.strip()) > 0
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
            with _cli_serialize():
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

    def unresolved(self, total: bool = False, counts: bool = False,
                   verbose: bool = False, fmt: Optional[str] = None) -> list[str]:
        """List unresolved links in vault.

        fmt: tsv (default), json, csv.
        """
        args = ["unresolved"]
        if total:
            args.append("total")
        if counts:
            args.append("counts")
        if verbose:
            args.append("verbose")
        if fmt:
            args.append(f"format={fmt}")
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
              todo: bool = True, done: bool = False, total: bool = False,
              verbose: bool = False, status: Optional[str] = None,
              fmt: Optional[str] = None) -> list[str]:
        """List tasks.

        verbose: group by file with line numbers.
        status: filter by status character (e.g. " ", "x", "/").
        fmt: text (default), json, tsv, csv.
        """
        args = ["tasks"]
        if path:
            args.append(f"path={path}")
        if file:
            args.append(f"file={file}")
        if todo:
            args.append("todo")
        if done:
            args.append("done")
        if total:
            args.append("total")
        if verbose:
            args.append("verbose")
        if status is not None:
            args.append(f"status={status}")
        if fmt:
            args.append(f"format={fmt}")
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

        WARNING: Obsidian search has an IPC buffering bug (1.12.1–1.12.5)
        that returns empty output from Python subprocess. Works from
        interactive shell with file redirect:
            obsidian vault=memex search query=X > /tmp/out.txt
        For reliable search from Python, use search.py (FTS5 + vector).
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

        WARNING: Same IPC buffering bug as search() — empty from Python.
        Use search.py for reliable text search.
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
    # File operations (1.12.5+)
    # ================================================================

    def create(self, name: Optional[str] = None, path: Optional[str] = None,
               content: Optional[str] = None, template: Optional[str] = None,
               overwrite: bool = False) -> list[str]:
        """Create a new file. Obsidian updates the graph immediately.

        template: template name to use (from templates/ folder).
        """
        args = ["create"]
        if name:
            args.append(f"name={name}")
        if path:
            args.append(f"path={path}")
        if content:
            args.append(f"content={content}")
        if template:
            args.append(f"template={template}")
        if overwrite:
            args.append("overwrite")
        return self.run(args)

    def append(self, content: str, file: Optional[str] = None,
               path: Optional[str] = None, inline: bool = False) -> list[str]:
        """Append content to a file."""
        args = ["append"]
        if file:
            args.append(f"file={file}")
        if path:
            args.append(f"path={path}")
        args.append(f"content={content}")
        if inline:
            args.append("inline")
        return self.run(args)

    def prepend(self, content: str, file: Optional[str] = None,
                path: Optional[str] = None, inline: bool = False) -> list[str]:
        """Prepend content to a file (after frontmatter)."""
        args = ["prepend"]
        if file:
            args.append(f"file={file}")
        if path:
            args.append(f"path={path}")
        args.append(f"content={content}")
        if inline:
            args.append("inline")
        return self.run(args)

    def move(self, to: str, file: Optional[str] = None,
             path: Optional[str] = None) -> list[str]:
        """Move a file. Obsidian updates all backlinks automatically."""
        args = ["move"]
        if file:
            args.append(f"file={file}")
        if path:
            args.append(f"path={path}")
        args.append(f"to={to}")
        return self.run(args)

    def rename(self, name: str, file: Optional[str] = None,
               path: Optional[str] = None) -> list[str]:
        """Rename a file. Obsidian updates all backlinks automatically."""
        args = ["rename"]
        if file:
            args.append(f"file={file}")
        if path:
            args.append(f"path={path}")
        args.append(f"name={name}")
        return self.run(args)

    def delete(self, file: Optional[str] = None, path: Optional[str] = None,
               permanent: bool = False) -> list[str]:
        """Delete a file (moves to trash by default)."""
        args = ["delete"]
        if file:
            args.append(f"file={file}")
        if path:
            args.append(f"path={path}")
        if permanent:
            args.append("permanent")
        return self.run(args)

    def property_remove(self, name: str, file: Optional[str] = None,
                        path: Optional[str] = None) -> list[str]:
        """Remove a property from a file's frontmatter."""
        args = ["property:remove", f"name={name}"]
        if file:
            args.append(f"file={file}")
        if path:
            args.append(f"path={path}")
        return self.run(args)

    # ================================================================
    # Vault structure (1.12.5+)
    # ================================================================

    def folders(self, folder: Optional[str] = None, total: bool = False) -> list[str]:
        """List folders in vault, optionally filtered by parent folder."""
        args = ["folders"]
        if folder:
            args.append(f"folder={folder}")
        if total:
            args.append("total")
        return self.run(args)

    def recents(self, total: bool = False) -> list[str]:
        """List recently opened files."""
        args = ["recents"]
        if total:
            args.append("total")
        return self.run(args)

    def tag(self, name: str, total: bool = False, verbose: bool = False) -> list[str]:
        """Get info for a single tag.

        verbose: include file list and count.
        name: tag name with or without '#' prefix (auto-added if missing).
        """
        if not name.startswith("#"):
            name = f"#{name}"
        args = ["tag", f"name={name}"]
        if total:
            args.append("total")
        if verbose:
            args.append("verbose")
        return self.run(args)

    # ================================================================
    # Task operations (1.12.5+)
    # ================================================================

    def task_toggle(self, file: Optional[str] = None, path: Optional[str] = None,
                    line: Optional[int] = None, ref: Optional[str] = None) -> list[str]:
        """Toggle a task's completion status.

        ref: shorthand 'path:line' reference.
        """
        args = ["task"]
        if ref:
            args.append(f"ref={ref}")
        if file:
            args.append(f"file={file}")
        if path:
            args.append(f"path={path}")
        if line is not None:
            args.append(f"line={line}")
        args.append("toggle")
        return self.run(args)

    def task_done(self, file: Optional[str] = None, path: Optional[str] = None,
                  line: Optional[int] = None, ref: Optional[str] = None) -> list[str]:
        """Mark a task as done."""
        args = ["task"]
        if ref:
            args.append(f"ref={ref}")
        if file:
            args.append(f"file={file}")
        if path:
            args.append(f"path={path}")
        if line is not None:
            args.append(f"line={line}")
        args.append("done")
        return self.run(args)

    # ================================================================
    # Templates & history (1.12.5+)
    # ================================================================

    def templates(self, total: bool = False) -> list[str]:
        """List available templates."""
        args = ["templates"]
        if total:
            args.append("total")
        return self.run(args)

    def template_read(self, name: str, resolve: bool = False,
                      title: Optional[str] = None) -> str:
        """Read template content.

        resolve: replace template variables with values.
        title: title for variable resolution.
        """
        args = ["template:read", f"name={name}"]
        if resolve:
            args.append("resolve")
        if title:
            args.append(f"title={title}")
        return self.run_text(args)

    def history_list(self) -> list[str]:
        """List files with local history."""
        return self.run(["history:list"])

    def history_read(self, version: int = 1, file: Optional[str] = None,
                     path: Optional[str] = None) -> str:
        """Read a previous version of a file."""
        args = ["history:read", f"version={version}"]
        if file:
            args.append(f"file={file}")
        if path:
            args.append(f"path={path}")
        return self.run_text(args)

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
                    open: bool = True, newtab: bool = False) -> list[str]:
        """Create a new item in the current base view.

        open: whether to open the created note (default True, pass False to suppress).
        """
        args = ["base:create", f"name={name}"]
        if content:
            args.append(f"content={content}")
        if not open:
            args.append("open=false")
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
    bl.add_argument("--total", action="store_true")

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
    ur.add_argument("--counts", action="store_true")

    # Tags
    subparsers.add_parser("tags", help="List all tags with counts")

    # Tasks
    tk = subparsers.add_parser("tasks", help="List tasks")
    tk.add_argument("--path", help="File path to get tasks from")
    tk.add_argument("--file", help="File name (wikilink resolution)")
    tk.add_argument("--total", action="store_true")
    tk.add_argument("--verbose", action="store_true", help="Group by file with line numbers")
    tk.add_argument("--done", action="store_true", help="Show completed tasks")
    tk.add_argument("--status", help="Filter by status character")
    tk.add_argument("--format", dest="fmt", choices=["text", "json", "tsv", "csv"],
                    help="Output format")

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

    # Outline (headings for a file)
    ol = subparsers.add_parser("outline", help="Show heading outline for a file")
    ol.add_argument("--file", help="File name (wikilink resolution)")
    ol.add_argument("--path", help="File path in vault")
    ol.add_argument("--format", dest="fmt", choices=["tree", "md", "json"],
                    default="tree", help="Output format (default: tree)")

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

    # Create
    cr = subparsers.add_parser("create", help="Create a new file")
    cr.add_argument("--name", help="File name")
    cr.add_argument("--path", help="File path")
    cr.add_argument("--content", help="Initial content")
    cr.add_argument("--template", help="Template to use")
    cr.add_argument("--overwrite", action="store_true")

    # Append
    ap = subparsers.add_parser("append", help="Append content to a file")
    ap.add_argument("file", help="File name (wikilink resolution)")
    ap.add_argument("content", help="Content to append")
    ap.add_argument("--inline", action="store_true", help="No newline before content")

    # Prepend
    pr = subparsers.add_parser("prepend", help="Prepend content to a file")
    pr.add_argument("file", help="File name (wikilink resolution)")
    pr.add_argument("content", help="Content to prepend")
    pr.add_argument("--inline", action="store_true", help="No newline before content")

    # Move
    mv = subparsers.add_parser("move", help="Move a file (updates backlinks)")
    mv.add_argument("file", help="File name (wikilink resolution)")
    mv.add_argument("to", help="Destination folder or path")

    # Rename
    rn = subparsers.add_parser("rename", help="Rename a file (updates backlinks)")
    rn.add_argument("file", help="File name (wikilink resolution)")
    rn.add_argument("name", help="New file name")

    # Delete
    dl = subparsers.add_parser("delete", help="Delete a file (moves to trash)")
    dl.add_argument("file", nargs="?", help="File name (wikilink resolution)")
    dl.add_argument("--path", help="File path in vault")
    dl.add_argument("--permanent", action="store_true", help="Skip trash, delete permanently")

    # Property remove
    prm = subparsers.add_parser("property-remove", help="Remove a frontmatter property")
    prm.add_argument("name", help="Property name to remove")
    prm.add_argument("--file", help="File name (wikilink resolution)")
    prm.add_argument("--path", help="File path in vault")

    # Folders
    fd = subparsers.add_parser("folders", help="List folders in vault")
    fd.add_argument("--folder", help="Filter by parent folder")
    fd.add_argument("--total", action="store_true")

    # Recents
    rc = subparsers.add_parser("recents", help="Recently opened files")
    rc.add_argument("--total", action="store_true")

    # Tag (single)
    tg = subparsers.add_parser("tag", help="Info for a single tag")
    tg.add_argument("name", help="Tag name")
    tg.add_argument("--verbose", action="store_true", help="Include file list")

    # Task toggle
    tt = subparsers.add_parser("task-toggle", help="Toggle a task's status")
    tt.add_argument("--ref", help="Task reference (path:line)")
    tt.add_argument("--file", help="File name")
    tt.add_argument("--path", help="File path")
    tt.add_argument("--line", type=int, help="Line number")

    # Task done
    td = subparsers.add_parser("task-done", help="Mark a task as done")
    td.add_argument("--ref", help="Task reference (path:line)")
    td.add_argument("--file", help="File name")
    td.add_argument("--path", help="File path")
    td.add_argument("--line", type=int, help="Line number")

    # Templates
    subparsers.add_parser("templates", help="List available templates")

    # Template read
    tr = subparsers.add_parser("template-read", help="Read a template")
    tr.add_argument("name", help="Template name")
    tr.add_argument("--resolve", action="store_true", help="Resolve variables")
    tr.add_argument("--title", help="Title for variable resolution")

    # History list
    subparsers.add_parser("history-list", help="List files with history")

    # History read
    hr = subparsers.add_parser("history-read", help="Read a previous file version")
    hr.add_argument("file", help="File name (wikilink resolution)")
    hr.add_argument("--version", type=int, default=1, help="Version number (default: 1)")

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
        for line in cli.backlinks(args.file, counts=args.counts, total=args.total):
            print(line)

    elif args.command == "orphans":
        for line in cli.orphans(total=args.total):
            print(line)

    elif args.command == "deadends":
        for line in cli.deadends(total=args.total):
            print(line)

    elif args.command == "unresolved":
        for line in cli.unresolved(total=args.total, verbose=args.verbose, counts=args.counts):
            print(line)

    elif args.command == "tags":
        for line in cli.tags():
            print(line)

    elif args.command == "tasks":
        for line in cli.tasks(path=args.path, file=args.file, total=args.total,
                              verbose=args.verbose, done=args.done,
                              status=args.status, fmt=args.fmt):
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

    elif args.command == "outline":
        for line in cli.outline(file=args.file, path=args.path, fmt=args.fmt):
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
            safe_file = cli._safe_path(file_arg)
            result = cli.eval_js(
                f"app.metadataCache.getFirstLinkpathDest('{safe_file}', '')?.path || ''"
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

    elif args.command == "create":
        for line in cli.create(name=args.name, path=args.path, content=args.content,
                               template=args.template, overwrite=args.overwrite):
            print(line)

    elif args.command == "append":
        for line in cli.append(args.content, file=args.file, inline=args.inline):
            print(line)

    elif args.command == "prepend":
        for line in cli.prepend(args.content, file=args.file, inline=args.inline):
            print(line)

    elif args.command == "move":
        for line in cli.move(args.to, file=args.file):
            print(line)

    elif args.command == "rename":
        for line in cli.rename(args.name, file=args.file):
            print(line)

    elif args.command == "delete":
        for line in cli.delete(file=args.file, path=args.path, permanent=args.permanent):
            print(line)

    elif args.command == "property-remove":
        for line in cli.property_remove(args.name, file=args.file, path=args.path):
            print(line)

    elif args.command == "folders":
        for line in cli.folders(folder=args.folder, total=args.total):
            print(line)

    elif args.command == "recents":
        for line in cli.recents(total=args.total):
            print(line)

    elif args.command == "tag":
        for line in cli.tag(args.name, verbose=args.verbose):
            print(line)

    elif args.command == "task-toggle":
        for line in cli.task_toggle(file=args.file, path=args.path,
                                     line=args.line, ref=args.ref):
            print(line)

    elif args.command == "task-done":
        for line in cli.task_done(file=args.file, path=args.path,
                                   line=args.line, ref=args.ref):
            print(line)

    elif args.command == "templates":
        for line in cli.templates():
            print(line)

    elif args.command == "template-read":
        print(cli.template_read(args.name, resolve=args.resolve, title=args.title))

    elif args.command == "history-list":
        for line in cli.history_list():
            print(line)

    elif args.command == "history-read":
        print(cli.history_read(version=args.version, file=args.file))

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
