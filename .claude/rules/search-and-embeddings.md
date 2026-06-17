---
paths:
  - "scripts/search.py"
  - "scripts/hybrid_search.py"
  - "scripts/embeddings.py"
  - "scripts/index_rebuild.py"
  - "src/memex/scripts/search.py"
  - "src/memex/scripts/hybrid_search.py"
  - "src/memex/scripts/embeddings.py"
  - "src/memex/scripts/index_rebuild.py"
---

# Search & Embeddings

## Embedding Providers

Configure in `~/.memex/config.json`:

**Gemini Embedding 2 (primary):**
```json
{
  "embeddings": {
    "provider": "google",
    "model": "gemini-embedding-2",
    "dimensions": 3072,
    "index_dimensions": 768,
    "api_key_env": "GEMINI_API_KEY"
  }
}
```

**Matryoshka truncation — `index_dimensions` (v0.15.0).** `dimensions` is the *native* model output (3072): what the API returns and what `embedding_cache` stores at full fidelity. `index_dimensions` is the dimension actually stored in the vec0 tables and used for KNN queries. When set below `dimensions`, embeddings are truncated to the first N dims and L2-renormalized (Gemini Embedding 2 is MRL-trained, so this is valid — ~0.26% retrieval-quality loss at 768d for a 4× smaller vector index). Omit it (or set it equal to `dimensions`) for no truncation — the default, fully backward-compatible. Because the cache keeps full 3072d, the choice is reversible: re-run the migration at any dimension, no re-embedding. `embed_query` and the search functions truncate the query vector to match the stored dimension automatically.

To change an existing index's dimension: set `index_dimensions`, then run `memex index migrate-vec` (truncates + adds vec0 metadata columns in place — no API calls). Do NOT run `memex index rebuild` first — the schema's dimension-mismatch guard prints a warning and skips the destructive auto-drop precisely so a truncation never triggers a re-embed.

Note: `output_dimensionality` is not passed to the API — memex always requests the native `dimensions` and truncates locally, which keeps the cache full-fidelity and the dimension choice reversible.

