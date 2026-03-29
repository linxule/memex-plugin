---
description: Search memos and transcripts in the memex vault using hybrid search
allowed-tools: Read, Bash
argument-hint: "<query> - search keywords (use OR between terms)"
effort: low
---

# Search Command

Search the memex vault for memos and transcripts matching the query.

## Instructions

1. **Formulate a good query** - Extract keywords, don't use full questions:
   - Bad: "why did we choose JWT for authentication"
   - Good: "JWT OR authentication"

2. **Run the search** using hybrid search:
   ```bash
   memex search "<query>"
   ```

3. **Parse results** which include:
   - `path`: File path
   - `title`: Document title
   - `type`: memo, transcript, or concept
   - `project`: Project name
   - `snippet`: Matching text snippet
   - `score`: Relevance score (hybrid combines BM25 + vector)

4. **Present results** in a readable format with relevant snippets

## Search Modes

```bash
# Hybrid (default) - combines keyword + semantic for best results
memex search "JWT authentication"

# Keyword search (FTS5) - fast, exact token matching
memex search "JWT OR authentication" --mode=fts

# Semantic search (vector) - conceptual matching
memex search "why we chose this auth approach" --mode=vector
```

## Query Syntax

| Syntax | Meaning | Example |
|--------|---------|---------|
| `term1 OR term2` | Match either term | `auth OR authentication` |
| `"exact phrase"` | Exact phrase match | `"error handling"` |
| `term1 term2` | Match both (AND) | `JWT token` |

## Filters

```bash
# Filter by type
memex search "oauth" --type=memo

# Filter by project
memex search "oauth" --project=myapp

# Limit results
memex search "oauth" --limit=5
```

## Date Filters

```bash
# Recent docs only
memex search "oauth" --since=7d

# Natural-language dates
memex search "oauth" --since=yesterday

# Before a cutoff
memex search "oauth" --before="last week"

# Date range
memex search "oauth" --between "2026-03-01" "2026-03-15"
```

Supports: `yesterday`, `today`, `last week`, `this week`, `last monday`, `3 days ago`, `7d`, `2w`, `3m`, `march 15`, ISO dates.

## Output Format

```
Found 5 results for "authentication":

📝 Memos:
1. **OAuth Token Refresh Fix** (myproject, 2026-01-25)
   "...implemented retry logic for authentication failures..."

2. **API Design Decisions** (myproject, 2026-01-20)
   "...chose JWT for authentication because..."

📜 Transcripts:
3. Session 2026-01-25 (myproject)
   "...debugging the authentication flow..."
```

## Tips

- Use 2-5 keywords joined with OR for broad matching
- Try synonyms if no results (auth vs authentication)
- For recall questions, extract the topic words only
- Use `--json` for programmatic parsing
