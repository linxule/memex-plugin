---
paths:
  - "scripts/obsidian_cli.py"
  - "scripts/graph_queries.py"
  - "scripts/crystallization_check.py"
---

# Obsidian CLI & Graph Navigation

Two tools for graph queries — prefer Obsidian CLI when Obsidian is running (faster, uses pre-built index with correct wikilink resolution), fall back to SQLite queries when it's not.

## Obsidian CLI (preferred, requires Obsidian 1.12.2+ running)

```bash
# Quick vault health check (uses native vault command)
uv run scripts/obsidian_cli.py status

# Backlinks (uses Obsidian's wikilink resolution — more accurate than SQLite)
uv run scripts/obsidian_cli.py backlinks claude-code-hooks

# Outgoing links from a file (new in 1.12.2)
uv run scripts/obsidian_cli.py links attractor-basins

# Orphans, dead-ends, unresolved links
uv run scripts/obsidian_cli.py orphans [--total]
uv run scripts/obsidian_cli.py deadends [--total]
uv run scripts/obsidian_cli.py unresolved [--total] [--verbose]

# Aliases (new in 1.12.2 — replaces eval hacks)
uv run scripts/obsidian_cli.py aliases --verbose              # All aliases with file paths
uv run scripts/obsidian_cli.py aliases --total                # Count only

# Validate wikilinks in a file (useful after condensation)
uv run scripts/obsidian_cli.py check-links --path="projects/memex/_project.md"
uv run scripts/obsidian_cli.py check-links attractor-basins

# Tags and properties
uv run scripts/obsidian_cli.py tags
uv run scripts/obsidian_cli.py properties
uv run scripts/obsidian_cli.py properties --path="projects/memex/_project.md" --format=json

# Word count (new in 1.12.2)
uv run scripts/obsidian_cli.py wordcount attractor-basins

# File info / listing (new in 1.12.2)
uv run scripts/obsidian_cli.py file-info attractor-basins
uv run scripts/obsidian_cli.py files --folder=topics --total
uv run scripts/obsidian_cli.py vault-info

# Tasks for a specific file
uv run scripts/obsidian_cli.py tasks --path="projects/memex/memos/some-memo.md"

# Execute JavaScript against Obsidian's API (escape hatch for anything)
uv run scripts/obsidian_cli.py eval "app.vault.getMarkdownFiles().length"
uv run scripts/obsidian_cli.py eval "JSON.stringify(app.metadataCache.getCache('topics/claude-code-hooks.md')?.frontmatter)"

# Base views (native Obsidian table views)
uv run scripts/obsidian_cli.py bases                          # List all .base files
uv run scripts/obsidian_cli.py base-query --path="_views/by-project.base" --format=md  # Query a base view
uv run scripts/obsidian_cli.py base-views --file="by-project"  # List views (requires file open in Obsidian)
```

## SQLite Fallback (always available, index may be stale)

```bash
uv run scripts/graph_queries.py stats
uv run scripts/graph_queries.py backlinks topics/claude-code-hooks.md
uv run scripts/graph_queries.py tasks --project=memex
uv run scripts/graph_queries.py broken
uv run scripts/graph_queries.py tags claude-code
uv run scripts/graph_queries.py recent --days=7
uv run scripts/graph_queries.py orphans
```

## When to use what

- "What links to X?" → `obsidian_cli.py backlinks` (wikilink-aware) or `graph_queries.py backlinks`
- "What links FROM X?" → `obsidian_cli.py links <file>` (native, 1.12.2+)
- "What's open/pending?" → `obsidian_cli.py tasks --path=<file>` or `graph_queries.py tasks`
- "Find content about X" → `search.py` (FTS + semantic) — Obsidian CLI search still empty in 1.12.2
- "File metadata/size?" → `obsidian_cli.py file-info <file>` or `wordcount <file>`
- "How many aliases?" → `obsidian_cli.py aliases --total`
- "Are my links valid?" → `obsidian_cli.py check-links <file>` (after condensation or topic creation)
- "Vault health" → `obsidian_cli.py status` for quick counts, `graph_queries.py stats` for detailed breakdown
- "Custom graph traversal" → `obsidian_cli.py eval` with `app.metadataCache.resolvedLinks`
- "Query a dashboard view" → `obsidian_cli.py base-query --path=<base-file>` for native Base views

## Gotchas

