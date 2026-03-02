---
description: Search memos and transcripts in the memex vault using hybrid search
allowed-tools: Read, Bash
argument-hint: "<query> - search keywords (use OR between terms)"
---

# Search Command

Search the memex vault for memos and transcripts matching the query.

## Path Resolution (Required First)

**IMPORTANT:** `${CLAUDE_PLUGIN_ROOT}` points to the plugin cache, NOT the vault!

Resolve the vault path:
1. Read `~/.memex/config.json` → use `memex_path` if present
2. Fallback: use the plugin's own location (where `scripts/` lives)

Store this as `$VAULT` for use in paths below.

## Instructions

1. **Formulate a good query** - Extract keywords, don't use full questions:
   - Bad: "why did we choose JWT for authentication"
   - Good: "JWT OR authentication"

2. **Run the search** using hybrid search:
   ```bash
   cd $VAULT
   uv run scripts/search.py "<query>" --format=text
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
uv run scripts/search.py "JWT authentication" --mode=hybrid --format=text

# Keyword search (FTS5) - fast, exact token matching
uv run scripts/search.py "JWT OR authentication" --mode=fts --format=text

# Semantic search (vector) - conceptual matching
uv run scripts/search.py "why we chose this auth approach" --mode=vector --format=text
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
uv run scripts/search.py "oauth" --type=memo

# Filter by project
uv run scripts/search.py "oauth" --project=myapp

# Limit results
uv run scripts/search.py "oauth" --limit=5
```

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
- Use `--format=json` for programmatic parsing
