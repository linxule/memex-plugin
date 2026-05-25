# Memex - Collaborative Memory for Human-AI Work

Captures the collaborative process of working with Claude Code — decisions, deliberations, tensions, and breakthroughs — as searchable, interconnected knowledge in an Obsidian vault.

## What Makes This Different from Auto-Memory

Claude Code's built-in auto-memory stores preferences and conventions as flat key-value pairs — working memory for *how* you work. Memex captures the **collaborative journey**: full session transcripts and structured memos for every compaction window, preserving not just what was decided but how you and the user got there — what was tried, where you disagreed, what surprised both of you.

Memos explicitly capture "Perspectives & Tensions" because deliberation is often more valuable than conclusions. Garden-tending — where you and the user periodically review, condense, and synthesize accumulated knowledge — means the vault grows as a shared practice, not just a storage layer.

## Quick Start

```bash
# Check what's in the vault
memex status

# Search for something (RRF scoring is default)
memex search "authentication"

# Search recent docs only (7d, 2w, 3m)
memex search "plugin" --since=7d

# Use linear scoring instead of RRF
memex search "test" --scoring=linear --weights=0.7,0.3

# Rebuild index after changes
memex index rebuild --incremental
```

The `memex` CLI works from any directory. For Obsidian CLI and dreamer, `cd` to the vault is still needed.

## First-Run Setup (Guide the User)

When you detect this is a fresh install (no `~/.memex/config.json`, no `projects/` directory, or empty vault), guide the user through personalization:

1. **Vault path**: Ask where they cloned this repo. Create `~/.memex/config.json` with their `memex_path`.
2. **Obsidian vault name**: If they use Obsidian and their vault folder name differs from "memex", note this — the `/memex:open obsidian` command uses `obsidian://open?vault=memex` by default.
3. **Embedding provider**: Ask if they want semantic search. Options: Gemini Embedding 2 (cloud, primary, needs `GEMINI_API_KEY`), LM Studio (local fallback, free), or skip (keyword-only).
4. **Import existing sessions**: If the user has been using Claude Code, they already have valuable transcripts in `~/.claude/projects/`. Run `memex session discover --triage` to see what's available, then `memex session discover --import --apply` to bring them into the vault. Skip the currently-running session (it will be archived automatically when the session ends). This gives them an instant searchable archive of their prior work.
5. **Build initial index**: Run `memex index rebuild --full` to create the search index (including any imported transcripts).
6. **MEMORY.md**: Help them customize the starter MEMORY.md with their active projects and preferences.

Run `uv run scripts/setup.py` to handle steps 1-3 interactively. Steps 4-6 are best done conversationally. Project name detection is automatic (git remote → git root → cwd), so no `project_mappings` block is needed — the field is supported as a manual override for edge cases (e.g., when git-remote picks the wrong name) and is read directly by `detect_project()` in `src/memex/scripts/utils.py`.

## How Claude Uses This Plugin

