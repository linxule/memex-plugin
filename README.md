# Memex

Collaborative memory for human-AI work — persistent, searchable, interconnected.

## What It Is

When you work with Claude Code, something valuable happens between you: decisions get made, approaches get debated, ideas get refined through back-and-forth. Memex captures that collaborative process — not just outcomes, but the deliberation — and makes it searchable across sessions. Built as an Obsidian vault with hybrid search, wikilinks, and a knowledge graph that grows with you.

## The Philosophy

Memos aren't activity logs. They capture the **collaborative journey**: what you and Claude tried, where you disagreed, what surprised both of you, how decisions actually got made. The memo format explicitly preserves "Perspectives & Tensions" — moments where human and AI had different takes — because those deliberations are often more valuable than the conclusions.

Then there's **garden-tending**: you and Claude periodically review accumulated memos together, condense project knowledge into overviews, crystallize recurring patterns into topic notes, and surface contradictions across projects. The vault isn't just storage — it's a shared knowledge practice that both human and AI cultivate over time.

## How It Complements Claude's Built-in Memory

Claude Code's native auto-memory stores preferences and conventions — "always use uv", "prefer Sonnet for quick tasks." Think of it as **working memory**: how you work.

Memex is **collaborative long-term memory**: what you've worked on together, how you got there, and what's still open.

| | Auto-memory (built-in) | Memex |
|---|---|---|
| **Scope** | Session-scoped preferences | Cross-session archive |
| **Captures** | Conventions, patterns | Full transcripts + structured memos |
| **Granularity** | Key-value pairs | Per-compaction-window transcripts |
| **Search** | Exact match | Hybrid FTS + semantic |
| **Answers** | "What does this user prefer?" | "Why did we choose this approach 3 weeks ago?" |

The key: memex archives a transcript for **every compaction window** (when Claude Code automatically summarizes a long conversation to free up context space), not just per session. A long session might compact 3-4 times — each window gets its own searchable transcript and memo. Your entire collaborative history is preserved — the debates, the pivots, the breakthroughs.

## How It Works

### Two-Layer Memo Generation

