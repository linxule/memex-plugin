---
paths:
  - "scripts/search.py"
  - "scripts/hybrid_search.py"
  - "scripts/embeddings.py"
  - "scripts/index_rebuild.py"
---

# Search & Embeddings

## Embedding Providers

Configure in `~/.memex/config.json`:

**LM Studio (local, recommended):**
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

**Gemini API (cloud, fallback):**
```json
{
  "embeddings": {
    "provider": "google",
    "model": "gemini-embedding-001",
    "dimensions": 3072,
    "api_key_env": "GEMINI_API_KEY"
  }
}
```

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
- **LM Studio must be running** - Vector search requires LM Studio with embedding model loaded. Falls back to FTS-only if unavailable. Start with: LM Studio app or `lms server start` in headless mode
- **Provider dimension mismatch** - Switching providers with different dimensions (1024↔3072) requires full rebuild with `--full`. The dimension migration code auto-detects and drops vec_chunks table
- **Model filename case sensitivity** - HuggingFace GGUFs use exact case: `Qwen3-Embedding-8B-Q4_K_M.gguf` not lowercase
- **Gemini Tier 2 TPM limit** - 5M tokens/min is the bottleneck, not 5K RPM. With 100-chunk batches (~40K tokens), need 500ms inter-batch delay. (Only relevant if using `provider: "google"`)
- **Archived files excluded from index** - Documents with `status: archived` in frontmatter are skipped during index rebuild. Change status to `active` and run `--incremental` to re-index
- **`_project.md` included despite `_` prefix** - Special-cased in `find_documents()`. Other `_*` files (templates, views) remain excluded
- **FTS is instant, vector is batched** - New memos are keyword-searchable immediately, but need `--incremental` for semantic search
- **sqlite-vec must be loaded** - Vector queries fail silently without the extension; scripts handle this automatically
- **FTS needs keywords, not questions** - "Why did we choose X?" won't match; use `X OR related-term`
- **Presence vs score** - Don't use `score > 0` to check if a search matched; normalized scores can be 0 for worst-but-valid matches. Use presence flags instead
- **Embedding queue** - New memos are FTS-indexed immediately; embeddings queued to `~/.memex/pending_embeddings.jsonl` for batch processing
- **fts_content schema is limited** - Only has: `path, title, content, type, project, date`. No `messages` or `has_memo` - use file size as proxy for transcript value
