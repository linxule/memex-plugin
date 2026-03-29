---
name: recall
effort: max
argument-hint: "[yesterday|today|last week|TOPIC|QUESTION]"
description: |
  Search memos and transcripts for prior context — temporal browsing, targeted keyword lookup, or deep synthesis redirect. Use when:
  - User asks "what did I do yesterday?", "show me last week", "today's sessions"
  - User asks "what was the decision...", "remind me...", "find the memo about..."
  - User references past work: "last time", "previously", "earlier we..."
  - User explicitly says "search for...", "recall...", "when did we..."

  For complex why/how questions that require synthesizing across many sessions or projects, prefer `ask-memex` instead.

  Do NOT trigger for:
  - Future-oriented questions ("how should we implement X?")
  - General knowledge ("what is a closure?")
  - Questions answerable from current session context
  - Vault health, graph structure, or task queries (use garden-tending)
  - Deep cross-project pattern questions (use ask-memex)

  <example>
  Context: User asks what they worked on recently
  User: "What did I do yesterday?"
  Assistant: Runs temporal scan, presents timeline of sessions and memos.
  <commentary>
  Date reference "yesterday" triggers TEMPORAL mode. No keywords needed.
  </commentary>
  </example>

  <example>
  Context: User asks about a past decision
  User: "Why did we choose JWT for authentication?"
  Assistant: Searches with expanded keyword variants, synthesizes answer.
  <commentary>
  "Why did we..." triggers KEYWORD mode. Expand: "JWT OR authentication", "OAuth OR token OR auth", "session OR credential"
  </commentary>
  </example>

  <example>
  Context: User asks about cross-project patterns
  User: "What patterns do we use for config management across projects?"
  Assistant: Redirects to ask-memex skill for deep synthesis.
  <commentary>
  "across projects" + "patterns" triggers DEEP mode → redirect to ask-memex.
  </commentary>
  </example>
allowed-tools: Read, Bash, Glob
---

# Recall: Retrieving Session Memory

## Context

**Project:** !`basename $(git remote get-url origin 2>/dev/null | sed 's/\.git$//' | xargs basename 2>/dev/null) 2>/dev/null || basename $(pwd)`
**Vault:** !`sqlite3 $(memex path 2>/dev/null)/_index.sqlite "SELECT COUNT(*) || ' documents indexed'" 2>/dev/null || echo "(index unavailable)"`

---

## Step 0: Classify the Query

Before doing anything, classify the user's question into one of three modes:

### TEMPORAL — date-based browsing
**Triggers:** "yesterday", "last week", "today", "what did I do on Monday", "show me recent work", "last 3 days", "this week's sessions", any date reference without topic keywords.

**Action:** Go to → [Temporal Recall](#temporal-recall)

### KEYWORD — topic/decision lookup
**Triggers:** "why did we...", "find the memo about...", "what was the decision on...", "remind me about the retry pattern", any question with specific technical terms or project names.

**Action:** Go to → [Keyword Recall](#keyword-recall)

### DEEP — cross-project synthesis
**Triggers:** "what patterns do we use across...", "how has our approach to X evolved...", "compare how we handle X in different projects", questions spanning multiple sessions or projects.

**Action:** Redirect to the **ask-memex** skill. Do not handle here.

**If mixed** (date + topic, e.g., "what auth work did I do last week"): Start with TEMPORAL to narrow the date range, then scan the results for the topic.

---

## Temporal Recall

Run the temporal scanner to browse sessions and memos by date:

```bash
memex timeline "<date-expression>"
```

With project filter:
```bash
memex timeline "<date-expression>" --project=<name>
```

Filter by type:
```bash
memex timeline "<date-expression>" --type=memo
```

### Supported date expressions
`yesterday`, `today`, `3 days ago`, `last 5 days`, `this week`, `last week`, `last monday`, `7d`, `2w`, `march 15`, `2026-03-15`

### After getting results
1. Present the timeline clearly — group by project if multiple projects
2. If the user wants details on a specific session, read the full memo or transcript
3. Go to → [One Thing Synthesis](#one-thing-synthesis)

---

## Keyword Recall

### Step 1: Generate 2-3 query variants

Before searching, proactively generate keyword variants to compensate for FTS's literal matching. This is not optional — always expand.

**Process:**
1. Extract exact technical terms from the question
2. Generate synonyms and alternative phrasings
3. Add related concepts that might appear in memos about this topic

**Example expansions:**

| User Question | Variant 1 (exact) | Variant 2 (synonyms) | Variant 3 (related) |
|---|---|---|---|
| "Why did we choose JWT?" | `JWT OR authentication` | `OAuth OR token OR auth` | `session OR credential OR stateless` |
| "Remind me about the retry pattern" | `retry OR pattern` | `backoff OR resilience` | `fault OR tolerance OR circuit` |
| "What was the API rate limiting decision?" | `rate OR limiting OR API` | `throttle OR quota` | `429 OR backpressure OR queue` |

### Step 2: Run searches in parallel

Run all variants simultaneously:

```bash
memex search "JWT OR authentication"
memex search "OAuth OR token OR auth"
```

Or use the slash command: `/memex:search "JWT OR authentication"`

### Search modes

- **Hybrid (default):** Best for most queries — combines keyword precision with semantic understanding
- **FTS (`--mode=fts`):** Fastest. Best for exact terms, names, acronyms, error codes
- **Vector (`--mode=vector`):** Best for conceptual questions when exact wording is unknown

### Step 3: Merge and deduplicate

If the same document appears in multiple searches, keep the highest-scoring instance. Present the top 5-8 unique results.

### Step 4: Present results

1. **Summarize relevance** — explain how results relate to the question
2. **Quote key snippets** — pull the most relevant sentences
3. **Acknowledge gaps** — if results don't fully answer, say so
4. **Offer to load more** — if a memo looks promising, offer to `/memex:load` the full content

### If searches return nothing

1. Try broader terms: `"JWT"` → `"auth OR token OR JWT"`
2. Try vector mode for conceptual matching: `--mode=vector`
3. Remove project filter if you added one
4. Try temporal scan to find sessions from the right time period
5. Check spelling of technical terms

### Go to → [One Thing Synthesis](#one-thing-synthesis)

---

## One Thing Synthesis

**Every recall ends with ONE specific next action.** Not "what would you like to do?" — a concrete recommendation.

After presenting results (temporal or keyword), synthesize the single highest-leverage next step based on:

1. **Momentum** — What's almost done? What was actively being worked on?
   → "Continue the auth middleware rewrite in alcor — the token refresh handler is the last piece"

2. **Blockers** — What's stuck or waiting on a decision?
   → "The rate limiter is blocked on the Redis config decision from last week — resolve that first"

3. **Recency** — What was just active and could benefit from a follow-up?
   → "You were working on the MCP server yesterday — pick up where you left off with tool registration"

**Rules:**
- Be specific: include the project name and concrete task
- Reference the evidence: "Based on the memo from March 15..."
- If nothing actionable emerges, say so honestly: "These sessions are complete — no open threads"
- If multiple threads are open, pick the one with the most momentum

---

## When NOT to Search

1. **Answer is already in context** — current conversation already contains the information
2. **Question is general knowledge** — "What is a closure?" (not project-specific)
3. **Question is purely future-oriented with no past context** — "What's the best way to do X?"
4. **User explicitly asks for fresh perspective** — "Without looking at past decisions..."
5. **Already searched this session** — avoid redundant searches for the same topic
