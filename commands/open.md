---
description: Open the memex vault in Finder or Obsidian
allowed-tools: Bash
argument-hint: "[finder|obsidian] - where to open (default: finder)"
---

# Open Command

Open the memex vault in Finder or Obsidian for browsing.

## Vault Path

`cd $(memex path)` before any `uv run` command.

## Instructions

1. **Parse argument** to determine target:
   - `finder` or `f` or empty: Open in Finder
   - `obsidian` or `o`: Open in Obsidian

2. **Open the vault**:

   For Finder (macOS):
   ```bash
   open $(memex path)
   ```

   For Obsidian:
   ```bash
   open "obsidian://open?vault=memex"
   # Or if vault name is different:
   open -a Obsidian $(memex path)
   ```

3. **Confirm** the action

## Platform Notes

- **macOS**: Uses `open` command
- **Linux**: Use `xdg-open` for Finder equivalent
- **Windows**: Use `explorer` or `start`

## Output

```
📂 Opened memex vault in [Finder/Obsidian]

Path: <resolved vault path>

Quick links:
- projects/ - Your project memos
- topics/ - Cross-project concepts
- _views/ - Obsidian dashboards
```

## Examples

- `/memex:open` - Open in Finder
- `/memex:open obsidian` - Open in Obsidian
- `/memex:open f` - Open in Finder (shorthand)
