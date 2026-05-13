# Changelog

All notable changes to the memex plugin. Dates in YYYY-MM-DD.

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
  opt-in — old archives without it keep current behavior (signals
  silently land on the archived stub). To start routing signals to
  canonical replacements, add `redirect_to:` to archive frontmatter as
  you encounter them during garden-tending.
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
