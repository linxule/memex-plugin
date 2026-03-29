# Memex Setup Guide

First-time setup for the memex plugin.

## Prerequisites

- **Claude Code CLI** installed and working
- **Python 3.11+** with `uv` package manager
- **Optional**: `GEMINI_API_KEY` for semantic search (keyword search works without it)
- **Optional**: Obsidian for visual graph navigation

## Quick Install

```bash
# 1. Clone or download memex to your preferred location
git clone https://github.com/linxule/memex-plugin.git ~/memex

# 2. Add as local plugin marketplace
claude plugin marketplace add ~/memex

# 3. Install the plugin
claude plugin install memex@memex-plugins --scope user

# 4. Restart Claude Code to load hooks
# Exit current session (Ctrl+C) and start fresh
claude
```

## What Gets Created

### Plugin State (`~/.memex/`)

```
~/.memex/
├── config.json          # Your settings (create manually, see below)
├── logs/                # Debug logs (auto-created)
├── locks/               # Session locks (auto-created)
├── pending_memos/       # Failed memo queue (auto-created)
└── pending_embeddings.jsonl  # Embedding job queue (auto-created)
```

### Vault Data (`memex/` folder)

```
memex/
├── projects/<name>/memos/       # Session memos per project
├── projects/<name>/transcripts/ # Full conversation logs
├── topics/                      # Cross-project concepts
├── _index.sqlite                # Search index (auto-created)
└── MEMORY.md                    # Global synthesis
```

## Configuration

Create `~/.memex/config.json` to customize settings:

```json
{
  "memex_path": "/path/to/your/memex/vault",
  "session_context": {
    "verbosity": "standard"
  }
}
```

See `~/.memex/config.json.example` in the repo for all options.

### Verbosity Levels

| Level | Token Cost | What's Injected |
|-------|------------|-----------------|
| `minimal` | ~20 | Just "memex available" hint |
| `standard` | ~150 | Project + memo titles + open threads (default) |
| `full` | ~500+ | Full memo content + all context |

## Semantic Search (Optional)

For AI-powered semantic search (finds conceptually similar content):

```bash
# Set Gemini API key
export GEMINI_API_KEY=your-key-here

# Build embeddings
memex index rebuild --full
```

Without the API key, keyword search (FTS5) still works perfectly.

## Verify Installation

```bash
# 1. Check plugin is enabled
claude plugin list | grep memex

# 2. Start a session and check hooks loaded
claude
/hooks  # Should show SessionStart, SessionEnd, PreCompact

# 3. Test search
/memex:status

# 4. Test that hooks work
/compact  # Should generate memo (check projects/*/memos/)
```

## Troubleshooting

### Hooks Not Firing

1. **Restart Claude Code** - Hooks are captured at session startup
2. **Check plugin enabled** - `claude plugin list`
3. **Check hooks registered** - `/hooks` in a session
4. **Run with debug** - `claude --debug` to see hook execution

### Path Issues

If memos save to wrong location:
1. Create `~/.memex/config.json` with explicit `memex_path`
2. Restart Claude Code

### Search Not Finding Content

1. Check index status: `memex status`
2. Rebuild if needed: `memex index rebuild --incremental`
3. For semantic search, ensure `GEMINI_API_KEY` is set

## Uninstall

```bash
# Remove plugin
claude plugin uninstall memex@memex-plugins

# Remove state (optional - keeps your memos)
rm -rf ~/.memex

# Remove vault data (CAUTION - deletes all memos)
rm -rf ~/memex
```

## Next Steps

- Read [CLAUDE.md](./CLAUDE.md) for full documentation
- Run `/memex:status` to see vault statistics
- Use `/memex:search` to find past decisions
- Check `/memex:maintain` for vault health
