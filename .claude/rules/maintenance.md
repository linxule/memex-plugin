---
paths:
  - "src/memex/**/*.py"
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
memex index status

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