- **Obsidian CLI requires running app** - CLI connects to running Obsidian instance. If Obsidian isn't open, first CLI command launches it (slow). Use `obsidian_cli.py` wrapper which filters the loading-line noise
- **Obsidian CLI search still broken (1.12.2)** - `search` and `search:context` commands still return empty output. Use `search.py` for all text/semantic search. Native `aliases`, `links`, `properties format=json` work well for structured queries
- **Obsidian CLI output buffering** - Large listing commands (`tasks todo`, `search`, `recents`) return empty without `total`. Use `total` for counts, or file-specific queries for listings. Scalar commands (`aliases total`, `tasks todo total`) work fine
- **Obsidian CLI eval empty result** - When eval returns empty, Obsidian outputs `=>` (no trailing space). `eval_js()` checks for `=> ` (with space) as prefix — the `==` check for bare `=>` was added to handle this. Without it, `=>` leaks as return value and can appear as phantom wikilinks
- **Obsidian CLI eval injection** - Compound queries (resolved_backlinks, etc.) interpolate paths into JavaScript strings. Paths with single quotes are now escaped, but don't pass untrusted input to these methods
- **Obsidian CLI stderr ignored by default** - `_run_raw()` logs stderr on non-zero exit code but returns whatever stdout contains. Check `is_available()` first
- **Obsidian CLI early access instability** - CLI is marked "early access" — commands and syntax may change between versions. The `eval` escape hatch is the most stable interface
- **Obsidian CLI doesn't resolve aliases in `unresolved`** - `unresolved` command checks filenames only, not frontmatter `aliases`. Many "unresolved" links (e.g., `[[my-app]]` → `my-app-project.md` via alias) are actually fine in Obsidian. Use `aliases verbose` to get the full alias→file mapping for filtering, or `crystallization_check.py` which handles this automatically
- **Obsidian CLI `property:read` only works for scalars** - Reading list properties (`topics`, `aliases`) errors. Use `properties path=<file> format=json` instead to get the full frontmatter as structured JSON
- **Wikilink resolution mismatch** - Indexer uses strict path matching; Obsidian resolves `[[name]]` fuzzy. "Broken links" from `graph_queries.py` may work fine in Obsidian
- **Task filtering reduces noise** - Exclude transcripts, filter "Open Threads" section only, use 14-day window. See `graph_queries.py tasks --help`
- **Crystallization check requires Obsidian running** - `crystallization_check.py` exits with code 1 if Obsidian isn't open. Not suitable for launchd/cron automation. Keep as manual check during garden-tending sessions

## Version Dependencies: Obsidian CLI (tested: 1.12.2, early access)

**Known broken:** `search`/`search:context` (empty output), `tasks todo` vault-wide listing (empty, but `total` works), `recents` (empty), `history:list` (empty).

**Working (existing):** `backlinks`, `orphans`, `deadends`, `unresolved`, `tags`, `properties`, `outline`, `eval`, `read`, file-specific `tasks`.

**Working (new in 1.12.2):** `aliases` (with verbose/total), `links` (outgoing), `property:read`/`property:set` (scalar only), `wordcount`, `file` (info), `files` (listing), `vault` (info), `folders`, `rename`, `append`/`prepend`, `create` (with templates), `daily:*`, `bookmark`/`bookmarks`, `plugin:*`, `dev:*` (console, errors, screenshot, DOM).

**Parameter changes from 1.12.1:** `all` replaced by `active` for per-file targeting; `silent` replaced by `open`; commands default to silent operation (no active file required); `--help` alias added.

**Loading-line format:** `YYYY-MM-DD HH:MM:SS Loading updated app package` — unchanged from 1.12.1, filtered by `obsidian_cli.py`.

**What we migrated off eval:**
- `frontmatter()` → `properties path=<file> format=json` (with eval fallback)
- `resolved_outlinks()` → `links path=<file>` (with eval fallback)
- `get_alias_map()` in crystallization_check → `aliases verbose` (with eval fallback)
- `vault_file_count()` → `files ext=md total` (with eval fallback)

**New CLI commands added (not eval migrations):**
- `check-links` — validates all wikilinks in a file, reports unresolved ones
- `properties --path=<file> --format=json` — CLI args now exposed (were Python-only before)

**When to re-test (after any Obsidian update):**
1. `uv run scripts/obsidian_cli.py status` — verify connectivity
2. Test `search query="test"` — check if search finally works
3. Test `tasks todo` — check if vault-wide listing works
4. Test `recents` — check if recently opened files works
5. Check if loading-line format changed
