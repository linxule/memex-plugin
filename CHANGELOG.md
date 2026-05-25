# Changelog

All notable changes to the memex plugin. Dates in YYYY-MM-DD.

## [0.12.2] — 2026-05-25

Wrap-up release closing four open threads from v0.12.1. Two real bug
fixes (mark-saved cross-contamination, an unscrubbed write path), one
helper extraction that consolidates the v0.12.1 scrub-gate pattern into
a shared utility for future write-paths, and a doc-tracking cleanup.

### Fixed

- **`memex mark-saved` no longer cross-contaminates sessions.** The
  selection heuristic (newest state file by mtime, no project filter)
  would mark a session in project B's signal when invoked from project A
  if B's state file was the most recently touched. Now prefers
  `CLAUDE_CODE_SESSION_ID` (Claude Code 2.1+ exposes this in env to any
  tool/CLI invoked from a session) for unambiguous selection, and falls
  back to newest-by-mtime only when the env var is absent. When the env
  var points at a session with no state file (config drift), warn to
  stderr and fall back to mtime rather than silently fixing the wrong
  session. Real-world trigger: 2026-05-25 afternoon `memex mark-saved`
  invoked from the memex cwd cleared the kimi-plugin-cc session's
  signal instead of memex's own.
- **`extract.py::append_contradictions_to_memo` now scrubs before
  write.** The contradictions-frontmatter rewrite path bypassed the
  PostToolUse hook (Python writes via `Path.write_text`, not via
  Claude's Write tool). Body content is normally already-scrubbed
  (post-v0.12.0 writes or retroactive sweep), but a v0.11.x-era memo
  touched here for the first time would have escaped. Now uses the new
  shared `safe_write_text` helper.

### Added

- **`memex.scrub.safe_write_text(path, content) -> int`** — shared
  write-gate for any Python code path that writes prose to disk without
  going through Claude Code's Write/Edit/MultiEdit tools (the boundary
  the PostToolUse hook covers). Pre-scrubs content via `scrub_text`,
  writes via `path.write_text`, returns the redaction count. Single
  audit point — anywhere `Path.write_text` is called on user-prose
  content, prefer this helper.

### Changed

- `sync_auto_memory.py::sync_file` refactored to use `safe_write_text`
  (the v0.12.1 fix is functionally identical, just shares the helper
  now instead of inlining the scrub call).

### Tests

- 4 new tests in `tests/test_mark_saved.py`: env-var-driven selection
  picks the right session even when another session's state file is
  newer; mtime fallback when env var absent; warning when env var
  points at a missing state file; pending-memo signal cleanup.
- 4 new tests in `tests/test_scrub.py::TestSafeWriteText`: clean
  content unchanged, single-secret redaction, multi-secret redaction,
  OSError propagation on bad parent path.
- 222 total passing (up from 214 in v0.12.1).

## [0.12.1] — 2026-05-25

Pattern-catalog expansion and one real gap closure. The v0.12.0 catalog
covered the providers most likely to leak via subagent transcripts
(Anthropic, OpenAI, Slack, GitHub, Google, AWS, JWT, PEM); v0.12.1
extends to four more high-confidence-shape providers, and patches the
`memex sync` path that previously bypassed the PostToolUse hook because
it writes via `Path.write_text` (not via the Claude `Write` tool).

### Added

- **HuggingFace tokens** (`hf_[A-Za-z0-9]{34,}`) — covers user-access
  tokens and fine-grained tokens. Unique prefix; low false-positive risk.
- **Stripe keys** (`(sk|pk|rk)_(live|test)_[A-Za-z0-9]{24,}`) — covers
  secret, publishable, and restricted keys in both live and test mode.
  Uses `_` (not `-`) so this never overlaps with the existing `sk-ant-*`
  / `sk-proj-*` / generic-sk patterns above it.
- **Notion integration secrets** (`secret_[A-Za-z0-9]{43}`) — 50-char
  total format. The 43-char alphanumeric run after `secret_` is
  shape-distinctive enough to avoid prose collisions with phrases like
  `secret_password`.
- **Sentry DSNs** (`https?://<32hex>(:<32hex>)?@*sentry*/<id>`) — covers
  both the modern (public-only) and legacy (public+secret) DSN forms;
  matches SaaS hosts (`*.ingest.sentry.io`) and self-hosted Sentry
  installs that include `sentry` in the host name.

