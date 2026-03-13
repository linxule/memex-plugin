---
paths:
  - "scripts/**/*.py"
  - "_views/**"
  - "topics/**/*.md"
---

# Maintenance & Dev Commands

## Periodic Maintenance Tasks

Run these when asked or during memex maintenance sessions.

### Nightly Incremental Rebuild (Automated)
Scheduled via launchd at 3am daily - indexes new/changed documents automatically.
Check logs: `tail ~/.memex/logs/nightly-rebuild.log`

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
