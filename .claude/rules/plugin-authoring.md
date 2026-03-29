---
paths:
  - "commands/**/*.md"
  - "skills/**/*.md"
  - "hooks/**/*.py"
  - "scripts/**/*.py"
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
- **File existence before read** — Scripts accepting `--store-json` or similar file args should check existence before `read_text()`. Unhandled `FileNotFoundError` produces unhelpful tracebacks for agents

## Hooks

- **`sys.path.insert` is intentional** — Hook scripts run via `uv run --script` with PEP 723 inline deps. The `memex` package and `scripts.utils` aren't PyPI packages, so `sys.path.insert(0, parent.parent)` and `sys.path.insert(0, parent.parent / "src")` are the correct pattern. Don't add TODO comments to remove them
- **Hook timeouts must match workload** — SessionStart: 7s, UserPromptSubmit: 3s, PreCompact: 10s, SessionEnd: 30s. If a hook does more work, increase its timeout or it silently gets killed

## Plugin Cache

- **Version in `plugin.json` drives cache updates** — Bumping `pyproject.toml` version without bumping `plugin.json` version means the cache never invalidates. Always bump both together
- **Stale cache versions accumulate** — After bumping version, check `~/.claude/plugins/cache/memex-plugins/memex/` and remove old version directories
- **Plugin cache venv is separate** — The cache at `~/.claude/plugins/cache/` has its own venv. After vault venv fixes, also check/fix the cache venv: `cd ~/.claude/plugins/cache/memex-plugins/memex/<version>/ && uv run python -c "import memex; print('ok')"`
- **Open sessions keep stale config** — Reinstalling the plugin updates the cache but already-open sessions still use the old config. They must be restarted to pick up changes

## Cross-Cutting

- **skill vs command consistency** — If `save.md` adds a step (like observation extraction), the `memo-writing` skill should mention it too, or agents that invoke the skill directly will skip the step
- **recall vs ask-memex boundary** — `recall` = targeted keyword lookup. `ask-memex` = deep cross-session synthesis. Both trigger on "why did we..." phrasing. Keep their descriptions distinct with explicit tiebreakers
