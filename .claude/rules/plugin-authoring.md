---
paths:
  - "commands/**/*.md"
  - "skills/**/*.md"
  - "hooks/**/*.py"
  - "scripts/**/*.py"
  - "src/memex/**/*.py"
  - ".claude-plugin/**"
---

# Plugin Authoring — Error Patterns to Watch For

Common mistakes found during audits. Check these before committing changes to commands, skills, hooks, or scripts.

## Command & Skill Files

- **Undefined shell variables in bash blocks** — If a bash snippet uses `$VAR`, it must be defined in that snippet or a preceding one. Prose instructions ("set $VAR to X") don't create shell variables. Either hardcode the value or add `VAR=value` as the first line of the block
- **`memex` CLI eliminates `cd` for most commands** — `memex search`, `memex timeline`, `memex index`, etc. resolve the vault path automatically. Only `uv run scripts/obsidian_cli.py` and `uv run python -m memex.dreamer` still require `cd $(memex path) &&` prepended
- **Subagent prompts inherit nothing** — Subagents spawned via the Agent tool have no shell state, no env vars, no prior context. Prompts must include absolute paths, full commands, and self-contained instructions. Never reference `$VAULT` or assume prior `cd`
- **YAML frontmatter delimiter** — Skills need `---` on both the first and last line of the YAML block. Missing the closing `---` traps the instruction body inside the frontmatter (silently — no error, just empty skill)
- **Placeholder values in code blocks** — `LIMIT_VALUE`, `DOC_PATH`, `<query>` in bash snippets are easy for agents to miss. Add inline comments on the same line: `LIMIT 25  # replace with --limit arg`

## Python Scripts

- **FTS5 query injection** — Raw user input with hyphens (`predictive-ai`), colons, or quotes breaks FTS5 MATCH. Always use `sanitize_fts_query()` or `extract_fts_keywords()` before passing to MATCH. The error fallback should catch ALL `sqlite3.OperationalError`, not just specific strings
- **Missing imports after venv rebuild** — `rm -rf .venv && uv sync` can leave editable packages broken (metadata present, source missing). Always verify after rebuild: `uv run python -c "from memex.extract import main; print('ok')"`
- **Prefer `--stdin` over temp files** — Commands like `memex backfill obs` support `--stdin` to receive JSON via pipe. This avoids sandbox permission errors when running from other projects (sandbox blocks writes to `/tmp/`). `--store-json` still works but only within the project directory
- **File existence before read** — Scripts accepting `--store-json` or similar file args should check existence before `read_text()`. Unhandled `FileNotFoundError` produces unhelpful tracebacks for agents

## Hooks

- **`sys.path.insert` is intentional** — Hook scripts run via `uv run --script` with PEP 723 inline deps. The `memex` package and `scripts.utils` aren't PyPI packages, so `sys.path.insert(0, parent.parent)` and `sys.path.insert(0, parent.parent / "src")` are the correct pattern. Don't add TODO comments to remove them
- **Hook timeouts must match workload** — SessionStart: 7s, UserPromptSubmit: 3s, PreCompact: 10s, SessionEnd: 30s. If a hook does more work, increase its timeout or it silently gets killed

## Plugin Manifest (`plugin.json`)

- **Don't declare `"hooks"` as a directory path** — Unlike `"commands"` and `"skills"`, `hooks` does NOT accept a directory path string. The plugin loader auto-discovers `hooks/hooks.json` from the hooks directory. Adding `"hooks": "./hooks/"` causes `Validation errors: hooks: Invalid input` and prevents the plugin from loading

## Plugin Cache

- **Version must be bumped in FOUR files together** — `pyproject.toml`, `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, AND `src/memex/__init__.py` (which `scripts/mcp_server.py` imports as `__version__`). Missing any one causes drift — plugin cache won't invalidate (if plugin.json is stale) or runtime version reports wrong number (if `__init__.py` is stale). Consider wiring `__init__.py` to read from `importlib.metadata.version("memex")` to eliminate the drift surface altogether.
- **Stale cache versions accumulate** — After bumping version, check `~/.claude/plugins/cache/memex-local/memex/` and remove old version directories
- **Plugin cache venv is separate** — The cache at `~/.claude/plugins/cache/` has its own venv. After vault venv fixes, also check/fix the cache venv: `cd ~/.claude/plugins/cache/memex-local/memex/<version>/ && uv run python -c "import memex; print('ok')"`
- **Open sessions keep stale config** — Reinstalling the plugin updates the cache but already-open sessions still use the old config. They must be restarted to pick up changes
- **Two-layer distribution** — `uv tool install .` gives the global `memex` CLI (any agent). `claude plugin install` adds hooks/skills (Claude Code only). Scripts live in `src/memex/scripts/`, originals in `scripts/` are backward-compat shims. `package = true` in `pyproject.toml` enables both paths

## Cross-Cutting

- **skill vs command consistency** — If `save.md` adds a step (like observation extraction), the `memo-writing` skill should mention it too, or agents that invoke the skill directly will skip the step
- **recall's 4-mode routing** — `recall` handles all retrieval via internal routing: TEMPORAL (date-based browsing), KEYWORD (targeted FTS lookup), DEEP (cross-session synthesis, formerly ask-memex), and LOAD (pull specific topic/memo into context). The ask-memex skill no longer exists — its deep synthesis capability was absorbed into recall's DEEP mode. When "why did we..." triggers, recall routes to DEEP automatically
- **curator-practice vs garden-tending boundary** — `curator-practice` = autonomous operating philosophy (WHEN to act, judgment heuristics). `garden-tending` = procedural reference (HOW to do each operation). Curator-practice loads garden-tending as needed. Both trigger on "tend the garden" — tiebreaker: if the user says "use your judgment" or the agent is running autonomously → curator-practice. If the user gives specific instructions → garden-tending
