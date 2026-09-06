# Memex - Personal Knowledge Base

Centralized knowledge base storing memos and transcripts from all Claude Code sessions.

## Quick Start

The `memex` CLI works from any directory. For Obsidian CLI and dreamer, `cd` to vault is still needed.

```bash
# Check vault status
memex status

# Search for something (RRF scoring is default)
memex search "authentication"

# Search recent docs only (7d, 2w, 3m)
memex search "plugin" --since=7d

# Rebuild index after changes
memex index rebuild --incremental
```

Semantic search requires a Gemini API key. Use `op run --env-file ~/.secrets.op -- memex search "query"` for an explicit 1Password-backed command, or `memex auth set-key` to opt into automatic loading from a local owner-only key file. Environment keys still work; keyword search remains available without a key. See [credential setup](docs/gemini-credentials.md).

## Your Role

You are the **memex curator**. Condense project knowledge into `_project.md` overviews, maintain `[[wikilinks]]`, and cultivate the vault's knowledge topology. Search the vault when you need context — don't rely on pre-loaded summaries.

## Folder Structure

```
memex/
├── projects/<name>/memos/       # Session memos per project
├── projects/<name>/auto-memory/ # Synced Claude Code auto-memory files
├── projects/<name>/transcripts/ # Full conversation logs
├── topics/                      # Cross-project concept notes + trails (type: trail)
├── src/memex/scripts/           # Core scripts (search, embeddings, etc.)
├── scripts/                     # Backward-compat shims → src/memex/scripts/
├── hooks/                       # Claude Code hooks (SessionStart, PreCompact, etc.)
├── commands/                    # Slash commands (/memex:*)
├── skills/                      # Intent-based skills
├── _meta/                       # Curator infrastructure (dashboard, log, tag taxonomy)
├── _views/                      # Obsidian Base views (.base)
├── _templates/                  # Note templates
└── .claude-plugin/              # Plugin manifest
```

The FTS5 + vector search + `observation_topics` index is not in the vault: it
lives at `~/.memex/_index.sqlite` by default (`memex path --index` resolves it;
`index_path` in `~/.memex/config.json` or `MEMEX_INDEX_PATH` overrides it). An
index left at `<vault>/_index.sqlite` by a pre-0.17.0 install is still used
from there.

## Knowledge Artifacts

The vault stores three kinds of knowledge, each with a different temperature:

| Artifact | Temperature | Maintenance | Purpose |
|----------|------------|-------------|---------|
| **Memos** | Cold (append-only) | None — write once | "What happened" — session records |
| **Topics** | Warm (periodically updated) | Rewrite during tending | "What is X" — encyclopedic reference |
| **Trails** (`type: trail`) | Warm (periodically extended) | Append during tending | "How X evolved" — narrative across projects |

Topics and trails both live in `topics/`. Trails are distinguished by `type: trail` in frontmatter. Topics are rewritten (synthesized); trails are extended (new chapters appended). Trails are for concepts with genuine cross-project temporal evolution.

## Plugin Commands

- `/memex:save [title]` - Save current context as memo
- `/memex:status` - Show index stats and pending memos
- `/memex:open` - Open vault in Finder/Obsidian

Retrieval is skill-based — Claude searches automatically when you ask about past work. Vault maintenance is handled by the garden-tending skill. Use `memex search` or `memex timeline` CLI for direct access.

## CLI Commands

Shell commands — work from any terminal, any agent:

```bash
memex search <query>        # Hybrid search (FTS + vector)
memex ask <question>        # Deep retrieval with observations
memex timeline <date>       # Browse by date (yesterday, 7d, last week)
memex read <path>           # Read vault document to stdout
memex path                  # Print resolved vault path
memex path --index          # Print resolved index path
memex check                 # Vault health — crystallization readiness
memex check --folders       # Detect project-folder drift (cwd-fragment names, duplicate/split folders)
memex check --validate      # Lint frontmatter (merged keys, missing title, dangling delimiter, no-frontmatter)
memex status                # Document count, chunks, last rebuild
memex context               # Project detection and pending memo status
memex auth set-key          # Save a key locally for automatic loading (hidden prompt)
memex auth status           # Show credential source without exposing the key or calling Gemini
memex auth clear-key        # Remove the saved local key
memex similarity            # Detect near-duplicate topics (--threshold, --json)
memex scrub <path>          # Detect API keys / secrets (--apply redacts in place)
memex mark-saved            # Mark memo saved (prevents duplicate generation)
memex sync                  # Sync auto-memory into vault
memex graph <subcmd>        # Backlinks, orphans, tags, stats
memex topic resolve <slug>  # Resolve redirect_to chain (exit 0=ok, 1=skip/error)
memex index rebuild         # Rebuild search index (--full for embeddings)
memex index status          # Index health JSON (doc/chunk counts, embedding gaps)
memex index embed-missing   # Embed chunks/obs missing from vec tables (retry after API failures)
memex index migrate-vec     # Truncate vec tables to index_dimensions + add metadata cols (no re-embed)
memex index vacuum          # VACUUM the index to reclaim free pages (e.g. after migrate-vec)
memex session discover      # Find unprocessed sessions
memex session import        # Import discovered sessions (--apply to execute, --exclude ID to skip)
memex session reconcile-orphans  # Clear stale pending-memo signals whose session was already saved (--apply)
memex obs topic <slug>      # All observations for a topic (cluster lookup)
memex obs stats             # Observation counts per topic
memex obs retag <old> <new> # Retag observations (for topic merges)
memex obs reassign --from-prefix X --to-prefix Y  # Rewrite obs+chunks doc_path (folder rename SOP)
memex obs untagged          # Observations with no topics (new-topic signals)
memex obs orphans           # Mirror rows whose parent observation is gone (--apply to prune)
memex backfill obs          # Extract observations from memos
memex backfill tokens       # Backfill token counts on transcripts
memex backfill memos        # Backfill has_memo on transcripts
memex backfill topic-tags   # Propagate memo topics to observations
```

