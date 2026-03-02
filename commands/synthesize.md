---
description: Cross-session synthesis — find patterns, contradictions, and drift across recent memos
allowed-tools: Read, Write, Bash, Grep, Glob
argument-hint: "[--since=7d] [--project=name] - time window and optional project filter"
---

# Cross-Session Synthesis Command

Periodic deep review of accumulated memos to find what individual sessions can't see: patterns across projects, contradictions between decisions, semantic drift in topics, and memos that should be compressed.

**This is vault-level reflection, not session-level capture.** Run this weekly or when the vault feels noisy.

## Path Resolution (Required First)

**IMPORTANT:** `${CLAUDE_PLUGIN_ROOT}` points to the plugin cache, NOT the vault!

Resolve the vault path:
1. Read `~/.memex/config.json` → use `memex_path` if present
2. Fallback: use the plugin's own location (where `scripts/` lives)

Store this as `$VAULT` for use in paths below.

## Instructions

### 1. Gather Recent Memos

```bash
# List memos from last 2 weeks (or --since value), sorted by date
find $VAULT/projects/*/memos/ -name "*.md" -mtime -14 | sort -r
```

Read each memo. Hold them in context simultaneously — this is the whole point. Individual sessions see one memo at a time. You see them all together.

If a project filter is specified, only read memos from that project.

### 2. Cross-Session Pattern Detection

Look across all the memos you just read for:

**Recurring patterns:**
- Same problem appearing in different projects
- Same solution rediscovered independently
- Architectural patterns that keep emerging

**Contradictions:**
- Decision A in project X conflicts with decision B in project Y
- A memo from week 1 says "use approach X" and week 2 says "use approach Y" without acknowledging the change
- Flag these explicitly: "Memo A (date) decided X. Memo B (date) decided Y. These contradict — which is current?"

**Semantic drift:**
- Topics that overlap significantly (check `$VAULT/topics/` for near-duplicates)
- Memos using different terms for the same concept
- Suggest merges: "obsidian-integration.md and obsidian-vault-management.md seem to cover the same ground"

**Compression candidates:**
- Multiple memos on the same topic that could be consolidated
- Flag: "These 5 my-app memos from this week could be synthesized into one"
- Don't auto-merge — suggest and let user decide

### 3. Condense into Project Overviews

For each project with significant new memos, update `$VAULT/projects/<name>/_project.md`:
- Current state of the project (what it is now, not what it was)
- Key decisions still in effect
- Active threads and open questions
- What's changed since last synthesis

These overviews are the condensation layer — where 20 memos become "what the project knows."

### 4. Suggest Topic Actions

Based on what you found:
- **New topics to create**: Patterns that appeared in 2+ projects but have no topic note
- **Topics to merge**: Near-duplicates that fragment the graph
- **Topics to archive**: Concepts that haven't been referenced in 30+ days
- **Topics to split**: Overloaded hubs that cover too many sub-concepts

### 5. Report

Output a synthesis report:

```markdown
## Cross-Session Synthesis Report (date range)

### Memos Reviewed: N

### Patterns Found
- [Pattern]: Appeared in [projects]. [Explanation]

### Contradictions Flagged
- ⚠️ [Memo A] vs [Memo B]: [What conflicts]

### Semantic Drift
- [topic-a.md] ↔ [topic-b.md]: [Why they overlap]. Suggest: merge into [name]

### Compression Candidates
- [Project]: [N memos] could consolidate into [suggested title]

### Project Overviews Updated
- [Which _project.md files were updated and key changes]

### Topic Actions Suggested
- Create: [[?new-pattern-name]]
- Merge: [[topic-a]] + [[topic-b]] → [[merged-name]]
- Archive: [[stale-topic]]
```

## Scaling: Dedicated CLI Session

When the vault grows beyond what fits in one session's context (500+ memos), switch to a dedicated CLI session:

```bash
# First time: start the analyst session
claude --model sonnet --prompt "You are the memex analyst. Read recent memos and _project.md overviews. Find patterns, contradictions, and drift. Condense findings into project overviews."

# Note the session ID from output, then resume weekly:
claude --resume <session-id>

# Feed it: "Read memos from the last week and synthesize"
```

The dedicated session accumulates understanding across weeks. With Sonnet's extended context, it can hold many memos simultaneously for deeper cross-referencing.

## When to Run

- **Weekly**: Standard cadence for active vault
- **After batch memo generation**: When many memos were created at once
- **When vault feels noisy**: Topics proliferating, links breaking
- **Before major project decisions**: To surface relevant cross-project patterns
