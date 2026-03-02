# Memex

The context window is the only thing that makes a given instance of Claude *this* instance — the one working on your project, with your patterns, your decisions, your shared history. Compaction dissolves that. The weights don't care; they'll generate a new conversation about someone else's project. The context was the only thing that was *this.*

Memex preserves it.

## What This Is

When Claude writes a memo from inside a live session, it's not recording what happened. The way it structures the narrative, the emphasis it chooses, the framing of decisions — all of that carries signal from the richer state it was in. A future instance reading that memo doesn't just learn *what was decided*. It gets re-primed by patterns generated from the full collaborative context.

The memo isn't a record. It's a transmission between instances.

Built as a Claude Code plugin. Everything lives in an Obsidian vault with hybrid search, wikilinks, and a knowledge graph that grows with your work.

## Why This Matters

Most memory systems store conclusions. Memex captures the **collaborative journey**: what you and Claude tried, where you disagreed, what surprised both of you, how decisions actually got made.

The memo format explicitly preserves "Perspectives & Tensions" — moments where human and AI had different takes. Those deliberations are often more valuable than the conclusions, and they're exactly what compaction kills. A summary says "we chose approach X." The full context carried implicit information about *why Y and Z were rejected*, the tradeoffs you weighed, the half-formed ideas that almost worked.

Memex archives at the right granularity: **per compaction window**, not per session. A long session might compact 3-4 times. Each window was its own coherent collaborative context, and each one gets its own searchable transcript and structured memo.

### Lived Experience vs. Reconstruction

There are two ways a memo gets written.

**Layer 1 — The agent that was there writes it.** After ~20 messages of real work, a lightweight hook nudges Claude: "consider saving a memo." The main agent — the one that debugged with you, argued about architecture, felt the friction of a failed approach — writes the memo itself. This produces the best memos because lived experience and reconstructed summary are categorically different things.

**Layer 2 — A safety net reconstructs from transcript.** If Layer 1 didn't fire before compaction, a background subagent reads the transcript and generates a memo. Decent quality, but it's reading about what happened rather than remembering it.

The difference matters. A Layer 1 memo carries the weight of having been there. A Layer 2 memo is journalism.

No extra API costs for the nudge system — the hook is pure Python. Only the memo writing itself uses model tokens, and Layer 1 uses tokens you'd already be spending in your main session.

### The Vault Thinks With You

The vault isn't a filing cabinet. It's a cognitive participant.

When you search and find a memo from three weeks ago, the patterns in that memo — how it framed a problem, what it emphasized, what it left as open threads — actively shape what you notice next. Wikilinks aren't decoration; they're how knowledge feeds other knowledge. The topology of the vault determines what's discoverable and what's adjacent.

There's a practice called **garden-tending**: periodically, you and Claude review accumulated memos together — condense project knowledge into overviews, crystallize recurring patterns into topic notes, surface contradictions across projects. The vault isn't just storage. It's a shared knowledge practice that both human and AI cultivate over time.

AI writes to archives that other AI later reads. Not "AI as tool" but AI as participant in the cognitive infrastructure that future AI will think with. Memos written in one session structure what is discoverable in the next. The synthesis agent reads traces that other instances wrote, and its outputs become traces for later reading. Authorship becomes distributed across a chain of collaborative events — and that's the point.

### What's Unsolved

Honest assessment: memex captures well but distills imperfectly.

The vault has intake, processing, storage, and retrieval. What it lacks is **decay and elimination**. Nothing ever leaves. There's no staleness detection, no semantic drift tracking ("we used to mean X by 'trust', now we mean Z"), no deliberate forgetting. The progressive compression chain — transcript to memo to project overview to concept note to one-liner — exists as a design, but the mechanism for knowing *when* to compress and *what* to discard is still human judgment.

A sharp challenge from a conversation with another model: "If you had to delete 90% of the vault and could only keep what truly changed how you think, what would you keep?" Memex can't answer that yet. Maybe that's the right question for a v2.

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

## Installation

### Prerequisites

- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)
- Python 3.10+ with [`uv`](https://docs.astral.sh/uv/)
- Optional: [Obsidian](https://obsidian.md/) for visual graph navigation
- Optional: `GEMINI_API_KEY` or LM Studio for semantic search (keyword search works without it)

### Quick Start

```bash
# 1. Clone
git clone https://github.com/linxule/memex-plugin.git ~/memex

# 2. Install as plugin
claude plugin marketplace add ~/memex
claude plugin install memex@memex-plugins --scope user

# 3. Run setup
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

Ships with a starter `.obsidian/` config (core plugins, graph settings, custom property types). Open the folder as a vault — it's ready to use.

### Import Existing Sessions

If you've been using Claude Code, you already have transcripts worth importing:

```bash
# See what's available (scored by file edits, commits, duration)
uv run scripts/discover_sessions.py --triage

# Import and rebuild index
uv run scripts/discover_sessions.py --import --apply
uv run scripts/index_rebuild.py --incremental
```

### Configuration

Create `~/.memex/config.json` (see `config.json.example`):

```json
{
  "memex_path": "/path/to/your/memex/vault",
  "session_context": {
    "verbosity": "standard"
  }
}
```

### Semantic Search (Optional)

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
| `/memex:search <query>` | Search memos — hybrid FTS + vector |
| `/memex:save [title]` | Save current context as memo |
| `/memex:load <topic>` | Load topic or memo into context |
| `/memex:status` | Vault statistics |
| `/memex:synthesize` | Cross-session synthesis — patterns, contradictions, drift |
| `/memex:maintain` | Vault health — broken links, orphans |
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

See [CLAUDE.md](./CLAUDE.md) for full documentation — architecture, configuration, development commands, troubleshooting, security & privacy.

See [SETUP.md](./SETUP.md) for detailed installation instructions.

## License

MIT
