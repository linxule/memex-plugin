---
paths:
  - "scripts/transcript_to_md.py"
  - "scripts/discover_sessions.py"
  - "scripts/backfill_has_memo.py"
  - "scripts/backfill_tokens.py"
---

# Transcript Processing

## Gotchas

- **Transcript value proxy** - When `messages` count isn't queryable, use `file.stat().st_size` - sessions >100KB are usually substantial, <10KB often aborted
- **Transcript minimum viability** - SessionEnd now skips archiving sessions with <6 JSONL lines and no tool usage (catches "hi", test prompts, aborted sessions)
- **Transcript system tag cleaning** - `transcript_to_md.py` strips `<system-reminder>`, `<local-command-*>`, `<command-name>` tags from user messages AND tool results, and compresses skill prompt injections to one-liners
- **Transcript noise types skipped** - `queue-operation` (queued user input) and `system` (local command scaffolding) messages are skipped entirely, alongside `file-history-snapshot`, `progress`, `summary`
- **Skill expansion detection is two-tier** - Exact match for known memex skills (Save Memo, Search, etc.) + generic heading + secondary marker (## Instructions, ARGUMENTS:) for other plugins. Uses `\A` anchor to avoid false-positive on headings mid-message
- **Session ID format mismatch** - Claude stores sessions as full UUIDs (`0bf2b767-fb2e-441b-...`), memex renames with date prefix (`20260202-150000-0bf2b767`). `discover_sessions.py` matches on the 8-char UUID prefix to cross-reference correctly
- **Token usage comes from assistant messages** - `usage.input_tokens`, `usage.output_tokens`, `usage.cache_read_input_tokens` are in the `message` dict of `type: assistant` entries. `input_tokens` in frontmatter includes cache_read. Sessions without assistant responses (aborted) have no usage data
- **Triage scoring is additive** - Write/Edit (+3 each, cap 15), git commits (+5 each, cap 15), compactions (+4), subagents (+3 each, cap 9), turns (+1 each, cap 10), duration (+1 to +8), opus model (+2), long opening prompt (+2), errors (-1 each, cap -3). Score >= 9 is "worth importing"
- **`backfill_tokens.py` is frontmatter-only** - Patches 3 lines into existing `---` block without touching markdown body. Safe to re-run (skips files that already have `input_tokens:`). Does NOT update the Summary section — only new conversions via `transcript_to_md.py` get the token summary line
- **`claude_dir_to_project_name()` is heuristic** - Strips `/Users/<name>/Documents/` etc. from hyphenated paths. `~home` (user home dir sessions) maps to `_uncategorized`. Adapted from peteromallet/dataclaw

## Version Dependencies: Claude Code Message Format (as of Feb 2026)

**System tags stripped:** `<system-reminder>`, `<local-command-*>`, `<command-name>`, `<command-message>`, `<command-args>`.

**Message types skipped:** `file-history-snapshot`, `progress`, `summary`, `queue-operation`, `system`.

**Skill expansion format:** Full prompt (50+ lines) arrives as user message with heading like `# Save Memo Command`. Compressed to: `[Skill invoked: Save Memo — args]`.

**When to re-test (after Claude Code updates):**
1. Run a test session, invoke `/memex:save`, check transcript for new tag formats
2. Grep recent transcripts for unstripped `<` patterns: new tags need adding to `SYSTEM_TAG_PATTERNS`
3. If skill heading format changes, update `SKILL_EXPANSION_EXACT_RE` / `SKILL_EXPANSION_GENERIC_RE`
