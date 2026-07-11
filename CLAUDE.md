# Memex - Collaborative Memory for Human-AI Work

> **This is the live `memex-local` marketplace source**, not legacy or
> disposable code. `~/.claude/plugins/known_marketplaces.json` →
> `memex-local.source.path` points at this directory. It's a clean public
> clone (code only, no vault data) kept in lockstep with the private vault at
> `~/Documents/Apps/memex/` — currently v0.15.9. The vault itself is
> deliberately *not* registered as the marketplace source: doing so would
> snapshot 5-8GB of user data (`projects/`, `_index.sqlite`, `topics/`) into
> the plugin cache on every install/update. Do not delete or treat this
> directory as disposable.

Captures the collaborative process of working with Claude Code — decisions, deliberations, tensions, and breakthroughs — as searchable, interconnected knowledge in an Obsidian vault.

## What Makes This Different from Auto-Memory

Claude Code's built-in auto-memory stores preferences and conventions as flat key-value pairs — working memory for *how* you work. Memex captures the **collaborative journey**: full session transcripts and structured memos for every compaction window, preserving not just what was decided but how you and the user got there. Garden-tending — periodic review, condensation, synthesis — means the vault grows as a shared practice, not just a storage layer.

## Quick Start

```bash
memex status                              # what's in the vault
memex search "authentication"             # hybrid search (RRF default)
memex search "plugin" --since=7d          # recent docs only
memex index rebuild --incremental         # rebuild after changes
```

The `memex` CLI resolves the vault path automatically from any directory. Obsidian CLI and dreamer still need `cd` to the vault.

## First-Run Setup (Guide the User)

Detect a fresh install by: no `~/.memex/config.json`, no `projects/` directory, or empty vault. Then:

1. **Vault path** — ask where they cloned this repo; write `~/.memex/config.json` → `memex_path`.
2. **Obsidian vault name** — if it differs from "memex", note it (`/memex:open obsidian` defaults to `obsidian://open?vault=memex`).
3. **Embedding provider** — Gemini Embedding 2 (cloud, needs `GEMINI_API_KEY`), LM Studio (local fallback), or skip (keyword-only).
4. **Import existing sessions** — `memex session discover --triage` then `memex session discover --import --apply` to bring `~/.claude/projects/` transcripts into the vault.
5. **Build initial index** — `memex index rebuild --full`.
6. **MEMORY.md** — customize with active projects and preferences.

`uv run scripts/setup.py` handles steps 1-3 interactively. Project name detection is automatic (git remote → git root → cwd); `project_mappings` in config is a manual override for edge cases only, read by `detect_project()` in `src/memex/scripts/utils.py`.

## How Claude Uses This Plugin

Claude acts as the **memex curator** — condensing project knowledge into `_project.md` overviews, maintaining `[[wikilinks]]`, cultivating the vault's knowledge topology. Claude searches on demand rather than relying on pre-loaded context (see `configuration.md` rule for the SessionStart injection policy).

## CLI Commands

```bash
memex search <query>        # Hybrid search (FTS + vector)
memex ask <question>        # Deep retrieval with observations
memex timeline <date>       # Browse by date (yesterday, 7d, last week)
memex read <path>           # Read vault document to stdout
memex path                  # Print resolved vault path
memex check                 # Vault health — crystallization readiness
memex check --folders       # Detect project-folder drift
memex check --validate      # Lint frontmatter
memex status                # Document count, chunks, last rebuild
memex context               # Project detection and pending memo status
memex similarity            # Detect near-duplicate topics (--threshold, --json)
memex scrub <path>          # Detect API keys / secrets (--apply redacts in place)
memex mark-saved            # Mark memo saved (prevents duplicate generation)
memex sync                  # Sync auto-memory into vault
memex graph <subcmd>        # Backlinks, orphans, tags, stats
memex topic resolve <slug>  # Resolve redirect_to chain
memex index rebuild         # Rebuild search index (--full for embeddings)
memex index status          # Index health JSON
memex index embed-missing   # Retry embeddings after API failures
memex index migrate-vec     # Truncate vec tables to index_dimensions
memex index vacuum          # Reclaim free pages after migrate-vec
memex session discover      # Find unprocessed sessions
memex session import        # Import discovered sessions (--apply)
memex session reconcile-orphans  # Clear stale pending-memo signals (--apply)
memex obs topic <slug>      # All observations for a topic
memex obs stats             # Observation counts per topic
memex obs retag <old> <new> # Retag observations (for topic merges)
memex obs untagged          # Observations with no topics
memex backfill obs          # Extract observations from memos
memex backfill tokens       # Backfill token counts on transcripts
memex backfill memos        # Backfill has_memo on transcripts
memex backfill topic-tags   # Propagate memo topics to observations
```

Retrieval (search, timeline, ask, load, synthesize, merge, maintain, retry, backfill) is **skill-based** as of v0.11 — Claude invokes the `recall` skill for retrieval questions and `garden-tending` for synthesis/maintenance. Only `/memex:save`, `/memex:status`, `/memex:open` remain as slash commands.

## Available Skills

