---
paths:
  - "scripts/**/*.py"
  - "src/memex/**/*.py"
  - "_views/**"
  - "topics/**/*.md"
---

# Maintenance & Dev Commands

## Periodic Maintenance Tasks

Run these when asked or during memex maintenance sessions.

### Nightly Incremental Rebuild (Optional, User-Configured)

`scripts/nightly-rebuild.sh` is a generic wrapper that:
1. Sources `~/.secrets` (Gemini key — launchd does not inherit shell env)
2. Runs `memex index rebuild --incremental`
3. Runs `memex index embed-missing` to retry any vec gaps

Public repo does not ship a launchd plist (the label and paths are
inherently per-user). To schedule it, follow the templated example in
`SETUP.md` under "Optional: Nightly Rebuild" — write a plist named
`com.YOURNAME.memex.nightly-rebuild.plist` with your own paths, drop
it in `~/Library/LaunchAgents/`, and bootstrap with launchctl. Same
pattern works for cron (`@daily $(memex path | xargs -I{} {})/scripts/nightly-rebuild.sh`)
or systemd timers on Linux.

Check logs at the path you configure (the wrapper writes to
`~/.memex/logs/nightly-rebuild.log` by default).

### API Key Rotation → Embedding Gaps
Rotating the Gemini key invalidates the old key immediately. Any rebuild or `backfill obs` call that runs before the new key takes effect inserts FTS/chunks/observations without embeddings.

Failure logs come from two different paths:
- **Rebuild path** (`index_rebuild` → `embed_chunks`): `Embedding batch partially failed: N/M items missing.`
- **Backfill-obs path** (`store_observations`): `⚠️ N/M observations stored without embeddings (<ExceptionType>: <msg>). Run memex index embed-missing after fixing the cause.`

`memex backfill obs` now exits with code 2 when any embedding failed — chained scripts / hooks can detect this. `memex index rebuild` still exits 0 with gaps (see exit-code split below).

After rotating:
1. Update `~/.secrets` with new key
2. **Restart Claude Code** (process inherits env at launch; sourcing `.secrets` in a separate terminal won't update the running process)
   - Alternative for one-off terminal runs: prefix the command with `source ~/.secrets &&`. This does NOT update an already-running Claude Code process but works for direct shell use.
3. Run `memex index status` — look for `embedding_gaps` section
4. Run `memex index embed-missing` to backfill any vec gaps

The status output now includes an "Embedding gaps" section whenever chunks/observations are missing from vec_chunks/vec_observations. Rebuild output also surfaces gaps in a warning block at the end.

**Exit-code split (important for scripts):**

- `memex index rebuild` exits **0 even with gaps**, for backward-compat with cron/launchd. The warning block is the actionable signal; CI / scheduled jobs that care about gap safety must inspect stdout or run `embed-missing` afterward.
- `memex index embed-missing` exits **non-zero when any items remain unembedded** (1 for pipeline errors, 2 for per-item failures). Wire it into scripts after a rebuild; don't rely on rebuild's exit code.

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

**Synthesis-driven crystallization**: Use the garden-tending skill (Diagnose + Condense) to identify which concepts to crystallize as topic stubs. The synthesis agent finds cross-project patterns that reveal which `?suggested` links have enough substance to become real topics.

### Discover & Import Unprocessed Sessions
Run `memex session discover --triage` to find sessions in `~/.claude/projects/` not yet in memex. Triage scores them by viability (file edits, git commits, duration, etc.). Import high-value ones with `--min-score=9 --import --apply`.

### Find Orphans
Find:
- Transcripts without memos (need processing)
- Memos without links (isolated)
- Concepts that reference deleted memos

### Project Summary
Generate a summary of a specific project's current state based on its memos.

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

# Check index status (includes graph stats + embedding_gaps field when > 0)
memex index status

# Retry missing embeddings (chunks/observations in indexes but not in vec_*)
# Idempotent. Use after a rebuild that reported embedding gaps — e.g., expired
# API key, rate-limit exhaustion — once the root cause is fixed. Non-zero exit
# when items still fail, so safe to chain in scripts.
memex index embed-missing
memex index embed-missing --json

# Reclaim free pages after migrate-vec drops the old (larger-dim) vec tables.
# VACUUM + wal_checkpoint(TRUNCATE) — the DB is persistent-WAL, so the main
# file only shrinks after the TRUNCATE checkpoint. Needs free disk ~= file size.
memex index vacuum
memex index vacuum --json

# Crystallization readiness check (alias-aware, delta tracking)
memex check                    # full report
memex check --tier ready       # actionable items only
memex check -v                 # with source files
memex check --json             # programmatic output

# Backfill has_memo on transcripts (match memos to transcripts)
memex backfill memos                        # dry-run (default)
memex backfill memos --apply -v             # apply + verbose

# Backfill token usage into existing transcript frontmatter
memex backfill tokens                          # dry-run
memex backfill tokens --apply -v               # apply + verbose

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
```

