"""Unified memex CLI — run from anywhere.

Agent-facing surface with smart defaults. Advanced flags pass through
to underlying scripts transparently.
"""

from __future__ import annotations

import importlib
import json as json_mod
import os
import sys
from pathlib import Path
from typing import Optional

import typer


# ── App setup ───────────────────────────────────────────────────────

app = typer.Typer(
    name="memex",
    help="Memex — personal knowledge base CLI.",
    no_args_is_help=True,
)
index_app = typer.Typer(help="Index management.", no_args_is_help=True)
session_app = typer.Typer(help="Session discovery and import.", no_args_is_help=True)
backfill_app = typer.Typer(help="Backfill metadata.", no_args_is_help=True)
app.add_typer(index_app, name="index")
app.add_typer(session_app, name="session")
app.add_typer(backfill_app, name="backfill")


# ── Internals ───────────────────────────────────────────────────────

def _setup() -> Path:
    """Chdir to the configured vault."""
    from memex.paths import get_memex_path

    vault = get_memex_path()
    os.chdir(vault)
    return vault


def _delegate(script_name: str, args: list[str]) -> None:
    """Delegate to an existing script's main() via sys.argv injection."""
    _setup()
    sys.argv = [script_name] + args
    mod_name = "memex.scripts." + script_name.removesuffix(".py")
    mod = importlib.import_module(mod_name)
    try:
        mod.main()
    except SystemExit as e:
        if e.code:
            raise


def _caller_cwd() -> str:
    """Original cwd before _setup() changes directory."""
    return os.environ.get("MEMEX_CALLER_CWD", os.environ.get("PWD", os.getcwd()))


def _fmt(json: bool, paths: bool) -> str:
    """Resolve output format from boolean flags."""
    if json:
        return "json"
    if paths:
        return "paths"
    return "text"


# ── search ──────────────────────────────────────────────────────────

@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Keywords (use OR between terms)"),
    since: Optional[str] = typer.Option(None, help="Recency: 7d, 2w, yesterday"),
    project: Optional[str] = typer.Option(None, help="Filter by project"),
    type: Optional[str] = typer.Option(None, "--type", help="memo, transcript, concept"),
    limit: int = typer.Option(20, help="Max results"),
    json: bool = typer.Option(False, "--json", help="JSON output"),
    paths: bool = typer.Option(False, "--paths", help="One path per line"),
) -> None:
    """Search memos, transcripts, and concepts."""
    args = [query, "--format", _fmt(json, paths), "--limit", str(limit)]
    if since:
        args.extend(["--since", since])
    if project:
        args.extend(["--project", project])
    if type:
        args.extend(["--type", type])
    args.extend(ctx.args)
    _delegate("search.py", args)


# ── ask ─────────────────────────────────────────────────────────────

@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def ask(
    ctx: typer.Context,
    question: str = typer.Argument(..., help="Natural language question"),
    project: Optional[str] = typer.Option(None, help="Scope to project"),
    depth: str = typer.Option("quick", help="quick (fast) or thorough (semantic)"),
) -> None:
    """Deep retrieval — cross-session synthesis from memos and observations."""
    vault = _setup()
    index = vault / "_index.sqlite"
    sys.argv = [
        "memex.ask", question,
        "--index", str(index),
        "--vault", str(vault),
        "--depth", depth,
    ]
    if project:
        sys.argv.extend(["--project", project])
    sys.argv.extend(ctx.args)
    from memex.ask import main as ask_main
    ask_main()


# ── timeline ────────────────────────────────────────────────────────

@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def timeline(
    ctx: typer.Context,
    date: str = typer.Argument(..., help="yesterday, last week, 7d, 2026-03-15"),
    project: Optional[str] = typer.Option(None, help="Filter by project"),
    type: Optional[str] = typer.Option(None, "--type", help="memo or transcript"),
    json: bool = typer.Option(False, "--json", help="JSON output"),
    paths: bool = typer.Option(False, "--paths", help="One path per line"),
) -> None:
    """Browse sessions and memos by date."""
    args = [date, "--format", _fmt(json, paths)]
    if project:
        args.extend(["--project", project])
    if type:
        args.extend(["--type", type])
    args.extend(ctx.args)
    _delegate("temporal_scan.py", args)


# ── read ────────────────────────────────────────────────────────────

@app.command()
def read(
    path: str = typer.Argument(..., help="Relative path within vault"),
) -> None:
    """Read a vault document to stdout."""
    vault = _setup()
    target = (vault / path).resolve()
    try:
        target.relative_to(vault.resolve())
    except ValueError:
        typer.echo(f"Error: Path traversal blocked: {path}", err=True)
        raise typer.Exit(1)
    if not target.exists():
        typer.echo(f"Error: Not found: {path}\nFix: memex search '<keywords>' --paths", err=True)
        raise typer.Exit(1)
    try:
        typer.echo(target.read_text(), nl=False)
    except (UnicodeDecodeError, IsADirectoryError):
        typer.echo(f"Error: Not a text file: {path}", err=True)
        raise typer.Exit(1)


# ── check ───────────────────────────────────────────────────────────

@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def check(
    ctx: typer.Context,
    tier: Optional[str] = typer.Option(None, help="overdue, ready, maturing, seedling, all"),
    json: bool = typer.Option(False, "--json", help="JSON output"),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Show source files"),
) -> None:
    """Vault health — crystallization readiness, unresolved links."""
    args: list[str] = []
    if tier:
        args.extend(["--tier", tier])
    if json:
        args.append("--json")
    if verbose:
        args.append("-v")
    args.extend(ctx.args)
    _delegate("crystallization_check.py", args)


