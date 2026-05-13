---
description: Show memex statistics and status including projects, memos, and pending items
allowed-tools: Read, Bash, Glob
effort: low
---

# Status Command

Display comprehensive status of the memex vault.

## Instructions

1. **Count files** by type:
   ```bash
   # Quick vault summary (if Obsidian is running)
   cd $(memex path) && uv run scripts/obsidian_cli.py status

   # Count memos
   find $(memex path)/projects -name "*.md" -path "*/memos/*" 2>/dev/null | wc -l

   # Count transcripts
   find $(memex path)/projects -name "*.md" -path "*/transcripts/*" 2>/dev/null | wc -l

   # Count topics (native CLI if available)
   cd $(memex path) && uv run scripts/obsidian_cli.py files --folder=topics --total 2>/dev/null || ls $(memex path)/topics/*.md 2>/dev/null | wc -l

   # List projects
   ls -d $(memex path)/projects/*/ 2>/dev/null | xargs -n1 basename
   ```

2. **Check pending memos** (respects custom `state_dir` from `~/.memex/config.json`):
   ```bash
   # `memex context` prints "Pending memos: N" only when N > 0; absent => 0
   memex context 2>/dev/null | awk -F': ' '/^Pending memos:/ {print $2; found=1} END {if (!found) print 0}'
   ```

3. **Check index status**:
   ```bash
   memex index status
   ```

4. **Check condensation staleness** (which projects have undigested memos):
   ```bash
   for d in $(memex path)/projects/*/; do
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
   find $(memex path)/projects -name "*.md" -mtime -7 | head -10
   ```

For Obsidian CLI commands, prefix with `cd $(memex path) &&`.

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
- cognition: 4 undigested memos (last condensed: 2026-02-14)
- (other projects current)

⚠️ Pending: 2 memos failed to generate
   Ask Claude to retry pending memos

Recent Activity (last 7 days):
- 3 new memos
- 5 sessions archived
```