### Fixed

- **`memex sync --apply` now scrubs auto-memory content before disk.**
  The PostToolUse hook from v0.12.0 only gates Claude's
  `Write`/`Edit`/`MultiEdit` tool invocations. `memex sync` writes via
  `Path.write_text`, bypassing the hook entirely — so secrets in
  `~/.claude/projects/<project>/memory/*.md` files (which Claude's
  memory system writes without any scrub gate of its own) would
  round-trip into the vault un-redacted and become discoverable via
  search. `sync_file()` now pre-scrubs the assembled content via
  `scrub_text(content, apply=True)` before the disk write, and surfaces
  the redaction count in verbose output (`[scrubbed: N]`).

### Tests

- 13 new scrubber tests: 8 pattern-detection tests covering each new
  provider (using runtime-concatenation fixtures so source bytes don't
  trip GitHub push protection on Stripe/Sentry shapes), plus 5 new
  prose-FP tests guarding against false matches on phrases like
  `secret_password`, `hf_dataset = load_dataset(...)`, `sk_live_demo`,
  and the Sentry docs URL.
- 4 new sync-scrub tests covering: scrub on a single secret, scrub on
  multiple secrets, clean-content has no `scrubbed` field in the result
  dict, and dry-run does neither.
- 214 total passing (up from 201 baseline).

### Retroactive sweep

Targeted scan (memos + topics + `_meta` + auto-memory + transcripts)
found two `pk_live_*` Stripe-shape matches in a single research-starter
transcript (an embedded HuggingFace publishable key from a captured
API response). Publishable Stripe keys are low-risk by definition, but
the scrubber is shape-based, not value-based — both occurrences were
redacted to `<REDACTED:stripe>` for consistency with the v0.12.0 policy.
No other vault content matched the new patterns.

## [0.12.0] — 2026-05-25

First feature release in the 0.12.x line. Introduces a secret-scrubber
CLI + library + deterministic write-time hook, triggered by an incident
in which a subagent probe sequence read a local config file and surfaced
three API keys into an on-disk transcript. The lesson — instruction-
based controls fail in exactly the cases they're meant to catch — drove
the architectural choice to make a PostToolUse hook (not subagent
instructions) the primary defense.

### Added

- **`memex scrub <path>`** CLI + library at `src/memex/scrub.py` with
  shims at `src/memex/scripts/scrub.py` and `scripts/scrub.py`. Detects
  API keys, tokens, and PEM private-key blocks via curated high-precision
  regex (Anthropic, OpenAI variants, generic-sk + generic-sk-vendor,
  GitHub PATs, Google API, AWS access keys, Slack tokens, JWT, private-
  key blocks). Specificity-first overlap resolution. Idempotent
  (`<REDACTED:provider>` markers don't match any pattern, so re-scrub
  is a no-op). Atomic write via tempfile + `os.replace` + fsync;
  preserves CRLF line endings byte-for-byte; safe on long filenames
  (truncates `mkstemp` prefix to leave NAME_MAX headroom). Exit codes:
  `0` clean / `1` dry-run with matches / `2` apply error.
- **`hooks/post-tool-use.py` PostToolUse gate.** Auto-scrubs every
  `Write` / `Edit` / `MultiEdit` operation targeting
  `projects/<name>/memos/**` or `projects/<name>/auto-memory/**`. The
  deterministic primary defense — doesn't depend on subagent compliance.
  Other paths (transcripts, topics, etc.) pass through untouched.
  Errors log but never block the user's write.
- New step in **`commands/save.md`** (4b — "Scrub for Secrets") and
  **`skills/memo-writing/SKILL.md`** (After Saving step 2) calling
  `memex scrub --apply` before observation extraction. These are the
  belt-and-suspenders layer; the PostToolUse hook is the primary control.
- L2 subagent prompt in **`hooks/session-start.py`** now includes
  explicit "do not transcribe API keys..." guidance plus a
  `memex scrub` invocation as a final guard. Path arguments are
  shell-quoted and the Python f-string escapes single quotes in
  `transcript_path` / `project` to prevent literal-corruption hazards.
- `memex scrub` documented in CLAUDE.md's command table.

### Quality

