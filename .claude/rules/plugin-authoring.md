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

- **Prefer harness-exposed env vars over filesystem-derived state.** Claude Code 2.1+ exposes `CLAUDE_CODE_SESSION_ID`, `CLAUDE_PROJECT_DIR`, `CLAUDE_CODE_EXECPATH`, `CLAUDE_CODE_ENTRYPOINT`, `CLAUDECODE` inside any tool or CLI invoked from a session. Before adding a state-file mtime heuristic, lockfile dance, or "guess by parsing recent activity" pattern to identify the current session/project/context, check whether the env already gives you what you need. Pattern (from v0.12.2 `mark_saved` fix): read the env var first; fall back to the filesystem heuristic only when the env var is empty; emit a `WARN:` to stderr when the env var is set but the filesystem state it points at doesn't match. The warn-and-fallback pattern handles both backward compat (older Claude Code) and ad-hoc CLI use, while signaling config drift loudly instead of silently fixing the wrong session.
- **Every release, grep `Path.write_text` and `Path.write_bytes` in `src/memex/` and `hooks/`.** Each match must fall into one of three categories: (a) **structural content** — state files, signal files, lock files, anything with no prose body and no secret risk; (b) **frontmatter-only edit** — the body is preserved verbatim (no new prose introduced), so any existing scrub is unchanged; (c) **prose write routed through `memex.scrub.safe_write_text(path, content)`** — the shared write-gate that pre-scrubs via `scrub_text` before write. Anything else is a hook bypass: the v0.12.0 PostToolUse hook only fires on Claude Code's `Write`/`Edit`/`MultiEdit` tool invocations, NOT on Python filesystem writes. Single audit point, two enforceable categories. v0.12.2 audit baseline: 14 call sites, `sync_auto_memory` + `extract.py` use the helper, dreamer + `backfill_*` deliberately kept un-gated (structural/frontmatter-only with low secret risk). New write paths default to the helper unless you can justify (a) or (b) in a code comment.
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

