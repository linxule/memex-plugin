---
description: Load a specific topic, memo, or project context into the current session
allowed-tools: Read, Glob
argument-hint: "<topic|memo|project> - what to load"
effort: low
---

# Load Command

Load specific content from the memex vault into the current session context.

## Vault Path

`cd $(memex path)` before any `uv run` command.

## Instructions

1. **Parse the argument** to determine what to load:
   - Topic name: Load from `topics/<name>.md`
   - Memo reference: Load from `projects/<project>/memos/<name>.md`
   - Project name: Load `projects/<project>/_project.md` and recent memos

2. **Find the file(s)**:
   ```bash
   # For topics
   ls $(memex path)/topics/*.md | grep -i "<query>"

   # For memos
   find $(memex path)/projects -name "*.md" -path "*/memos/*" | xargs grep -l "<query>"

   # For projects
   ls $(memex path)/projects/
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