- 54 new tests (46 for the scrubber + 8 for the PostToolUse hook).
  Regression tests for the overlap-algorithm specificity invariant,
  CRLF preservation on read+apply round-trip, long-filename mkstemp
  prefix, atomic-write temp cleanup on rename failure, and idempotency
  of `<REDACTED:provider>` markers.
- Three review rounds: in-house Claude code-reviewer (4 issues — 2 HIGH
  overlap algorithm + self-test FP, 2 MEDIUM atomic write + per-file
  errors — all addressed), Codex correctness audit (3 MEDIUM ship-
  blockers — long-filename mkstemp prefix, CRLF normalization on apply,
  unquoted memo-path in L2 prompt — all addressed), Claude design
  challenge (architectural pushback addressed by adding the PostToolUse
  hook), plugin-validator (PASS).

### Migration

Existing memos and auto-memory files are not auto-scrubbed by the hook
installation alone (the hook only fires on new writes). Backfill once:

```bash
memex scrub "$(memex path)" --apply
```

This runs against the full vault — memos, auto-memory, project docs,
topics, and transcripts. Idempotent and safe to re-run. Transcripts
are typically gitignored but still readable on disk; scrubbing them
closes the local-disk exposure window.

### Notes on what the scrubber does NOT catch

Line-wrapped keys (e.g. word-wrapped inside a markdown table), keys
inside base64-wrapped JSON payloads, low-entropy custom-format tokens,
secrets in commit messages (out of vault scope), and providers not in
the catalog (HuggingFace `hf_*`, Stripe `sk_live_*` / `pk_live_*`,
Notion `secret_*`, Sentry DSN URLs — slated for v0.12.1). Treat the
scrubber as one layer of defense, not the only one. If you're saving
a memo that intentionally discusses a secret, redact it manually
rather than relying on the scrubber.

## [0.11.6] — 2026-05-25

One-character bug-fix release closing a latent flaw in the v0.11.5
idempotency guard.

### Fixed

