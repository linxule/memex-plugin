---
description: Manually save current context as a memo to the memex vault
allowed-tools: Read, Write, Bash, Grep, Glob
argument-hint: "[title] - optional title for the memo"
---

# Save Memo Command

Save the current session context as a memo to the memex vault.

## Path Resolution (Required First)

**IMPORTANT:** `${CLAUDE_PLUGIN_ROOT}` points to the plugin cache, NOT the vault!

Resolve the vault path:
1. Read `~/.memex/config.json` → use `memex_path` if present
2. Fallback: use the plugin's own location (where `scripts/` lives)

Store this as `$VAULT` for use in paths below.

## Instructions

### 1. Detect the Project

Use git remote or working directory to identify the project.

### 2. Connect to Existing Knowledge

If you know (or suspect) this session relates to previous work, search:
```bash
uv run $VAULT/scripts/search.py "<keywords>" --mode=hybrid --format=text --limit=5
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

Save to: `$VAULT/projects/<project>/memos/<date>-<title-slug>.md`

Example: `$VAULT/projects/my-app/memos/2026-02-13-multi-agent-architecture-decision.md`

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
uv run $VAULT/scripts/mark_memo_saved.py
```

### 5. Verify Quality

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