| Skill | Purpose | When to Invoke |
|-------|---------|---------------|
| `recall` | Retrieve session memory — temporal, keyword, deep synthesis, or direct load | "what did I do yesterday?", "why did we…", "load the X topic" |
| `garden-tending` | Full vault lifecycle: diagnose, condense, connect, grow, maintain | "tend the garden", "update project overview", "check vault health" |
| `curator-practice` | Autonomous curator operating philosophy | autonomous tending, scheduled/cron agents |
| `memo-writing` | Memo format + quality guidelines | `/memex:save`, "remember this" |
| `project-consolidation` | Merge drifted/duplicate project folders (preserves observations via `obs reassign`) | "consolidate project folders", after `memex check --folders` reports drift |

## Reinstalling / Clearing Cache

The marketplace is registered as **`memex-local`** (confirm against `~/.claude/plugins/known_marketplaces.json` if this ever drifts). Claude Code loads the plugin from `~/.claude/plugins/cache/`, not this live source — after changing `plugin.json` or hooks, reinstall:

```bash
claude plugin uninstall memex@memex-local --scope user && claude plugin install memex@memex-local --scope user
```

Already-open sessions keep the old config until restarted. The cache venv at `~/.claude/plugins/cache/memex-local/memex/<version>/` is separate from the vault's own venv — if plugin behavior differs from local runs, check the cache environment independently.

## Gotchas Not Covered in `.claude/rules/`

- **Project detection uses git root** — memos are stored by project detected from `cwd`, not the memex folder itself.
- **`${CLAUDE_PLUGIN_ROOT}` is cache, not vault** — in command files this env var points to the plugin cache location. Read `~/.memex/config.json` or use the `memex` CLI for vault path resolution.
- **`bin/memex` uses `PYTHONPATH=src` for live source** — the shell wrapper runs the local package without rebuilding a wheel, so edits are picked up immediately. Keep that behavior for local development.
- **Background bash output buffering** — `2>/dev/null`, `| head`, and `2>&1` redirects can swallow or buffer Python output in background tasks. Write to a file directly (`> /tmp/results.txt`) and `cat` it after, or use `PYTHONUNBUFFERED=1`.
- **Debug perf by narrowing, not orchestrating** — when something is slow, don't spawn background agents or build elaborate profiling harnesses; narrow to the exact call and inspect.
- **Two failures is information, three is a pattern** — if the same approach fails twice, change strategy entirely rather than tweaking flags.
- **`redirect_to:` resolver mechanics** — an archived topic (`status: archived` + `redirect_to: <target>`) has its "Recent signals" routed to the target by `memex topic resolve <slug>`, followed by `/memex:save` and the `memo-writing` skill (5-hop limit, cycles detected and reported on stderr rather than misreported as "exceeded hops"). Target can be a **bare slug** (`embedding-models` → `topics/embedding-models.md`) or a **vault-relative path** (`projects/foo/_project.md`), so a topic can redirect into a project overview. A **terminal archive** (`status: archived`, no `redirect_to:`) causes the resolver to emit `WARN: archived with no redirect_to — skipping signal` and drop the signal rather than land it on the stub — typical when content belongs in a `_project.md`. Implemented in `src/memex/scripts/topic_resolve.py`; verify a chain manually with `memex topic resolve <slug-or-path>` (exit 0 = ok, exit 1 = skip/warn).

## Where to Go Next

Domain-specific details load automatically via `.claude/rules/` when you work on relevant files:

| Rules File | Covers | Loaded When Editing |
|------------|--------|-------------------|
| `architecture.md` | Memo generation layers, session lifecycle, search pipeline, frontmatter schema, hook responsibilities | `src/memex/`, `hooks/`, `commands/`, `skills/` |
| `configuration.md` | Config paths, path resolution, session verbosity, linking conventions, security & privacy | `src/memex/`, `hooks/`, `.claude-plugin/` |
| `maintenance.md` | Periodic tasks, dev commands (rebuild, backfill, discover, sync), nightly rebuild, key rotation | `src/memex/`, `_views/`, `topics/` |
| `search-and-embeddings.md` | Embedding providers (Gemini primary, LM Studio fallback), Matryoshka truncation, chunking, search gotchas | `src/memex/scripts/search.py`, `hybrid_search.py`, `embeddings.py`, `index_rebuild.py` |
| `obsidian-cli.md` | Obsidian CLI commands, SQLite fallback, graph navigation, version dependencies | `scripts/obsidian_cli.py`, `graph_queries.py`, `crystallization_check.py` |
| `hooks.md` | Hook implementation details, timing constraints, hooks.json schema | `hooks/` |
| `plugin-authoring.md` | Error patterns for commands, skills, hooks, scripts, plugin cache, public-repo sync, vault operations | `commands/`, `skills/`, `hooks/`, `src/memex/`, `.claude-plugin/` |
| `python-patterns.md` | SQL/regex/sqlite3 patterns used across the codebase | `scripts/`, `hooks/` |
| `transcripts.md` | Transcript processing, JSONL format, system tag cleaning, triage scoring | transcript-related scripts |

For linking conventions (`[[wikilinks]]`, `redirect_to:` archive pattern), frontmatter schema, and security/privacy notes, see `configuration.md` and `architecture.md` above rather than duplicating them here.