- **`grep -Fxq` flag-parsing in the topic-signal dedup.** The v0.11.5
  guard `grep -Fxq "$SIGNAL_LINE" "$TOPIC_FILE"` silently failed when
  `$SIGNAL_LINE` started with `- ` (the bullet prefix grep parsed as a
  flag). The dedup never fired and duplicate signal lines accumulated
  on touched topics whenever the title or bullet content contained
  anything grep tried to interpret as an option. Fix: add `--` to
  terminate option parsing in both grep calls in `commands/save.md`
  (the signal-line dedup AND the adjacent `## Recent signals`
  section-existence check, hardened belt-and-suspenders even though
  the literal pattern doesn't start with `-` today).
- **Skill prose pointer mirrors the requirement.**
  `skills/memo-writing/SKILL.md` step 3 now mentions `grep -Fxq --`
  explicitly so future readers transcribing the bash loop don't
  reintroduce the bug.

### Migration

None. Existing duplicate signal lines created during the v0.11.5
window are cosmetic; the user manually deduped or left them for the
next garden-tending pass. Future saves no longer hit the bug.

## [0.11.5] — 2026-05-17

Bug-fix release. The `/memex:save` topic-stamping step in
`commands/save.md` was not idempotent: when a memo's frontmatter listed
multiple slugs that all redirect to the same canonical topic (e.g.
`claude-code-plugins`, `plugin-architecture`, `plugin-development` all
→ `Claude-Code-Plugins`), the bash loop appended the same signal line
once per input slug. One save could leave two- or three-times duplicates
on the canonical topic file. The same pattern hit
`multi-agent-code-review` → `multi-agent-review`. No data loss — just
visual noise in `## Recent signals` sections.

### Fixed

- **Idempotent topic stamping.** The bash loop in
  `commands/save.md` step 6 now wraps the append in
  `grep -Fxq "$SIGNAL_LINE" "$TOPIC_FILE"` so the same line is never
  written twice to the same topic file. `-F` treats `[[`, `|`, `]])`,
  `.` as literal (no regex surprises); `-x` requires whole-line match
  (no substring false positives). Also covers the case where
  `/memex:save` re-runs on an existing memo across sessions.
- **Skill instruction mirrors the requirement.**
  `skills/memo-writing/SKILL.md` step 3 now explicitly states the
  dedup requirement so out-of-band invocations (skill triggered
  without going through the slash command) inherit the same behavior.

### Migration

None. Existing duplicate signal lines in topic files are cosmetic;
clean them by hand when convenient or let the next garden-tending
pass handle them. Future saves will no longer create new duplicates.

## [0.11.4] — 2026-05-13

Hotfix for v0.11.3 across four rounds of multi-reviewer audit (3+codex
on the v0.11.3 ship, then 4 on the fix-set, then 1 narrow gate on the
final delta — 22 + 10 findings total). The audit caught two HIGH-severity
data-loss paths in v0.11.3 and several class-of-bug repeats; this release
hardens all of them. No schema changes — drop-in upgrade.

### Fixed

- **Partial observation preservation no longer ships a half-baked index
  (HIGH).** v0.11.3's `rebuild_full --atomic` caught `sqlite3.OperationalError`
  during ATTACH-old preservation and only logged to stderr, then the
  `finally` block committed and the atomic swap proceeded. A disk-full
  partway through the four INSERT…SELECT statements would have produced
  a "successful" rebuild with broken observation FTS/vector search and
  no error signal — strictly worse than the May 7 wipe (which at least
  failed loudly). Preservation now runs inside `SAVEPOINT obs_preserve`;
  any exception triggers ROLLBACK, sets `stats["preservation_error"]`,
  raises `RuntimeError` with a stable prefix, and the CLI exits 4 with
  a clear message instead of falling through to the swap.
- **Fresh-install slash commands no longer break out of the box (HIGH).**
  README/SETUP Quick Start used to install the plugin before the `memex`
  CLI, so the first `/memex:status` after install failed with
  `memex: command not found`. Quick Start is reordered: `uv tool install`
  is now Step 1, plugin install is Step 2.
- **`redirect_to:` resolver handles cross-namespace targets (P0).** The
  bash resolver in `commands/save.md` hardcoded `$VAULT/topics/$slug.md`,
  so real-world redirects like `topics/bloom.md → projects/clawd-world/_project.md`
  silently dropped signals. Resolver extracted to a Python module +
  `memex topic resolve` CLI subcommand. Bare slugs resolve under
  `topics/`; targets containing `/` resolve as vault-relative paths
  (with auto `.md` suffix). Path traversal (`../../etc/passwd`) is blocked
  via `Path.resolve()` + containment check.
- **Cycle detection in redirect chains (P0).** The 5-hop limit is now a
  fallback, not the primary guard. The resolver tracks a visited-set
  and reports cycles as `WARN: redirect cycle detected: A -> B -> A`
  with the actual chain — not the previous generic
  "exceeded 5 hops" message.
- **CHANGELOG migration note for v0.11.3 was factually inverted.**
  v0.11.3's text said terminal-archive (`status: archived` without
  `redirect_to:`) would "silently land on the archived stub". Actual
  behavior is "skip with stderr warning". The note is rewritten to
  describe both archive shapes correctly.
- **`batch_extract_observations.py` exit code reflects partial failure.**
  Previously exited 0 if any single memo succeeded — a run of 1-ok +
  99-store-failed exited 0, hiding data loss from automation. Now
  exits 0 only when all results are ok-or-skipped, 1 when no success,
  2 when partial failure (matches the v0.11.0 `backfill obs` convention).
- **`commands/status.md` no longer hardcodes `~/.memex/pending-memos`.**
  The path resolves through `state_dir` config, which the hardcoded
  pipeline ignored. Replaced with `memex context` lookup.
- **`init_observation_schema` no longer commits internally.** Restores
  the v0.11.1 "callers own transactions" convention. All call sites
  audited; each already commits downstream.
- **`config.json.example` cleanup**: prior fix mistakenly claimed
  `project_mappings` was an unused phantom field. It's actively read by
  `detect_project()` in `src/memex/scripts/utils.py` as priority-1
  project name override. Example block restored with accurate comment.

### Changed

- **Observation preservation refactored to registry + helper.** Module
  constant `_OBS_PRESERVATION_TABLES` lists every preserved table.
  Test `test_preservation_registry_covers_init_schema` enforces that
  any new table added to `init_observation_schema` either appears in
  the registry or in the documented FTS/vec special-case branches —
  converts the v0.11.3 fts_observations bug class from
  runtime-symptom to CI-time discovery.
