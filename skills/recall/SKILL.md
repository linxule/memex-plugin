---
name: recall
effort: max
argument-hint: "[yesterday|today|last week|TOPIC|QUESTION]"
description: |
  Retrieve session memory — temporal browsing, keyword search, deep cross-session synthesis, or direct file loading. Use when:
  - User asks "what did I do yesterday?", "show me last week", "today's sessions"
  - User asks "what was the decision...", "remind me...", "find the memo about..."
  - User references past work: "last time", "previously", "earlier we..."
  - User explicitly says "search for...", "recall...", "when did we..."
  - User asks cross-project synthesis: "what patterns across...", "how has X evolved..."
  - User asks to load specific content: "load the X topic", "pull up project Y"
  - Working in any project and encountering a problem that may have been discussed in prior sessions

  Do NOT trigger for:
  - Future-oriented questions ("how should we implement X?")
  - General knowledge ("what is a closure?")
  - Questions answerable from current session context
  - Vault health, graph structure, or maintenance tasks (use garden-tending)

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
  "Why did we..." triggers KEYWORD mode. Expand: "JWT OR authentication", "OAuth OR token OR auth"
  </commentary>
  </example>

  <example>
  Context: User asks about cross-project patterns
  User: "What patterns do we use for config management across projects?"
  Assistant: Checks compiled topics, runs deep retrieval, synthesizes across projects.
  <commentary>
  "across projects" + "patterns" triggers DEEP mode — cross-session synthesis.
  </commentary>
  </example>

  <example>
  Context: User asks to load a specific topic
  User: "Load the trust calibration topic"
  Assistant: Reads the topic file, presents content with related memos.
  <commentary>
  Explicit "load" request triggers LOAD mode — direct file retrieval.
  </commentary>
  </example>
allowed-tools: Read, Write, Bash, Glob
---

# Recall: Retrieving Session Memory

## Context

**Project:** !`basename $(git remote get-url origin 2>/dev/null | sed 's/\.git$//' | xargs basename 2>/dev/null) 2>/dev/null || basename $(pwd)`
**Vault:** !`sqlite3 $(memex path 2>/dev/null)/_index.sqlite "SELECT (SELECT COUNT(*) FROM fts_content) || ' documents indexed'" 2>/dev/null || echo "(index unavailable)"`

---

## Step 0: Classify the Query

Before doing anything, classify the user's question into one of four modes:

### TEMPORAL — date-based browsing
**Triggers:** "yesterday", "last week", "today", "what did I do on Monday", "show me recent work", "last 3 days", "this week's sessions", any date reference without topic keywords.

