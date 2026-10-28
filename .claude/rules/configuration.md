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
| `~/.memex/pending-memos/` | PreCompact signal files for orphan-memo retry (SessionEnd writes the same shape for a substantial relay worker) |
| `~/.memex/compact-summaries/<session_id>/<n>.md` | PostCompact: Claude Code's compaction summary, one file per compaction. State dir, never the vault, never pushed; kept, not pruned. The Layer-2 recovery recipe reads these before the transcript |
| `~/.memex/last-archives.jsonl` | SessionEnd filing receipts: one JSON line per archived session (project + the rule that chose it); trimmed to the newest 500 lines past 256 KB |
| `~/.memex/receipts-shown.json` | Cursor for the receipts the vault's SessionStart has already shown |
| `~/.memex/cwd-projects.json` | cwd (git root) → project memory behind the `remembered` detection rule |
| `~/.memex/session-state/<sid16>.memo` | Path of the memo stamped for a session, recorded by PreCompact (the same dir holds UserPromptSubmit's nudge state); `<sid16>` = first 16 chars of the session id |
| `~/.memex/cloud-forget.json` | Docs `memex cloud forget` removed from the cloud; a push never re-publishes the forgotten version |
| `<state_dir>/credentials/gemini-api-key` | Optional owner-only local key, saved explicitly with `memex auth set-key` |

## Config Keys Added in v0.22.0

All in `~/.memex/config.json` (declared fields of `Settings` in `src/memex/config.py`). The hooks that use `channel_mappings` and `session_start_pointer` get them through `get_config()`, which falls back to the raw JSON when pydantic is not installed.

```json
{
  "channel_mappings": { "chan_…": "project-folder" },
  "discover_ignore": ["/var/folders/*/T/tmp*", "scratch/"],
  "session_start_pointer": true,
  "cloud": { "publish": "working-tree" }
}
```

- **`channel_mappings`** — claude.ai project channel (`chan_…`, from a relay worker's first turn) → vault project folder. Checked before any cwd rule; one entry per claude.ai project, ever. `memex check --channels` lists relay sessions whose channel is missing here
- **`discover_ignore`** — cwd patterns `session discover` / `session import` never list or import: a substring of the cwd, or an fnmatch glob over the whole cwd when it contains `*`, `?` or `[`; `~` expands; `/private` is optional on macOS temp paths. For deliberate skips (the June eval fleet under `/var/folders/*/T/tmp*`)
- **`session_start_pointer`** — default `true`. `false` turns off SessionStart's one-line project pointer
- **`cloud.publish`** — what `memex cloud push` (and the hourly `scripts/cloud-sync.sh`) publishes. Unset or `"working-tree"` (the default, for every vault): every indexed doc, uncommitted agent drafts included — claude.ai sessions get the same access as local agents (owner decision, 2026-10-28). `"committed"` is the opt-in review gate (git vaults only): a doc changed since HEAD keeps the version the cloud has, an uncommitted one never goes. `memex cloud push --publish …` overrides it for one run; `memex cloud forget <path>` is the privacy lever

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
  - **memex cloud** (opt-in, `memex cloud login`): `memex cloud push` sends indexed docs, chunks, observations and vectors to your Worker, and claude.ai reads them through the connector. The default publishes the working tree, so an un-reviewed draft is readable from claude.ai until the next push removes it — see `cloud.publish` above and the redaction SOP in `maintenance.md`. Every push redacts secrets from what it sends (`outbound_text`, `memex.scrub`), and the hourly `scripts/cloud-sync.sh` redacts changed memos and auto-memory in place first (sidecars are left to the push-time scrub: rewriting an observation changes its hash), because Codex, Kimi Code and Obsidian write memos without Claude Code's write-time scrub hook
  - **With LM Studio provider (if configured), all embedding processing stays fully local**

**Access controls:**
- Local filesystem permissions apply
- Transcripts excluded from git by default (see `.gitignore`)
- No authentication layer - anyone with filesystem access can read

**Privacy note:**
Transcripts contain your full conversation history, which may include sensitive information discussed during sessions. Consider what you discuss before enabling memex. Transcripts are stored in `projects/<name>/transcripts/` and excluded from git.
