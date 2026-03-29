---
paths:
  - "src/memex/**/*.py"
  - "scripts/**/*.py"
  - "hooks/**/*.py"
  - ".claude-plugin/**"
---

# Configuration

## File Locations

| Path | Purpose |
|------|---------|
| `~/.memex/config.json` | All configuration (weights, limits, embeddings) |
| `<vault>/prompts/memo-default.md` | Rich memo prompt for background subagent fallback |
| `~/.memex/logs/` | Nightly rebuild logs, hook logs |
| `~/.memex/locks/` | Session and index locks |
| `~/.memex/pending_embeddings.jsonl` | Embedding queue for batch processing |

## Path Resolution

The memex vault path is resolved in this order:

1. **`~/.memex/config.json` → `memex_path`** (user override, highest priority)
2. **Script location fallback** (assumes scripts are in `memex/scripts/`)

**WARNING:** `CLAUDE_PLUGIN_ROOT` points to the plugin cache (`~/.claude/plugins/cache/`), NOT the vault. Never use it for vault path resolution in commands or skills. Hooks use `__file__`-relative paths instead.

For new users, create `~/.memex/config.json`:
```json
{
  "memex_path": "/path/to/your/memex/vault"
}
```

## Session Context Verbosity

Control how much context is injected at SessionStart (affects token usage):

```json
{
  "session_context": {
    "verbosity": "standard"
  }
}
```

| Level | What's Injected | Token Cost | Use Case |
|-------|-----------------|------------|----------|
| `minimal` | "Memex available" hint only | ~20 | Quick tasks, minimal overhead |
| `standard` | Project + 3 memo titles + open thread count + graph summary | ~150 | **Default** - balanced awareness |
| `full` | Full memo summaries + all open threads + recent decisions | ~500+ | Deep context sessions |

**Post-compact behavior:** After compaction, minimal context is injected ("Session compacted. Use /memex:search...") regardless of verbosity level. Claude can search on-demand to recall prior context.

## Linking Conventions

Use Obsidian wikilinks for cross-references:
- `[[topic-name]]` - Link to topic in topics/
- `[[projects/myproject/memos/memo-name]]` - Link to specific memo
- `[[projects/myproject/_project|My Project]]` - Link with alias
- `[[?new-concept]]` - Suggest new concept (doesn't exist yet)

## Security & Privacy

**What data is stored:**
- **Memos** - Summaries of session decisions, learnings, open threads (auto-generated)
- **Transcripts** - Full conversation history in JSONL + markdown format
- **Search index** - FTS5 + vector embeddings for search

**Where it's stored:**
- All data stays local in the memex vault (path configured in `~/.memex/config.json`)
- Plugin state in `~/.memex/` (session tracking, config)
- No data is sent externally except:
  - Anthropic API calls for memo generation (uses your existing Claude session)
  - Gemini API calls for embeddings (only if using `provider: "google"`)
  - **With LM Studio provider (if configured), all embedding processing stays fully local**

**Access controls:**
- Local filesystem permissions apply
- Transcripts excluded from git by default (see `.gitignore`)
- No authentication layer - anyone with filesystem access can read

**Privacy note:**
Transcripts contain your full conversation history, which may include sensitive information discussed during sessions. Consider what you discuss before enabling memex. Transcripts are stored in `projects/<name>/transcripts/` and excluded from git.