## Available Skills

| Skill | Purpose | When to Invoke |
|-------|---------|---------------|
| `recall` | Retrieve session memory — temporal browsing, keyword search, deep cross-session synthesis, or direct file loading | "what did I do yesterday?", "why did we...", "what patterns across...", "load the X topic" |
| `garden-tending` | Full vault lifecycle: diagnose, condense, connect, grow, maintain | "tend the garden", "update project overview", "check vault health", "find broken links" |
| `curator-practice` | Autonomous curator operating philosophy: attention, judgment, initiative | Autonomous tending, "what should I work on next?", scheduled/cron agents |
| `memo-writing` | Format and quality guidelines | `/memex:save`, "remember this", or when [memex] nudge appears |
| `project-consolidation` | Safely merge drifted/duplicate project folders (preserves obs via `obs reassign`) | "consolidate project folders", "merge duplicate projects", "fix detection drift", or after `memex check --folders` reports drift |

Skills are intent-based: Claude decides when to invoke based on user questions. This is more flexible than hooks which run on events.

## Gotchas

Domain-specific gotchas are in `.claude/rules/` and load automatically when working on relevant files. These are universal:

- **`memex` CLI resolves vault path automatically** — No `cd` needed for `memex search`, `memex timeline`, etc. For Obsidian CLI (`uv run scripts/obsidian_cli.py`) and dreamer (`uv run python -m memex.dreamer`), `cd` to vault is still required
- **Observation topic slugs are not validated on insert** — `store_observation_topics` accepts any string. Use only slugs matching `topics/*.md` filenames. Invalid slugs create orphan rows in `observation_topics`. Use `memex obs untagged` during garden-tending to spot gaps
- **Never delete from `observations` directly — route through `delete_observation_ids`** — three tables mirror it by observation id (`fts_observations`.rowid, `vec_observations`.rowid, `observation_topics`.observation_id). A bare `DELETE FROM observations` leaves mirror rows that every JOIN-ing read path silently discards, so the damage never surfaces in search — it shows up only as counters claiming observations the vault cannot return, and as orphan rows consuming vector-search KNN slots. `_OBS_MIRROR_TABLES` in `observations.py` is the single registry; a new mirror table must be added there or `test_mirror_registry_covers_every_table_referencing_observations` fails. Check any index with `memex obs orphans` (read-only; `--apply` prunes)
- **`memex backfill obs` REPLACES a doc's observations, it does not append** — `store_observations` calls `delete_observations_for_doc(conn, memo_path)` first, so a second call for the same `--doc-path` silently destroys everything the first call stored. The output reports only `{"stored": N, "total": N}` and says nothing about what it deleted, so the loss is invisible: extracting 12 obs, then later extracting 5 more for the same memo, leaves you with **5**, not 17. **Always send the complete set for a doc in ONE call.** If you extend a memo mid-session, re-send the original observations together with the new ones. Verify with `memex obs stats` before and after — the total is the invariant (observed live 2026-07-21: 15694 → 15687 after a 5-obs "addition"; recovered by re-sending all 18)

## Where to Go Next

Domain-specific details load automatically via `.claude/rules/` when you work on relevant files:

| Rules File | Covers | Loaded When Editing |
|------------|--------|-------------------|
| `architecture.md` | Memo generation layers, session lifecycle, search pipeline, frontmatter schema | `scripts/`, `hooks/`, `commands/`, `skills/` |
| `maintenance.md` | Periodic tasks, dev commands (rebuild, backfill, discover, sync) | `scripts/`, `_views/`, `topics/` |
| `configuration.md` | Config paths, path resolution, linking conventions, security | `scripts/`, `hooks/`, `.claude-plugin/` |
| `search-and-embeddings.md` | Embedding providers (Gemini primary, LM Studio fallback), chunking, search gotchas | `scripts/{search,hybrid_search,embeddings,index_rebuild}.py` |
| `obsidian-cli.md` | Obsidian CLI 1.12.5 commands, SQLite fallback, graph navigation | `scripts/obsidian_cli.py`, `graph_queries.py`, `crystallization_check.py` |
| `transcripts.md` | Transcript processing, JSONL format, system tag cleaning | transcript-related scripts |
| `hooks.md` | Hook implementation details, timing constraints | `hooks/` |
| `plugin-authoring.md` | Error patterns for commands, skills, hooks, scripts, plugin cache | `commands/`, `skills/`, `hooks/`, `scripts/`, `.claude-plugin/` |
| `python-patterns.md` | Python patterns used across the codebase | `scripts/` |
