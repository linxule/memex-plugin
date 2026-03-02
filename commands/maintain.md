---
description: Check vault health - broken links, orphans, and maintenance suggestions
allowed-tools: Read, Bash, Glob, Write
argument-hint: "[--fix]"
---

# Vault Maintenance Command

Check the memex vault for issues and suggest improvements.

## Path Resolution (Required First)

**IMPORTANT:** `${CLAUDE_PLUGIN_ROOT}` points to the plugin cache, NOT the vault!

Resolve the vault path:
1. Read `~/.memex/config.json` → use `memex_path` if present
2. Fallback: use the plugin's own location (where `scripts/` lives)

Store this as `$VAULT` for use in paths below.

## Instructions

### 1. Quick Health Check (if Obsidian is running)

```bash
# Vault overview + key metrics
uv run scripts/obsidian_cli.py status

# Crystallization readiness (alias-aware unresolved link analysis)
uv run scripts/crystallization_check.py --tier ready
```

If Obsidian CLI is unavailable, fall back to the grep/find methods below.

### 2. Check for Broken Links

```bash
# Via Obsidian CLI (preferred — alias-aware, accurate)
uv run scripts/obsidian_cli.py unresolved --verbose

# Validate links in a specific file
uv run scripts/obsidian_cli.py check-links --path="projects/<project>/_project.md"

# Fallback: extract wikilinks manually
grep -rh '\[\[' $VAULT/projects/ $VAULT/topics/ 2>/dev/null | \
  grep -o '\[\[[^]?][^]]*\]\]' | sed 's/\[\[//;s/\]\]//' | sort | uniq
```

For each unresolved link, decide: add alias to existing topic, create new topic, or leave as seedling.

### 3. Find Orphan and Dead-End Notes

```bash
# Via Obsidian CLI
uv run scripts/obsidian_cli.py orphans --total
uv run scripts/obsidian_cli.py deadends --total

# Fallback
uv run scripts/graph_queries.py orphans
```

Most orphans are transcripts (expected). Focus on orphan topics and memos.

### 4. Check Suggested Concepts

```bash
grep -rh '\[\[?' $VAULT/projects/ 2>/dev/null | \
  grep -o '\[\[?[^]]*\]\]' | sort | uniq -c | sort -rn
```

Concepts with 3+ mentions across projects are candidates for crystallization.

### 5. Project Overview Freshness

```bash
# Which projects have undigested memos since last condensation?
for d in $VAULT/projects/*/; do
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
uv run scripts/index_rebuild.py --status
```

Check if FTS docs match actual file count. Run `--incremental` if out of sync.

### 7. Process Embedding Queue

```bash
# Check pending embedding count
wc -l < ~/.memex/pending_embeddings.jsonl 2>/dev/null || echo "0"

# Process pending embeddings (incremental rebuild handles this)
uv run scripts/index_rebuild.py --incremental
```

## Output Format

```
## Vault Health Report

### Overview
- Files: N | Markdown: N | Aliases: N
- Unresolved links: N (N actionable after noise + alias filtering)
- Orphans: N (mostly transcripts)

### Condensation Staleness
- my-app: 14 undigested memos (last condensed: 2026-02-14)
- webapp: 4 undigested memos (last condensed: 2026-02-14)

### Crystallization
- OVERDUE: [[topic-name]] (N refs), [[another-topic]] (N refs)
- READY: (N items)
- MATURING: N items

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
