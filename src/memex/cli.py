"""Unified memex CLI — run from anywhere.

Agent-facing surface with smart defaults. Advanced flags pass through
to underlying scripts transparently.
"""

from __future__ import annotations

import importlib
import json as json_mod
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from memex.paths import get_index_path

import typer

from memex.auth import app as auth_app

# ── App setup ───────────────────────────────────────────────────────

app = typer.Typer(
    name="memex",
    help="Memex — personal knowledge base CLI.",
    no_args_is_help=True,
)
index_app = typer.Typer(help="Index management.", no_args_is_help=True)
session_app = typer.Typer(help="Session discovery and import.", no_args_is_help=True)
backfill_app = typer.Typer(help="Backfill metadata.", no_args_is_help=True)
obs_app = typer.Typer(help="Observation-topic queries.", no_args_is_help=True)
topic_app = typer.Typer(help="Topic graph operations.", no_args_is_help=True)
app.add_typer(index_app, name="index")
app.add_typer(session_app, name="session")
app.add_typer(backfill_app, name="backfill")
app.add_typer(obs_app, name="obs")
app.add_typer(topic_app, name="topic")
app.add_typer(auth_app, name="auth")


# ── Internals ───────────────────────────────────────────────────────

def _setup() -> Path:
    """Chdir to the configured vault."""
    from memex.paths import get_memex_path

    vault = get_memex_path()
    os.chdir(vault)
    return vault


@contextmanager
def _script_context(script_name: str, args: list[str]) -> Iterator[Path]:
    """Give legacy entry points their argv and vault cwd for one invocation."""
    caller_cwd = Path.cwd()
    caller_argv = sys.argv
    try:
        vault = _setup()
        sys.argv = [script_name] + args
        yield vault
    finally:
        sys.argv = caller_argv
        os.chdir(caller_cwd)


def _delegate(script_name: str, args: list[str]) -> None:
    """Delegate to an existing script's main() via temporary sys.argv injection."""
    with _script_context(script_name, args):
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
    scope: Optional[str] = typer.Option(None, "--scope", help="observations (search learnings only)"),
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
    if scope:
        args.extend(["--scope", scope])
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
    with _script_context("memex.ask", [question]) as vault:
        index = get_index_path(vault)
        sys.argv.extend([
            "--index", str(index),
            "--vault", str(vault),
            "--depth", depth,
        ])
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
    folders: bool = typer.Option(False, "--folders", help="Audit project folders for detection drift (cwd-fragment names, duplicates)"),
    validate: bool = typer.Option(False, "--validate", help="Lint frontmatter (merged keys, missing title, dangling delimiter)"),
    signals: bool = typer.Option(False, "--signals", help="Open Recent-signals backlog per topic (closed-section-aware)"),
    condense: bool = typer.Option(False, "--condense", help="Projects whose overview lags their memos (condensed: date / memos_digested)"),
) -> None:
    """Vault health — crystallization readiness, unresolved links, folder drift, frontmatter lint, curation backlog."""
    args: list[str] = []
    if tier:
        args.extend(["--tier", tier])
    if json:
        args.append("--json")
    if verbose:
        args.append("-v")
    if folders:
        args.append("--folders")
    if validate:
        args.append("--validate")
    if signals:
        args.append("--signals")
    if condense:
        args.append("--condense")
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
) -> None:
    """Show project detection and pending memo status."""
    caller_cwd = _caller_cwd()
    _setup()

    from memex.scripts.utils import detect_project, get_pending_memos

    proj = project or detect_project(caller_cwd)
    pending = get_pending_memos()

    typer.echo(f"Project: {proj or 'unknown'}")
    if pending:
        typer.echo(f"Pending memos: {len(pending)}")


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


# ── similarity ─────────────────────────────────────────────────────

@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def similarity(
    ctx: typer.Context,
    threshold: float = typer.Option(0.85, help="Cosine similarity threshold (0-1)"),
    json: bool = typer.Option(False, "--json", help="JSON output"),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Show file paths"),
) -> None:
    """Detect near-duplicate or overlapping topics."""
    args: list[str] = ["--threshold", str(threshold)]
    if json:
        args.append("--json")
    if verbose:
        args.append("-v")
    args.extend(ctx.args)
    _delegate("similarity_detection.py", args)


