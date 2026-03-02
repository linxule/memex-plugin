# Memex - Collaborative Memory for Human-AI Work

Captures the collaborative process of working with Claude Code — decisions, deliberations, tensions, and breakthroughs — as searchable, interconnected knowledge in an Obsidian vault.

## What Makes This Different from Auto-Memory

Claude Code's built-in auto-memory stores preferences and conventions as flat key-value pairs — working memory for *how* you work. Memex captures the **collaborative journey**: full session transcripts and structured memos for every compaction window, preserving not just what was decided but how you and the user got there — what was tried, where you disagreed, what surprised both of you.

Memos explicitly capture "Perspectives & Tensions" because deliberation is often more valuable than conclusions. Garden-tending — where you and the user periodically review, condense, and synthesize accumulated knowledge — means the vault grows as a shared practice, not just a storage layer.

## Quick Start

```bash
# Check what's in the vault
uv run scripts/search.py --status

# Set up LM Studio (for semantic search)
# 1. Install LM Studio from https://lmstudio.ai
# 2. Load Qwen3-Embedding-0.6B model
# 3. Start server (runs at http://localhost:1234)

# Search for something (RRF scoring is default)
uv run scripts/search.py "authentication" --mode=hybrid --format=text

# Search recent docs only (7d, 2w, 3m)
uv run scripts/search.py "plugin" --since=7d --format=text

# Use linear scoring instead of RRF
uv run scripts/search.py "test" --scoring=linear --weights=0.7,0.3 --format=text

# Rebuild index after changes
uv run scripts/index_rebuild.py --incremental
```

## First-Run Setup (Guide the User)

When you detect this is a fresh install (no `~/.memex/config.json`, no `projects/` directory, or empty vault), guide the user through personalization:

1. **Vault path**: Ask where they cloned this repo. Create `~/.memex/config.json` with their `memex_path`.
2. **Obsidian vault name**: If they use Obsidian and their vault folder name differs from "memex", note this — the `/memex:open obsidian` command uses `obsidian://open?vault=memex` by default.
3. **Embedding provider**: Ask if they want semantic search. Options: LM Studio (local, free), Gemini API (cloud, needs key), or skip (keyword-only).
4. **Context verbosity**: Ask their preference — minimal (~20 tokens), standard (~150), or full (~500+). Update config.
5. **Project mappings**: If Claude Code's auto-detected project name (derived from git root) doesn't match what the user wants to call a project in memex, add explicit `"project_mappings"` to `config.json` (e.g., `"/Users/them/work/my-app": "my-app"`).
6. **Import existing sessions**: If the user has been using Claude Code, they already have valuable transcripts in `~/.claude/projects/`. Run `uv run scripts/discover_sessions.py --triage` to see what's available, then `uv run scripts/discover_sessions.py --import --apply` to bring them into the vault. Skip the currently-running session (it will be archived automatically when the session ends). This gives them an instant searchable archive of their prior work.
7. **Build initial index**: Run `uv run scripts/index_rebuild.py --full` to create the search index (including any imported transcripts).
8. **MEMORY.md**: Help them customize the starter MEMORY.md with their active projects and preferences.

Run `uv run scripts/setup.py` to handle steps 1-4 interactively. Steps 5-8 are best done conversationally.

## How Claude Uses This Plugin

Claude acts as the **memex curator** — condensing project knowledge into `_project.md` overviews, maintaining `[[wikilinks]]`, and cultivating the vault's knowledge topology. Claude searches the vault when context is needed rather than relying on pre-loaded summaries.

## Folder Structure

```
memex/
├── projects/<name>/memos/       # Session memos per project
├── projects/<name>/auto-memory/ # Synced Claude Code auto-memory files
├── projects/<name>/transcripts/ # Full conversation logs
├── topics/                      # Cross-project concept notes
├── scripts/                     # Python utilities (search, embeddings, etc.)
├── hooks/                       # Claude Code hooks (SessionStart, PreCompact, etc.)
├── commands/                    # Slash commands (/memex:*)
├── skills/                      # Intent-based skills
├── _templates/                  # Note templates
├── _index.sqlite                # FTS5 + vector search index
├── .claude-plugin/              # Plugin manifest
└── MEMORY.md                    # Vault awareness guide
```

## Key Files

