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

## Public-Repo Sync (memex → memex-plugin on GitHub)

- **New module in `src/memex/scripts/` needs a shim.** Every canonical script module has a matching top-level `scripts/<name>.py` shim (3 lines: sys.path insert + delegate to `memex.scripts.<name>.main`). Easy to miss on additions. Kimi-review flagged two missing shims on the v0.11.1 catch-up sync (2026-04-21) — `backfill_topic_tags.py` and `similarity_detection.py` — that had been missing in BOTH repos since v0.11. Add shims the moment you add a new canonical module.
- **Personal-branded launchd plist labels leak identity.** Private vault ships `com.linxule.memex.*` plists, which are correct for the author but never appropriate for the public repo — even templating paths can't hide the label. Public-repo shape: exclude the plist entirely, ship a template snippet in `SETUP.md` for users to customize. The associated `.sh` script can be generic and shipped.
- **Surgical updates to public README/CLAUDE.md are mandatory, not optional.** The old rule "don't touch them" led to 5 versions of doc rot (retired slash commands still listed, dead config fields, removed skills in the skills table). When a command/skill/config-field retires, grep public's README + CLAUDE.md for references and fix surgically. The `plugin-validator` agent catches this class of rot in ~90s.
- **Pre-release doc-grep checklist — batch into the release commit, not a follow-up.** Plugin-validator passing ≠ docs are done; it checks manifest hygiene, not narrative docs. Before tagging any release, grep these surfaces for stale references and update in the same commit:

  ```bash
  # In the PUBLIC clone:
  grep -nE "memex (search|ask|backfill|status|check|sync|graph|topic|obs|index|session|similarity|mark-saved|scrub|context|read|path|timeline)" \
       README.md CLAUDE.md SETUP.md CHANGELOG.md
  grep -nE "^\| (SessionStart|UserPromptSubmit|SessionEnd|PreCompact|PostToolUse)" README.md
  tail -5 CHANGELOG.md  # confirm latest entry matches release version
  ```

  Each release category has its own minimum:
  - **Feature release** (e.g. v0.12.0 added `memex scrub`): README CLI table + CHANGELOG entry + CLAUDE.md command table; new hook → README hooks table; new pattern in any rule file → update both private and public `.claude/rules/*` copies.
  - **Bug-fix release** (e.g. v0.11.6): CHANGELOG entry sufficient; README only if behavior visible to users changed.
  - **Sync release** (catch-up commits): nothing usually, but spot-check that the CHANGELOG isn't silent across the gap.

  Caught real drift on 2026-05-25 ship: v0.12.0 shipped with stale README (missing `memex scrub` + PostToolUse) AND CHANGELOG silent since v0.11.5 (missing both v0.11.6 and v0.12.0). Required a follow-up commit (`ba847b9` public) that should have been part of the release commit. The closing-arc updates I did (CLAUDE.md, MEMORY.md, `_project.md`, memo, observations, signals) covered curator-facing docs but not user-facing ones — different surfaces, different checks. Run the grep BEFORE `git commit`, not after.

- **Private repo does NOT get a separate CHANGELOG.** Decided 2026-05-25 post-v0.12.0. The private `projects/memex/_project.md` "What's Active" section already serves as the curator-facing release log, with much richer per-release context than a CHANGELOG bullet would carry (review-round counts, ship-blocker findings, sweep results, reviewer attribution). Two release-log surfaces would just double the drift risk for an audience of one. The pre-release grep checklist targets `CHANGELOG.md` in the PUBLIC repo only; for the private repo, the equivalent check is "append a `What's Active` bullet to `_project.md` with the version, date, scope, review attribution, and commit SHA — same as prior entries." Don't introduce a private CHANGELOG without explicit reconsideration.

