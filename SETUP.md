# Memex Setup Guide

First-time setup for the memex plugin.

## Prerequisites

- **Claude Code CLI** installed and working
- **Python 3.11+** with `uv` package manager
- **Optional**: `GEMINI_API_KEY` for semantic search (keyword search works without it)
- **Optional**: Obsidian for visual graph navigation

## Quick Install

Every slash command (`/memex:status`, `/memex:save`, `/memex:open`) shells out to the `memex` CLI, so install the CLI **first** — otherwise the first slash command will fail with "command not found".

```bash
# Step 1: Install the memex CLI (do this BEFORE installing the plugin)
uv tool install git+https://github.com/linxule/memex-plugin.git

# Step 2: Add marketplace and install the plugin (inside a Claude Code session)
/plugin marketplace add linxule/memex-plugin
/plugin install memex@memex-plugins --scope user

# Step 3: Restart Claude Code to load hooks
# Exit current session (Ctrl+C) and start fresh
claude
```

Or from a local clone (if you want to customize or develop):

```bash
git clone https://github.com/linxule/memex-plugin.git ~/memex

# Step 1: Install the CLI from the local checkout
cd ~/memex && uv tool install .

# Step 2: Add marketplace and install the plugin
/plugin marketplace add ~/memex
/plugin install memex@memex-plugins --scope user

# Step 3: Restart Claude Code to load hooks
claude
```

## What Gets Created

### Plugin State (`~/.memex/`)

```
~/.memex/
├── config.json          # Your settings (create manually, see below)
├── logs/                # Debug logs + nightly rebuild output (auto-created)
├── locks/               # Session locks (auto-created)
└── pending_memos/       # Failed memo queue (auto-created)
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
  "embeddings": {
    "provider": "google",
    "model": "gemini-embedding-2",
    "dimensions": 3072,
    "api_key_env": "GEMINI_API_KEY"
  }
}
```

See `config.json.example` in the repo for all options.

Retrieval is **skill-based** — Claude invokes the `recall` skill automatically
when you ask about past work. There's no "verbosity" setting to tune; skills
decide how much to load based on the question. Use `memex search`,
`memex timeline`, and `memex ask` from the CLI when you want direct access.

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
/hooks  # Should show SessionStart, UserPromptSubmit, SessionEnd, PreCompact

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

## Nightly Rebuild (Optional)

Re-chunk new docs and retry any embeddings that failed during the day.
A generic `scripts/nightly-rebuild.sh` is shipped — wire it into launchd
(macOS) or cron.

The shell script already handles sourcing `~/.secrets` for your
`GEMINI_API_KEY`, falls back to FTS-only if unavailable, and retries
missing embeddings via `memex index embed-missing`. Exit codes:
`0` = clean, `1` = rebuild failed, `2` = embed gaps remain, `3` = secrets
file exists but couldn't be sourced.

Create `~/Library/LaunchAgents/com.<yourname>.memex.nightly.plist`
(adjust paths):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.YOURNAME.memex.nightly</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-l</string>
        <string>/ABSOLUTE/PATH/TO/memex/scripts/nightly-rebuild.sh</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>0</integer></dict>
    <key>StandardOutPath</key>
    <string>/Users/YOURNAME/.memex/logs/nightly-rebuild.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOURNAME/.memex/logs/nightly-rebuild-error.log</string>
    <key>ProcessType</key><string>Background</string>
    <key>LowPriorityIO</key><true/>
    <key>Nice</key><integer>10</integer>
</dict>
</plist>
```

Install: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.YOURNAME.memex.nightly.plist`

Test immediately: `launchctl kickstart gui/$(id -u)/com.YOURNAME.memex.nightly`

On Linux, a cron entry like `0 3 * * * /path/to/scripts/nightly-rebuild.sh`
achieves the same.

## Next Steps

- Read [CLAUDE.md](./CLAUDE.md) for full documentation
- Run `memex status` to see vault statistics
- Run `memex search "<query>"` to find past decisions
- Ask Claude a question like "what did I work on yesterday?" — the
  `recall` skill handles retrieval automatically
