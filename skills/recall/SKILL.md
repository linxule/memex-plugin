---
name: recall
description: |
  Search memos and transcripts for prior context. Use when:
  - User asks "why did we...", "what was the decision...", "remind me..."
  - User references past work: "last time", "previously", "earlier we..."
  - User asks about past patterns, solutions, or architectural decisions
  - User explicitly says "find the memo about...", "search for...", "recall..."
  - User asks "when did we...", "what was the approach..."

  Do NOT trigger for:
  - Future-oriented questions ("how should we implement X?")
  - General knowledge ("what is a closure?")
  - Questions answerable from current session context
  - Vault health, graph structure, or task queries (use garden-tending)

  <example>
  Context: User asks about a past decision
  User: "Why did we choose JWT for authentication?"
  Assistant: "Let me search our memos for that decision."
  <commentary>
  "Why did we..." triggers recall. Extract keywords: "JWT OR authentication"
  </commentary>
  </example>

  <example>
  Context: User wants to recall previous work
  User: "What did we discuss last session about error handling?"
  Assistant: "I'll search the memex for our error handling discussions."
  <commentary>
  "last session" and "what did we discuss" indicate recall need. Search: "error OR handling"
  </commentary>
  </example>

  <example>
  Context: User asks about current implementation
  User: "How should we implement caching?"
  Assistant: [Does NOT search - this is about future work, not past decisions]
  <commentary>
  "How should we" is future-oriented. No memo search needed unless user references past work.
  </commentary>
  </example>
allowed-tools: Read, Bash, Glob
---

# Recall: Retrieving Session Memory

Search the memex vault for prior context when users ask about past decisions, approaches, or learnings.

---

## When to Search

Search memos when the user's prompt suggests they want prior context:

### Decision Questions
- "Why did we choose X?"
- "What was the decision about Y?"
- "How did we solve Z before?"

### Recall Requests
- "Remind me about..."
- "What did we do about..."
- "Can you recall..."

### Continuity Questions
- "Last time we discussed..."
- "Previously we..."
- "Where were we with..."

### Technical Lookups
- Questions about architectural decisions
- Questions about patterns or solutions used before
- Questions referencing "we did" or "we decided"

---

## When NOT to Search

1. **Answer is already in context** — current conversation already contains the information
2. **Question is general knowledge** — "What is a closure?" (not project-specific)
3. **Question is purely future-oriented with no past context** — "What's the best way to do X?" (general approach, not referencing past work). But if implementing something the vault has worked on before, consider searching for past decisions
4. **User explicitly asks for fresh perspective** — "Without looking at past decisions..."
5. **Already searched this session** — avoid redundant searches for the same topic

---

## Query Formulation

**Do NOT search with the full question.** Extract keywords for effective FTS matching.

### Step 1: Identify content words
Remove question words (why, what, how, when) and common verbs (did, do, is, are, was, were).

### Step 2: Extract domain terms
Keep technical terms, project names, feature names, library names.

### Step 3: Format as FTS query
Join 2-5 keywords with OR for broad matching.

### Examples

| User Question | Bad Query | Good Query |
|---------------|-----------|------------|
| "Why did we choose JWT for authentication?" | why did we choose JWT for authentication | `JWT OR authentication` |
| "What was the decision about error handling?" | what was the decision about error handling | `error OR handling OR decision` |
| "Remind me about the retry pattern" | remind me about the retry pattern | `retry OR pattern` |
| "What's left to do on the API?" | what's left to do on the API | `API OR todo OR thread` |

### Stop Words to Remove
- Question words: why, what, how, when, where, who, which
- Common verbs: did, do, does, is, are, was, were, have, has, had, can, could, would, should
- Pronouns: we, you, i, they, he, she, it, me, us, them
- Prepositions: the, a, an, of, to, for, with, on, at, by, from, in, about
- Recall words: remind, remember, recall, previously, earlier, last, time, decide, chose

---

## Running the Search

```bash
# Hybrid search (default — combines BM25 keyword + vector semantic via RRF scoring)
uv run scripts/search.py "JWT OR authentication" --format=text

# Recent docs only
uv run scripts/search.py "retry pattern" --since=7d --format=text

# Filter by type or project
uv run scripts/search.py "architecture" --type=memo --format=text
uv run scripts/search.py "auth" --project=my-app --format=text

# Choose search mode explicitly
uv run scripts/search.py "auth" --mode=fts --format=text     # fastest, keyword-only
uv run scripts/search.py "auth" --mode=vector --format=text   # semantic-only
uv run scripts/search.py "auth" --mode=hybrid --format=text   # both (default)
```

Or use the slash command: `/memex:search "JWT OR authentication"`

**Note:** Default output is JSON. Use `--format=text` for human-readable results. Hybrid mode requires LM Studio or Gemini for embeddings; falls back to FTS-only if unavailable.

### When to Use Each Mode

- **Hybrid (default):** Best for most queries — combines keyword precision with semantic understanding
- **FTS (`--mode=fts`):** Fastest. Best for exact terms, names, acronyms, error codes
- **Vector (`--mode=vector`):** Best for conceptual questions when exact wording is unknown

### If Search Returns Nothing

1. Try broader terms: `"JWT"` → `"auth OR token OR JWT"`
2. Try vector mode for conceptual matching: `--mode=vector`
3. Remove project filter if you added one
4. Check spelling of technical terms

---

## Presenting Results

1. **Summarize relevance** - Explain how results relate to the question
2. **Quote key snippets** - Pull the most relevant sentences
3. **Acknowledge gaps** - If results don't fully answer, say so
4. **Offer to load more** - If a memo looks promising, offer to `/memex:load` the full content

### Example Response

```
I found relevant context from a previous session:

**OAuth Token Refresh Fix** (2026-01-25):
> "...chose JWT for authentication because it's stateless and works well
> with our microservices architecture. We considered session tokens but
> rejected them due to the distributed nature of our backend..."

This answers why JWT was chosen. Want me to load the full memo for more details?
```
