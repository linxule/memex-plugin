# Memex Setup Guide

First-time setup for the memex plugin.

## Prerequisites

- **Claude Code CLI** installed and working
- **Python 3.10+** with `uv` package manager
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
├── pending-memos/       # Failed memo queue (auto-created)
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

See `config.json.example` in the repo for all options.

### Verbosity Levels

| Level | Token Cost | What's Injected |
|-------|------------|-----------------|
| `minimal` | ~20 | Just "memex available" hint |
| `standard` | ~150 | Project + memo titles + open threads (default) |
| `full` | ~500+ | Full memo content + all context |

## Obsidian Setup

The repo ships with a starter `.obsidian/` config including:
- Core plugins enabled (graph, backlinks, tags, outline, templates)
- Dataview community plugin recommended
- Custom property types for memex frontmatter
- Graph view settings (hide unresolved, hide orphans)

To use:
1. Open Obsidian
2. "Open folder as vault" → select your memex directory
3. Trust the vault when prompted
4. Install the Dataview community plugin (recommended for dashboards)

## Semantic Search (Optional)

For AI-powered semantic search (finds conceptually similar content):

### Option A: LM Studio (Fully Local)

1. Install [LM Studio](https://lmstudio.ai)
2. Load the `Qwen3-Embedding-0.6B` model
3. Start the local server (runs at `http://localhost:1234`)

### Option B: Gemini API

```bash
# Set Gemini API key
export GEMINI_API_KEY=your-key-here
```

Then build embeddings:
```bash
cd ~/memex
uv run scripts/index_rebuild.py --full
```

Without either option, keyword search (FTS5) still works.

## Verify Installation

```bash
# 1. Check plugin is enabled
claude plugin list | grep memex

# 2. Run setup check
cd ~/memex
uv run scripts/setup.py --check

# 3. Start a session and check hooks loaded
claude
/hooks  # Should show SessionStart, SessionEnd, PreCompact

# 4. Test status
/memex:status
```

## Troubleshooting

### Hooks Not Firing

1. **Restart Claude Code** — Hooks are captured at session startup
2. **Check plugin enabled** — `claude plugin list`
3. **Check hooks registered** — `/hooks` in a session
4. **Run with debug** — `claude --debug` to see hook execution

### Path Issues

If memos save to wrong location:
1. Create `~/.memex/config.json` with explicit `memex_path`
2. Restart Claude Code

### Search Not Finding Content

1. Check index status: `uv run scripts/index_rebuild.py --status`
2. Rebuild if needed: `uv run scripts/index_rebuild.py --incremental`
3. For semantic search, ensure embedding provider is configured

## Uninstall

```bash
# Remove plugin
claude plugin uninstall memex@memex-plugins

# Remove state (optional — keeps your memos)
rm -rf ~/.memex

# Remove vault data (CAUTION — deletes all memos)
rm -rf ~/memex
```

## Import Existing Sessions

If you've been using Claude Code already, you have transcripts in `~/.claude/projects/` that can be imported into your vault — giving you an instant searchable archive of your prior work.

```bash
# See what's available (scored by value: file edits, commits, duration)
uv run scripts/discover_sessions.py --triage

# Preview high-value sessions
uv run scripts/discover_sessions.py --triage --min-score=9 -v

# Import all (dry-run first)
uv run scripts/discover_sessions.py --import

# Apply the import
uv run scripts/discover_sessions.py --import --apply

# Rebuild index to make imported transcripts searchable
uv run scripts/index_rebuild.py --incremental
```

The triage scorer considers file edits, git commits, session duration, and conversation depth to rank which sessions are worth importing. Start with high-value ones (`--min-score=9`) if you have many.

## Next Steps

- Read [CLAUDE.md](./CLAUDE.md) for full documentation
- Run `/memex:status` to see vault statistics
- Use `/memex:search` to find past decisions
- Check `/memex:maintain` for vault health