**Layer 1 — Proactive Save (best quality):**
The `UserPromptSubmit` hook is a lightweight Python script — no model, no API calls — that counts messages per session. After ~20 messages, it prints a one-line nudge to stdout. Claude Code injects this into the conversation as a system reminder, and the **main agent** (whatever model you're running — Opus, Sonnet, etc.) sees it and decides to run `/memex:save`. The main agent writes the memo itself, with full experiential context — it was *there*. This produces the best memos.

**Layer 2 — Background Safety Net (Haiku):**
If Layer 1 didn't fire before context compaction, the `PreCompact` hook writes a signal file. On the next session start, `SessionStart` detects it and instructs the main agent to spawn a **background Haiku subagent** to read the transcript and generate a reconstructed memo. Cheaper and decent quality, but working from transcript rather than lived experience.

**No extra API costs for the nudge system** — the hook is pure Python. Only the memo writing itself uses model tokens, and Layer 1 uses tokens you'd already be spending in your main session.

### Search Pipeline

Queries combine FTS5 keyword matching (BM25) with vector embeddings (semantic similarity) using Reciprocal Rank Fusion (RRF, k=60). This means searching "auth" finds both exact keyword matches and conceptually related content like "login flow."

## Features

- **Full transcript archival**: Every compaction window archived as searchable markdown — nothing lost
- **Auto-save**: Memos generated proactively during sessions and as safety net before compaction
- **Hybrid search**: FTS5 keywords + vector embeddings with RRF fusion
- **Cross-project synthesis**: Find patterns, contradictions, and drift across all your work
- **Obsidian integration**: Wikilinks, graph view, Dataview queries
- **Session lifecycle**: Hooks for SessionStart, SessionEnd, PreCompact, UserPromptSubmit
- **Knowledge gardening**: Condense memos into project overviews, crystallize recurring concepts into topic notes

## Installation

### Prerequisites

- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)
- Python 3.10+ with [`uv`](https://docs.astral.sh/uv/)
- Optional: [Obsidian](https://obsidian.md/) for visual graph navigation
- Optional: `GEMINI_API_KEY` for semantic search (keyword search works without it)

### Quick Start

```bash
# 1. Clone to your preferred location
git clone https://github.com/linxule/memex-plugin.git ~/memex

# 2. Add as plugin marketplace and install
claude plugin marketplace add ~/memex
claude plugin install memex@memex-plugins --scope user

# 3. Run setup wizard
cd ~/memex
uv run scripts/setup.py

# 4. Restart Claude Code to load hooks
claude
```

For quick testing without persistent install:
```bash
claude --plugin-dir ~/memex
```

### Open in Obsidian

The repo ships with a starter `.obsidian/` config (core plugins, graph settings, custom property types). Open the folder as a vault in Obsidian — it's ready to use.

### Import Existing Sessions

If you've been using Claude Code, you already have transcripts worth importing:

```bash
# See what's available (scored by file edits, commits, duration)
uv run scripts/discover_sessions.py --triage

# Import and rebuild index
uv run scripts/discover_sessions.py --import --apply
uv run scripts/index_rebuild.py --incremental
```

### Verify Installation

```bash
# Check hooks are loaded
/hooks  # Should show SessionStart, SessionEnd, PreCompact

# Check plugin status
/memex:status

# Test search
/memex:search "your query here"
```

### Configuration

Create `~/.memex/config.json` to customize (see `config.json.example`):

```json
{
  "memex_path": "/path/to/your/memex/vault",
  "session_context": {
    "verbosity": "standard"
  }
}
```

### Semantic Search (Optional)

For AI-powered semantic search that finds conceptually similar content:

```bash
# Option A: LM Studio (fully local, recommended)
# Install LM Studio, load Qwen3-Embedding-0.6B, start server

# Option B: Gemini API
export GEMINI_API_KEY=your-key

# Build embeddings
uv run scripts/index_rebuild.py --full
```

Without embeddings, keyword search (FTS5) still works.

## Commands

| Command | Description |
|---------|-------------|
| `/memex:search <query>` | Search memos with hybrid FTS + vector |
| `/memex:save [title]` | Save current context as memo |
| `/memex:load <topic>` | Load topic or memo into context |
| `/memex:status` | Show vault statistics |
| `/memex:synthesize` | Cross-session synthesis (patterns, contradictions, drift) |
| `/memex:maintain` | Check vault health (broken links, orphans) |
| `/memex:merge` | Synthesize multiple memos into concept note |
| `/memex:open` | Open vault in Finder/Obsidian |
| `/memex:retry` | Retry failed memo generations |

## Automatic Behavior

| Hook | When | What |
|------|------|------|
| SessionStart | New session | Loads project context, recent memos, open threads |
| UserPromptSubmit | Each message | Tracks activity, nudges to save after ~20 messages |
| SessionEnd | Session closes | Archives transcript |
| PreCompact | Before compaction | Writes signal file for safety-net memo generation |

## Vault Structure

After using memex for a while, your vault grows organically:

```
memex/
├── projects/<name>/memos/       # Session memos per project
├── projects/<name>/transcripts/ # Full conversation logs
├── topics/                      # Cross-project concept notes
├── _templates/                  # Note templates
├── _index.sqlite                # Search index (auto-generated)
└── MEMORY.md                    # Global synthesis & preferences
```

## Documentation

See [CLAUDE.md](./CLAUDE.md) for full documentation including:
- Architecture details
- Configuration options
- Development commands
- Troubleshooting
- Security & privacy

See [SETUP.md](./SETUP.md) for detailed installation instructions.

## Requirements

- Claude Code CLI
- Python 3.10+ with `uv`
- Optional: Obsidian for visual navigation
- Optional: `GEMINI_API_KEY` or LM Studio for semantic search

## License

MIT
