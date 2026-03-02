---
name: memo-writing
description: |
  How to write effective session memos. Use when:
  - User invokes /memex:save
  - User says "save this for later" or "remember this"
  - User asks to "create a memo" or "document this session"
  - Manually capturing important decisions or learnings
  - The [memex] activity nudge appears in context

  <example>
  Context: User asks to save current context
  User: "Can you save what we discussed about the retry pattern?"
  Assistant: "I'll create a memo capturing the retry pattern discussion."
  <commentary>
  Use memo-writing skill to ensure proper format with future context, decisions, tensions, and open threads.
  </commentary>
  </example>

  <example>
  Context: Activity nudge appears
  System: "[memex] Substantial session activity detected. Consider /memex:save"
  Assistant: "We've done substantial work. Let me save a memo before context gets too long."
  <commentary>
  When the nudge appears, proactively suggest saving. You have the best context right now.
  </commentary>
  </example>
allowed-tools: Write, Read, Bash, Grep, Glob
---

# Writing Effective Session Memos

## Core Principle

**You were there. A subagent reading the transcript later wasn't.** Your memo from full context will always be better than a reconstructed one. Write from experience, not summary.

Capture the **journey**, not just the destination. Future-Claude needs to understand:
- How things were discovered, not just what was found
- What was tried and failed, not just what worked
- What surprised you or was unexpectedly hard
- Where user and AI disagreed or changed direction

## Memo Format

```markdown
---
type: memo
title: [Specific, searchable title]
project: [project-name]
date: [ISO date]
topics:
  - topic-name-kebab-case
  - another-topic
manual: true
---

# [Title]

## For Future Context

[1-2 sentences: the single most important thing for someone continuing this work]

## Summary

[2-4 sentences: what was the actual work, not "we discussed X" but "we tried Y, discovered Z"]

## What Happened

### Key Decisions
- **[Decision]**: [Why this over alternatives. Note tradeoffs.]

### What Worked / What Didn't
- **Tried**: [Approach that failed or was abandoned] → **Why**: [Reason it failed]
- **Solution**: [What actually worked] → **Insight**: [Why it worked]

### Surprises & Difficulties
- [Unexpectedly hard things, multiple attempts, friction]
- [Surprisingly easy things, or things that solved bigger problems]

### Insights
- [Patterns, gotchas, or principles that transfer to other contexts]
- [Non-obvious discoveries — not "X is complex" but "X fails silently when Y"]

## Perspectives & Tensions

[When user and AI had different takes, or user expressed uncertainty/frustration/changed direction. Preserve quotes when they reveal thinking:]

- **User's priority**: [What the user emphasized or pushed for]
- **AI's approach**: [Where Claude offered alternatives or raised concerns]
- **Resolution**: [How it was decided, or if still open]

[Omit this section entirely if the session was straightforward]

## Open Threads

- [ ] [Concrete next step with enough context to act on]
- [ ] [Unresolved question with why it matters]
- [ ] [Blocked item with what's blocking it]

## Related

- [[existing-topic]]
- [[?suggested-new-concept]]

## Context Signals

- **Difficulty**: [Easy / Moderate / Complex / Grinding]
- **Session character**: [Exploration / Debugging / Implementation / Planning / Discussion]
```

---

## Before Writing: Search for Connections

Always search the vault before writing to find related memos:
```bash
uv run scripts/search.py "<keywords>" --mode=hybrid --format=text --limit=5
```

Use results to:
- Add wikilinks to related memos
- Note if this contradicts or supersedes a previous decision
- Suggest topic links that already exist

---

## Quality Criteria

### Good Memos Capture:

1. **"For Future Context"** — the one-liner briefing for your successor self
2. **Failed approaches** — what was tried and abandoned (often more valuable than what worked)
3. **Surprises** — what was harder or easier than expected
4. **User's voice** — quotes that reveal priorities, concerns, or thinking
5. **Specific open threads** — "decide between A and B for scenario C" not "think about edge cases"
6. **Alternatives considered** — what were the options? Why were some rejected?
7. **Friction points** — where did things get stuck? What took multiple attempts?

### Avoid:

- Event logs ("user asked, Claude answered")
- Tautologies ("sandbox configured successfully" — that says nothing)
- Generic learnings ("authentication is important")
- Repeating what's in the code (code is the source of truth)
- Vague open threads ("maybe add tests", "consider performance")

---

## Title Guidelines

Specific and searchable:

| Bad | Good |
|-----|------|
| "Session Notes" | "JWT Token Refresh Bug — Race Condition in Middleware" |
| "Debugging" | "Rate Limiting Fix — Exponential Backoff with Jitter" |
| "Planning" | "Memex Memo Architecture — From API Hooks to Subagents" |

---

## Length

| Session type | Target length |
|---|---|
| Quick fix / simple task | 200-300 words |
| Standard session | 400-800 words |
| Complex multi-thread | 800-1200 words |
| Deep exploration / design session | 1000+ words |

**The measure isn't word count — it's "can future-Claude pick up where we left off?"**

---

## When to Save

- **After the [memex] nudge appears** — the system detected substantial activity
- **After major decisions** — don't wait for session end
- **Before you think compaction might happen** — long sessions, many tool calls
- **When changing direction** — capture why the pivot happened
- **When the user says "remember this"** or similar

---

## Open Threads Format

Specific and actionable:

```markdown
## Open Threads

- [ ] Test retry logic with network timeout > 30s (currently untested)
- [ ] Decide: RRF k=60 vs k=30 — lower k favors keyword matches
- [ ] Blocked: need LM Studio running to test vector search changes
```

Not:
```markdown
- [ ] Think about edge cases
- [ ] Maybe add tests
- [ ] Consider performance
```

---

## Wikilinks

- `[[topic-name]]` — link to existing topic
- `[[projects/myproject/memos/memo-name]]` — link to specific memo
- `[[?new-concept]]` — suggest concept that doesn't exist yet (prefixed with ?)

---

## Quality Check

Before finalizing, ask:
1. If I loaded this cold in a new session, could I continue the work?
2. Does "For Future Context" give the single most important thing?
3. Does it capture what was *difficult* or *surprising*, not just accomplished?
4. Are open threads specific enough to act on?
5. Would the user recognize this as faithful to the session?
6. Do wikilinks point to things that actually exist?