- **Test-fixture hygiene against third-party secret scanners.** GitHub push protection (and similar scanners) pattern-match the LITERAL bytes of source files. A test like `text = "token=xoxb<dash><10digits><dash><10digits><dash><24chars>"` (the full Slack-bot-token shape spelled out as one literal) trips the Slack detector even when the value is obviously fake — the scanner sees the full shape in source bytes and doesn't validate against the actual provider. Defanging to all-zero IDs + all-A token doesn't bypass it; shape matters, not value. Same risk for the Google API prefix (`AIza` + 35 chars), JWT (`ey<base64>.ey<base64>.<base64>`), Anthropic (`sk<dash>ant<dash>api03<dash>` + base62), GitHub PAT (`ghp_` + 36 chars), AWS (`AKIA` + 16 uppercase), and any other provider-prefix-anchored shape. **Build provider-shaped test fixtures at runtime via concatenation** so source bytes contain split substrings (no scanner match) but runtime values still exercise the regex:

  ```python
  def _slack_fixture() -> str:
      return "xox" + "b-" + "0" * 10 + "-" + "0" * 10 + "-" + "A" * 24

  def _anthropic_fixture() -> str:
      return "sk-ant" + "-api03-" + "A" * 56
  ```

  See `tests/test_scrub.py` for the canonical builder pattern. Private repos can opt out of push protection (memex private accepted realistic-shape fixtures), but public repos generally cannot — and a private-only test that breaks on public sync is its own incident. Build the helpers from day one.
- **Reviewer fan-out: ≥2 rounds, continue until a reviewer explicitly clears as ready-to-ship.** The older "2-stage fan-out (planning + post-ship)" framing is a **floor, not a ceiling** for substantive releases. v0.11.1 sync ran 6 review passes (3 planning + 3 post-commit). v0.11.4 ran 6 rounds total (rounds 1–6, each round 3–5 parallel reviewers). New findings kept appearing through round 5; round 6 was the explicit gate. Across both releases the pattern repeats: don't stop on a schedule, stop on a clear ship signal. Fan out internal Sonnet reviewers (code-reviewer, plugin-validator, code-architect) and external delegated reviewers (codex-rescue, kimi-review, kimi-challenge) in parallel each round — overlap stays under ~20%, complementarity is the value.
- **Multi-round audit catches wrong fixes, not just missing fixes.** Round 3 on v0.11.4 had a plugin-validator finding that called `project_mappings` a phantom field and recommended removing it. Round 4 caught the bad fix before it shipped — `project_mappings` is actively read by `utils.py:136`. Trust-but-verify applies to reviewers the same way it applies to subagents: a confident reviewer can still be wrong, and the next round's job includes re-checking the prior round's prescriptions, not just executing them.
- **Cost framing.** 6 rounds × 3–5 parallel reviewers × ~3–5 min wall clock each ≈ 30–45 min total. Compare with the May 7 observation wipe recovery (~$10–20 in subagent time + half a day of human attention) and the cost arithmetic is obvious — cheap insurance for any release that touches transaction boundaries, schema, or atomic-swap code paths.

## Cross-Cutting

- **skill vs command consistency** — If `save.md` adds a step (like observation extraction), the `memo-writing` skill should mention it too, or agents that invoke the skill directly will skip the step
- **recall's 4-mode routing** — `recall` handles all retrieval via internal routing: TEMPORAL (date-based browsing), KEYWORD (targeted FTS lookup), DEEP (cross-session synthesis, formerly ask-memex), and LOAD (pull specific topic/memo into context). The ask-memex skill no longer exists — its deep synthesis capability was absorbed into recall's DEEP mode. When "why did we..." triggers, recall routes to DEEP automatically
- **curator-practice vs garden-tending boundary** — `curator-practice` = autonomous operating philosophy (WHEN to act, judgment heuristics). `garden-tending` = procedural reference (HOW to do each operation). Curator-practice loads garden-tending as needed. Both trigger on "tend the garden" — tiebreaker: if the user says "use your judgment" or the agent is running autonomously → curator-practice. If the user gives specific instructions → garden-tending
