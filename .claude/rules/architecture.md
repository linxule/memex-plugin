---
paths:
  - "scripts/**/*.py"
  - "src/memex/**/*.py"
  - "hooks/**/*.py"
  - "commands/**/*"
  - "skills/**/*"
---

# Architecture

## Memo Generation (Two Layers)

Memos are generated without external API calls — everything runs through Claude Code sessions.

**Layer 1 — Proactive Save (primary, best quality):**
- `UserPromptSubmit` hook tracks session activity
- After ~20 messages, nudges Claude: "[memex] Consider /memex:save"
- Claude writes memo using `/memex:save` with full experiential context
- This produces the best memos because Claude was *there*, not reconstructing from transcript

**Layer 2 — Background Subagent (safety net):**
- `PreCompact` hook writes signal file to `~/.memex/pending-memos/`
- Post-compaction, `SessionStart` detects pending memo and instructs Claude to spawn a background sonnet subagent
- Subagent reads transcript, searches vault for related memos, writes memo, then extracts 5–15 atomic observations and pipes them to `memex backfill obs --stdin`
- Decent quality, but reconstructed — only fires when Layer 1 didn't catch it

**Cross-Session Synthesis (periodic, manual):**
- Run cross-session synthesis periodically via the garden-tending skill
- Finds: patterns across projects, contradictions, semantic drift, compression candidates
- Updates `_project.md` overviews with condensed project knowledge
- For large vaults: use a dedicated CLI session with `claude --resume <analyst-id> --model sonnet`

## Session Lifecycle

1. **SessionStart** → Minimal by design. Injects nothing on new sessions unless there are pending memo retries; on `compact` source, looks for a pending-memo signal and injects subagent-spawn instructions for memo recovery
2. **UserPromptSubmit** → Tracks activity, nudges Claude to save when substantial work accumulates
3. **During session** → Skills guide Claude when to search/save (intent-based); Claude saves memo via `/memex:save`
4. **PreCompact** → Writes signal file as safety net (no API calls)
5. **SessionEnd** → Archives full transcript to `projects/<name>/transcripts/`

## Search Pipeline

1. Query comes in via recall skill or `memex search` CLI
2. FTS5 scores documents by BM25 keyword relevance
3. Vector embeddings score by semantic similarity (Gemini Embedding 2 API)
4. RRF (Reciprocal Rank Fusion, k=60) combines rankings - industry standard
5. Result diversity applied (max 3 chunks per document)
6. Optional `--since` filter for recency (e.g., `--since=7d`)

## Project Detection

1. Check explicit mappings in `~/.memex/config.json`
2. Parse git remote URL for repo name
3. Use git root folder name
4. Fall back to cwd folder name or `_uncategorized`

## How the Plugin Works

**Hooks:**
1. **SessionStart** - Minimal injection: nothing on new sessions unless pending memos exist; post-compaction detects pending memos and instructs subagent spawn for memo + observation recovery
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

## Frontmatter Schema

**Memos:** `type: memo`, `project`, `title`, `date`, `topics: []`, `status: active|archived`, `source_cwd`

**Transcripts:** `type: transcript`, `project`, `session_id`, `date`, `messages`, `has_memo`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `models: []`, `commits: []`, `duration_minutes`

**Concepts:** `type: concept`, `title`, `projects: []`, `related_memos: []`

**Trails:** `type: trail`, `title`, `concept`, `projects: []`, `started`, `last_extended`, `status: active`, `aliases: []`, `tags: []`

**Projects:** `type: project`, `name`, `created`, `condensed`, `memos_digested`, `status: active`

**Auto-Memory:** `type: auto-memory`, `title`, `project`, `date`, `source`, `source_hash`, `synced`, `volatile: true|false`, `topics: []`, `status: active`

**Meta:** `type: meta`, `title`, `created`, `updated` — curator infrastructure files in `_meta/` (dashboard, log, tag taxonomy). Not indexed.

**Legacy types (normalized):** `pattern`, `meta-pattern`, `dimension-cluster`, `topic` were used in early vault files and have been normalized to `concept`. `project-overview` normalized to `project`.
