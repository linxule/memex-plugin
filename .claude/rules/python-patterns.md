---
paths:
  - "scripts/**/*.py"
  - "src/memex/**/*.py"
  - "hooks/**/*.py"
---

# Python Coding Patterns

## Gotchas

- **Regex in f-strings needs `{{` `}}`** - Curly braces must be doubled (e.g., `r'#{{2,}}'` not `r'#{2,}'`)
- **Regex on JSON: avoid `[^}]+` with repetition** - Patterns like `\{[^}]+\}` cause catastrophic backtracking on nested JSON. Use line-anchored matching (`\{[^\n]*\n`) instead
- **SQL in Python** - Always use parameterized queries, even for LIMIT clauses. Never f-string user input into SQL
- **Path validation** - Use `path.relative_to(base)` in try/except, not string startswith checks
- **sqlite3 connections** - Always wrap in try/finally in CLI entry points to prevent leaks on early exit
- **Testing inline scripts** - `uv run python3 -c "..."` doesn't pick up inline script deps; run through existing script or create wrapper with same deps
- **SQLite tables need UNIQUE constraints** - Tables like `tasks` need `UNIQUE(doc_path, line_number)` + `INSERT OR IGNORE` to prevent duplicates on re-index
- **Never build the index path by hand** - Use `memex.paths.get_index_path(vault)`, not `vault / "_index.sqlite"`. Since Aug 2026 the configured vault's index lives in `~/.memex/` (out of iCloud/Dropbox); only non-configured vaults (tests, `--vault`) keep it in-vault. `memex path --index` prints it for shell use
- **tiktoken lazy import** - `utils.py` imports tiktoken lazily. Callers that only need state management (e.g. `memex mark-saved`) work without tiktoken installed. (`scripts/mark_memo_saved.py` deleted v0.16.5 — the CLI is the sole mark-saved path)
- **Always use `uv run python`, never bare `python3`** — System Python is 3.9 (Xcode); memex requires >=3.11 via uv. Bare `python3` will fail on `X | None` union syntax
- **`bin/memex` uses PYTHONPATH for live source** — The shell wrapper sets `PYTHONPATH=src` so edits take effect immediately. Don't switch to `uv run --with .` — that caches the wheel and misses uncommitted changes
- **Always use `memex.db_utils.connect_index` for `_index.sqlite`** — bare `sqlite3.connect(index_path)` is a lurking "database is locked" under parallel `memex backfill obs --stdin`. The helper applies `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=10000`. Matching `load_vec_extension(conn)` for sqlite-vec — clears `enable_load_extension=True` in try/finally so a failed load doesn't leave the connection privileged. Read paths benefit too (WAL is free for readers); no reason to open the index any other way
- **`SAVEPOINT doc` pattern in rebuild loops** — For per-doc atomicity, wrap work in `conn.execute("SAVEPOINT doc")` → work → `conn.execute("RELEASE SAVEPOINT doc")`, and on exception call `_rollback_savepoint_or_die(conn, "doc")` + `_release_savepoint_if_exists(conn, "doc")`. Critical prerequisite: the functions doing the work (e.g., `index_document`, `embed_chunks`) must NOT call `conn.commit()` — a commit releases the savepoint at the SQL level and the RELEASE afterward raises `no such savepoint`. Commit belongs to the batch caller, not the per-doc helper
- **Gemini returns unit-norm vectors — enforced by `_assert_unit_norm`** — If you write a new embedding provider, call `_assert_unit_norm(batch_results, provider="your-name")` at the end of `embed_texts`. Samples head+mid+tail so drift is caught at write time. sqlite-vec is L2-distance by default; only monotonic with cosine when inputs are normalized