- **Version must be bumped in THREE files together** — `pyproject.toml`, `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`. `src/memex/__init__.py` auto-tracks via `_read_version()` which reads from pyproject.toml at runtime (memex uses `package = false`, so importlib.metadata is unreliable — direct tomllib read is the working pattern). Missing any one of the three causes drift — plugin cache won't invalidate (if plugin.json is stale) or runtime version reports wrong number (if pyproject.toml is stale). **Both clones ship the `_read_version()` pattern as of 2026-05-25**; if you fork or add a new clone, port `_read_version()` immediately — DO NOT hardcode `__version__` even temporarily. The "hardcoded then forget to bump" failure mode recurred from v0.12.2 → v0.13.0 on the public clone before the pattern was ported there; treat hardcoded `__version__` as a regression on either clone.
- **Stale cache versions accumulate** — After bumping version, check `~/.claude/plugins/cache/memex-local/memex/` and remove old version directories
- **Stale `.dist-info` churns the vault venv on EVERY `memex` call** — Version bumps leave the old `memex-<oldver>.dist-info` dir in `.venv/lib/python3.13/site-packages/` *without a RECORD file*. The `bin/memex` shim runs `uv run --directory`, so on every invocation uv tries to reconcile, fails to cleanly uninstall the RECORD-less dir, re-installs, and prints `warning: Failed to uninstall ... due to missing RECORD file` to stderr — slow + noisy, and it polluted a skill `!`command`` injection in the 2026-06-09 garden-tending pass. **Add to the release SOP, right after the 3-file version bump:** `rm -rf .venv/lib/python*/site-packages/memex-*.dist-info` (or `uv sync --reinstall`), then run `memex status` once to confirm a clean (warning-free) invocation. Symptom check: `ls .venv/lib/python*/site-packages/memex-*.dist-info` should show ONLY the current version.
- **`claude plugin update`/`install` from a `directory` source snapshots `.git/` too — re-bloats the cache.** The "register from clean clone" fix killed the 5–8GB *vault-data* bloat, but the clean clone still has a ~93MB `.git/`, and `claude plugin update memex@memex-local` copies the WHOLE directory into `~/.claude/plugins/cache/memex-local/memex/<version>/`, `.git/` included (observed 2026-06-09: a fresh 0.14.0 cache was 93M vs the prior rsync'd-clean 0.12.2 at 2.4M). **Add to the release SOP, right after `claude plugin update`:** prune the cache — `rm -rf ~/.claude/plugins/cache/memex-local/memex/<ver>/.git` (+ `.venv`, `__pycache__`, `.pytest_cache`) → drops to ~2-3MB. Verify the plugin survives: `grep version <cache>/.claude-plugin/plugin.json` + skill/command spot-check. (Permanent fix would be a `.git`-less export as the marketplace source, but per-update prune is the cheap operational step.)
- **Plugin cache venv is separate** — The cache at `~/.claude/plugins/cache/` has its own venv. After vault venv fixes, also check/fix the cache venv: `cd ~/.claude/plugins/cache/memex-local/memex/<version>/ && uv run python -c "import memex; print('ok')"`
- **Open sessions keep stale config** — Reinstalling the plugin updates the cache but already-open sessions still use the old config. They must be restarted to pick up changes
- **Two-layer distribution** — `uv tool install .` gives the global `memex` CLI (any agent). `claude plugin install` adds hooks/skills (Claude Code only). Scripts live in `src/memex/scripts/`, originals in `scripts/` are backward-compat shims. `package = true` in `pyproject.toml` enables both paths
- **Register the marketplace from the clean public clone, not the working vault dir.** Memex is a "vault-style" plugin: the working directory contains BOTH plugin code (`hooks/`, `commands/`, `skills/`, `src/`) AND user data (`projects/`, `_index.sqlite`, `topics/`, `_meta/`). When `/plugin marketplace add /path/to/working/vault` is run, `claude plugin install` snapshots the entire directory tree into `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`, including all gitignored data files. Per-version cache becomes 5–8GB instead of the ~1.5MB it should be (the plugin code itself). **Always use the public clean clone path** (e.g., `/Users/xulelin/Documents/Apps/mcp/memex-plugin`) for `/plugin marketplace add`. The clean clone contains only code — no `projects/`, no `_index.sqlite`. The plugin's CLI reads `~/.memex/config.json` → `memex_path` to find the vault at runtime, so the cache install location doesn't have to live next to user data. **Symptom check**: `du -sh ~/.claude/plugins/cache/memex-local/memex/<version>/` — if it's >100MB you've snapshotted the vault. **Fix**: edit `~/.claude/plugins/known_marketplaces.json` → `memex-local.source.path` to point at the clean clone; `rm -rf` the bloated cache dir; `rsync -a --exclude='.git/' --exclude='__pycache__/' --exclude='.venv/' --exclude='.pytest_cache/' <clean-clone>/ <cache-dir>/`; `/reload-plugins`. Applied 2026-05-25 evening, freed 7.7GB. Diagnosed May 7 but the documented fix wasn't actually applied until May 25 — when the next version installed it re-bloated the cache. Lesson: when MEMORY notes a "permanent fix", verify it's been operationalized at the data layer, not just documented in prose.

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

  # In BOTH clones — rules-doc external-version drift (recurring: Obsidian runtime,
  # embedding model, vendored dep versions). This is the surface plugin-validator does
  # NOT check, and where the Obsidian 1.12.5→1.13.1 lag hid for two releases:
  grep -rnE "Obsidian [0-9]+\.[0-9]+|gemini-embedding-[0-9]|sqlite-vec[ >=]+[0-9]|google-genai[ >=]+[0-9]" \
       .claude/rules/*.md
  ```

  Each release category has its own minimum:
  - **Feature release** (e.g. v0.12.0 added `memex scrub`): README CLI table + CHANGELOG entry + CLAUDE.md command table; new hook → README hooks table; new pattern in any rule file → update both private and public `.claude/rules/*` copies.
  - **External-dependency version change** (Obsidian runtime, Gemini embedding model, `sqlite-vec`, `google-genai`, any vendored tool version): run the rules-doc grep above and fix the stale version string in BOTH clones. Version facts in `.claude/rules/*` drift silently — no validator checks them — which is the root cause of the v0.14.2/v0.14.3 Obsidian-version lag (docs said 1.12.5 while the runtime was 1.13.1). Treat any dep bump as a doc-grep trigger, not just CLI/hook/skill changes.
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

## Vault Operations

- **Folder rename: use `memex obs reassign` to preserve observations, not cascade-delete + re-extract.** When migrating `projects/<old-name>/` to `projects/<new-name>/` (e.g. the 2026-05-25 `Apps-pi-proxy/` → `pi-proxy/` consolidation), the naive sequence is `git mv` + `memex index rebuild --incremental` — but the incremental rebuild detects the old paths as deleted and cascade-deletes the matching `observations` rows (and the `chunks` rows behind them). The morning's video-production migration lost 43 obs that way; an afternoon's pi-proxy migration preserved all 68 via a manual SQL UPDATE pattern that's now first-class via `memex obs reassign` (shipped v0.13.0).

  **Schema insight that makes the cleaner pattern work**: `observations.doc_path` is a plain `TEXT` column, but `fts_observations`, `vec_observations`, and `observation_topics` all join by `observation_id` rowid — NOT by doc_path. So a plain SQL UPDATE on `observations.doc_path` (plus the mirror `chunks.doc_path`) preserves all the index mirror state automatically. No re-extraction, no re-embedding, no cascade ceremony.

  **Canonical SOP** (uses `memex obs reassign`, shipped 2026-05-25 in the W4 sprint that promoted the manual pattern to a first-class CLI):

  ```bash
  # 1. Move the files (git mv preserves history as renames)
  git mv projects/<old-name>/memos/*.md projects/<new-name>/memos/
  mv projects/<old-name>/{auto-memory,transcripts}/* projects/<new-name>/{auto-memory,transcripts}/

  # 2. Update the project: field in each moved memo's frontmatter
  for f in projects/<new-name>/memos/<moved-files>.md; do
    sed -i.bak 's/^project: <old-name>$/project: <new-name>/' "$f" && rm "${f}.bak"
  done

  # 3. Dry-run the reassign to verify match counts look right
  memex obs reassign --from-prefix "projects/<old-name>/" --to-prefix "projects/<new-name>/"
  # → reports obs_matched + chunks_matched; nothing committed yet

  # 4. Apply — atomic transaction with invariant check (matched == updated, else rollback + exit 2)
  memex obs reassign --from-prefix "projects/<old-name>/" --to-prefix "projects/<new-name>/" --apply

  # 5. Remove empty old directory
  rmdir projects/<old-name>/{memos,auto-memory,transcripts} && rmdir projects/<old-name>/

  # 6. Incremental rebuild — now a no-op on the moved content (paths still match disk)
  memex index rebuild --incremental
  ```

  The UPDATE-then-incremental sequence is strictly lighter than `--full` rebuild's `SAVEPOINT obs_preserve` preservation registry: incremental treats the rename naturally when doc_path on disk matches, no all-or-nothing ceremony needed. The pattern was validated on the pi-proxy migration (68 obs + 249 chunks preserved, total obs count unchanged at 13545) and then promoted to CLI with 9 regression tests (`tests/test_obs_reassign.py`).

  **Why the CLI uses `SUBSTR(LENGTH(prefix) + 1)` not `REPLACE()`** as the original manual pattern did: `REPLACE` rewrites every occurrence of the substring, so a path like `projects/Apps-X/memos/Apps-X-backup.md` would have both occurrences swapped. `SUBSTR` is prefix-only. The original manual UPDATE was safe by accident (no such paths in the pi-proxy migration); the CLI version is safe by construction. Test `test_substr_replaces_only_prefix_not_internal_occurrences` pins this.

  **UNIQUE collision handling**: `chunks` has `UNIQUE(doc_path, chunk_index)`. If a reassign would collide with existing rows at the target prefix (rare but possible during partial migrations), the CLI exits 3 with the IntegrityError and rolls back — data loss is worse than a failed migration. The integrity-check invariant on exit also catches the case where matched != updated and rolls back with exit 2.

  **Also fix forward wikilinks** in topic-level `## Recent signals` sections and any other vault content that references the old path. `grep -rln "projects/<old-name>/" topics/ projects/` then sed-batch the canonical paths.

- **Archive a topic with the closure pattern, never silent delete.** When an archived topic still has signals from memos that haven't been absorbed into a canonical home yet, the right state is *closed-with-audit-trail*, not deleted. Pattern (see 2026-05-25 `#27 archive-target signal migration sweep` for 6 examples):

  ```markdown
  ## Recent signals (closed — migrated to canonical)

  > **Closed YYYY-MM-DD.** All N signals below have been [migrated to / absorbed into]
  > [[canonical-target]] (the merge target per the body note / project_overview). Preserved
  > here as audit trail; future signals route to [canonical-target] directly via memo
  > `topics:` updates.

  - [existing signal list preserved verbatim, chronological]
  ```

  Plus: ensure the frontmatter has `redirect_to: <canonical-slug>` so `memex topic resolve <archived-slug>` returns the canonical. The body note's "Merged into [[X]]" wikilink is for human readers; the frontmatter `redirect_to:` is for the resolver. Both should always agree; one without the other is broken state. As of 2026-05-25 all 21 archived topics in the vault have `redirect_to:` set and all resolve cleanly.

## Cross-Cutting

- **skill vs command consistency** — If `save.md` adds a step (like observation extraction), the `memo-writing` skill should mention it too, or agents that invoke the skill directly will skip the step
- **recall's 4-mode routing** — `recall` handles all retrieval via internal routing: TEMPORAL (date-based browsing), KEYWORD (targeted FTS lookup), DEEP (cross-session synthesis, formerly ask-memex), and LOAD (pull specific topic/memo into context). The ask-memex skill no longer exists — its deep synthesis capability was absorbed into recall's DEEP mode. When "why did we..." triggers, recall routes to DEEP automatically
- **curator-practice vs garden-tending boundary** — `curator-practice` = autonomous operating philosophy (WHEN to act, judgment heuristics). `garden-tending` = procedural reference (HOW to do each operation). Curator-practice loads garden-tending as needed. Both trigger on "tend the garden" — tiebreaker: if the user says "use your judgment" or the agent is running autonomously → curator-practice. If the user gives specific instructions → garden-tending