**`task_type` caveat:** Neither `gemini-embedding-2` (GA) nor `gemini-embedding-2-preview` accept the `task_type` parameter. Intent (query vs document) must be encoded in the text itself. `GeminiProvider._build_embed_config()` strips `task_type` for any model whose name starts with `gemini-embedding-2`. Do not add it back. Both model names produce 3072d unit-norm vectors and are interchangeable. Google moved `gemini-embedding-2` to General Availability in April 2026 (public preview began 2026-03-10); `gemini-embedding-2` is now the canonical, no-longer-preview model id and remains the latest/best Google embedding model (MTEB ~68.3) — there is no `gemini-embedding-3` as of mid-2026. The local config flipped from `-preview` to the GA id on 2026-05-07 after smoke-testing both (a config change distinct from Google's GA timeline). The only model-level lever now is dimensionality (Matryoshka truncation to 1536/768), not a new model.

**LM Studio (local fallback):**
```json
{
  "embeddings": {
    "provider": "lmstudio",
    "model": "Qwen3-Embedding-0.6B-GGUF",
    "base_url": "http://localhost:1234/v1",
    "dimensions": 1024
  }
}
```

Switching from LM Studio (1024d) to Gemini (3072d) requires a full rebuild: `memex index rebuild --full`. The dimension migration code auto-detects the change and drops the vec_chunks table.

## Content-Type Chunking

- **Transcripts**: Turn-based chunking (User + Assistant together as semantic unit)
- **Memos**: Whole-doc embedding (already summaries at 500-2000 tokens)
- **Concepts**: Whole-doc embedding
- **Projects** (`_project.md`): Whole-doc embedding
- **Other**: Section-based markdown chunking

## Gotchas

- **Local embedding model size matters** - 8B models take 60+ min for 40K chunks, 0.6B takes ~12 min. Start small, scale up only if quality insufficient
- **LM Studio model ID prefix** - API expects `text-embedding-` prefix: use `text-embedding-qwen3-embedding-0.6b` not `Qwen3-Embedding-0.6B-GGUF`
- **Provider migration workflow** - When switching providers: (1) update config provider + dimensions, (2) run `--full` rebuild (dimension migration auto-detects), (3) test search
- **LM Studio must be running** - If using LM Studio as fallback, vector search requires the app with embedding model loaded. Falls back to FTS-only if unavailable
- **Provider dimension mismatch** - Switching providers with different dimensions (1024↔3072) requires full rebuild with `--full`. The dimension migration code auto-detects and drops vec_chunks table
- **Model filename case sensitivity** - HuggingFace GGUFs use exact case: `Qwen3-Embedding-8B-Q4_K_M.gguf` not lowercase
- **Gemini embedding quota is TPM-bound, not RPM-bound** — Real published numbers: Free=100 RPM / 30K TPM, Tier 1=3K/1M, Tier 2=5K/5M, Tier 3=10K/10M. Free-tier sessions burst past 30K TPM easily on multi-paragraph backfills. `GeminiProvider` uses token-aware batching (`GEMINI_TOKEN_BUDGET_PER_BATCH=8000`, leaves headroom under Gemini's 8192-per-request ceiling) and concurrent dispatch (`asyncio.Semaphore(GEMINI_CONCURRENCY=5)`) — no fixed inter-batch delay; throughput is gated by the semaphore, not by sleep. Source: https://ai.google.dev/gemini-api/docs/rate-limits
- **`PartialEmbeddingFailure` is the retry-exhaustion contract** — After 4 attempts (initial + 3 retries with 10s/30s/90s backoff ±20% jitter) on 429/500/503, `GeminiProvider.embed_texts` raises `PartialEmbeddingFailure(results=partial_list, last_error=exc)`. Results are positionally aligned to the input `texts` (failed slots are `None`). Callers of `embed_texts` must handle it. `EmbeddingPipeline.embed_text` (single-text wrapper) catches it and degrades to `None` for graceful query-path behavior. `EmbeddingPipeline.embed_chunks` catches it, caches whatever succeeded, and logs failures to stderr. Non-retryable `ClientError` (4xx non-429) also wraps as `PartialEmbeddingFailure` — callers only need to handle one exception type
- **`embed_texts` is sync but works inside an event loop** — The public `GeminiProvider.embed_texts` is synchronous. Internally it drives `_aembed_texts` via `asyncio.run` for the common (no-loop) caller, or punts to a worker-thread fresh loop when invoked from inside a running event loop (e.g., `scripts/mcp_server.py` async tool handlers). Either entry point preserves the same return contract
- **Verification script** — `scripts/verify_embedding_retry.py` exercises the batch + retry + typed-exception paths against monkey-patched fakes (no real API, no real sleeps). Run with `uv run scripts/verify_embedding_retry.py`. Five scenarios: clean batch, 429 twice then succeed, persistent 429, concurrent multi-batch, sync-inside-running-loop. Use it as a regression check before touching the embedding path
- **Archived files excluded from index** - Documents with `status: archived` in frontmatter are skipped during index rebuild. Change status to `active` and run `--incremental` to re-index
- **`_project.md` included despite `_` prefix** - Special-cased in `find_documents()`. Other `_*` files (templates, views) remain excluded
- **FTS is instant, vector is batched** - New memos are keyword-searchable immediately, but need `--incremental` for semantic search
- **sqlite-vec must be loaded** - Vector queries fail silently without the extension; scripts handle this automatically
- **vec0 metadata filter-pushdown (v0.15.0)** — `vec_chunks`/`vec_observations` carry `doc_project text, doc_type text, doc_date integer` metadata columns. `vector_search()` pushes `--type`/`project`/`--since`/`--before` filters INTO the KNN (`WHERE v.doc_project = ? AND v.doc_date >= ?`) instead of over-fetching `limit*3` candidates and post-filtering. This fixes recall-collapse: a narrow `--since=7d` used to discard the whole candidate window when the top semantic hits were old. `doc_date` is an integer `YYYYMMDD` (range filters need INTEGER — sqlite-vec TEXT metadata only supports `=`/`IN`). **sqlite-vec rejects NULL for TEXT metadata columns** — every insert/preservation path must pass `""`, never `None`. Indexes created before v0.15.0 lack these columns; `vector_search` catches the `OperationalError` and falls back to bare KNN + post-filter, so search keeps working until `memex index migrate-vec` runs.
- **FTS needs keywords, not questions** - "Why did we choose X?" won't match; use `X OR related-term`
- **FTS5 treats hyphens as column operators** - `predictive-ai` is parsed as column `predictive`, term `ai` → "no such column" error. `search.py` sanitizes via `sanitize_fts_query()` (strips punctuation, joins with OR). If bypassing `search.py` with raw SQL, quote or strip hyphens manually
- **Presence vs score** - Don't use `score > 0` to check if a search matched; normalized scores can be 0 for worst-but-valid matches. Use presence flags instead
- **Embedding queue removed in v0.11.0** — `enqueue_embedding_job` / `dequeue_embedding_jobs` / `get_embedding_queue_count` and the `~/.memex/pending_embeddings.jsonl` file are gone. They were dead code (zero call sites) and duplicated the purpose of `memex index embed-missing`, which uses LEFT JOIN over `chunks` / `vec_chunks` as the single source of truth for "what still needs embedding." See `utils.py` for the tombstone comment.
- **`memex index embed-missing` is the retry command** — if a rebuild or `backfill obs` run inserted FTS/chunks/observations but failed the embed step (expired API key, rate limit), rows land in `chunks`/`observations` without matching rows in `vec_chunks`/`vec_observations`. `memex index embed-missing` finds the gap via LEFT JOIN and embeds what's missing. Idempotent. Implemented in `reembed_missing()` in `index_rebuild.py`. Rebuild output + `memex index status` both now surface gaps in a warning block so they don't accumulate silently.
- **Rebuild exit code does NOT reflect embedding failures** — `rebuild_incremental` / `rebuild_full` return exit 0 even when every embedding in a batch fails. `PartialEmbeddingFailure` is logged to stderr only. The `embedding_gaps` field in stats + the warning block in `format_rebuild_stats` are the actionable signals. Don't treat exit 0 as "fully embedded."
- **fts_content schema is limited** - Only has: `path, title, content, type, project, date`. No `messages` or `has_memo` - use file size as proxy for transcript value
- **Dual Gemini/Google API key warning** — `google-genai` SDK emits stderr noise when both `GOOGLE_API_KEY` and `GEMINI_API_KEY` are set. `embeddings.py` handles this with env var stash/restore in `_get_client()` — don't remove that workaround
- **Unit-norm invariant on provider output (v0.11.1)** — `GeminiProvider.embed_texts` and `LMStudioProvider.embed_texts` call `_assert_unit_norm(batch, provider)` which samples {head, mid, tail} of each batch and raises `ValueError` if any is not unit-norm within ±0.02 on ||v||². sqlite-vec uses L2 distance by default; rankings are only monotonic with cosine when inputs are normalized. If you add a new provider, call `_assert_unit_norm` at the end of its `embed_texts` method. Tests: `tests/test_embedding_norm.py`
- **chunk_transcript_turns slice bug (fixed v0.11.1)** — For any transcript indexed before commit `46cccfd`, the turn chunks were wrong: the function used `parts[1::2]` with a non-capturing regex, so it silently dropped every other turn and misaligned surviving headers with the wrong bodies. After pulling the fix, run `memex index rebuild --full` to re-chunk every transcript with correct semantics. Regression pinned in `tests/test_chunking.py`
- **Shared connection helper: `memex.db_utils`** — All index writers (and most readers) now go through `memex.db_utils.connect_index(index_path)` which applies `PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA busy_timeout=10000`. Never bare-`sqlite3.connect(index_path)` — writers without WAL collide with `memex backfill obs --stdin`. `memex.db_utils.load_vec_extension(conn)` is the matching helper for sqlite-vec loading; it clears `enable_load_extension=True` via try/finally even on load failure so the connection never stays in the privileged state
- **`index_document` / `embed_chunks` do NOT commit (v0.11.1)** — Transaction boundaries belong to the caller. `rebuild_full` and `rebuild_incremental` wrap each doc in `SAVEPOINT doc` → work → `RELEASE SAVEPOINT doc` (or `ROLLBACK TO SAVEPOINT doc` on exception), with a single `conn.commit()` at the end of the batch. `_rollback_savepoint_or_die` / `_release_savepoint_if_exists` helpers split the cleanup so a real bug on ROLLBACK surfaces while a benign "already-gone" on RELEASE is tolerated. Single-file CLI callers commit at the end themselves
