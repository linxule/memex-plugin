---
name: memo-writing
description: How to write effective session memos — format, frontmatter schema, observation extraction, topic-signal append. Trigger on `/memex:save`, "save this for later", "remember this", "save what we discussed", "document this session", "create a memo", or when the `[memex]` activity nudge appears in context. Do NOT trigger for general note-taking, scratchpads, technical specs, design docs, READMEs, or code comments — memos are session-reflection artifacts that capture what happened (decisions, tensions, surprises, open threads), not project documentation.
allowed-tools: Write, Read, Bash, Grep, Glob
---

# Writing Effective Session Memos

## Context

**Date:** !`date +%Y-%m-%d`
**Project:** !`basename $(git remote get-url origin 2>/dev/null | sed 's/\.git$//' | xargs basename 2>/dev/null) 2>/dev/null || basename $(pwd)`
**Topics available for [[wikilinks]]:** !`ls $(memex path 2>/dev/null)/topics/*.md 2>/dev/null | xargs -I{} basename {} .md | sort | tr '\n' ' ' || echo "(no topics found)"`

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
project: [project-name]   # detected from cwd git root; set explicitly if writing from outside the project
date: [ISO date]
topics:
  - topic-name-kebab-case
  - another-topic
manual: true              # distinguishes manually-written memos from auto-extracted ones
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

Always search the vault before writing to find related memos.
Run from the vault directory:
```bash
memex search "<keywords>" --limit=5
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

## Callouts

Use sparingly in memos — the memo format already structures information well. Reserve for critical gotchas or warnings that need to visually stand out:

```markdown
> [!warning] Token refresh silently fails under load
> The JWT refresh endpoint returns 200 with an expired token when Redis is saturated.

> [!tip] Use `--incremental` not `--full`
> Full rebuild re-embeds everything (~$2). Incremental only processes new/changed docs.
```

Common types: `warning` (gotchas), `tip` (best practices), `bug` (known issues), `question` (unresolved). Full reference in `skills/garden-tending/references/obsidian-formatting.md`.

---

## After Saving

After writing the memo, complete these steps (whether saving via `/memex:save` or directly from this skill):
1. **Mark saved** — run `memex mark-saved` to prevent duplicate generation
2. **Scrub for secrets** — run `memex scrub "<absolute-or-relative-memo-path>" --apply` to redact API keys, tokens, or PEM private-key blocks that slipped into the memo. Pattern-matching backstop, not magic: catches Anthropic/OpenAI/GitHub/Google/AWS/Slack/JWT shapes but misses line-wrapped keys, base64-wrapped JSON payloads, and custom-format low-entropy tokens. The primary defense is the PostToolUse hook (`hooks/post-tool-use.py`) that auto-scrubs every Write to `projects/*/memos/**`; this step is the belt-and-suspenders for when you save via some other path. Idempotent — re-running on already-scrubbed text is a no-op.
3. **Extract observations** — generate 5-15 atomic facts as JSON (each with `topics` field listing 0-3 relevant topic slugs) and pipe to `memex backfill obs --stdin --doc-path "<relative-path>"`. Run scrub BEFORE this step so secrets don't propagate into the observations table + search index.
4. **Signal touched topics** — for each existing topic wikilinked in the memo, append a dated one-liner to its `## Recent signals` section. Resolve `redirect_to:` in frontmatter first so archived topics route to their canonical replacement, then **dedup the resolved targets and skip the append if the exact signal line already exists in the topic file** — frontmatter often lists multiple slugs that redirect to the same canonical, and naïve appends create duplicates (see save command for the bash loop with `resolve_topic` + `grep -Fxq --` idempotency guard — the `--` is required so `$SIGNAL_LINE`'s leading `- ` is parsed as a pattern, not flags)

---

## Quality Check

Before finalizing, ask:
1. If I loaded this cold in a new session, could I continue the work?
2. Does "For Future Context" give the single most important thing?
3. Does it capture what was *difficult* or *surprising*, not just accomplished?
4. Are open threads specific enough to act on?
5. Would the user recognize this as faithful to the session?
6. Do wikilinks point to things that actually exist?