| File | Purpose |
|------|---------|
| `scripts/hybrid_search.py` | Combined FTS5 + vector search logic |
| `scripts/embeddings.py` | Multi-provider embeddings (LM Studio, Gemini), chunking, caching |
| `scripts/index_rebuild.py` | Full/incremental index rebuild |
| `skills/recall/SKILL.md` | Search decision logic — when/how to search memos |
| `skills/garden-tending/SKILL.md` | Full vault lifecycle: diagnose, condense, connect, grow, maintain |
| `skills/memo-writing/SKILL.md` | Guide for effective memo format |
| `hooks/session-start.py` | Loads context at session start; detects pending memos post-compaction |
| `hooks/user-prompt-submit.py` | Tracks activity, nudges Claude to save memos |
| `hooks/pre-compact.py` | Writes signal file for safety net memo generation (no API calls) |
| `prompts/memo-default.md` | Rich memo prompt for background subagent fallback |
| `commands/synthesize.md` | Cross-session synthesis (patterns, contradictions, drift) |
| `scripts/transcript_to_md.py` | JSONL transcript to markdown — system tag cleaning, skill compression |
| `scripts/obsidian_cli.py` | Obsidian CLI wrapper — backlinks, orphans, deadends, eval, tags |
| `scripts/crystallization_check.py` | Alias-aware unresolved link analysis with maturation tiers and delta tracking |
| `scripts/backfill_has_memo.py` | Match memos to transcripts, update `has_memo` frontmatter |
| `scripts/backfill_tokens.py` | Patch token usage (input/output/cache) into existing transcript frontmatter |
| `scripts/discover_sessions.py` | Find unprocessed sessions in `~/.claude/projects/`, triage by viability, batch import |
| `scripts/sync_auto_memory.py` | Sync Claude Code auto-memory into vault with source tracking |
| `scripts/mark_memo_saved.py` | Unified state marking after `/memex:save` |
| `~/.memex/config.json` | All configuration (weights, limits, etc.) |

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
- Run `/memex:synthesize` weekly to review accumulated memos
- Finds: patterns across projects, contradictions, semantic drift, compression candidates
- Updates `_project.md` overviews with condensed project knowledge
- For large vaults: use a dedicated CLI session with `claude --resume <analyst-id> --model sonnet`

### Session Lifecycle

1. **SessionStart** → Loads project context, recent memos, open threads; checks for pending memos post-compaction
2. **UserPromptSubmit** → Tracks activity, nudges Claude to save when substantial work accumulates
3. **During session** → Skills guide Claude when to search/save (intent-based); Claude saves memo via `/memex:save`
4. **PreCompact** → Writes signal file as safety net (no API calls)
5. **SessionEnd** → Archives full transcript to `projects/<name>/transcripts/`

**Search Pipeline:**
1. Query comes in via `/memex:search` or recall skill
2. FTS5 scores documents by BM25 keyword relevance
3. Vector embeddings score by semantic similarity (LM Studio local or Gemini API)
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
# LM Studio (recommended): Install LM Studio, load Qwen3-Embedding-0.6B, start server
# OR Gemini API (fallback): export GEMINI_API_KEY=your-key
```

## Plugin Commands

- `/memex:save [title]` - Save current context as memo (primary memo generation path)
- `/memex:search <query>` - Search memos (hybrid: FTS + vector)
- `/memex:synthesize [--since=7d]` - Cross-session synthesis (patterns, contradictions, drift)
- `/memex:load <topic>` - Load topic or memo into context
- `/memex:status` - Show index stats and pending memos
- `/memex:maintain` - Check vault health (broken links, orphans, isolated memos)
- `/memex:open` - Open vault in Finder/Obsidian
- `/memex:merge` - Synthesize multiple memos into a concept or summary note
- `/memex:retry` - Retry failed memo generations

## Periodic Maintenance Tasks

Run these when asked or during memex maintenance sessions:

### Full Rebuild (Only When Needed)
Run when switching providers, after schema upgrades, or if index corrupted:
```bash
uv run scripts/index_rebuild.py --full
```

**When to run full:**
- Switching embedding providers (dimension change)
- Schema upgrades (new tables/columns)
- Index corruption

**Not needed for:** Daily growth (incremental handles it)

### Synthesize Cross-Project Insights
Review recent memos across all projects. Condense findings into `_project.md` overviews. Create new concept notes in `topics/` for ideas that appear in 2+ projects.

### Discover & Import Unprocessed Sessions
Run `uv run scripts/discover_sessions.py --triage` to find sessions in `~/.claude/projects/` not yet in memex. Triage scores them by viability (file edits, git commits, duration, etc.). Import high-value ones with `--min-score=9 --import --apply`.

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

The memex plugin includes three intent-based skills that teach Claude when to act:

| Skill | Purpose | When to Invoke |
|-------|---------|---------------|
| `recall` | Search memos, recall prior context | "why did we...", "remind me...", "what was the decision...", "find the memo about..." |
| `garden-tending` | Full vault lifecycle: diagnose, condense, connect, grow, maintain | "where are we with X?", "tend the garden", "update project overview", "check vault health", "find broken links" |
| `memo-writing` | Format and quality guidelines | `/memex:save`, "remember this", or when [memex] nudge appears |

Skills are intent-based: Claude decides when to invoke based on user questions. This is more flexible than hooks which run on events.

## Dev Commands

```bash
# Test hooks manually (pipe JSON to stdin)
echo '{"session_id": "test", "cwd": "'$(pwd)'", "source": "startup"}' | uv run hooks/session-start.py

