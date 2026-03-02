---
paths:
  - "scripts/**/*.py"
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
- **CLI scripts need absolute db path** - Use `Path(__file__).parent.parent / "_index.sqlite"` not relative `Path('_index.sqlite')` for portability
- **tiktoken lazy import** - `utils.py` imports tiktoken lazily. Scripts that only need state management (mark_memo_saved.py) work without tiktoken installed