- **`memex topic resolve <slug>`** is a first-class CLI subcommand
  (registered under the new `topic` Typer group). Used internally by
  `/memex:save` and available standalone for testing or scripting
  redirect-aware tools.

### Added

- **Advisory file lock at `~/.memex/locks/full-rebuild.lock`** prevents
  `memex backfill obs` and `memex.dreamer` from racing against an
  in-progress `memex index rebuild --full`. New helper
  `memex.db_utils.writer_lock()` (context manager, `LOCK_SH | LOCK_NB`)
  wraps observation-write call sites in `extract.py` and `dreamer.py`.
  `--full` rebuild acquires `LOCK_EX | LOCK_NB`; contention exits 3
  with a clear message. Also: `BEGIN IMMEDIATE` on the old DB before
  ATTACH belt-and-suspenders against stragglers that haven't yet hit
  the lock check.
- **`tests/test_topic_resolve.py`** — 24 tests covering the redirect
  resolver: slug/path resolution, cross-namespace targets, cycle
  detection, quoted/comment/whitespace edge cases, path traversal
  blocked, binary-target graceful handling, empty frontmatter
  fall-through, auto-`.md` suffix on path-form targets.
- **`tests/test_batch_extract_observations.py`** — 13 tests for
  `parse_json_array` (fenced JSON, prose preamble, trailing prose,
  nested arrays, empty input, whitespace-only).
- **Additional tests in `tests/test_index_rebuild.py`**: registry
  coverage, vec_observations preservation across atomic swap,
  observation_topics broken-ref filter, preservation-failure aborts
  swap, CLI exits 4 on preservation failure, CLI re-raises unrelated
  RuntimeErrors.
- **FTS5 rowid invariant documented in code** at both the
  `INSERT INTO fts_observations` site (`extract.py`) and the
  `CREATE VIRTUAL TABLE` site (`observations.py`). Future maintainers
  who change either side know the preservation path depends on the
  equality `fts_observations.rowid == observations.id`.
- **`memex index status` listed in CLAUDE.md CLI commands table** (was
  documented in prose only).

### Migration notes

- Existing installs: no action required. The fixes are internal
  hardening + UX tightening. A future `memex index rebuild --full`
  will exercise the new SAVEPOINT-protected preservation path.
- If running automation that invokes `memex backfill obs` or
  `memex.dreamer` while a rebuild may be in progress: the new lock
  causes the backfill/dreamer to exit 3 on contention rather than
  silently lose writes. Wrap automation in retry-with-backoff if you
  need transparent recovery.

---

## [0.11.3] — 2026-05-13

Three fixes addressing curator-tending findings: redirect-aware signal
routing, fast `memex status` on large indexes, and observation preservation
across atomic full rebuilds. No schema changes — drop-in upgrade.

### Fixed

- **`memex status` no longer hangs at 100% CPU on large indexes.**
  `count_embedding_gaps` was running `LEFT JOIN chunks × vec_chunks`
  against the sqlite-vec virtual table — the join can't use an index, so
  it probed per-row (~107s on 136K chunks). Replaced with a
  `COUNT(chunks) - COUNT(vec_chunks)` delta — ~2000× faster (107s → 53ms).
  The per-doc DISTINCT query only runs when a gap actually exists (rare
  + actionable). Observations LEFT JOIN stays (low cardinality).
- **`rebuild_full --atomic` preserves observations across the swap.**
  Background: observations are extracted by sonnet subagents
  (`memex backfill obs --stdin`), not derived from documents at index
  time. The May 7, 2026 incident on the maintainer's vault wiped ~3265
  observations because the atomic swap installed a fresh empty DB.
  Recovery cost ~$10-20 in subagent time. Now `rebuild_full` runs
  `ATTACH DATABASE old` → `INSERT...SELECT` for observations +
  `observation_topics` + `vec_observations`, filtered to `doc_paths`
  still present in the new `fts_content` table. Dangling refs (memos
  deleted between rebuilds) are intentionally dropped. Defensive:
  handles pre-0.11 schemas without obs tables, commits before DETACH
  so the unlock succeeds.

### Changed