# ── scrub ──────────────────────────────────────────────────────────

@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def scrub(
    ctx: typer.Context,
    path: str = typer.Argument(".", help="File or directory to scan (default: cwd)"),
    apply: bool = typer.Option(False, "--apply", help="Rewrite files in place with <REDACTED:provider>"),
    json: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """Detect (and optionally redact) API keys and credentials in vault files."""
    args: list[str] = [path]
    if apply:
        args.append("--apply")
    if json:
        args.append("--json")
    args.extend(ctx.args)
    _delegate("scrub.py", args)


# ── path ───────────────────────────────────────────────────────────

@app.command()
def path(
    index: bool = typer.Option(False, "--index", help="Print the index path instead of the vault path"),
) -> None:
    """Print the resolved vault path (or, with --index, the index path)."""
    vault = _setup()
    typer.echo(get_index_path(vault) if index else vault)


# ── mark-saved ─────────────────────────────────────────────────────

@app.command()
def mark_saved() -> None:
    """Mark current session's memo as saved (prevents duplicate generation)."""
    import json as _json
    import os as _os

    state_dir = Path.home() / ".memex" / "session-state"
    if not state_dir.exists():
        typer.echo("No session state found.", err=True)
        raise typer.Exit(1)

    # Prefer the harness-provided session id when available. The old heuristic
    # (newest state file by mtime, no cwd filter) cross-contaminates between
    # concurrent sessions in different projects — running `memex mark-saved`
    # from cwd A could mark a session in cwd B whose state file was touched a
    # moment later. CLAUDE_CODE_SESSION_ID is set by Claude Code 2.1+ inside
    # any tool/CLI invoked from a session, which is the only context where
    # mark-saved gets called in practice.
    session_id_env = _os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    state_file = None
    if session_id_env:
        candidate = state_dir / f"{session_id_env[:16]}.json"
        if candidate.exists():
            state_file = candidate

    if state_file is None:
        # Fallback: newest state file by mtime. Best-effort when the harness
        # didn't expose the session id (older Claude Code, ad-hoc CLI use).
        state_files = sorted(state_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
        if not state_files:
            typer.echo("No active session found.", err=True)
            raise typer.Exit(1)
        state_file = state_files[0]
        if session_id_env:
            # We had the env var but no matching state file — warn so the
            # user can spot config drift instead of silently fixing the wrong
            # session.
            typer.echo(
                f"WARN: CLAUDE_CODE_SESSION_ID={session_id_env[:16]} had no state file; "
                f"falling back to newest-by-mtime ({state_file.stem})",
                err=True,
            )

    try:
        state = _json.loads(state_file.read_text())
    except (_json.JSONDecodeError, ValueError):
        typer.echo("Could not read session state.", err=True)
        raise typer.Exit(1)

    session_prefix = state_file.stem

    from memex.scripts.utils import mark_session_phase

    pending_dir = Path.home() / ".memex" / "pending-memos"
    # Resolve the FULL session id for the canonical state key. The 16-char
    # state-file prefix is not a valid key: PreCompact (and every other
    # reader) looks up the full id via is_session_processed, so marking under
    # the prefix silently misses and PreCompact re-signals an already-saved
    # session as a stale orphan (v0.16.4 fix; 305 prefix keys had accumulated
    # in state.json by 2026-08-25). Sources, in trust order: the harness env
    # var when it matches this state file, then a pending signal's recorded
    # id, then the prefix as a last resort (is_session_processed now also
    # falls back to prefix keys, so even that degrades gracefully).
    full_session_id = session_prefix
    if session_id_env and session_id_env[:16] == session_prefix:
        full_session_id = session_id_env
    if pending_dir.exists():
        for pf in pending_dir.glob("*.json"):
            try:
                signal = _json.loads(pf.read_text())
            except (_json.JSONDecodeError, ValueError):
                continue
            if signal.get("session_id", "")[:16] == session_prefix:
                if full_session_id == session_prefix:
                    full_session_id = signal["session_id"]
                # Clean up the signal even when the env var already gave us
                # the full id — a signal for a saved session is exactly the
                # stale-orphan state this command exists to prevent. Unlink
                # races (concurrent mark-saved, reconcile-orphans) must not
                # abort between mark_session_phase and the session-state
                # write below — missing=fine, someone else cleaned it up.
                try:
                    pf.unlink()
                except OSError:
                    pass
                break

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


@index_app.command(name="embed-missing")
def index_embed_missing(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    """Embed chunks and observations currently missing from vec tables.

    Idempotent. Run after a rebuild that reported embedding gaps
    (e.g. from an expired API key or rate-limit exhaustion) once the
    root cause has been fixed. Exits with non-zero status if any items
    could not be embedded, so the command is safe to chain in scripts.
    """
    args = ["--embed-missing"]
    if json_out:
        args.append("--json")
    _delegate("index_rebuild.py", args)


@index_app.command(name="migrate-vec")
def index_migrate_vec(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    """Migrate vec tables to `index_dimensions` + metadata columns.

    Matryoshka-truncates stored embeddings to the configured
    `embeddings.index_dimensions` and adds doc_project/doc_type/doc_date
    columns so search can push project/type/date filters into the KNN. No
    API calls — vectors come from the existing index (full fidelity stays
    in embedding_cache). Idempotent. Back up `_index.sqlite` first.
    """
    args = ["--migrate-vec"]
    if json_out:
        args.append("--json")
    _delegate("index_rebuild.py", args)


@index_app.command(name="vacuum")
def index_vacuum(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    """VACUUM the index to reclaim free pages.

    Dropping the old vec tables during `migrate-vec` leaves free pages in
    `_index.sqlite` — the file won't shrink until vacuumed. Needs free disk
    roughly equal to the current file size for the temporary copy.
    """
    args = ["--vacuum"]
    if json_out:
        args.append("--json")
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


@session_app.command(
    name="reconcile-orphans",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def reconcile_orphans(
    ctx: typer.Context,
    apply: bool = typer.Option(False, "--apply", help="Clear signals whose memo is stamped with their session_id (default: dry-run)"),
    trust_window: bool = typer.Option(False, "--trust-window", help="With --apply, also clear likely matches (transcript Write or date window)"),
) -> None:
    """Clear orphan pending-memo signals whose session already has a memo."""
    args: list[str] = []
    if apply:
        args.append("--apply")
    if trust_window:
        args.append("--trust-window")
    args.extend(ctx.args)
    _delegate("reconcile_orphans.py", args)


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


@backfill_app.command(name="topic-tags", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def topic_tags(ctx: typer.Context) -> None:
    """Backfill observation topic tags from memo frontmatter."""
    _delegate("backfill_topic_tags.py", ctx.args)


# ── obs ────────────────────────────────────────────────────────────

@obs_app.command()
def topic(
    slug: str = typer.Argument(..., help="Topic slug (e.g. agent-architecture)"),
    limit: Optional[int] = typer.Option(None, help="Max observations"),
    json: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """List all observations for a topic."""
    vault = _setup()
    index = get_index_path(vault)

    from memex.observations import (
        fetch_observations_by_topic,
        init_observation_schema,
    )

    from memex.db_utils import connect_index
    conn = connect_index(index)
    try:
        init_observation_schema(conn)
        observations = fetch_observations_by_topic(conn, slug, limit=limit)
        if not observations:
            typer.echo(f"No observations for topic '{slug}'", err=True)
            raise typer.Exit(1)
        if json:
            typer.echo(json_mod.dumps([
                {"content": o.content, "doc_path": o.doc_path, "type": o.obs_type, "date": o.created_at}
                for o in observations
            ], indent=2))
        else:
            typer.echo(f"# {slug} — {len(observations)} observations\n")
            for o in observations:
                typer.echo(f"  [{o.created_at[:10]}] {o.content}")
    finally:
        conn.close()


@obs_app.command()
def stats() -> None:
    """Show observation counts per topic."""
    vault = _setup()
    index = get_index_path(vault)

    from memex.observations import (
        init_observation_schema,
        observation_count,
        topic_observation_counts,
    )

    from memex.db_utils import connect_index
    conn = connect_index(index)
    try:
        init_observation_schema(conn)
        total = observation_count(conn)
        counts = topic_observation_counts(conn)
        tagged = sum(c for _, c in counts)
        typer.echo(f"Total observations: {total}")
        typer.echo(f"Tagged: {tagged} across {len(counts)} topics\n")
        for slug, cnt in counts:
            typer.echo(f"  {cnt:4d}  {slug}")
    finally:
        conn.close()


@obs_app.command()
def retag(
    old: str = typer.Argument(..., help="Old topic slug"),
    new: str = typer.Argument(..., help="New topic slug"),
) -> None:
    """Retag observations from one topic to another (for merges)."""
    vault = _setup()
    index = get_index_path(vault)

    from memex.observations import (
        doc_paths_for_topic,
        init_observation_schema,
        retag_topic,
    )
    from memex.sidecars import write_sidecar

    from memex.db_utils import connect_index, writer_lock

    # writer_lock: retag now also rewrites sidecar files, so it must not
    # overlap a full rebuild's ATTACH snapshot (same convention as reassign).
    with writer_lock():
        conn = connect_index(index)
        try:
            init_observation_schema(conn)
            # Collect BEFORE retagging — the topic tag is what's changing, so
            # the affected doc_paths are only knowable against the OLD slug.
            affected_doc_paths = doc_paths_for_topic(conn, old)
            moved = retag_topic(conn, old, new)
            for doc_path in affected_doc_paths:
                write_sidecar(conn, vault, doc_path)
            conn.commit()
            typer.echo(f"Retagged {moved} observations: {old} → {new}")
        finally:
            conn.close()


@obs_app.command()
def reassign(
    from_prefix: str = typer.Option(..., "--from-prefix", help="Doc-path prefix to rewrite (e.g. 'projects/Apps-pi-proxy/')"),
    to_prefix: str = typer.Option(..., "--to-prefix", help="Replacement prefix (e.g. 'projects/pi-proxy/')"),
    apply: bool = typer.Option(False, "--apply", help="Actually update rows (default: dry-run)"),
    json: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """Rewrite ``doc_path`` prefix on observations + chunks across a folder rename.

    Promotes the manual SQL UPDATE pattern from the 2026-05-25 Apps-pi-proxy
    migration to a first-class CLI. Preserves all observations + chunks
    across the rename (cf. the morning's video-production migration which
    lost 43 obs to cascade-delete).

    Recommended workflow:

      1. ``git mv`` the folder (e.g., ``projects/Apps-X/`` → ``projects/X/``)
      2. Update memo frontmatter ``project:`` field in moved memos
      3. ``memex obs reassign --from-prefix projects/Apps-X/ --to-prefix projects/X/``
         (dry-run; verify obs_matched + chunks_matched look right)
      4. Same command + ``--apply`` to commit
      5. ``memex index rebuild --incremental`` to pick up the renames

    Dry-run by default to match ``scrub`` and ``backfill`` conventions.

    Exit codes:
      0 — success (dry-run or apply)
      1 — vault or arguments invalid
      2 — apply succeeded but invariant violated (matched != updated)
      3 — IntegrityError (UNIQUE constraint collision under to_prefix)
    """
    vault = _setup()
    index = get_index_path(vault)

    from memex.observations import (
        init_observation_schema,
        reassign_doc_path_prefix,
    )
    from memex.sidecars import sidecar_path, write_sidecar
    from memex.db_utils import connect_index, writer_lock
    import sqlite3

    # writer_lock blocks concurrent `memex index rebuild --full` from
    # ATTACH-snapshotting a moving target. Same convention as
    # extract.py::main and memex.dreamer.
    with writer_lock():
        conn = connect_index(index)
        try:
            # Skip DDL when tables already exist — avoids unnecessary DDL
            # in dry-run (which doesn't commit) and reflects that a fresh
            # index with no observations table can't have any rows to
            # reassign anyway.
            has_obs = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='observations'"
            ).fetchone()
            if not has_obs:
                init_observation_schema(conn)
                conn.commit()

            # Collect the OLD doc_paths BEFORE the UPDATE — only knowable
            # while the prefix still matches. Only acted on for --apply.
            old_doc_paths = (
                [
                    row[0]
                    for row in conn.execute(
                        "SELECT DISTINCT doc_path FROM observations "
                        "WHERE SUBSTR(doc_path, 1, ?) = ?",
                        (len(from_prefix), from_prefix),
                    ).fetchall()
                ]
                if apply
                else []
            )

            try:
                stats = reassign_doc_path_prefix(
                    conn,
                    from_prefix=from_prefix,
                    to_prefix=to_prefix,
                    dry_run=not apply,
                )
            except ValueError as e:
                typer.echo(f"Error: {e}", err=True)
                raise typer.Exit(1)
            except sqlite3.IntegrityError as e:
                conn.rollback()
                typer.echo(f"IntegrityError (UNIQUE collision under to_prefix): {e}", err=True)
                typer.echo("No changes committed. Likely cause: a doc already exists at the target prefix.", err=True)
                raise typer.Exit(3)

            if apply:
                # Invariant check before commit: matched must equal updated.
                if stats["obs_updated"] != stats["obs_matched"] or stats["chunks_updated"] != stats["chunks_matched"]:
                    conn.rollback()
                    typer.echo(
                        f"Invariant violated: obs matched={stats['obs_matched']} updated={stats['obs_updated']}, "
                        f"chunks matched={stats['chunks_matched']} updated={stats['chunks_updated']}. Rolled back.",
                        err=True,
                    )
                    raise typer.Exit(2)

                # Write-through: a sidecar has no doc_path field — location
                # implies it — so a file move IS a reassign. Render a fresh
                # sidecar at the new location from the now-updated DB rows,
                # then remove the old one. The old sidecar may already be
                # gone if the user `git mv`'d it along with the folder —
                # nothing to unlink in that case.
                for old_doc_path in old_doc_paths:
                    new_doc_path = to_prefix + old_doc_path[len(from_prefix):]
                    written = write_sidecar(conn, vault, new_doc_path)
                    if written is None:
                        # New sidecar could not be written (typically the
                        # target folder doesn't exist yet — reassign ran
                        # before the `git mv` step). Keep the old sidecar:
                        # it is the only vault-side copy of these rows.
                        typer.echo(
                            f"  warning: kept old sidecar for {old_doc_path} — "
                            f"new sidecar for {new_doc_path} was not written",
                            err=True,
                        )
                        continue
                    old_sidecar = sidecar_path(vault, old_doc_path)
                    if old_sidecar is not None and old_sidecar.exists():
                        old_sidecar.unlink()
                    conn.execute(
                        "DELETE FROM obs_sidecars WHERE doc_path = ?", (old_doc_path,)
                    )

                conn.commit()

            if json:
                typer.echo(json_mod.dumps(stats, indent=2))
            else:
                verb = "Would reassign" if not apply else "Reassigned"
                typer.echo(f"{verb} {stats['obs_matched']} observations and {stats['chunks_matched']} chunks")
                typer.echo(f"  from_prefix: {from_prefix}")
                typer.echo(f"  to_prefix:   {to_prefix}")
                if not apply:
                    typer.echo("  (dry-run — re-run with --apply to commit)")
                else:
                    typer.echo(f"  obs_updated={stats['obs_updated']}, chunks_updated={stats['chunks_updated']} — committed")
                    typer.echo("  next: memex index rebuild --incremental")
        finally:
            conn.close()


@obs_app.command()
def orphans(
    apply: bool = typer.Option(False, "--apply", help="Delete the orphaned rows"),
    json: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """Report (or prune) index rows whose parent observation no longer exists.

    Read-only by default. Orphans are invisible to every JOIN-ing read path,
    so they surface only here — and they cost vector-search recall, because
    they consume KNN result slots before the join discards them.
    """
    vault = _setup()
    index = get_index_path(vault)

    from memex.observations import (
        count_orphaned_observation_rows,
        delete_orphaned_observation_rows,
        init_observation_schema,
        unchecked_mirror_tables,
    )

    from contextlib import nullcontext

    from memex.db_utils import connect_index, load_vec_extension, writer_lock
    # Same advisory lock every other index writer takes, acquired BEFORE the
    # connection is opened: a rebuild that completes while we wait would swap
    # the file under an already-open handle and the prune would hit a stale DB.
    lock = writer_lock() if apply else nullcontext()
    with lock:
        _obs_orphans_report(index, apply, json, connect_index, load_vec_extension,
                            init_observation_schema, count_orphaned_observation_rows,
                            delete_orphaned_observation_rows, unchecked_mirror_tables)


def _obs_orphans_report(index, apply, json, connect_index, load_vec_extension,
                        init_observation_schema, count_orphaned_observation_rows,
                        delete_orphaned_observation_rows, unchecked_mirror_tables) -> None:
    conn = connect_index(index)
    try:
        load_vec_extension(conn)
        init_observation_schema(conn)
        if apply:
            result = delete_orphaned_observation_rows(conn)
            conn.commit()
        else:
            result = count_orphaned_observation_rows(conn)
        total = sum(result.values())
        # Tables absent from `result` were never queried (e.g. sqlite-vec
        # unavailable, so `vec_observations` was never created). Reported on
        # BOTH paths — a machine-readable report that lists only what it
        # measured is the one most likely to be trusted as complete.
        unchecked = unchecked_mirror_tables(conn, result)

        if json:
            typer.echo(json_mod.dumps({
                "applied": apply,
                "total": total,
                "tables": result,
                "unchecked": unchecked,
            }, indent=2))
        else:
            verb = "Deleted" if apply else "Orphaned"
            if not result:
                typer.echo("No mirror tables could be checked.")
            elif total == 0:
                typer.echo("No orphaned rows — every mirror row has a live parent observation.")
            else:
                typer.echo(f"{verb} {total} orphaned row(s):")
                for table, count in result.items():
                    typer.echo(f"  {count:6d}  {table}")
                if not apply:
                    typer.echo("\n  (read-only — re-run with --apply to remove)")
            if unchecked:
                typer.echo(f"\n  not checked (table absent): {', '.join(unchecked)}")

        # Exit 2 distinguishes "some mirror could not be checked" from a clean
        # 0, so a scripted caller cannot read a partial answer as an all-clear.
        if total and not apply:
            raise typer.Exit(1)
        if unchecked:
            raise typer.Exit(2)
    finally:
        conn.close()


@obs_app.command()
def untagged(
    limit: int = typer.Option(50, help="Max observations to show"),
    json: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """List observations with no topic tags — signals for new topics."""
    vault = _setup()
    index = get_index_path(vault)

    from memex.observations import init_observation_schema

    from memex.db_utils import connect_index
    conn = connect_index(index)
    try:
        init_observation_schema(conn)
        rows = conn.execute(
            """
            SELECT o.id, o.content, o.doc_path, o.created_at
            FROM observations o
            WHERE o.id NOT IN (SELECT observation_id FROM observation_topics)
            ORDER BY o.doc_path, o.id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE id NOT IN (SELECT observation_id FROM observation_topics)"
        ).fetchone()[0]
        if json:
            typer.echo(json_mod.dumps([
                {"id": r[0], "content": r[1], "doc_path": r[2], "date": r[3]}
                for r in rows
            ], indent=2))
        else:
            typer.echo(f"# Untagged observations: {total}\n")
            current_project = ""
            for obs_id, content, doc_path, created_at in rows:
                project = doc_path.split("/")[1] if "/" in doc_path else ""
                if project != current_project:
                    current_project = project
                    typer.echo(f"\n  [{project}]")
                typer.echo(f"    {obs_id}: {content[:120]}")
            if total > limit:
                typer.echo(f"\n  ... and {total - limit} more (use --limit to see all)")
    finally:
        conn.close()


@obs_app.command(name="export-sidecars")
def export_sidecars_cmd(
    apply: bool = typer.Option(False, "--apply", help="Actually write sidecar files (default: dry-run)"),
    force: bool = typer.Option(False, "--force", help="Overwrite sidecars that differ from DB state"),
    json: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """One-off migration: write every doc's `.obs.jsonl` sidecar from the DB.

    Dry-run by default (repo convention, cf. `reassign`, `scrub`). Without
    `--force`, an existing sidecar that differs from the rendered DB state is
    left untouched and reported as a conflict — run this only on the machine
    of record, since on a stale-DB machine (e.g. m5) it would otherwise
    clobber sidecars synced in from elsewhere.
    """
    vault = _setup()
    index = get_index_path(vault)

    from memex.observations import init_observation_schema
    from memex.sidecars import export_sidecars, find_sidecars
    from memex.db_utils import connect_index, writer_lock

    with writer_lock():
        conn = connect_index(index)
        try:
            init_observation_schema(conn)
            # Addendum A4 (2026-09-13): zero sidecars on disk means this is
            # the first-ever export — the operating assumption throughout
            # (see maintenance.md) is that observation-mutating commands run
            # from one machine at a time, and this migration in particular
            # must run from the machine whose DB is authoritative.
            if not find_sidecars(vault):
                typer.echo(
                    "Warning: vault has zero sidecars — this looks like the "
                    "first export. Run this only on the machine of record "
                    "(m4max). On any other machine, this DB is a stale copy "
                    "and exporting would clobber sidecars synced in from "
                    "elsewhere once they arrive.",
                    err=True,
                )
            stats = export_sidecars(conn, vault, apply=apply, force=force)
            if apply:
                conn.commit()

            if json:
                typer.echo(json_mod.dumps(stats, indent=2))
            else:
                verb = "Wrote" if apply else "Would write"
                typer.echo(f"{verb} {stats['written'] or stats['would_write']} sidecar(s)")
                typer.echo(f"  current (already up to date): {stats['current']}")
                if stats["conflict"]:
                    typer.echo("  conflict (differs from DB, left untouched — re-run with --force):")
                    for doc_path in stats["conflict"]:
                        typer.echo(f"    {doc_path}")
                if stats["unportable"]:
                    typer.echo("  unportable (absolute or escapes vault, skipped):")
                    for doc_path in stats["unportable"]:
                        typer.echo(f"    {doc_path}")
                if not apply:
                    typer.echo("  (dry-run — re-run with --apply to write)")
        finally:
            conn.close()


@obs_app.command(name="ingest-sidecars")
def ingest_sidecars_cmd(
    force: bool = typer.Option(False, "--force", help="Re-ingest every sidecar, ignoring recorded hashes"),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    """Diff every vault sidecar into the index without a full rebuild.

    Run this after an iCloud sync brings in sidecars from another machine —
    cheaper than `memex index rebuild --incremental` when the documents
    themselves haven't changed. Holds the writer lock and commits. Prints a
    hint to run `memex index embed-missing` when any observation was
    inserted (sidecar ingest never writes vectors).
    """
    vault = _setup()
    index = get_index_path(vault)

    from memex.observations import init_observation_schema
    from memex.sidecars import ingest_all_sidecars
    from memex.db_utils import connect_index, writer_lock

    with writer_lock():
        conn = connect_index(index)
        try:
            init_observation_schema(conn)
            stats = ingest_all_sidecars(conn, vault, indexed_paths=None, force=force)
            conn.commit()

            if json_out:
                typer.echo(json_mod.dumps(stats, indent=2))
            else:
                typer.echo(
                    f"Sidecars: {stats['files']} found, {stats['ingested']} ingested, "
                    f"{stats['unchanged']} unchanged, {stats['errors']} error(s)"
                )
                typer.echo(
                    f"  observations: {stats['inserted']} inserted, {stats['deleted']} deleted, "
                    f"{stats['updated']} updated, {stats['retagged']} retagged, "
                    f"{stats['skipped_foreign']} skipped (foreign hash), "
                    f"{stats['adopted']} adopted (renamed doc)"
                )
                if stats["empty"]:
                    typer.echo(f"  {stats['empty']} empty sidecar(s) skipped (never authoritative)")
                if stats["foreign_conflicts"]:
                    typer.echo(
                        f"  ⚠️  {len(stats['foreign_conflicts'])} foreign-hash conflict(s) — "
                        "see `memex obs sidecars`"
                    )
                if stats["pending_sources_unresolved"]:
                    typer.echo(
                        f"  {stats['pending_sources_unresolved']} source_obs reference(s) "
                        "could not be resolved"
                    )
            if stats["inserted"] > 0:
                typer.echo("  next: memex index embed-missing", err=True)
            if stats["errors"]:
                raise typer.Exit(1)
        finally:
            conn.close()


@obs_app.command(name="sidecars")
def sidecars_health_cmd(
    json: bool = typer.Option(False, "--json", help="JSON output"),
) -> None:
    """Health report for vault-backed observation sidecars.

    Read-only. Exits 0 regardless of findings — this is a report, not a
    gate; act on `missing`/`stale` with `export-sidecars`/`ingest-sidecars`.
    """
    vault = _setup()
    index = get_index_path(vault)

    from memex.observations import init_observation_schema
    from memex.sidecars import sidecar_health
    from memex.db_utils import connect_index

    conn = connect_index(index)
    try:
        init_observation_schema(conn)
        report = sidecar_health(conn, vault)

        if json:
            typer.echo(json_mod.dumps(report, indent=2))
        else:
            typer.echo(f"Sidecars on disk: {report['sidecar_count']}")
            typer.echo(f"Missing (DB obs, no sidecar): {len(report['missing'])}")
            for doc_path in report["missing"]:
                typer.echo(f"  {doc_path}")
            typer.echo(f"Orphan (sidecar, no live doc): {len(report['orphan'])}")
            for doc_path in report["orphan"]:
                typer.echo(f"  {doc_path}")
            typer.echo(f"Stale (pending ingest): {len(report['stale'])}")
            for doc_path in report["stale"]:
                typer.echo(f"  {doc_path}")
            if report["empty"]:
                typer.echo(f"Empty (never authoritative, not ingested): {len(report['empty'])}")
                for doc_path in report["empty"]:
                    typer.echo(f"  {doc_path}")
            if report["foreign_conflicts"]:
                typer.echo(f"Foreign conflicts (content_hash claimed elsewhere): {len(report['foreign_conflicts'])}")
                for pair in report["foreign_conflicts"]:
                    typer.echo(f"  {pair['doc_path']} <-> {pair['foreign_doc_path']}")
            if report["pending_sources"]:
                typer.echo(f"Pending source_obs references: {report['pending_sources']}")
            if report["conflicts"]:
                typer.echo(f"Conflict copies (iCloud): {len(report['conflicts'])}")
                for path in report["conflicts"]:
                    typer.echo(f"  {path}")
            if report["unportable"]:
                typer.echo(f"Unportable doc_paths: {len(report['unportable'])}")
                for doc_path in report["unportable"]:
                    typer.echo(f"  {doc_path}")
    finally:
        conn.close()


# ── topic ──────────────────────────────────────────────────────────

@topic_app.command(name="resolve")
def topic_resolve(
    slug: str = typer.Argument(..., help="Topic slug or vault-relative path"),
    vault: Optional[Path] = typer.Option(None, help="Override vault path"),
) -> None:
    """Resolve a ``redirect_to`` chain to its destination.

    Prints the destination slug (or vault-relative path without the
    ``.md`` suffix) on stdout and exits 0 on success. Exits 1 with an
    empty stdout when the chain terminates at an archived stub without
    ``redirect_to``, when a target is missing, when a cycle is detected,
    or when the chain exceeds the hop limit. Warnings go to stderr.
    """
    from memex.scripts.topic_resolve import resolve

    if vault is not None:
        v = vault.expanduser().resolve()
    else:
        from memex.paths import get_memex_path

        v = get_memex_path()

    if not v.exists():
        typer.echo(f"Error: vault path does not exist: {v}", err=True)
        raise typer.Exit(1)

    result = resolve(v, slug)
    if result is None:
        raise typer.Exit(1)
    typer.echo(result)


# ── entry point ─────────────────────────────────────────────────────

def main() -> None:
    app()


if __name__ == "__main__":
    main()