# Test search (use OR between keywords, not full questions)
uv run scripts/search.py "JWT OR authentication" --mode=hybrid --format=text

# Rebuild index (incremental - only changed docs)
uv run scripts/index_rebuild.py --incremental

# Full rebuild with embeddings
uv run scripts/index_rebuild.py --full

# Check index status (includes graph stats)
uv run scripts/index_rebuild.py --status

# Crystallization readiness check (alias-aware, delta tracking)
uv run scripts/crystallization_check.py                    # full report
uv run scripts/crystallization_check.py --tier ready       # actionable items only
uv run scripts/crystallization_check.py -v                 # with source files
uv run scripts/crystallization_check.py --json             # programmatic output

# Backfill has_memo on transcripts (match memos to transcripts)
uv run scripts/backfill_has_memo.py                        # dry-run (default)
uv run scripts/backfill_has_memo.py --apply -v             # apply + verbose

# Backfill token usage into existing transcript frontmatter
uv run scripts/backfill_tokens.py                          # dry-run
uv run scripts/backfill_tokens.py --apply -v               # apply + verbose

# Discover unprocessed sessions in ~/.claude/projects/
uv run scripts/discover_sessions.py                        # summary by project
uv run scripts/discover_sessions.py --triage               # score by viability
uv run scripts/discover_sessions.py --triage -v            # with first-message preview
uv run scripts/discover_sessions.py --triage --min-score=9 # high-value only
uv run scripts/discover_sessions.py --all-projects         # list all Claude projects
uv run scripts/discover_sessions.py --import --apply       # batch import

# Sync Claude Code auto-memory into vault
uv run scripts/sync_auto_memory.py --discover              # list files + coverage report
uv run scripts/sync_auto_memory.py --sync                  # dry-run
uv run scripts/sync_auto_memory.py --sync --apply          # write files
uv run scripts/sync_auto_memory.py --status                # fresh/stale/new/orphaned
```

## Linking Conventions

Use Obsidian wikilinks for cross-references:
- `[[topic-name]]` - Link to topic in topics/
- `[[projects/myproject/memos/memo-name]]` - Link to specific memo
- `[[projects/myproject/_project|My Project]]` - Link with alias
- `[[?new-concept]]` - Suggest new concept (doesn't exist yet)

## Gotchas

Domain-specific gotchas are in `.claude/rules/` and load only when working on relevant files. These are general gotchas that apply across the project:

- **Project detection uses git root** - Memos are stored by project detected from `cwd`, not the memex folder itself
- **Plugin cache staleness** - Claude Code loads from `~/.claude/plugins/cache/`, not live source. After changing `plugin.json` (especially hooks), reinstall: `claude plugin uninstall memex@memex-plugins --scope user && claude plugin install memex@memex-plugins --scope user`
- **`${CLAUDE_PLUGIN_ROOT}` is cache, not vault** - In command files, this env var points to the plugin cache location, NOT the memex vault. Commands must read `~/.memex/config.json` → `memex_path` first to get the actual vault path
- **Debug perf by narrowing, not orchestrating** - When something is slow, don't spawn background agents or build elaborate profiling harnesses. Go direct: narrow to the exact call, inspect
- **Background bash output buffering** - `2>/dev/null`, `| head`, and `2>&1` redirects can swallow or buffer Python output in background tasks. Write to a file directly (`> /tmp/results.txt`) and `cat` it after, or use `PYTHONUNBUFFERED=1`
- **Two failures is information, three is a pattern** - If the same approach fails twice, change strategy entirely rather than tweaking flags

## Configuration

Config file: `~/.memex/config.json`
Memo prompt: `~/.memex/prompts/memo-default.md`
Logs: `~/.memex/logs/`
Locks: `~/.memex/locks/` (session and index locks)
Embedding queue: `~/.memex/pending_embeddings.jsonl`

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

### Session Context Verbosity

Control how much context is injected at SessionStart (affects token usage):

```json
{
  "session_context": {
    "verbosity": "standard"
  }
}
```

| Level | What's Injected | Token Cost | Use Case |
|-------|-----------------|------------|----------|
| `minimal` | "Memex available" hint only | ~20 | Quick tasks, minimal overhead |
| `standard` | Project + 3 memo titles + open thread count + graph summary | ~150 | **Default** - balanced awareness |
| `full` | Full memo summaries + all open threads + recent decisions | ~500+ | Deep context sessions |

**Post-compact behavior:** After compaction, minimal context is injected ("Session compacted. Use /memex:search...") regardless of verbosity level. Claude can search on-demand to recall prior context.

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