**Action:** Go to → [Temporal Recall](#temporal-recall)

### KEYWORD — topic/decision lookup
**Triggers:** "why did we...", "find the memo about...", "what was the decision on...", "remind me about the retry pattern", any question with specific technical terms or project names.

**Action:** Go to → [Keyword Recall](#keyword-recall)

### DEEP — cross-project synthesis
**Triggers:** "what patterns do we use across...", "how has our approach to X evolved...", "compare how we handle X in different projects", questions spanning multiple sessions or projects, complex why/how questions needing synthesis.

**Action:** Go to → [Deep Recall](#deep-recall)

### LOAD — direct file retrieval
**Triggers:** "load the X topic", "pull up project Y", "show me the memo about Z", explicit topic or memo names, "what does the X topic say".

**Action:** Go to → [Load Recall](#load-recall)

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

### Search reference

**Modes:**
- **Hybrid (default):** Best for most queries — combines keyword + semantic
- **FTS (`--mode=fts`):** Fastest. Best for exact terms, names, acronyms, error codes
- **Vector (`--mode=vector`):** Best for conceptual questions when exact wording is unknown

**Filters:**
```bash
memex search "query" --type=memo          # Filter by type
memex search "query" --project=myapp      # Filter by project
memex search "query" --limit=5            # Limit results
memex search "query" --scope=observations # Search extracted learnings/decisions
memex search "query" --since=7d           # Recent only
memex search "query" --before="last week" # Before cutoff
memex search "query" --between "2026-03-01" "2026-03-15"  # Date range
```

**Query syntax:** `term1 OR term2` (either), `"exact phrase"` (literal), `term1 term2` (both/AND)

### Step 3: Merge and deduplicate

If the same document appears in multiple searches, keep the highest-scoring instance. Present the top 5-8 unique results.

### Step 4: Check for trails

If the question is about how thinking on a topic evolved, check whether a trail exists:

```bash
grep -rl '^type: trail' $(memex path 2>/dev/null)/topics/ 2>/dev/null | xargs -I{} basename {} .md
```

If a relevant trail exists, read it directly — it captures the narrative arc better than memo search results. Present its phase structure as the answer.

### Step 5: Present results

1. **Summarize relevance** — explain how results relate to the question
2. **Quote key snippets** — pull the most relevant sentences
3. **Acknowledge gaps** — if results don't fully answer, say so
4. **Offer to read more** — if a memo looks promising, offer to read the full content

### If searches return nothing

1. Try broader terms: `"JWT"` → `"auth OR token OR JWT"`
2. Try vector mode for conceptual matching: `--mode=vector`
3. Remove project filter if you added one
4. Try temporal scan to find sessions from the right time period
5. Check spelling of technical terms

### Go to → [One Thing Synthesis](#one-thing-synthesis)

---

## Deep Recall

For complex questions that require synthesizing across many sessions or projects.

### Step 1: Check for compiled knowledge first

Before searching, check if a relevant topic already exists. Compiled topics are higher quality than search-assembled answers.

```bash
CONCEPT="trust-calibration"  # set to the main subject of the question
ls "$(memex path)/topics/" | grep -i "$CONCEPT" 2>/dev/null
```

If `ls` finds nothing, try a type-filtered search:
```bash
memex search "$CONCEPT" --type=concept --limit=3 2>/dev/null
```

If a matching topic exists, use it as a **prior** — not a gate:
1. Read it — check the frontmatter for `updated:` or `last_extended:` date
2. If fresh (updated within 60 days), non-stub (50+ lines), and covers the question → **use the topic as the primary answer**, supplemented by search only if gaps remain. If older than 30 days, mention: *"Based on [[topic]] (last updated DATE). This may not reflect the most recent work."*
3. **Always fall through to search** when: the topic is a stub (< 50 lines), freshness metadata is missing, multiple topics partially match, or the question spans concepts beyond what one topic covers.

### Step 2: Deep retrieval via search

```bash
memex ask "<question>"
```

For thorough (semantic + keyword) retrieval:
```bash
memex ask "<question>" --depth=thorough
```

Also run an **observation-specific search** to surface atomic claims:
```bash
memex search "<keywords>" --scope=observations --limit=10
```

Observations are structured claims extracted from memos — they provide precision that document-level search misses.

### Step 3: Synthesize and offer to save

After receiving results:
1. Read the `content` field of each result directly.
2. Check `observations` for atomic facts that answer the question.
3. Synthesize across results for agreements, contradictions, and evolution over time.
4. Cite source memo paths when making claims.
5. Note anything missing from `query_info.gaps`.
6. **Offer to save valuable syntheses.** If the synthesis spans 3+ projects or 5+ memos and produces a cross-cutting insight, offer: "This synthesis touches N projects — save as a trail or topic note?" Save trails to `topics/trail-<concept>.md` with `type: trail`. Save encyclopedic syntheses to `topics/<concept>.md` with `type: concept`.
7. **Flag stale topics.** If you used a topic but found it outdated by search results, note: "The [[topic]] page may need refreshing — recent memos contain newer information."

### Go to → [One Thing Synthesis](#one-thing-synthesis)

---

## Load Recall

Direct retrieval of a specific topic, memo, or project context.

### Step 1: Parse the argument

Determine what to load:
- **Topic name** → look in `topics/<name>.md`
- **Project name** → look in `projects/<project>/_project.md` + recent memos
- **Memo reference** → search `projects/*/memos/` for matching filename

### Step 2: Find and read

```bash
VAULT=$(memex path)

# For topics (fuzzy match)
ls "$VAULT/topics/" | grep -i "<query>" 2>/dev/null

# For projects
ls "$VAULT/projects/" | grep -i "<query>" 2>/dev/null

# For memos (search by filename)
find "$VAULT/projects" -path "*/memos/*" -name "*<query>*" 2>/dev/null | head -5
```

Read the matched file(s) with the Read tool.

### Step 3: Present loaded content

1. Show the full content for single files
2. For projects: show `_project.md` overview + list of recent memos
3. Mention related backlinks if visible in the content
4. Offer to load related items

### Go to → [One Thing Synthesis](#one-thing-synthesis)

---

## One Thing Synthesis

**Every recall ends with ONE specific next action.** Not "what would you like to do?" — a concrete recommendation.

After presenting results (any mode), synthesize the single highest-leverage next step based on:

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
