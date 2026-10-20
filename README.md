# Memex

[![PyPI](https://img.shields.io/pypi/v/memex-plugin)](https://pypi.org/project/memex-plugin/)

[Install the CLI](#installation) · [PyPI package](https://pypi.org/project/memex-plugin/) · [Latest release](https://github.com/linxule/memex-plugin/releases/latest) · [Setup guide](./SETUP.md)

Memex saves what you and Claude worked out in each session as searchable notes, so the next session starts where the last one stopped.

The context window is the only thing that makes a given instance of Claude "this" instance. It is the one working on your project, with your patterns, your decisions and your shared history. Compaction dissolves that. The weights do not care. They will generate a new conversation about someone else's project. The context was the only thing that was "this".

Memex preserves it.

## What this is

When Claude writes a memo from inside a live session, it is not just recording what happened. How it structures the story, what it emphasizes and how it frames decisions all carry signal from the richer state it was in. A future instance reading that memo learns more than what was decided. It is re-primed by patterns that came from the full collaborative context.

The memo is not a record. It is a transmission between instances.

Memex is a command-line tool, `memex-plugin` on PyPI, with plugins for Claude Code, Codex and Kimi Code:

- memos and transcripts live in a local vault that works with Obsidian, with hybrid search, wikilinks and a knowledge graph that grows with your work
- configuration and the search index, which can always be rebuilt, live separately in `~/.memex/` by default
- Claude Code gets the full integration, with hooks and slash commands
- Codex and Kimi Code use the [portable skills](#codex-and-kimi-code)
- optionally, [memex cloud](#remote-access-memex-cloud) puts a searchable copy on your own Cloudflare account, so claude.ai and other remote agents can use it too

## Why this matters

Most memory systems store conclusions. Memex captures the collaborative journey: what you and Claude tried, where you disagreed, what surprised you both, and how decisions actually got made.

Every memo has a "Perspectives & Tensions" section for moments when you and Claude saw things differently. Those deliberations are often worth more than the conclusions, and they are exactly what compaction destroys. A summary says "we chose approach X". The full context also carried why Y and Z were rejected, the tradeoffs you weighed and the half-formed ideas that almost worked.

Memex archives each compaction window, not each session. A long session might compact 3 to 4 times. Each window was its own coherent context, so each one gets its own searchable transcript and structured memo.

### Lived experience and reconstruction

A memo gets written in one of 2 ways.

#### Layer 1: the agent that was there writes it

After about 20 messages of real work, a lightweight hook nudges Claude to consider saving a memo. The main agent writes it: the one that debugged with you, argued about architecture and felt the friction of a failed approach. These are the best memos, because lived experience and a reconstructed summary are different in kind.

#### Layer 2: a safety net reconstructs it from the transcript

If Layer 1 did not happen before compaction, a background subagent reads the transcript and writes a memo. The quality is decent, but the subagent is reading about what happened, not remembering it.

The difference matters. A Layer 1 memo carries the weight of having been there. A Layer 2 memo is journalism.

The nudge makes no extra API calls, because the hook is plain Python. Only writing the memo uses model tokens, and Layer 1 uses tokens your main session would spend anyway.

### The vault thinks with you

The vault is not a filing cabinet. It takes part in the thinking.

When you find a memo from 3 weeks ago, its patterns shape what you notice next. That includes how it framed a problem, what it emphasized and what it left open. Wikilinks are not decoration. They are how one piece of knowledge feeds another. The shape of the vault decides what is easy to find and what sits next to what.

Garden-tending is the practice of reviewing the vault with Claude from time to time. Together you condense project knowledge into overviews, turn recurring patterns into topic notes and surface contradictions across projects. That makes the vault a shared practice that you and Claude grow over time, not just storage.

AI writes to archives that later AI reads. AI is then not just a tool but a participant in the infrastructure that future AI thinks with. Memos written in one session shape what the next session can find. The synthesis agent reads traces that other instances wrote, and its output becomes a trace for later reading. Authorship is spread across a chain of collaborative events, and that is the point.

### What is still unsolved

Memex captures well but distills imperfectly.

The vault has intake, processing, storage and retrieval. It has no decay or elimination: nothing ever leaves. It does not detect staleness, track semantic drift ("we used to mean X by 'trust', now we mean Z") or forget on purpose. A compression chain exists as a design: transcript, then memo, project overview, concept note and one-liner. Deciding when to compress and what to discard is still a human judgment.

Another model once asked: "If you had to delete 90% of the vault and could only keep what truly changed how you think, what would you keep?" Memex cannot answer that yet. Maybe that is the right question for a version 2.

## How memex complements Claude's built-in memory

Claude Code's built-in auto-memory stores preferences and conventions, such as "always use uv" or "prefer Sonnet for quick tasks". It is working memory: how you work.

Memex is collaborative long-term memory: what you have worked on together, how you got there and what is still open.

| | Auto-memory (built-in) | Memex |
|---|---|---|
| Scope | Preferences for each session | An archive across sessions |
| Captures | Conventions and patterns | Full transcripts and structured memos |
| Granularity | Key-value pairs | A transcript for each compaction window |
| Search | Exact match | Hybrid: full-text and semantic |
| Answers | "What does this user prefer?" | "Why did we choose this approach 3 weeks ago?" |

## Installation

### Prerequisites

You need:

- Python 3.11 or later, with [`uv`](https://docs.astral.sh/uv/)
- Claude Code, Codex or Kimi Code, if you want the plugin (the CLI on its own needs no agent host)

Optional:

- [Obsidian](https://obsidian.md/), to navigate the knowledge graph visually
- a Gemini API key or LM Studio, for semantic search (keyword search works without either)

You can save a Gemini key once with `memex auth set-key` or set it as an environment variable.

### Quick start

Install the CLI first. Every slash command calls the `memex` CLI, so without it `/memex:status` and the other commands fail with "command not found". The CLI also works on its own, so any agent with a shell can use it to search, inspect and maintain a vault.

```bash
# Step 1: install the memex CLI (before installing the plugin)
uv tool install memex-plugin

# Step 2: add the marketplace and install the plugin (inside a Claude Code session)
/plugin marketplace add linxule/memex-plugin
/plugin install memex@memex-local --scope user

# Step 3: restart Claude Code to load the hooks
claude
```

To install from a local clone, for development or customization:

```bash
git clone https://github.com/linxule/memex-plugin.git ~/memex

# Step 1: install the CLI from the local checkout
cd ~/memex && uv tool install .

# Step 2: add the marketplace and install the plugin
/plugin marketplace add ~/memex
/plugin install memex@memex-local --scope user

# Step 3: restart Claude Code to load the hooks
claude
```

To try it without installing:

```bash
claude --plugin-dir ~/memex
```

### Update or migrate the CLI

The [PyPI distribution](https://pypi.org/project/memex-plugin/) is called `memex-plugin`. The command and the Python import are still `memex`. The unrelated PyPI project called `memex` is not this project.

If you installed memex before 0.20.1 with `uv tool install git+https://github.com/linxule/memex-plugin.git`:

1. Run `uv tool list` and check that it shows that Git source under `memex`.
2. Replace only that tool environment:

   ```sh
   uv tool uninstall memex
   uv tool install memex-plugin
   ```

This removes the old CLI environment, not your vault or your `~/.memex` state. Do not uninstall another project's `memex` tool.

To update the CLI later:

```sh
uv tool upgrade memex-plugin
```

Update the host plugin separately through its marketplace, and keep it on the same release as the CLI. To install a particular CLI release, run `uv tool install 'memex-plugin==0.21.3'`. You can still install from Git or a local checkout for development. If you publish releases, read [publishing to PyPI in DEVELOPMENT.md](./DEVELOPMENT.md#publishing-to-pypi).

### Codex and Kimi Code

This repository is also a Codex plugin and a Kimi Code plugin. Install the CLI first (step 1 of the quick start), then:

```bash
# Codex
codex plugin marketplace add linxule/memex-plugin
codex plugin add memex@memex-plugin

# Kimi Code (inside a kimi session)
/plugins install https://github.com/linxule/memex-plugin
```

Both get the 5 skills: `recall`, `memo-writing`, `garden-tending`, `curator-practice` and `project-consolidation`. Ask for a memo and the memo-writing skill does what `/memex:save` does. Both write to the same vault as Claude Code.

These stay with Claude Code only:

- the slash commands, because they delegate work with Claude's Task tool
- the hooks: transcript archiving, the save nudge, memo signals before compaction and the PostToolUse hook that scrubs secrets

Memex archives Claude Code transcripts, and neither Codex nor Kimi Code gives a hook the transcript path those hooks need. After you save a memo from another host, run `memex scrub <memo> --apply` yourself.

The Codex and Kimi skill copies live in `plugins/memex/skills/`. `uv run python scripts/portable_plugin.py` generates them from `skills/`. It drops Claude-only frontmatter and turns Claude's load-time `` !`cmd` `` context lines into plain "run this" instructions. Do not edit the copies by hand: `tests/test_portable_plugin.py` fails if they drift.

Once the CLI is installed, you can use it from any shell:

```bash
memex search "authentication"
memex timeline "last week"
memex ask "Why did we choose this architecture?"
```

### Open in Obsidian

The repository includes a starter `.obsidian/` configuration with core plugins, graph settings and custom property types. Open the folder as a vault and it is ready to use.

### Import existing sessions

If you have used Claude Code before, you already have transcripts worth importing:

```bash
# See what's available (scored by file edits, commits and duration)
memex session discover --triage

# Import, then rebuild the index
memex session discover --import --apply
memex index rebuild --incremental

# Score, filter and import in one pass, skipping the session you're in
memex session discover --triage --min-score=9 --import --apply --exclude 1a2b3c4d
```

### Configuration

Create `~/.memex/config.json`, using `config.json.example` as a model:

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

#### Where the index lives

Since v0.17.0, the index lives at `~/.memex/_index.sqlite` by default, outside the vault. A vault synced by iCloud Drive or Dropbox then does not re-upload a multi-gigabyte file on every write. `index_path` is optional.

Set `index_path` or `MEMEX_INDEX_PATH` only to put the index somewhere else, for example `"index_path": "/Volumes/Fast/memex/_index.sqlite"`. A set `index_path` is used exactly as written and skips the older lookup in the vault.

An index that is already at `<vault>/_index.sqlite` keeps being used from there. To move it out, run `mv <vault>/_index.sqlite* ~/.memex/`. Keep the `*`: it moves the `-wal` and `-shm` files too. `memex path --index` prints the path in use.

#### A smaller index

Since v0.15.0, you can add `"index_dimensions": 768` to `embeddings`. Memex then truncates stored and query vectors (Matryoshka truncation), so vector storage is about 4 times smaller for about 0.26% loss in retrieval quality. The API and the cache keep the full `dimensions` (3072), so you can undo it.

On an existing index, set it and run `memex index migrate-vec`, which truncates in place without re-embedding. The same migration adds vec0 metadata columns, so `--type` and `--since` filters run inside the nearest-neighbor search. Then run `memex index vacuum` to reclaim the space: the file does not shrink until you vacuum it.

### Semantic search

Without embeddings, keyword search (FTS5) still works. For semantic search, use LM Studio or a Gemini API key:

```bash
# Option A: LM Studio (fully local, recommended)
# Install LM Studio, load Qwen3-Embedding-0.6B, start the server

# Option B: Gemini API. Save the key once (recommended: hooks and skills
# inside Claude Code inherit no shell exports, and a saved key needs none)
memex auth set-key       # hidden prompt, saved to an owner-only file under ~/.memex/credentials/
memex auth status        # shows which source is in use; never prints the key
# ...or export it in each shell if you prefer:
# export GEMINI_API_KEY=your-key

# Build the embeddings
memex index rebuild --full
```

Environment variables override a saved key, and `memex auth clear-key` removes the saved copy. Memex never calls a password manager itself. If you keep the key in 1Password, wrap a command with `op run --env-file ... --`. For details, read [Gemini credentials](docs/gemini-credentials.md).

### Remote access (memex cloud)

Memex cloud lets agents that do not run on your machine use your memex. These include claude.ai connectors, Claude Projects, Claude Code on the web and other MCP clients. You deploy the remote MCP server in [`cloud/`](cloud/README.md) to your own Cloudflare account.

Your vault stays the source of truth on your machine. The cloud holds a searchable copy plus an inbox, and embedding still happens on your machine.

[![Deploy to Cloudflare](https://deploy.workers.cloudflare.com/button)](https://deploy.workers.cloudflare.com/?url=https://github.com/linxule/memex-plugin/tree/main/cloud)

On the Deploy form:

- for the Vectorize index, enter `768` for Dimensions and `cosine` for Metric
- set `OWNER_PASSWORD` to the password you will type to sign in
- set `SYNC_TOKEN` and `CONSENT_SECRET` to the output of `openssl rand -hex 32`, one each

After deploying, add `GEMINI_API_KEY` in the Worker's settings to turn on semantic search. The [cloud deploy guide](cloud/README.md#deploy) covers the rest of the form.

Fill the cloud from your machine:

```bash
memex cloud login --url https://memex-cloud.<you>.workers.dev   # paste SYNC_TOKEN
memex cloud push        # memos, overviews, topics, observations and their vectors (not transcripts)
memex cloud pull        # bring home memos that remote agents saved
memex cloud status      # what the cloud holds, its version, pending inbox memos
```

To keep the copy current, run this on a schedule (cron or launchd) on one machine:

```bash
memex index rebuild --incremental && memex cloud pull && memex cloud push
```

#### Connect an agent

1. In claude.ai, go to Settings, then Connectors, and select **Add custom connector**.
2. Enter `https://<your-worker>/mcp` as the URL.
3. Sign in with your owner password, and choose whether the agent may read, save memos or both.

Agents get the `search`, `read`, `timeline`, `projects`, `observations` and `save_memo` tools. When an agent saves a memo, it also sends the observations it extracted. Both become ordinary vault files on your next `memex cloud pull`.

#### Keep a deployment up to date

A button deployment lives in a repository of your own. Its included workflow opens a pull request for each new `cloud-v*` release, and `memex cloud status` tells you when the server is behind. Read [updating memex cloud](cloud/README.md#updating) and its [known limitations](cloud/README.md#known-limitations).

## CLI usage

```bash
memex search "JWT OR authentication"
memex timeline "yesterday" --project=my-app
memex ask "What pattern keeps showing up in retry handling?"
```

Use the CLI when you want memex outside Claude Code. Inside a Claude Code session, use the plugin commands below to get hooks, slash commands and help writing memos.

## Commands

Slash commands inside Claude Code:

| Command | What it does |
|---------|-------------|
| `/memex:save [title]` | Saves the current context as a memo |
| `/memex:status` | Shows vault statistics and pending memos |
| `/memex:open` | Opens the vault in Finder or Obsidian |

Retrieval is skill-based: search, timeline, ask, synthesize, merge and maintain. Claude uses:

- the `recall` skill when you ask about past work
- the `garden-tending` skill for synthesis and vault maintenance
- the `memo-writing` skill when saving sessions
- the `curator-practice` skill for autonomous tending

For direct access from a shell, use the CLI:

```bash
memex search "<query>"      # hybrid search: full-text and vector
memex timeline yesterday    # browse by date
memex ask "<question>"      # deep retrieval with observations
memex backfill obs          # extract observations from existing memos
memex scrub <path>          # detect API keys and secrets (--apply redacts in place)
memex status                # vault statistics and pending memos
memex check                 # vault health (scans the filesystem when Obsidian isn't running)
memex check --folders       # project-folder drift (fragment or duplicate folders)
memex check --condense      # project overviews behind their memos (NEW by date, ARR by git add date, GAP vs stamp)
memex check --signals       # open Recent-signals per topic (what a fold pass should pick up)
memex session reconcile-orphans   # clear pending-memo signals whose session already saved a memo
memex auth status           # which Gemini credential source is active (never prints the key)
memex cloud status          # memex cloud: server version, what it holds, pending inbox memos
memex cloud push / pull     # sync the cloud copy; bring home memos remote agents saved
```

Run `memex --help` for the full CLI, including the `obs`, `index`, `session` and `graph` command groups.

## Automatic behavior

| Hook | When | What it does |
|------|------|------|
| SessionStart | A new session starts | Loads project context, recent memos and open threads |
| UserPromptSubmit | Each message | Tracks activity and nudges you to save after about 20 messages |
| SessionEnd | A session closes | Archives the transcript |
| PreCompact | Before compaction | Writes a signal file for safety-net memo generation |
| PostToolUse | After `Write`, `Edit` or `MultiEdit` | Redacts recognized secret patterns, in place, from a memo or auto-memory file just written. It runs after the write and fails open. |

## Vault structure

After a while, your vault grows like this:

```
memex/
├── projects/<name>/memos/       # Session memos per project
├── projects/<name>/transcripts/ # Full conversation logs
├── topics/                      # Cross-project concept notes
├── _templates/                  # Note templates
└── MEMORY.md                    # Global synthesis & preferences
```

The search index is not in the vault. By default it lives at `~/.memex/_index.sqlite`. Run `memex path --index` to see where it is.

## Documentation

More documentation:

- [CLAUDE.md](./CLAUDE.md) has the full documentation: architecture, configuration, development commands, troubleshooting, and security and privacy
- [SETUP.md](./SETUP.md) has detailed installation instructions

## License

MIT

## Dependency maintenance

The public repository tracks `uv.lock` so development and CI installs are reproducible. When contributing, use `uv sync --locked --all-extras` and `uv run --frozen pytest`. Dependabot groups weekly Python and GitHub Actions updates. CI tests Python 3.11 and 3.13 and audits the locked runtime and development dependencies. These checks do not update the private vault or an installed plugin environment.