Claude acts as the **memex curator** — condensing project knowledge into `_project.md` overviews, maintaining `[[wikilinks]]`, and cultivating the vault's knowledge topology. Claude searches the vault when context is needed rather than relying on pre-loaded summaries.

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
├── _index.sqlite                # FTS5 + vector search + observation_topics index
└── .claude-plugin/              # Plugin manifest
```

## Key Files

| File | Purpose |
|------|---------|
| `src/memex/scripts/hybrid_search.py` | Combined FTS5 + vector search logic |
| `src/memex/scripts/temporal_scan.py` | Filesystem-based date query for memos and transcripts |
| `src/memex/scripts/date_utils.py` | Natural-language date parsing (shared by temporal scan and search) |
| `src/memex/scripts/embeddings.py` | Multi-provider embeddings (Gemini primary, LM Studio fallback), chunking, caching |
| `src/memex/scripts/index_rebuild.py` | Full/incremental index rebuild |
| `skills/recall/SKILL.md` | Search decision logic — when/how to search memos |
| `skills/garden-tending/SKILL.md` | Full vault lifecycle: diagnose, condense, connect, grow, maintain |
| `skills/memo-writing/SKILL.md` | Guide for effective memo format |
| `hooks/session-start.py` | Loads context at session start; detects pending memos post-compaction |
| `hooks/user-prompt-submit.py` | Tracks activity, nudges Claude to save memos |
| `scripts/obsidian_cli.py` | Obsidian CLI 1.12.5 wrapper — graph queries, file ops, tasks, templates |
| `src/memex/scripts/crystallization_check.py` | Alias-aware unresolved link analysis with maturation tiers |
| `src/memex/scripts/discover_sessions.py` | Find unprocessed sessions, triage by viability, batch import |
| `src/memex/scripts/sync_auto_memory.py` | Sync Claude Code auto-memory into vault with source tracking |
| `src/memex/cli.py` | Unified CLI dispatcher — all `memex` commands route through here |
| `scripts/transcript_to_md.py` | JSONL transcript to markdown — system tag cleaning, skill compression |
| `scripts/mark_memo_saved.py` | Backward-compat entry point for memo state marking |
| `bin/memex` | Shell wrapper for live-source CLI execution from any directory |
| `~/.memex/config.json` | All configuration (weights, limits, embedding provider) |

## Architecture

### Memo Generation (Two Layers)

Memos are generated without external API calls — everything runs through Claude Code sessions.

**Layer 1 — Proactive Save (primary, best quality):**
- `UserPromptSubmit` hook is pure Python (no model, no API calls) — counts messages per session
- After ~20 messages, prints a one-line nudge to stdout
- Claude Code injects this into the conversation as a system reminder
- The **main agent** (whatever model the user runs — Opus, Sonnet, etc.) sees the nudge and runs `/memex:save`
- The main agent writes the memo itself with full experiential context — it was *there*
- No extra API costs for the nudge — only the memo writing uses tokens, from the existing session

**Layer 2 — Background Subagent (safety net, Haiku):**
- `PreCompact` hook writes signal file to `~/.memex/pending-memos/`
- Post-compaction, `SessionStart` detects pending memo and instructs the main agent to spawn a **background Haiku subagent**
- Haiku reads transcript, searches vault for related memos, generates memo
- Cheaper than Layer 1, decent quality, but reconstructed from transcript rather than lived experience
- Only fires when Layer 1 didn't catch it

**Cross-Session Synthesis (periodic, manual):**
- Invoke the `garden-tending` skill weekly ("tend the garden", "update
  project overview") to review accumulated memos
- Finds: patterns across projects, contradictions, semantic drift,
  compression candidates
- Updates `_project.md` overviews with condensed project knowledge
- For large vaults: use a dedicated session with `claude --resume
  <analyst-id> --model sonnet`

### Session Lifecycle

1. **SessionStart** → Loads project context, recent memos, open threads; checks for pending memos post-compaction
2. **UserPromptSubmit** → Tracks activity, nudges Claude to save when substantial work accumulates
3. **During session** → Skills guide Claude when to search/save (intent-based); Claude saves memo via `/memex:save`
4. **PreCompact** → Writes signal file as safety net (no API calls)
5. **SessionEnd** → Archives full transcript to `projects/<name>/transcripts/`

**Search Pipeline:**
1. Query comes in via `memex search` CLI or the `recall` skill
2. FTS5 scores documents by BM25 keyword relevance
3. Vector embeddings score by semantic similarity (Gemini Embedding 2 primary, LM Studio local fallback)
4. RRF (Reciprocal Rank Fusion, k=60) combines rankings - industry standard
5. Result diversity applied (max 3 chunks per document)
6. Optional `--since` filter for recency (e.g., `--since=7d`)

**Project Detection:**
1. Check explicit mappings in `~/.memex/config.json`
2. Parse git remote URL for repo name
3. Use git root folder name
4. Fall back to cwd folder name or `_uncategorized`

## Frontmatter Schema

**Memos:** `type: memo`, `project`, `title`, `date`, `topics: []`, `status: active|archived`, `source_cwd`

**Transcripts:** `type: transcript`, `project`, `session_id`, `date`, `messages`, `has_memo`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `models: []`, `commits: []`, `duration_minutes`

**Concepts:** `type: concept`, `title`, `projects: []`, `related_memos: []`

**Projects:** `type: project`, `name`, `created`, `condensed`, `memos_digested`, `status: active`

**Auto-Memory:** `type: auto-memory`, `title`, `project`, `date`, `source`, `source_hash`, `synced`, `volatile: true|false`, `topics: []`, `status: active`

## Environment

Optional for semantic search:
```bash
# Gemini Embedding 2 (recommended): export GEMINI_API_KEY=your-key
# OR LM Studio (local fallback): Install LM Studio, load Qwen3-Embedding-0.6B, start server
```

## Plugin Commands

- `/memex:save [title]` - Save current context as memo (primary memo generation path)
- `/memex:status` - Show index stats and pending memos
- `/memex:open` - Open vault in Finder/Obsidian

Retrieval (search, timeline, ask, load, synthesize, merge, maintain, retry,
backfill) is **skill-based** as of v0.11 — Claude invokes the `recall` skill
for retrieval questions and the `garden-tending` skill for synthesis /
maintenance workflows. Direct shell access lives in the `memex` CLI
(`memex search`, `memex ask`, `memex timeline`, `memex backfill obs`,
`memex check`, `memex index rebuild`, etc.) — see the CLI section below.

## CLI Commands

```bash
memex search <query>        # Hybrid search (FTS + vector)
memex ask <question>        # Deep retrieval with observations
memex timeline <date>       # Browse by date (yesterday, 7d, last week)
memex read <path>           # Read vault document to stdout
memex path                  # Print resolved vault path
memex check                 # Vault health — crystallization readiness
memex status                # Document count, chunks, last rebuild (fast — 53ms on large indexes since 0.11.3)
memex context               # Project detection and pending memo status
memex similarity            # Detect near-duplicate topics (--threshold, --json)
memex scrub <path>          # Detect API keys / secrets (--apply redacts in place)
memex mark-saved            # Mark memo saved (prevents duplicate generation)
memex sync                  # Sync auto-memory into vault
memex graph <subcmd>        # Backlinks, orphans, tags, stats
memex topic resolve <slug>  # Resolve redirect_to chain (exit 0=ok, 1=skip/error)
memex index rebuild         # Rebuild search index (--full for embeddings)
memex index status          # Index health JSON (doc/chunk counts, embedding gaps)
memex index embed-missing   # Embed chunks/obs missing from vec tables (retry after API failures)
memex session discover      # Find unprocessed sessions
memex session import        # Import discovered sessions (--apply to execute)
memex obs topic <slug>      # All observations for a topic (cluster lookup)
memex obs stats             # Observation counts per topic
memex obs retag <old> <new> # Retag observations (for topic merges)
memex obs untagged          # Observations with no topics (new-topic signals)
memex backfill obs          # Extract observations from memos
memex backfill tokens       # Backfill token counts on transcripts
memex backfill memos        # Backfill has_memo on transcripts
memex backfill topic-tags   # Propagate memo topics to observations
```

## Periodic Maintenance Tasks

Run these when asked or during memex maintenance sessions:

### Full Rebuild (Only When Needed)
Run when switching providers, after schema upgrades, or if index corrupted:
```bash
memex index rebuild --full
```

**When to run full:**
- Switching embedding providers (dimension change)
- Schema upgrades (new tables/columns)
- Index corruption

**Not needed for:** Daily growth (incremental handles it)

### Synthesize Cross-Project Insights
Review recent memos across all projects. Condense findings into `_project.md` overviews. Create new concept notes in `topics/` for ideas that appear in 2+ projects.

### Discover & Import Unprocessed Sessions
Run `memex session discover --triage` to find sessions in `~/.claude/projects/` not yet in memex. Triage scores them by viability (file edits, git commits, duration, etc.). Import high-value ones with `--min-score=9 --import --apply`.

### Find Orphans
Find:
- Transcripts without memos (need processing)
- Memos without links (isolated)
- Concepts that reference deleted memos

### Project Summary
Generate a summary of a specific project's current state based on its memos.

## How the Plugin Works

**Hooks:**
1. **SessionStart** - Loads context; post-compaction detects pending memos and instructs subagent spawn
2. **UserPromptSubmit** - Tracks message count, nudges Claude to `/memex:save` after ~20 messages
3. **SessionEnd** - Archives transcript to `projects/<project>/transcripts/`
4. **PreCompact** - Writes signal file to `~/.memex/pending-memos/` (no API calls, <100ms)

**Memo generation philosophy:**
- Claude writes memos from full experiential context (Layer 1) — best quality
- Background subagent reads transcript as fallback (Layer 2) — decent quality
- No external API calls — everything uses Claude Code subscription
- The nudge system (UserPromptSubmit) reminds Claude to save before compaction catches us

**Why skills over hooks for search:**
- Skills let Claude decide when to search (judgment-based)
- No timeout pressure (hooks have 5-10s limits)
- Claude can refine queries iteratively
- More transparent to user

**Skill-based Search:**
- The `recall` skill teaches Claude when to search memos (see `skills/recall/SKILL.md`)
- When user asks "why did we...", "remind me...", etc., Claude decides to search
- Claude extracts keywords (not full questions) for effective FTS matching
- Example: "Why did we choose JWT?" → search for `JWT OR authentication`

## Available Skills

The memex plugin ships four intent-based skills that teach Claude when to act:

| Skill | Purpose | When to Invoke |
|-------|---------|---------------|
| `recall` | Retrieve session memory — temporal browsing, keyword search, deep cross-session synthesis, or direct file loading | "what did I do yesterday?", "why did we…", "what patterns across…", "load the X topic" |
| `garden-tending` | Full vault lifecycle: diagnose, condense, connect, grow, maintain. Absorbs the former `synthesize` and `merge` slash-command behavior | "where are we with X?", "tend the garden", "update project overview", "check vault health", "find broken links" |
| `curator-practice` | Autonomous curator operating philosophy (attention, judgment, initiative) | autonomous tending, "what should I work on next?", scheduled/cron agents |
| `memo-writing` | Memo format + quality guidelines | `/memex:save`, "remember this", or when the [memex] nudge appears |

Skills are intent-based: Claude decides when to invoke based on user
questions. This is more flexible than hooks which run on fixed events, and
it replaces the slash-command surface that used to front each retrieval
action as its own `/memex:…` shortcut.

## Dev Commands

```bash
# Test hooks manually (pipe JSON to stdin)
echo '{"session_id": "test", "cwd": "'$(pwd)'", "source": "startup"}' | uv run hooks/session-start.py

