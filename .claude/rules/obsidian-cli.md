---
paths:
  - "scripts/obsidian_cli.py"
  - "scripts/graph_queries.py"
  - "scripts/crystallization_check.py"
  - "src/memex/scripts/graph_queries.py"
  - "src/memex/scripts/crystallization_check.py"
---

# Obsidian CLI & Graph Navigation

Two tools for graph queries — prefer Obsidian CLI when Obsidian is running (faster, uses pre-built index with correct wikilink resolution), fall back to SQLite queries when it's not.

## Obsidian CLI (preferred, requires Obsidian running — tested 1.13.1, installer 1.12.4)

```bash
# Quick vault health check (uses native vault command)
uv run scripts/obsidian_cli.py status

# Backlinks (uses Obsidian's wikilink resolution — more accurate than SQLite)
uv run scripts/obsidian_cli.py backlinks claude-code-hooks

# Outgoing links from a file
uv run scripts/obsidian_cli.py links attractor-basins

# Heading outline for a file (tree | md | json)
uv run scripts/obsidian_cli.py outline --path="topics/attractor-basins.md" --format=md

# Orphans, dead-ends, unresolved links
uv run scripts/obsidian_cli.py orphans [--total]
uv run scripts/obsidian_cli.py deadends [--total]
uv run scripts/obsidian_cli.py unresolved [--total] [--verbose]
uv run scripts/obsidian_cli.py unresolved verbose format=json    # structured JSON array

# Aliases
uv run scripts/obsidian_cli.py aliases --verbose              # All aliases with file paths
uv run scripts/obsidian_cli.py aliases --total                # Count only

# Validate wikilinks in a file (useful after condensation)
uv run scripts/obsidian_cli.py check-links --path="projects/memex/_project.md"
uv run scripts/obsidian_cli.py check-links attractor-basins

# Tags and properties
uv run scripts/obsidian_cli.py tags
uv run scripts/obsidian_cli.py tag add tagname --path="topics/foo.md"
uv run scripts/obsidian_cli.py properties
uv run scripts/obsidian_cli.py properties --path="projects/memex/_project.md" --format=json
uv run scripts/obsidian_cli.py property:remove propname --path="topics/foo.md"

# Word count
uv run scripts/obsidian_cli.py wordcount attractor-basins

# File info / listing / folders
uv run scripts/obsidian_cli.py file-info attractor-basins
uv run scripts/obsidian_cli.py files --folder=topics --total
uv run scripts/obsidian_cli.py vault-info
uv run scripts/obsidian_cli.py folders

# File operations (file-ops generation — move/rename auto-update all backlinks)
uv run scripts/obsidian_cli.py create --path="topics/new-topic.md"
uv run scripts/obsidian_cli.py create --path="topics/new-topic.md" --template="concept"
uv run scripts/obsidian_cli.py append --path="topics/foo.md" --content="New section"
uv run scripts/obsidian_cli.py prepend --path="topics/foo.md" --content="Top note"
uv run scripts/obsidian_cli.py move --path="topics/old.md" --dest="archive/old.md"
uv run scripts/obsidian_cli.py rename --path="topics/old-name.md" --name="new-name"
uv run scripts/obsidian_cli.py delete --path="topics/obsolete.md"

# Tasks
uv run scripts/obsidian_cli.py tasks --path="projects/memex/memos/some-memo.md"
uv run scripts/obsidian_cli.py task toggle --path="file.md" --line=42
uv run scripts/obsidian_cli.py task done --path="file.md" --line=42

# Templates
uv run scripts/obsidian_cli.py templates                       # List available templates
uv run scripts/obsidian_cli.py template:read "concept"         # Read template content

# History & recents
uv run scripts/obsidian_cli.py history:list --path="topics/foo.md"
uv run scripts/obsidian_cli.py history:read --path="topics/foo.md" --version=1
uv run scripts/obsidian_cli.py recents

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
- "What links FROM X?" → `obsidian_cli.py links <file>` (native)
- "Heading structure of a file?" → `obsidian_cli.py outline --path=<file> --format=md` (useful before condensing)
- "What's open/pending?" → `obsidian_cli.py tasks --path=<file>` or `graph_queries.py tasks`
- "Find content about X" → `search.py` (FTS + semantic) — Obsidian CLI search still empty in 1.13.1
- "File metadata/size?" → `obsidian_cli.py file-info <file>` or `wordcount <file>`
- "How many aliases?" → `obsidian_cli.py aliases --total`
- "Are my links valid?" → `obsidian_cli.py check-links <file>` (after condensation or topic creation)
- "Vault health" → `obsidian_cli.py status` for quick counts, `graph_queries.py stats` for detailed breakdown
- "Custom graph traversal" → `obsidian_cli.py eval` with `app.metadataCache.resolvedLinks`
- "Query a dashboard view" → `obsidian_cli.py base-query --path=<base-file>` for native Base views
- "Refactor file location" → `obsidian_cli.py move`/`rename` — auto-updates all backlinks, safe for link refactoring
- "Recently opened files" → `obsidian_cli.py recents`
- "File version history" → `obsidian_cli.py history:list` then `history:read`
- "Structured unresolved data" → `obsidian_cli.py unresolved verbose format=json`

## Gotchas

- **Obsidian CLI requires running app** - CLI connects to a running Obsidian instance. **As of v0.15.3, `is_available()` is gated behind `_obsidian_running()` (a `pgrep -f Obsidian.app` check), so a passive availability probe NEVER launches the app.** Previously, invoking the binary merely to probe it would *open* Obsidian on the last-used vault — the cause of spurious wrong-vault launches during headless `memex check`. Direct CLI commands and `ensure_running()` still launch it intentionally; on any pgrep error (missing binary, non-mac) the guard returns False so callers fall back rather than launch. Use the `obsidian_cli.py` wrapper which filters the loading-line noise
- **Obsidian CLI search still broken (1.13.1)** - `search` and `search:context` commands still return empty output (re-confirmed at 1.13.1: an async-IPC race — the CLI process exits before Obsidian flushes async search results, so ~1 in 12 calls returns and the rest are empty). Use `search.py` for all text/semantic search. Native `aliases`, `links`, `properties format=json` work well for structured queries
- **Obsidian CLI output buffering** - Large listing commands (`tasks todo`, `search`) return empty without `total`. Use `total` for counts, or file-specific queries for listings. Scalar commands (`aliases total`, `tasks todo total`) work fine
- **Obsidian CLI eval empty result** - When eval returns empty, Obsidian outputs `=>` (no trailing space). `eval_js()` checks for `=> ` (with space) as prefix — the `==` check for bare `=>` was added to handle this. Without it, `=>` leaks as return value and can appear as phantom wikilinks
- **Obsidian CLI eval injection** - Compound queries (resolved_backlinks, etc.) interpolate paths into JavaScript strings. Paths with single quotes are now escaped, but don't pass untrusted input to these methods
- **Obsidian CLI stderr ignored by default** - `_run_raw()` logs stderr on non-zero exit code but returns whatever stdout contains. Check `is_available()` first
- **Obsidian CLI early access instability** - CLI is marked "early access" — commands and syntax may change between versions. The `eval` escape hatch is the most stable interface
- **Obsidian CLI doesn't resolve aliases in `unresolved`** - `unresolved` command checks filenames only, not frontmatter `aliases`. Many "unresolved" links (e.g., `[[alcor]]` → `alcor-project.md` via alias) are actually fine in Obsidian. Use `aliases verbose` to get the full alias→file mapping for filtering, or `crystallization_check.py` which handles this automatically
- **Obsidian CLI `property:read` only works for scalars** - Reading list properties (`topics`, `aliases`) errors. Use `properties path=<file> format=json` instead to get the full frontmatter as structured JSON
- **move/rename auto-update backlinks** - These commands update all references across the vault. Use them instead of manual file moves for link-safe refactoring
- **Wikilink resolution mismatch** - Indexer uses strict path matching; Obsidian resolves `[[name]]` fuzzy. "Broken links" from `graph_queries.py` may work fine in Obsidian
- **Task filtering reduces noise** - 441 raw → 171 actionable with: exclude transcripts, "Open Threads" section only, 14-day window. See `graph_queries.py tasks --help`
- **Crystallization check has a headless fallback (v0.14.0+)** - when Obsidian isn't running, `crystallization_check.py` degrades to a filesystem markdown scan (`scan_unresolved_via_markdown`: resolves `[[links]]` against filename stems + frontmatter aliases) instead of exiting 1 — so `memex check` is usable in launchd/cron. The fallback strips fenced/inline code + ANSI escapes before scanning (v0.14.1, so TOML `[[section]]` / bash `if [[ ]]` / terminal dumps don't register as ghost nodes) and skips `status: archived` files as link *sources* (v0.14.2, so dead/duplicate notes don't cast votes), **and excludes `projects/*/transcripts/` + `projects/*/auto-memory/` as link *sources* (v0.15.3, via `_is_noncurated_source()` — raw conversation/terminal dumps emit wikilink-shaped phantoms like `[[$MEMO_PATH]]`/`[[%s]]`/`[[:space:]]` that survive code-span stripping through fenced-block edge cases; applied symmetrically to the markdown fallback AND the Obsidian-native path via `_filter_noncurated_sources()`)**, while keeping them as valid *targets*. The Obsidian-running path is still more precise (heading/block awareness, space/underscore normalization); the fallback only over-reports, never under-reports — a safe degraded mode
- **Indexer ↔ checker share one wikilink filter (v0.15.4)** - `extract_wikilinks` (the indexer that populates the `wikilinks` graph table) now routes through the same `memex.scripts.wikilink_filters` module as the crystallization checker: it skips `projects/*/transcripts/` + `projects/*/auto-memory/` as link *sources* and strips code spans before scanning, so `graph stats` total/broken-link counts no longer over-count transcript phantoms (~61% of previously-reported broken links came from transcripts). The shared `strip_code_spans` is newline-preserving so the indexer's per-link `line_number` stays accurate. Fully materializes after a `--full` rebuild; the transcript/auto-memory `wikilinks` rows were also purged via a one-time cleanup on ship

## Version Dependencies: Obsidian CLI (tested: 1.13.1, installer 1.12.4, early access)

> **Dual-version note:** Obsidian reports two versions — the *runtime* (auto-updates; `obsidian version` → `1.13.1`) and the *installer* (the Electron shell; `Info.plist` / `app.getVersion()` → `1.12.4`). The CLI is bundled with the desktop app — there is no independent CLI tool version. Quote both as `1.13.1 (installer 1.12.4)`.
>
> **Fan-out hazard (observed 2026-06-16):** a rapid burst of CLI calls (especially the async `search`/`eval` probes) can wedge the live renderer at ~99–140% CPU; afterward *all* commands return empty/`?` and the instance needs a manual restart. The wrapper has no rate-limiting and `is_available()` can still report true while the instance is wedged. Serialize calls when fanning out (e.g. garden-tending), and don't hammer `eval`/`search` in a tight loop.

**Known broken:** `search`/`search:context` (empty output), `tasks todo` vault-wide listing (empty, but `total` works).

**Working (existing):** `backlinks`, `orphans`, `deadends`, `unresolved`, `tags`, `properties`, `outline`, `eval`, `read`, file-specific `tasks`.

**Working (file-ops generation, confirmed at 1.13.1):** `create`, `append`, `prepend`, `move`, `rename`, `delete`, `property:remove`, `folders`, `tag`, `task` (toggle/done), `templates`, `template:read`, `history:list`, `history:read`, `recents`, `aliases` (with verbose/total), `links` (outgoing), `property:read`/`property:set` (scalar only), `wordcount`, `file` (info), `files` (listing), `vault` (info), `daily:*`, `bookmark`/`bookmarks`, `plugin:*`, `dev:*` (console, errors, screenshot, DOM). Native help lists 103 top-level commands; the wrapper exposes ~38. Unwrapped-but-useful: `outline` (now wrapped — heading structure), `backlinks format=json`, `commands`/`command` (run any Obsidian command), `diff` (version diffing).

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
4. Check if loading-line format changed
