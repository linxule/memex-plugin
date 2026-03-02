# Memex Vault Memory

Cross-project context loaded at the start of every Claude Code session.

## Active Projects

<!-- List your active projects with brief descriptions -->
- **project-name** - Brief description of what this project is about

## Key Preferences

<!-- Your personal tooling and style preferences -->
- **Python**: Use `uv` for package management
- **Style**: Prefer concise, readable code

## Important Patterns

<!-- Patterns you've discovered that work well -->

### Hybrid Search (70/30)
Combine vector similarity (70%) with BM25 keyword matching (30%) for best results. FTS catches exact terms, vectors catch semantic similarity.

### Hook Context Injection
- SessionStart: Surface open threads first, then key decisions
- UserPromptSubmit: Auto-nudge to save memos after ~20 messages

### Atomic Index Rebuild
Use temp DB → swap pattern to prevent corruption during rebuilds.

## Recent Learnings

<!-- Technical learnings that apply across projects -->
- **sqlite-vec syntax**: Use `WHERE embedding MATCH ? AND k = N`, not `LIMIT N`
- **Plugin structure**: Components at repo root, not nested in subdirectory
- **Hook timeouts**: UserPromptSubmit is 3s, PreCompact is 10s, SessionEnd is 30s, SessionStart is 7s

## Open Questions

<!-- Questions you're still exploring -->