# Test search (use OR between keywords, not full questions)
memex search "JWT OR authentication"

# Rebuild index (incremental - only changed docs)
memex index rebuild --incremental

# Full rebuild with embeddings
memex index rebuild --full

# Check index status (includes graph stats)
memex status

# Crystallization readiness check (alias-aware, delta tracking)
memex check                    # full report
memex check --tier ready       # actionable items only
memex check -v                 # with source files
memex check --json             # programmatic output

# Backfill has_memo on transcripts (match memos to transcripts)
uv run scripts/backfill_has_memo.py                        # dry-run (default)
uv run scripts/backfill_has_memo.py --apply -v             # apply + verbose

# Backfill token usage into existing transcript frontmatter
uv run scripts/backfill_tokens.py                          # dry-run
uv run scripts/backfill_tokens.py --apply -v               # apply + verbose

# Discover unprocessed sessions in ~/.claude/projects/
memex session discover                        # summary by project
memex session discover --triage               # score by viability
memex session discover --triage -v            # with first-message preview
memex session discover --triage --min-score=9 # high-value only
memex session discover --all-projects         # list all Claude projects
memex session discover --import --apply       # batch import

# Sync Claude Code auto-memory into vault
memex sync --discover              # list files + coverage report
memex sync --sync                  # dry-run
memex sync --sync --apply          # write files
memex sync --status                # fresh/stale/new/orphaned

