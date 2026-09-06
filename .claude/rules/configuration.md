---
paths:
  - "scripts/**/*.py"
  - "src/memex/**/*.py"
  - "hooks/**/*.py"
  - ".claude-plugin/**"
---

# Configuration

## File Locations

| Path | Purpose |
|------|---------|
| `~/.memex/config.json` | All configuration (weights, limits, embeddings) |
| `<vault>/skills/memo-writing/memo-default.md` | Rich memo prompt for background subagent fallback |
| `~/.memex/logs/` | Nightly rebuild logs, hook logs |
| `~/.memex/locks/` | Session and index locks |
| `~/.memex/pending-memos/` | PreCompact signal files for orphan-memo retry |
| `<state_dir>/credentials/gemini-api-key` | Optional owner-only local key, saved explicitly with `memex auth set-key` |

## Gemini Credentials

Missing-key messages offer an explicit `op run --env-file ~/.secrets.op -- memex search "query"`
command. Memex never invokes 1Password itself. `memex auth set-key` is an opt-in
alternative that saves a local unencrypted `0600` file for automatic loading.
`--from-env` explicitly persists an already injected environment key. Environment
variables take precedence over the saved file; `memex auth status` reports only
the source, and `memex auth clear-key` removes the saved copy. See
[setup details](../../docs/gemini-credentials.md). `embeddings.enabled=false`
skips provider and credential initialization entirely.

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

## Index Location

`_index.sqlite` (FTS5 + vectors, multi-GB) is deliberately kept **outside the vault**: vaults live in iCloud/Dropbox, and a WAL-mode sqlite file in a synced folder re-uploads on every write and spawns `_index 2.sqlite-wal` conflict copies. `memex.paths.get_index_path()` resolves, for the configured vault:

1. `index_path` in config.json / `MEMEX_INDEX_PATH`
2. `<state_dir>/_index.sqlite` if it exists
3. `<vault>/_index.sqlite` if it exists (legacy layout)
4. `<state_dir>/_index.sqlite` (fresh installs)

Migrating a legacy in-vault index: `mv <vault>/_index.sqlite* ~/.memex/` (same volume → instant). Non-configured vaults (tests, `--vault`) always use in-vault, so tests never touch the live index.

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
