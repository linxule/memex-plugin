---
description: Manually save current context as a memo to the memex vault
allowed-tools: Read, Write, Bash, Grep, Glob
argument-hint: "[title] - optional title for the memo"
---

# Save Memo Command

Save the current session context as a memo to the memex vault.

## Instructions

### 1. Detect the Project

Use git remote or working directory to identify the project.

### 2. Connect to Existing Knowledge

If you know (or suspect) this session relates to previous work, search:
```bash
memex search "<keywords>" --limit=5
```

Use results to:
- Add wikilinks to related memos
- Note if this contradicts or supersedes a previous decision
- Suggest topic links that already exist

Skip this step for standalone topics where you already know the relevant links.

### 3. Write the Memo

**Use the format and quality guidelines from the `memo-writing` skill.** You have full session context — you were THERE. Capture:

- The journey (what was tried, what failed, what surprised you)
- Alternatives considered and why they were rejected
- Failed approaches (often more valuable than what worked)
- User's voice (quotes that reveal priorities, concerns, thinking)
- Specific open threads (not vague "think about this")

The memo template is defined in the skill. Key sections:
- **For Future Context** — the one-liner briefing
- **Summary** — what was the actual work (not "we discussed X")
- **What Happened** — key decisions, what worked/didn't, surprises, insights
- **Perspectives & Tensions** — where user and AI disagreed or changed direction (omit if straightforward)
- **Open Threads** — concrete next steps, unresolved questions, blocked items
- **Related** — wikilinks to related topics/memos
- **Context Signals** — difficulty, session character

Target length: 400-800 words for substantial sessions, 200-300 for quick fixes, 800+ for complex multi-thread work. The measure: **can future-Claude pick up where we left off?**

### 4. Save

Save to: `$(memex path)/projects/<project>/memos/<date>-<title-slug>.md`

Example: `$(memex path)/projects/alcor/memos/2026-02-13-multi-agent-architecture-decision.md`

**Frontmatter fields:**
- `type: memo`
- `title: <Specific, searchable title>`
- `project: <detected-project>`
- `date: <YYYY-MM-DD>`
- `topics: [topic-kebab-case, another-topic]` (kebab-case, not wikilinks)
- `manual: true`

## After Saving

Mark the session so PreCompact knows a memo already exists (prevents duplicate generation):
```bash
memex mark-saved
```

### 5. Extract Observations

You just wrote the memo — you have full context. Now extract 5-15 atomic observations
from it. These feed the intelligence layer and make future search precise.

**Generate observations as JSON**, then store them:

1. Re-read the memo you just saved
2. Extract atomic facts following these rules:
   - Each observation must be independently understandable
   - Use absolute dates (e.g., "2026-03-16" not "today")
   - Capture: decisions, facts, preferences, constraints, open questions
   - For decisions: "Decision: X was chosen over Y because Z"
   - Do NOT extract meta-observations about the memo itself
   - Do NOT extract obvious/trivial facts
   - Types: `explicit` (directly stated), `deductive` (follows from combining facts)
   - Include a `topics` field with 0-3 topic slugs matching files in `topics/` (list with: `ls $(memex path)/topics/*.md | xargs -I{} basename {} .md`). Use `[]` when no topic fits. Invalid slugs are stored without error but break topic clustering.

3. Pipe the observations directly to the store command:
```bash
echo '[
  {"content": "Decision: X was chosen over Y because Z", "obs_type": "explicit", "confidence": "high", "topics": ["relevant-topic"]},
  {"content": "Constraint: A requires B", "obs_type": "explicit", "confidence": "high", "topics": ["another-topic", "second-topic"]},
  {"content": "Fact: Y occurred on 2026-03-16", "obs_type": "explicit", "confidence": "high", "topics": []}
]' | memex backfill obs --stdin --doc-path "<memo-relative-path>"
```

Replace `<memo-relative-path>` with the path relative to the vault
(e.g., `projects/memex/memos/2026-03-16-example.md`).

### 6. Signal Touched Topics

After saving, record which topics this memo touches so garden-tending knows where attention is needed.

Parse the wikilinks from the memo you just wrote. For each topic that exists in `topics/`, append a signal — following `redirect_to:` chains so archived topics route to their canonical replacement:

```bash
VAULT=$(memex path)
MEMO_PATH="<relative-path-to-memo-just-saved>"
MEMO_DATE="<YYYY-MM-DD>"
MEMO_TITLE="<short-title>"

# Resolve a topic's `redirect_to` chain to its canonical destination.
# Stdout is the resolved slug or vault-relative path (no .md); empty +
# non-zero exit when the chain ends in an archived stub without
# redirect_to, contains a cycle, or exceeds the 5-hop limit. Warnings
# stream to stderr so the user sees them in the session log.
resolve_topic() {
  memex topic resolve "$1"
}

# For each topic wikilinked in the memo, resolve redirects then append a signal
# Example: if memo links to [[embedding-models]] and [[hybrid-search]]
SIGNAL_LINE="- $MEMO_DATE: $MEMO_TITLE ([[$MEMO_PATH|memo]])"
for TOPIC in <list-of-topic-slugs-from-wikilinks>; do
  RESOLVED=$(resolve_topic "$TOPIC") || continue
  [ -z "$RESOLVED" ] && continue
  # Destination may be a bare topic slug or a vault-relative path
  # (e.g. `projects/clawd-world/_project`) — handle both.
  if [[ "$RESOLVED" == */* ]]; then
    TOPIC_FILE="$VAULT/$RESOLVED.md"
  else
    TOPIC_FILE="$VAULT/topics/$RESOLVED.md"
  fi
  [ -f "$TOPIC_FILE" ] || continue
  # Check if Recent signals section exists, create if not
  if ! grep -q -- "## Recent signals" "$TOPIC_FILE"; then
    echo -e "\n## Recent signals\n" >> "$TOPIC_FILE"
  fi
  # Idempotent append: skip if this exact signal line already exists.
  # Prevents duplicates when multiple frontmatter topics redirect to the
  # same canonical (e.g. claude-code-plugins + plugin-architecture both
  # → Claude-Code-Plugins) or when /memex:save reruns on the same memo.
  # The `--` terminates grep option parsing so $SIGNAL_LINE's leading
  # `- ` (and any future `-`-prefixed content) is read as a pattern,
  # not as flags.
  if ! grep -Fxq -- "$SIGNAL_LINE" "$TOPIC_FILE"; then
    echo "$SIGNAL_LINE" >> "$TOPIC_FILE"
  fi
done
```

This keeps topics warm between garden-tending sessions — each topic accumulates a breadcrumb trail of recent activity. During tending, topics with many unincorporated signals get prioritized. The `redirect_to:` resolution prevents signals from accumulating on archived stubs (e.g., `multi-agent-code-review` routes to its canonical replacement `multi-agent-review`), and supports cross-namespace targets (a topic redirecting into `projects/<name>/_project.md`). The resolver detects redirect cycles and surfaces a stderr warning identifying the chain.

**Skip this step** if the memo links to 0 existing topics or if the session was trivial.

### 7. Verify Quality

Before finalizing, check:
1. If I loaded this cold in a new session, could I continue the work?
2. Does "For Future Context" give the single most important thing?
3. Does it capture what was *difficult* or *surprising*, not just accomplished?
4. Are open threads specific enough to act on?
5. Would the user recognize this as faithful to the session?
6. Do wikilinks point to things that actually exist?

## Output

Confirm the save with:
- File path created
- Topics tagged
- Key thing captured (the "For Future Context" line)