# Batch-extract observations from existing memos (async, up to N concurrent
# `claude --print --model sonnet` subprocesses). Useful when importing
# pre-existing markdown that has no observations yet, or for recovery
# scenarios. Idempotent — skips memos that already have obs.
uv run scripts/batch_extract_observations.py \
    --work-list /tmp/missing_obs_memos.txt --workers 5
```

## Linking Conventions

Use Obsidian wikilinks for cross-references:
- `[[topic-name]]` - Link to topic in topics/
- `[[projects/myproject/memos/memo-name]]` - Link to specific memo
- `[[projects/myproject/_project|My Project]]` - Link with alias
- `[[?new-concept]]` - Suggest new concept (doesn't exist yet)

### Archived Topics: `redirect_to:` Convention

When a topic gets archived because its concept was absorbed into a different topic (or its content belongs in a project overview), set both `status: archived` and `redirect_to: <target-slug>` in frontmatter:

```yaml
---
type: concept
title: Multi-Agent Code Review (Archived — redirect)
status: archived
redirect_to: multi-agent-review
---
```

`/memex:save` and the `memo-writing` skill both follow `redirect_to:` chains (5-hop limit) when appending "Recent signals". The resolver accepts two target shapes:

- **Bare slug** (`redirect_to: embedding-models`) — resolved as `topics/embedding-models.md`.
- **Vault-relative path** (`redirect_to: projects/clawd-world/_project.md`) — resolved at that path directly, so a topic can redirect into a project overview.

The two archive shapes behave differently:

- **Redirected archive** (`status: archived` + `redirect_to: <target>`) — resolver follows the chain, the signal lands on the canonical replacement.
- **Terminal archive** (`status: archived`, no `redirect_to:`) — resolver emits `WARN: archived with no redirect_to — skipping signal` and the signal is **dropped**, not landed on the stub. Typical for project-target archives where the content belongs in a `_project.md`, not another topic. If you want signals routed elsewhere, add `redirect_to:` during garden-tending.

Cycles (A→B→A, self-loops, longer rings) are detected and surfaced on stderr with the offending chain rather than misreported as "exceeded hops". The resolver is implemented in `src/memex/scripts/topic_resolve.py` and exposed as `memex topic resolve <slug-or-path>` — use it to verify a chain manually (stdout = resolved destination, exit 0 = ok, exit 1 = skip/warn).

## Where to Go Next

Domain-specific details load automatically via `.claude/rules/` when you work on relevant files:

| Rules File | Covers | Loaded When Editing |
|------------|--------|-------------------|
| `architecture.md` | Memo generation layers, session lifecycle, search pipeline, frontmatter schema | `src/memex/`, `hooks/`, `commands/`, `skills/` |
| `configuration.md` | Config paths, path resolution, session verbosity, linking conventions, security | `src/memex/`, `hooks/`, `.claude-plugin/` |
| `maintenance.md` | Periodic tasks, dev commands (rebuild, backfill, discover, sync) | `src/memex/`, `_views/`, `topics/` |
| `search-and-embeddings.md` | Embedding providers (Gemini primary, LM Studio fallback), chunking, search gotchas | `src/memex/scripts/search.py`, `src/memex/scripts/hybrid_search.py`, `src/memex/scripts/embeddings.py`, `src/memex/scripts/index_rebuild.py` |
| `obsidian-cli.md` | Obsidian CLI 1.12.5 commands, SQLite fallback, graph navigation | `scripts/obsidian_cli.py`, `src/memex/scripts/graph_queries.py`, `src/memex/scripts/crystallization_check.py` |
| `hooks.md` | Hook implementation details, timing constraints | `hooks/` |
| `plugin-authoring.md` | Error patterns for commands, skills, hooks, scripts, plugin cache | `commands/`, `skills/`, `hooks/`, `src/memex/`, `.claude-plugin/` |
| `python-patterns.md` | Python patterns used across the codebase | `scripts/`, `hooks/` |
| `transcripts.md` | Transcript processing, JSONL format, system tag cleaning | transcript-related scripts |

## Gotchas

Domain-specific gotchas are in `.claude/rules/` and load only when working on relevant files. These are general gotchas that apply across the project:

- **Project detection uses git root** - Memos are stored by project detected from `cwd`, not the memex folder itself
- **Plugin cache staleness** - Claude Code loads from `~/.claude/plugins/cache/`, not live source. After changing `plugin.json` or hooks, reinstall: `claude plugin uninstall memex@memex-plugins --scope user && claude plugin install memex@memex-plugins --scope user`. Already-open sessions keep the old config until restarted
- **`package = true` + two-layer distribution** - `uv tool install .` gives the global `memex` CLI for any bash-capable agent. `claude plugin install` adds hooks and slash commands for Claude Code. Core code lives in `src/memex/`; `scripts/` exists for backward compatibility
- **`bin/memex` uses `PYTHONPATH=src` for live source** - The shell wrapper runs the local package without rebuilding a wheel, so edits are picked up immediately. Keep that behavior for local development
- **`${CLAUDE_PLUGIN_ROOT}` is cache, not vault** - In command files, this env var points to the plugin cache location, not the memex vault. Read `~/.memex/config.json` or use the `memex` CLI for vault path resolution
- **Plugin cache venv is separate** - The cache at `~/.claude/plugins/cache/memex-plugins/memex/<version>/` has its own venv. If plugin behavior differs from local runs, verify the cache environment separately
- **`memex` CLI resolves vault path automatically** - No `cd` needed for `memex search`, `memex timeline`, `memex ask`, or `memex index rebuild`. For Obsidian CLI and dreamer, `cd` to the vault is still required
- **Debug perf by narrowing, not orchestrating** - When something is slow, don't spawn background agents or build elaborate profiling harnesses. Go direct: narrow to the exact call, inspect
- **Background bash output buffering** - `2>/dev/null`, `| head`, and `2>&1` redirects can swallow or buffer Python output in background tasks. Write to a file directly (`> /tmp/results.txt`) and `cat` it after, or use `PYTHONUNBUFFERED=1`
- **Two failures is information, three is a pattern** - If the same approach fails twice, change strategy entirely rather than tweaking flags

## Configuration

Config file: `~/.memex/config.json`
Memo prompt: shipped with the plugin at `skills/memo-writing/memo-default.md`
Logs: `~/.memex/logs/`
Locks: `~/.memex/locks/` (session and index locks)
Pending memos: `~/.memex/pending-memos/` (PreCompact signal files; retried by Layer 2 subagent)

### Path Resolution

The memex vault path is resolved in this order:

1. **`~/.memex/config.json` → `memex_path`** (user override, highest priority)
2. **`CLAUDE_PLUGIN_ROOT` env var** (set automatically by plugin system)
3. **Script location fallback** (assumes scripts are in `memex/scripts/`)

For new users, create `~/.memex/config.json`:
```json
{
  "memex_path": "/path/to/your/memex/vault"
}
```

### Retrieval Is Skill-Based

As of v0.11, SessionStart no longer injects rich context at startup.
Retrieval happens on demand through the `recall` skill — Claude decides
when and how deep to search based on the user's question. There's no
`session_context.verbosity` setting to tune.

What SessionStart still does:

- On normal startup: injects nothing unless pending memos for this
  project need attention.
- Post-compaction: emits a short "session compacted; memo needed" nudge
  and instructs the main agent to spawn a Layer-2 subagent if the
  PreCompact hook left a signal file.
- On resume: surfaces any orphan pending memos with a short heads-up.

If you want more context up front, ask — "what was I working on?", "load
the X topic", "what patterns across the last week?" — and the `recall`
skill will route to the right depth.

## Security & Privacy

**What data is stored:**
- **Memos** - Summaries of session decisions, learnings, open threads (auto-generated)
- **Transcripts** - Full conversation history in JSONL + markdown format
- **Search index** - FTS5 + vector embeddings for search

**Where it's stored:**
- All data stays local in the memex vault
- Plugin state in `~/.memex/` (session tracking, config)
- No data is sent externally except:
  - Anthropic API calls for memo generation (uses your existing Claude session)
  - Gemini API calls for embeddings (only if using `provider: "google"`)
  - **With LM Studio provider, all embedding processing stays fully local**

**Access controls:**
- Local filesystem permissions apply
- Transcripts excluded from git by default (see `.gitignore`)
- No authentication layer - anyone with filesystem access can read

**Privacy note:**
Transcripts contain your full conversation history, which may include sensitive information discussed during sessions. Consider what you discuss before enabling memex. Transcripts are stored in `projects/<name>/transcripts/` and excluded from git.