- **`/memex:save` now follows `redirect_to:` chains when appending
  "Recent signals" to wikilinked topics.** When archiving a topic
  whose content was absorbed into a canonical replacement, set
  `redirect_to: <target-slug>` in frontmatter (alongside
  `status: archived`). Signals from new memos will route to the target
  instead of accumulating on the dead-end stub. The resolver walks up
  to 5 hops, then bails with a warning. Topics archived without
  `redirect_to:` (typical for project-target archives where content
  belongs in a `_project.md`) get a `WARN: ... skipping signal`
  message — by design, not a bug.
- `skills/memo-writing/SKILL.md` step 3 updated to match: "Resolve
  `redirect_to:` in frontmatter first so archived topics route to
  their canonical replacement."

### Added

- **`scripts/batch_extract_observations.py`** — async dispatcher that
  fans up to 5 concurrent `claude --print --model sonnet` subprocesses
  across a list of memos to populate the observations table. Useful
  for users who imported existing memos before observations existed,
  or who need to refresh observations en masse. Idempotent: skips
  memos that already have observations. Logs per-memo JSON results
  to `~/.memex/logs/batch-obs-extraction.jsonl`. Rate ~0.12 memos/s
  at 5-way concurrency.
- **4 new tests in `tests/test_index_rebuild.py`** (98 tests total,
  97 pass + 1 skipped):
  - `count_embedding_gaps` fast path: asserts no LEFT JOIN against
    vec_chunks when total == embedded.
  - `count_embedding_gaps` fallback: per-doc query DOES run when gap > 0.
  - `rebuild_full` preserves observations across atomic swap.
  - `rebuild_full` drops obs for deleted memos (filter correctness).

### Migration notes

- Existing installs: nothing required. The `redirect_to:` convention is
  opt-in, but the two archive shapes now behave differently:
  - **Redirected archive** (`status: archived` + `redirect_to: <slug>`)
    — the resolver follows the chain (up to 5 hops) and the signal
    lands on the canonical replacement.
  - **Terminal archive** (`status: archived`, no `redirect_to:`) —
    the resolver emits a stderr warning
    (`WARN: archived with no redirect_to — skipping signal`) and the
    signal is **dropped**, not silently landed on the archived stub.
  To preserve signals for archives whose content moved elsewhere, add
  `redirect_to:` to the archive's frontmatter as you encounter them
  during garden-tending.
- `memex status` speedup is automatic.
- Observation preservation is automatic on the next `--full` rebuild.
  No more "I just rebuilt and now `memex obs stats` is empty" surprises.

---

## [0.11.2] — 2026-05-07

Throughput pass on the embedding pipeline. No schema changes, no breaking
API changes — drop-in upgrade. After installing, no rebuild required;
your next `memex index rebuild --incremental` (and the nightly job) will
just be much faster.

### Changed

- **Default Gemini model flipped from `gemini-embedding-2-preview` to
  the GA `gemini-embedding-2`.** Both names produce 3072-dim unit-norm
  vectors and are interchangeable — the GA name is just the documented
  stable identifier. Existing embeddings keep working; new embeddings
  come from GA. If your `~/.memex/config.json` pinned the `-preview`
  name, it still works; switching is optional.

### Added

- **Async/concurrent embedding dispatch.** Sub-batches now run in
  parallel under `asyncio.Semaphore(GEMINI_CONCURRENCY=5)` instead of
  sequentially with a 1-second inter-batch sleep. Gemini Tier 2 paid
  is TPM-bound at 5M tokens/min; with 8K-token batches the realistic
  ceiling is ~625 RPM, so 5 concurrent ≈ 48% utilization with
  headroom. Expect order-of-magnitude speedup on `--full` rebuilds
  and noticeably faster nightly `--incremental` runs.
- **±20% jitter on retry backoff.** `GEMINI_BACKOFF_SCHEDULE` is now
  randomized per attempt to prevent thundering-herd retries when
  multiple concurrent batches all hit 429 at the same instant.
- **Running-event-loop fallback for the sync `embed_texts` API.** If
  invoked from inside an async context (the way `scripts/mcp_server.py`
  tool handlers would call it), `embed_texts` punts to a worker
  thread with its own loop instead of raising
  `RuntimeError: asyncio.run() cannot be called from a running event loop`.
- **`threading.Lock` around `_get_client`.** The env-var stash dance
  that suppresses the SDK's "both keys set" warning is now serialized
  across threads.