# ── status ──────────────────────────────────────────────────────────

@app.command()
def status() -> None:
    """Vault overview — document count, chunks, last rebuild."""
    vault = _setup()
    from memex.scripts.index_rebuild import get_index_status

    typer.echo(json_mod.dumps(get_index_status(vault), indent=2))


# ── context ─────────────────────────────────────────────────────────

@app.command()
def context(
    project: Optional[str] = typer.Option(None, help="Override project detection"),
    full: bool = typer.Option(False, help="Full context with memo content"),
    compact: bool = typer.Option(False, help="Minimal one-liner"),
) -> None:
    """On-demand project context — what SessionStart hook injects."""
    caller_cwd = _caller_cwd()
    vault = _setup()

    from memex.config import get_settings
    from memex.context import build_full_context, build_standard_context
    from memex.scripts.utils import detect_project

    settings = get_settings()
    proj = project or detect_project(caller_cwd)

    if compact:
        typer.echo(f"Memex: {proj or 'unknown'} project. Use `memex search` for past decisions.")
    elif full:
        typer.echo(build_full_context(vault, proj, settings) or "No context available.")
    else:
        typer.echo(build_standard_context(vault, proj, settings) or "No context available.")


# ── sync ────────────────────────────────────────────────────────────

@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def sync(ctx: typer.Context) -> None:
    """Sync Claude Code auto-memory into vault."""
    _delegate("sync_auto_memory.py", ctx.args)


# ── graph ───────────────────────────────────────────────────────────

@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def graph(ctx: typer.Context) -> None:
    """Knowledge graph — backlinks, orphans, tags, stats."""
    _delegate("graph_queries.py", ctx.args)


# ── path ───────────────────────────────────────────────────────────

@app.command()
def path() -> None:
    """Print the resolved vault path."""
    vault = _setup()
    typer.echo(vault)


# ── mark-saved ─────────────────────────────────────────────────────

@app.command()
def mark_saved() -> None:
    """Mark current session's memo as saved (prevents duplicate generation)."""
    import json as _json

    state_dir = Path.home() / ".memex" / "session-state"
    if not state_dir.exists():
        typer.echo("No session state found.", err=True)
        raise typer.Exit(1)

    state_files = sorted(state_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not state_files:
        typer.echo("No active session found.", err=True)
        raise typer.Exit(1)

    state_file = state_files[0]
    try:
        state = _json.loads(state_file.read_text())
    except (_json.JSONDecodeError, ValueError):
        typer.echo("Could not read session state.", err=True)
        raise typer.Exit(1)

    session_prefix = state_file.stem

    from memex.scripts.utils import mark_session_phase

    pending_dir = Path.home() / ".memex" / "pending-memos"
    full_session_id = session_prefix
    if pending_dir.exists():
        for pf in pending_dir.glob("*.json"):
            try:
                signal = _json.loads(pf.read_text())
                if signal.get("session_id", "")[:16] == session_prefix:
                    full_session_id = signal["session_id"]
                    pf.unlink()
                    break
            except (_json.JSONDecodeError, ValueError):
                continue

    mark_session_phase(full_session_id, "memo_generated")

    state["memo_saved"] = True
    state_file.write_text(_json.dumps(state))

    typer.echo(f"Memo marked as saved for session {session_prefix}")


# ── index ───────────────────────────────────────────────────────────

@index_app.command(name="status")
def index_status() -> None:
    """Show index statistics."""
    _delegate("index_rebuild.py", ["--status"])


@index_app.command(name="rebuild", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def index_rebuild(
    ctx: typer.Context,
    full: bool = typer.Option(False, "--full", help="Full rebuild with embeddings"),
) -> None:
    """Rebuild the search index (incremental by default)."""
    args = ["--full"] if full else ["--incremental"]
    args.extend(ctx.args)
    _delegate("index_rebuild.py", args)


# ── session ─────────────────────────────────────────────────────────

@session_app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def discover(
    ctx: typer.Context,
    triage: bool = typer.Option(False, "-t", "--triage", help="Score by viability"),
    min_score: Optional[int] = typer.Option(None, "--min-score", help="Minimum triage score"),
) -> None:
    """Find unprocessed sessions."""
    args: list[str] = []
    if triage:
        args.append("--triage")
    if min_score is not None:
        args.extend(["--min-score", str(min_score)])
    args.extend(ctx.args)
    _delegate("discover_sessions.py", args)


@session_app.command(name="import", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def session_import(
    ctx: typer.Context,
    apply: bool = typer.Option(False, "--apply", help="Actually import (default: dry-run)"),
) -> None:
    """Import discovered sessions."""
    args = ["--import"]
    if apply:
        args.append("--apply")
    args.extend(ctx.args)
    _delegate("discover_sessions.py", args)


# ── backfill ────────────────────────────────────────────────────────

@backfill_app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def tokens(ctx: typer.Context) -> None:
    """Backfill token counts into transcript frontmatter."""
    _delegate("backfill_tokens.py", ctx.args)


@backfill_app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def memos(ctx: typer.Context) -> None:
    """Backfill has_memo on transcripts."""
    _delegate("backfill_has_memo.py", ctx.args)


@backfill_app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def obs(ctx: typer.Context) -> None:
    """Extract observations from memos."""
    _delegate("extract_observations.py", ctx.args)


# ── entry point ─────────────────────────────────────────────────────

def main() -> None:
    app()


if __name__ == "__main__":
    main()
