---
description: Synthesize multiple memos into a concept or summary note
allowed-tools: Read, Write, Glob
argument-hint: "<topic> [memo1] [memo2] ... - topic name and optional specific memos"
---

# Merge Command

Combine insights from multiple memos into a unified concept note or synthesis.

## Path Resolution (Required First)

**IMPORTANT:** `${CLAUDE_PLUGIN_ROOT}` points to the plugin cache, NOT the vault!

Resolve the vault path:
1. Read `~/.memex/config.json` → use `memex_path` if present
2. Fallback: use the plugin's own location (where `scripts/` lives)

Store this as `$VAULT` for use in paths below.

## Instructions

1. **Identify memos to merge**:
   - If specific memos provided, load those
   - If only topic provided, find all memos tagged with that topic
   - If nothing provided, ask user what to merge

2. **Read all source memos** and extract:
   - Key learnings
   - Patterns that repeat
   - Decisions and rationale
   - Solutions that worked

3. **Synthesize into a concept note**:
   ```yaml
   ---
   type: concept
   title: [Topic Name]
   created: [today]
   sources: [list of memo paths]
   projects: [projects these came from]
   ---
   ```

4. **Save to** `$VAULT/topics/<topic-slug>.md`

5. **Update source memos** (optional):
   - Add backlink to the concept
   - Mark as merged

## Output

```
📚 Created concept: [[error-handling]]

Synthesized from 4 memos:
- OAuth Token Refresh Fix
- API Error Handling Patterns
- Database Connection Resilience
- Retry Logic Implementation

Key patterns extracted:
1. Exponential backoff for retries
2. Distinguish retriable vs fatal errors
3. Always log before retry

Saved to: topics/error-handling.md
```

## Examples

- `/memex:merge authentication` - Merge all memos tagged with authentication
- `/memex:merge "api patterns" memo1.md memo2.md` - Merge specific memos into "api patterns"
