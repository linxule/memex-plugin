---
description: Load a specific topic, memo, or project context into the current session
allowed-tools: Read, Glob
argument-hint: "<topic|memo|project> - what to load"
---

# Load Command

Load specific content from the memex vault into the current session context.

## Path Resolution (Required First)

**IMPORTANT:** `${CLAUDE_PLUGIN_ROOT}` points to the plugin cache, NOT the vault!

Resolve the vault path:
1. Read `~/.memex/config.json` → use `memex_path` if present
2. Fallback: use the plugin's own location (where `scripts/` lives)

Store this as `$VAULT` for use in paths below.

## Instructions

1. **Parse the argument** to determine what to load:
   - Topic name: Load from `topics/<name>.md`
   - Memo reference: Load from `projects/<project>/memos/<name>.md`
   - Project name: Load `projects/<project>/_project.md` and recent memos

2. **Find the file(s)**:
   ```bash
   # For topics
   ls $VAULT/topics/*.md | grep -i "<query>"

   # For memos
   find $VAULT/projects -name "*.md" -path "*/memos/*" | xargs grep -l "<query>"

   # For projects
   ls $VAULT/projects/
   ```

3. **Read and present** the content:
   - Show the full content for single files
   - Show summaries for multiple matches
   - Highlight key sections

4. **Offer to load more** if multiple matches found

## Output

Present the loaded content clearly:

```
📚 Loaded: [[error-handling]]

---
[Content of the topic file]
---

This topic is referenced in 3 memos:
- OAuth Token Refresh Fix
- API Error Handling Patterns
- Database Connection Resilience
```

## Examples

- `/memex:load error-handling` - Load the error-handling topic
- `/memex:load myproject` - Load myproject overview and recent memos
- `/memex:load "oauth fix"` - Search and load matching memo
