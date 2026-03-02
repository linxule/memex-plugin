---
description: Open the memex vault in Finder or Obsidian
allowed-tools: Bash
argument-hint: "[finder|obsidian] - where to open (default: finder)"
---

# Open Command

Open the memex vault in Finder or Obsidian for browsing.

## Path Resolution (Required First)

**IMPORTANT:** `${CLAUDE_PLUGIN_ROOT}` points to the plugin cache, NOT the vault!

Resolve the vault path:
1. Read `~/.memex/config.json` → use `memex_path` if present
2. Fallback: use the plugin's own location (where `scripts/` lives)

Store this as `$VAULT` for use in paths below.

## Instructions

1. **Parse argument** to determine target:
   - `finder` or `f` or empty: Open in Finder
   - `obsidian` or `o`: Open in Obsidian

2. **Open the vault**:

   For Finder (macOS):
   ```bash
   open "$VAULT"
   ```

   For Obsidian:
   ```bash
   open "obsidian://open?vault=memex"
   # Or if vault name is different:
   open -a Obsidian "$VAULT"
   ```

3. **Confirm** the action

## Platform Notes

- **macOS**: Uses `open` command
- **Linux**: Use `xdg-open` for Finder equivalent
- **Windows**: Use `explorer` or `start`

## Output

```
📂 Opened memex vault in [Finder/Obsidian]

Path: $VAULT

Quick links:
- projects/ - Your project memos
- topics/ - Cross-project concepts
- scripts/ - Utility scripts
```

## Examples

- `/memex:open` - Open in Finder
- `/memex:open obsidian` - Open in Obsidian
- `/memex:open f` - Open in Finder (shorthand)