- **Two new regression scenarios in `scripts/verify_embedding_retry.py`:**
  `concurrent_multi_batch` (force 3 sub-batches; assert order
  preservation + zero inter-batch sleeps) and `inside_running_loop`
  (the MCP-server regression we caught and fixed).

### Behavior change worth knowing

- **Partial failure is more granular.** Pre-0.11.2, a single bad
  sub-batch would None-pad itself *and every subsequent batch*. In
  0.11.2, parallel batches launch all at once; one batch's failure
  no longer kills its siblings — only the failing batch's slots
  become None, successful batches' vectors survive in the result
  list. `PartialEmbeddingFailure` is still raised so callers know
  something failed; positional alignment to input texts is preserved.

### Internal

- `GeminiProvider.embed_texts` now wraps `_aembed_texts` (async). The
  public API stays sync.
- The async path uses `await asyncio.to_thread(client.models.embed_content, ...)`
  rather than `client.aio.models.embed_content` directly, because the
  SDK's aio HTTP client binds to whichever loop first touched it and
  goes stale across our running-loop fallback's loop boundaries.

---

## [0.11.1] — 2026-04-21

Hardening pass on top of 0.11.0. Critical transcript-chunking fix.

### Fixed

- **Critical: transcript chunking slice bug.** `chunk_transcript_turns` used
  `parts[1::2]` against a non-capturing regex. `re.split` with a
  non-capturing pattern returns `[pre, body_1, ..., body_N]` — no
  interleaved delimiters. The old slice silently dropped every other turn
  and paired surviving bodies with the wrong headers. Net impact: every
  multi-turn transcript indexed before this fix had ~half its turns
  missing from the semantic search index, with mismatched headers on the
  rest. FTS5 was unaffected (tokenized raw content independently), which
  is why nothing surfaced as an error. **Existing installs must run
  `memex index rebuild --full` after upgrading** to re-chunk every
  transcript correctly.
- Rollback test in `test_index_rebuild.py` was vacuous — monkeypatched
  the wrong function. Now monkeypatches `index_document` (which runs
  after FTS is written) and asserts the FTS row stays at v1 content.
- `enable_load_extension(True)` no longer leaves the connection in a
  privileged state if sqlite-vec fails to load (try/finally).

### Added

- **Unit-norm invariant on embedding provider output.** Both
  `GeminiProvider` and `LMStudioProvider` call `_assert_unit_norm` at
  the end of `embed_texts`. Samples head+mid+tail of each batch,
  raises `ValueError` if `||v||²` drifts more than ±0.02.
  sqlite-vec uses L2 distance by default; only monotonic with cosine
  when inputs are normalized. New providers must call this helper.
- **`memex.db_utils` module.** `connect_index(path)` applies
  `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=10s` so
  writers don't collide with parallel `memex backfill obs --stdin`.
  `load_vec_extension(conn)` loads sqlite-vec and clears the
  privileged state in `finally`. Use this everywhere instead of bare
  `sqlite3.connect(path)`.
- **Per-doc SAVEPOINT pattern in rebuild loops.** `rebuild_full` and
  `rebuild_incremental` wrap each doc in `SAVEPOINT doc` → work →
  `RELEASE SAVEPOINT doc`, with `ROLLBACK TO SAVEPOINT doc` on
  exception. Split into `_rollback_savepoint_or_die` (propagates any
  ROLLBACK error) and `_release_savepoint_if_exists` (tolerant only
  of "no such savepoint" substring). Prevents partial commits on
  mid-doc exceptions while ensuring real SAVEPOINT bugs still surface.
- **Nightly rebuild support.** `scripts/nightly-rebuild.sh` handles
  incremental rebuild + `embed-missing` retry, sources `~/.secrets`,
  survives malformed secrets with a clear error. See SETUP.md for a
  launchd/cron snippet.
- Hermetic test suite shipped to public repo for the first time
  (11 files, 93 passed + 1 skipped on a fresh install).

### Changed

- `index_document` and `embed_chunks` no longer call `conn.commit()` —
  transaction boundaries belong to the caller. Single-file CLI callers
  commit explicitly; batch callers use the SAVEPOINT pattern above.

## [0.11.0] — 2026-04-20

### Added

