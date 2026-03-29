---
description: Check vault health - broken links, orphans, and maintenance suggestions
allowed-tools: Read, Bash, Glob, Write
argument-hint: "[--fix]"
effort: max
---

# Vault Maintenance Command

Check the memex vault for issues and run the maintenance dreamer when appropriate.

## Instructions

### 1. Quick Health Check (if Obsidian is running)

```bash
# Vault overview + key metrics
cd $(memex path) && uv run scripts/obsidian_cli.py status

# Crystallization readiness (alias-aware unresolved link analysis)
memex check --tier ready
```

If Obsidian CLI is unavailable, fall back to the grep/find methods below.

### 1b. Run the Dreamer

```bash
cd $(memex path) && uv run python -m memex.dreamer --scope=all --dry-run
cd $(memex path) && uv run python -m memex.dreamer --scope=project:memex --dry-run
```

Only run without `--dry-run` when the user wants maintenance changes applied. Without `--dry-run`, the dreamer calls Claude Sonnet via `claude` CLI for semantic reasoning (no API key needed — uses Claude Code auth). Falls back to token heuristics if `claude` is unavailable.

### 2. Check for Broken Links

```bash
# Via Obsidian CLI (preferred — alias-aware, accurate)
cd $(memex path) && uv run scripts/obsidian_cli.py unresolved --verbose

# Validate links in a specific file
cd $(memex path) && uv run scripts/obsidian_cli.py check-links --path="projects/<project>/_project.md"

# Fallback: extract wikilinks manually
grep -rh '\[\[' $(memex path)/projects/ $(memex path)/topics/ 2>/dev/null | \
  grep -o '\[\[[^]?][^]]*\]\]' | sed 's/\[\[//;s/\]\]//' | sort | uniq
```

For each unresolved link, decide: add alias to existing topic, create new topic, or leave as seedling.

### 3. Find Orphan and Dead-End Notes

```bash
# Via Obsidian CLI
cd $(memex path) && uv run scripts/obsidian_cli.py orphans --total
cd $(memex path) && uv run scripts/obsidian_cli.py deadends --total

# Fallback
memex graph orphans
```

Most orphans are transcripts (expected). Focus on orphan topics and memos.

### 4. Check Suggested Concepts

```bash
grep -rh '\[\[?' $(memex path)/projects/ 2>/dev/null | \
  grep -o '\[\[?[^]]*\]\]' | sort | uniq -c | sort -rn
```

Concepts with 3+ mentions across projects are candidates for crystallization.

### 5. Project Overview Freshness

```bash
# Which projects have undigested memos since last condensation?
for d in $(memex path)/projects/*/; do
  name=$(basename "$d")
  proj_md="$d/_project.md"
  count=$(find "$d/memos" -name "*.md" -newer "$proj_md" 2>/dev/null | wc -l | tr -d ' ')
  if [ "$count" -gt 0 ]; then
    condensed=$(grep -m1 'condensed:' "$proj_md" 2>/dev/null | awk '{print $2}')
    echo "$name: $count undigested (last condensed: ${condensed:-never})"
  fi
done
```

Projects with 5+ undigested memos need condensation.

### 6. Index Status

```bash
memex index status
```

Check if FTS docs match actual file count. Run `memex index rebuild --incremental` if out of sync.

### 7. Process Embedding Queue

```bash
# Check pending embedding count
wc -l < ~/.memex/pending_embeddings.jsonl 2>/dev/null || echo "0"

# Process pending embeddings (incremental rebuild handles this)
memex index rebuild --incremental
```

For Obsidian CLI commands, prefix with `cd $(memex path) &&`.

## Output Format

```
## Vault Health Report

### Overview
- Files: 1546 | Markdown: 996 | Aliases: 202
- Unresolved links: 492 (301 actionable after noise + alias filtering)
- Orphans: 1392 (mostly transcripts)

### Condensation Staleness
- memex: 14 undigested memos (last condensed: 2026-02-14)
- cognition: 4 undigested memos (last condensed: 2026-02-14)

### Crystallization
- OVERDUE: [[academic-writing]] (9 refs), [[engagement-first-revision]] (5 refs)
- READY: (0 items)
- MATURING: 21 items

### Broken Links (X actionable)
- [[missing-topic]] - 5 refs across 3 projects
  → Suggest: Create topic or add alias

### Index
- FTS: X docs, Vector: Y docs
- Status: Synced / Needs rebuild
- Pending embeddings: X
```

## With --fix Flag

If user passes `--fix`, automatically:
1. Create stub notes for high-frequency broken links (3+ refs)
2. Add aliases where variant phrasings point to existing topics
3. Run incremental index rebuild

Otherwise, just report findings.
