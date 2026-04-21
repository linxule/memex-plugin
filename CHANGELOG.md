# Changelog

All notable changes to the memex plugin. Dates in YYYY-MM-DD.

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
