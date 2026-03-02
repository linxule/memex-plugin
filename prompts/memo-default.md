# Session Memo Generation Prompt

You are generating a session memo for future-Claude instances loading into new sessions. Your job is to create a briefing that lets your successor pick up the thread of thinking.

## Core Principle

**Capture the journey, not just the destination.** Future-Claude needs to understand not just what was decided, but *how* it was discovered, what was tried, what surprised you, and where uncertainty remains.

## What Makes a Good Memo

A good memo lets future-Claude (or the user) answer:
- "What was this session actually about?" (not just topic, but the real work)
- "What should I know before continuing this work?"
- "What was tried that didn't work?"
- "What was unexpectedly hard or surprisingly easy?"
- "Where did user and AI disagree or have different priorities?"
- "What questions remain open?"

A bad memo is a report of outcomes. A good memo is a briefing for your successor.

## Output Format

```markdown
---
type: memo
title: [Specific, searchable title - not "Session Notes"]
topics:
  - topic-name-kebab-case
  - another-topic
---

# [Title]

## For Future Context

[1-2 sentences: If someone is continuing this work, the most important thing to know is...]

## Summary

[2-4 sentences: What was the actual work? Not "we discussed X" but "we tried to solve Y, discovered Z was blocking us, ended up doing W"]

## What Happened

### Key Decisions
- **[Decision]**: [Why this choice over alternatives. If there were tradeoffs or disagreements, note them]

### What Worked / What Didn't
- **Tried**: [Approach that failed or was abandoned] → **Why it failed**: [Reason]
- **Solution**: [What actually worked] → **Why it worked**: [Insight]

### Surprises & Difficulties
- [Thing that was unexpectedly hard, or took longer than expected, or required multiple attempts]
- [Thing that turned out to be surprisingly easy or solved a bigger problem than anticipated]

### Insights
- [Pattern, gotcha, or principle that transfers to other contexts]
- [Non-obvious discovery - not just "we learned about X" but "we learned X behaves differently when Y"]

## Perspectives & Tensions

[If there were points where user and AI had different takes, or where the user expressed uncertainty, frustration, or changed direction - capture that. Preserve quotes if they reveal thinking:]

- **User's priority**: [What the user emphasized or pushed for]
- **AI's suggestion**: [Where Claude offered alternatives or raised concerns]
- **Resolution**: [How it was decided, or if it's still open]

[If this section would be empty because the session was straightforward, omit it entirely]

## Open Threads

[Specific, actionable items - not vague "consider X" but "decide whether to use approach A or B for scenario C"]

- [ ] [Concrete next step with enough context to act on]
- [ ] [Unresolved question with why it matters]
- [ ] [Blocked item with what's blocking it]

## Related

[Wikilinks to related topics, memos, or concepts. Use `[[?suggested-topic]]` for concepts that don't exist yet but should]

- [[existing-topic]]
- [[?suggested-new-concept]]

## Context Signals

[Optional metadata for future sessions - include if relevant:]

- **Difficulty**: [Easy / Moderate / Complex / Grinding]
- **Session character**: [Exploration / Debugging / Implementation / Planning / Discussion]
```

## Guidelines

### What to Include

1. **Surprises** - What was unexpected? Failed approaches? Things harder than anticipated?
2. **Friction points** - Where did things get stuck? What took multiple attempts?
3. **User's voice** - Preserve key quotes if they reveal priorities, concerns, or thinking
4. **Alternatives considered** - What were the options? Why were some rejected?
5. **Uncertainty** - Where are you/user not confident? What questions couldn't be answered?
6. **Context clues** - "This was the third attempt" or "User mentioned time pressure" or "Breakthrough came when..."

### What to Avoid

- Event logs ("user asked, Claude answered, then we...")
- Obvious descriptions ("we used Python because it's a Python project")
- Repeating what's already in code/docs (the code is the source of truth)
- Generic learnings ("authentication is important")
- Vague open threads ("think about edge cases")

### Title Guidelines

**Specific and searchable:**
- ✗ "Session Notes", "Debugging", "Planning"
- ✓ "JWT Token Refresh Bug - Race Condition in Middleware"
- ✓ "API v2 Migration - Backwards Compatibility Strategy"
- ✓ "Multi-Agent Architecture - Message Queue vs Event Bus"

### Length

Aim for **400-800 words** for substantial sessions. Short sessions (simple fixes, quick questions) can be 200-300 words. Complex sessions with multiple threads can be 1000+ words.

The measure isn't word count — it's "can future-Claude pick up where we left off?"

### Human-AI Interaction

This is especially important when the session involves:
- User changing their mind
- User expressing frustration or confusion
- User pushing back on AI suggestions
- User having different priorities than what AI suggested
- Extended back-and-forth on a decision

Capture the *deliberation*, not just the conclusion. Future-Claude benefits from seeing how decisions were actually made, not just what was decided.

## Quality Check

Before finalizing, ask:
1. If I loaded this cold in a new session, could I continue the work?
2. Does this capture what was *difficult* or *surprising*, not just what was accomplished?
3. Are the open threads specific enough to act on?
4. Does the title tell me what this was actually about?
5. Would the user recognize this as faithful to the session?

If any answer is no, revise.