- **`memex index embed-missing`** command. Idempotent retry path for
  when rebuild completes but the embed step failed (expired API key,
  rate limit). Finds chunks/observations in the index but missing from
  `vec_chunks`/`vec_observations` via LEFT JOIN, re-embeds through the
  pipeline. Exits non-zero if any remain unembedded.
- `embedding_gaps` surfaced in `format_rebuild_stats` + `format_status`.
- Dual Gemini/Google API key stderr-noise handling (env stash/restore
  in `_get_client`).
- `PartialEmbeddingFailure` typed exception. After 4 attempts
  (initial + 3 retries with 10s/30s/90s backoff) on 429/500/503,
  `GeminiProvider.embed_texts` raises this with partial results
  attached. `EmbeddingPipeline.embed_text` (single-text wrapper)
  catches it and degrades to `None` for query-path resilience;
  `embed_chunks` caches partial success and logs failures to stderr.

### Removed

- **`pending_embeddings.jsonl` queue.** `enqueue_embedding_job` /
  `dequeue_embedding_jobs` / `get_embedding_queue_count` were dead
  code (zero call sites) and duplicated `embed-missing`. Removed.
- **`memex context --full` and `--compact`.** The rich context injection
  surface backed by `src/memex/context.py` was removed by design.
  `recall` skill DEEP mode replaces it. The `memex context` CLI
  command is preserved but trimmed to project-detection + pending
  memo status.

## [0.9.0 – 0.10.0] — 2026-04 (consolidated)

### Added

- **Observation-to-topic clustering.** `observation_topics` junction
  table links observations to topic slugs (many-to-many). Enables
  bounded complete retrieval: `memex obs topic <slug>`, `memex obs
  stats`, `memex obs retag <old> <new>`, `memex obs untagged`.
- **`memex backfill topic-tags`** propagates memo-frontmatter topics
  to `observation_topics`.
- **Gemini Embedding 2 support** (3072d). Dimension migration
  auto-drops `vec_chunks` on provider switch (1024d LM Studio ↔
  3072d Gemini). Preview model `gemini-embedding-2-preview` does NOT
  support `task_type` — `_build_embed_config` strips it.
- **Scoped observation search** and **similarity detection**
  (`memex similarity`).

## [0.7.0 – 0.8.0] — 2026-04 (consolidated)

### Added

- **Typer-based unified CLI.** Subcommand groups: `index` (rebuild,
  status, embed-missing), `obs` (topic, stats, retag, untagged),
  `backfill` (obs, tokens, memos, topic-tags), `session` (discover,
  import), `graph` (backlinks, orphans, tags, stats).
- `memex read`, `memex path`, `memex check`, `memex context`.

### Changed

- **Primary interface shifted to CLI + skills.** `scripts/mcp_server.py`
  still ships as an optional MCP integration, but the recommended path
  is now the `memex` CLI (shell access) plus intent-based skills
  (`recall`, `garden-tending`, `memo-writing`, `curator-practice`) for
  in-session use. New features land in the CLI first.

### Removed

- `scripts/batch_generate_memos.py` (the only `import anthropic` in
  the codebase, contradicted the "no external API calls" architecture).

## Breaking Changes Summary (0.6.0 → 0.11.1)

If you're upgrading from 0.6.0, expect these user-visible changes:

1. **Slash commands removed**: `/ask`, `/backfill`, `/load`,
   `/maintain`, `/merge`, `/retry`, `/search`, `/synthesize`,
   `/timeline`. Use the equivalent CLI (`memex ask`, `memex search`,
   `memex timeline`, `memex backfill`) or skill-based flows
   (`garden-tending` handles the former `synthesize` and `merge`
   behavior). Kept slash commands: `/memex:save`, `/memex:status`,
   `/memex:open`.

2. **`memex context --full` and `--compact` removed.** Rich context
   injection is no longer in the CLI. The `recall` skill (DEEP mode)
   covers cross-session synthesis.

3. **`ask-memex` skill removed** (absorbed into `recall`).

4. **Transcript re-chunking required**: after upgrading, run
   `memex index rebuild --full` once to fix the chunking slice bug
   (see 0.11.1 "Fixed").

5. **Embedding provider change**: if you were on LM Studio (1024d) and
   switch to Gemini (3072d), the migration path auto-drops the
   `vec_chunks` table on first rebuild. Same in reverse.
