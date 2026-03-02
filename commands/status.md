---
description: Show memex statistics and status including projects, memos, and pending items
allowed-tools: Read, Bash, Glob
---

# Status Command

Display comprehensive status of the memex vault.

## Path Resolution (Required First)

**IMPORTANT:** `${CLAUDE_PLUGIN_ROOT}` points to the plugin cache, NOT the vault!

Resolve the vault path:
1. Read `~/.memex/config.json` → use `memex_path` if present
2. Fallback: use the plugin's own location (where `scripts/` lives)

Store this as `$VAULT` for use in paths below.

## Instructions

1. **Count files** by type:
   ```bash
   cd $VAULT

   # Quick vault summary (if Obsidian is running)
   uv run scripts/obsidian_cli.py status

   # Count memos
   find projects -name "*.md" -path "*/memos/*" 2>/dev/null | wc -l

   # Count transcripts
   find projects -name "*.md" -path "*/transcripts/*" 2>/dev/null | wc -l

   # Count topics (native CLI if available)
   uv run scripts/obsidian_cli.py files --folder=topics --total 2>/dev/null || ls topics/*.md 2>/dev/null | wc -l

   # List projects
   ls -d projects/*/ 2>/dev/null | xargs -n1 basename
   ```

2. **Check pending memos**:
   ```bash
   find ~/.memex/pending-memos -name "*.json" 2>/dev/null | wc -l
   ```

3. **Check index status**:
   ```bash
   ls -la $VAULT/_index.sqlite 2>/dev/null
   ```

4. **Check condensation staleness** (which projects have undigested memos):
   ```bash
   for d in $VAULT/projects/*/; do
     name=$(basename "$d")
     proj_md="$d/_project.md"
     count=$(find "$d/memos" -name "*.md" -newer "$proj_md" 2>/dev/null | wc -l | tr -d ' ')
     if [ "$count" -gt 0 ]; then
       condensed=$(grep -m1 'condensed:' "$proj_md" 2>/dev/null | awk '{print $2}')
       echo "$name: $count undigested memos (last condensed: ${condensed:-never})"
     fi
   done
   ```

5. **Get recent activity**:
   ```bash
   find projects -name "*.md" -mtime -7 | head -10
   ```

## Output Format

```
📊 Memex Status

Projects: 5
├── myproject (12 memos, 8 transcripts)
├── another-project (3 memos, 2 transcripts)
└── ...

Totals:
- 📝 Memos: 24
- 📜 Transcripts: 15
- 💡 Topics: 8

Search Index: ✅ Up to date (1.2 MB)

Condensation:
- memex: 14 undigested memos (last condensed: 2026-02-14)
- webapp: 4 undigested memos (last condensed: 2026-02-14)
- (other projects current)

⚠️ Pending: 2 memos failed to generate
   Run /memex:retry to process

Recent Activity (last 7 days):
- 3 new memos
- 5 sessions archived
```
