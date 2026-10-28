---
paths:
  - "scripts/**/*.py"
  - "src/memex/**/*.py"
  - "_views/**"
  - "topics/**/*.md"
---

# Maintenance & Dev Commands

## Periodic Maintenance Tasks

Run these when asked or during memex maintenance sessions.

### Nightly Incremental Rebuild (Automated)

Scheduled via launchd at 3am daily via `scripts/nightly-rebuild.sh` → `scripts/com.linxule.memex.nightly-rebuild.plist`. The wrapper:
1. Sources `~/.secrets` (Gemini key — launchd does not inherit shell env)
2. Runs `memex index rebuild --incremental`
3. Runs `memex index embed-missing` to retry any vec gaps

Install (one-time, per machine):
```bash
cp scripts/com.linxule.memex.nightly-rebuild.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.linxule.memex.nightly-rebuild.plist
launchctl list | grep memex    # confirm loaded
```

Trigger on demand (testing):
```bash
launchctl kickstart gui/$(id -u)/com.linxule.memex.nightly-rebuild
tail -f ~/.memex/logs/nightly-rebuild.log
```

Uninstall:
```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.linxule.memex.nightly-rebuild.plist
rm ~/Library/LaunchAgents/com.linxule.memex.nightly-rebuild.plist
```

Check logs: `tail ~/.memex/logs/nightly-rebuild.log`

**History (2026-04-21):** The launchd job went missing before 2026-02-27 — no plist in LaunchAgents, no cron entry — and the rebuild silently fell off the schedule for ~2 months. Reinstalled with wrapper + plist committed to the repo so future resets are trivial. Paths in the plist are absolute — if the user or vault path ever changes, both the wrapper and plist must be updated together.

### API Key Rotation → Embedding Gaps
Rotating the Gemini key invalidates the old key immediately. Any rebuild or `backfill obs` call that runs before the new key takes effect inserts FTS/chunks/observations without embeddings.

Failure logs come from two different paths:
- **Rebuild path** (`index_rebuild` → `embed_chunks`): `Embedding batch partially failed: N/M items missing.`
- **Backfill-obs path** (`store_observations`): `⚠️ N/M observations stored without embeddings (<ExceptionType>: <msg>). Run memex index embed-missing after fixing the cause.`

`memex backfill obs` now exits with code 2 when any embedding failed — chained scripts / hooks can detect this. `memex index rebuild` still exits 0 with gaps (see exit-code split below).

After rotating:
1. Update `~/.secrets` with new key
2. **Restart Claude Code** (process inherits env at launch; sourcing `.secrets` in a separate terminal won't update the running process)
   - Alternative for one-off terminal runs: prefix the command with `source ~/.secrets &&`. This does NOT update an already-running Claude Code process but works for direct shell use.
3. Run `memex index status` — look for `embedding_gaps` section
4. Run `memex index embed-missing` to backfill any vec gaps

The status output now includes an "Embedding gaps" section whenever chunks/observations are missing from vec_chunks/vec_observations. Rebuild output also surfaces gaps in a warning block at the end.

**Exit-code split (important for scripts):**

- `memex index rebuild` exits **0 even with gaps**, for backward-compat with cron/launchd. The warning block is the actionable signal; CI / scheduled jobs that care about gap safety must inspect stdout or run `embed-missing` afterward.
- `memex index embed-missing` exits **non-zero when any items remain unembedded** (1 for pipeline errors, 2 for per-item failures). Wire it into scripts after a rebuild; don't rely on rebuild's exit code.

### Full Rebuild (Only When Needed)
Run when switching providers, after schema upgrades, or if index corrupted:
```bash
memex index rebuild --full
```

**When to run full:**
- Switching embedding providers (dimension change)
- Schema upgrades (new tables/columns)
- Index corruption

**Not needed for:** Daily growth (incremental handles it)

### Synthesize Cross-Project Insights
Review recent memos across all projects. Condense findings into `_project.md` overviews. Create new concept notes in `topics/` for ideas that appear in 2+ projects.

**Synthesis-driven crystallization**: Use the garden-tending skill (Diagnose + Condense) to identify which concepts to crystallize as topic stubs. The synthesis agent finds cross-project patterns that reveal which `?suggested` links have enough substance to become real topics.

### Discover & Import Unprocessed Sessions
Run `memex session discover --triage` to find sessions in `~/.claude/projects/` not yet in memex. Triage scores them by viability (file edits, git commits, duration, etc.). Import high-value ones with `--min-score=9 --import --apply`.

Skip the session you are running in — its transcript is still growing — with `--exclude <session-id-prefix>` (repeatable).

**Deliberate skips: `discover_ignore`.** Sessions whose cwd matches an entry of `discover_ignore` in `~/.memex/config.json` are never listed or imported. An entry is a substring of the cwd, or an fnmatch glob over the whole cwd when it has `*`, `?` or `[` (the June eval fleet: `/var/folders/*/T/tmp*`, 180 sessions that used to be listed every pass). Sessions shorter than 30 s are husks and are never listed or imported either (a `--min-score 4` import once took a 13-second one). Both exclusions are counted, not silent: the listing ends with a note ("N session(s) hidden by discover_ignore…"), so "nothing to import" is never an unexplained empty list. `session import` routes each session through `resolve_session_project`: a relay worker's channel in `channel_mappings` first, then the cwd rules; `memex check --channels` lists the channels still unmapped.

### Find Capture Gaps
- `memex check --harnesses` lists Codex Desktop threads (user- or agent-created; automation excluded) from the last 14 days (`--since Nd|YYYY-MM-DD`) that no memo stamps by `session_id:`/`session_ids:`. Read-only. Codex and Kimi Code write their own memos — this is how you see the ones that didn't
- `memex check --channels` lists relay sessions whose claude.ai channel is not in `channel_mappings`; add one entry per claude.ai project and re-file the strays with `memex project move --file`
- `memex check --folders` also reports seat- or temp-only folders, `<existing-project>-<suffix>` names, catch-all cwds (an ancestor of other projects' cwds) and two folders sharing one cwd

### Consolidate Project Folders
`memex project move <src> <dst> [--file <memo.md> …] [--apply]` is the consolidation command (dry run by default; the `project-consolidation` skill decides *whether* to merge, this does it). In order: ingests any changed sidecars (a hand-edited sidecar must be ingested before a reassign — reassign re-renders sidecars from the DB), reassigns the index rows of `memos/`, `transcripts/` and `auto-memory/` together, moves the files with their sidecars (`git mv` when tracked), restamps `project:`, and removes a template-only `_project.md`. It refuses on any collision, checks the observation total as an invariant, and rolls back (files put back) if it breaks. Exit codes: 0 ok · 1 refused · 2 invariant violated · 3 collision · 4 index busy. `--file` moves single documents (a `.md` under `memos/`, `transcripts/` or `auto-memory/`; its sidecar and raw transcript go with it). It lists wikilinks that point at moved paths but does not rewrite them. Transcripts are gitignored, so moving them leaves no git trail — the curator log is the record. Afterwards `memex index rebuild --incremental` is a no-op on the moved content and search already shows the new project. The manual sequence behind it is in `plugin-authoring.md` (Vault Operations).

### Cloud: Redactions and Renames
`scripts/cloud-sync.sh` pushes the **working tree** hourly (`cloud.publish`, default `working-tree`), so an un-reviewed agent draft is readable from claude.ai until a push removes it. After redacting or renaming anything sensitive:
1. `memex cloud forget <path>…` for an urgent redaction — it deletes the doc (with its observations and vectors) from the cloud now and no later push re-publishes that version; it comes back only when its content changes (with `publish=committed`: when a different version is committed). Fix the file, then push
2. For a rename or move, `memex cloud push` immediately instead of waiting for the hourly run: a push (default mode) deletes cloud docs that no longer exist locally
3. Verify through the connector: `read` the old path and expect "No document"

Known gap: `forget` does not remove the copy of a memo a remote agent saved that is still in the cloud inbox — that copy stays until `memex cloud pull` brings it into the vault, and the forget record then keeps that version from being published (`forget` prints a note when it meets one). Secrets are redacted from everything a push sends (`outbound_text`), and `cloud-sync.sh` redacts memos and auto-memory changed since the last run in place before indexing (not sidecars: the push scrubs observation text) — but redaction is pattern-based, not a review.

### Find Orphans
Find:
- Transcripts without memos (need processing)
- Memos without links (isolated)
- Concepts that reference deleted memos

### Project Summary
Generate a summary of a specific project's current state based on its memos.

### Multi-machine (obs sidecars)

Observations live in the vault as `<doc>.obs.jsonl` sidecars, committed alongside their memo — see
[[architecture#Observations]]. After pulling in changes from another machine (iCloud sync, `git
pull`), bring the index up to date with either:

```bash
memex index rebuild --incremental   # or a --full rebuild; both ingest sidecars too
# or, cheaper when the documents themselves haven't changed:
memex obs ingest-sidecars
memex index embed-missing           # sidecar ingest never writes vectors — embed what it inserted
```

**Machine of record: m4max.** Observation-mutating commands (`memex backfill obs`, the dreamer,
`memex obs retag`/`reassign`, `memex project move`, `memex obs export-sidecars`) operate on one machine at a time — there
is no cross-machine lock, and iCloud is last-write-wins on a sidecar file. m4max's `_index.sqlite` is
the authoritative one; run these from m4max unless you have a specific reason not to, and never run
two of them concurrently on different machines against the same vault.

**One-time migration** (existing vaults that predate sidecars): run `memex obs export-sidecars
--apply` on the **machine of record (m4max)** only — the one whose `_index.sqlite` is authoritative.
Never run it on a second machine whose DB is a stale copy; it would render sidecars from stale data
and clobber ones already synced in from elsewhere (`export-sidecars` refuses to overwrite a
differing existing sidecar unless you pass `--force`, and prints a loud warning if the vault
currently has zero sidecars — a signal you're about to run the first-ever export, so double-check
you're on m4max before proceeding).

**Cross-machine rename**: after renaming a project/memo folder on one Mac, let iCloud finish
syncing the moved files (memo + `.obs.jsonl` sidecar together) before running a rebuild on the
*other* machine. `memex index rebuild --incremental` ingests sidecars before it removes anything —
if the new memo + sidecar have already arrived, the row is **adopted** into the new doc_path (same
id, vector preserved) rather than deleted; the deleted-doc pass then finds nothing left under the
old path to remove. Rebuilding too early — before the new sidecar has synced in — just means the
old doc_path's rows are removed this run and adopted on a later one once the new sidecar catches up;
either way nothing is silently lost, but running after the sync settles avoids the extra round trip.

**Conflict copies**: iCloud sometimes leaves a file like `x.obs 2.jsonl` next to `x.obs.jsonl` when
two machines write around the same time. `memex obs sidecars` lists these under `conflicts`. To
resolve: read both, pick the one you want (usually the newer or the one with more observations),
delete the other, then `memex obs ingest-sidecars` to pick up any change.

`memex obs sidecars` is the health check — run it any time to see sidecars with no matching DB row
(`missing`), sidecars whose document is gone or unindexed (`orphan`), sidecars pending ingest
(`stale`), sidecars every ingest rejects, with the bad line (`malformed`), zero-byte/whitespace-only
sidecars that are never authoritative (`empty`), content-hash
collisions with a still-live doc elsewhere (`foreign_conflicts`), and outstanding deduction
provenance references awaiting their source's sidecar (`pending_sources`).

## Dev Commands

```bash
# Test hooks manually (pipe JSON to stdin)
echo '{"session_id": "test", "cwd": "'$(pwd)'", "source": "startup"}' | uv run hooks/session-start.py

# Test search (use OR between keywords, not full questions)
memex search "JWT OR authentication"

# Rebuild index (incremental - only changed docs)
memex index rebuild --incremental

# Full rebuild with embeddings
memex index rebuild --full

# Check index status (includes graph stats + embedding_gaps field when > 0)
memex index status

# Retry missing embeddings (chunks/observations in indexes but not in vec_*)
# Idempotent. Use after a rebuild that reported embedding gaps — e.g., expired
# API key, rate-limit exhaustion — once the root cause is fixed. Non-zero exit
# when items still fail, so safe to chain in scripts.
memex index embed-missing
memex index embed-missing --json

# Reclaim free pages after migrate-vec drops the old (larger-dim) vec tables.
# VACUUM + wal_checkpoint(TRUNCATE) — the DB is persistent-WAL, so the main
# file only shrinks after the TRUNCATE checkpoint. Needs free disk ~= file size.
memex index vacuum
memex index vacuum --json

# Crystallization readiness check (alias-aware, delta tracking)
memex check                    # full report
memex check --tier ready       # actionable items only
memex check -v                 # with source files
memex check --json             # programmatic output

# Backfill has_memo on transcripts (match memos to transcripts)
# Matches memo session_id:/session_ids: stamps, then filename suffix and state.json; -v counts per evidence kind
memex backfill memos                        # dry-run (default)
memex backfill memos --apply -v             # apply + verbose
memex backfill memos --apply --obsidian-cli # set the property via Obsidian CLI (opt-in: a closed Obsidian costs a 5 s timeout per run)

# Backfill token usage into existing transcript frontmatter
memex backfill tokens                          # dry-run
memex backfill tokens --apply -v               # apply + verbose

# Capture gaps (read-only)
memex check --harnesses [--since 7d] [--json] # Codex threads with no memo of their own
memex check --channels [--json]               # relay channels missing from channel_mappings

# Discover unprocessed sessions in ~/.claude/projects/
memex session discover                        # summary by project
memex session discover --triage               # score by viability
memex session discover --triage -v            # with first-message preview
memex session discover --triage --min-score=9 # high-value only
memex session discover --all-projects         # list all Claude projects
memex session discover --import --apply       # batch import
memex session discover --triage --min-score=9 --import --apply   # score, filter, import
memex session discover --import --exclude 1a2b3c4d --apply       # skip a session (repeatable)

# Sync Claude Code auto-memory into vault
memex sync --discover              # list files + coverage report
memex sync --sync                  # dry-run
memex sync --sync --apply          # write files
memex sync --status                # fresh/stale/new/orphaned

# Consolidate a project folder (dry-run unless --apply)
memex project move <src> <dst>
memex project move <src> <dst> --file projects/<src>/memos/<memo>.md --apply
```

**Flags that work but are hidden from `--help`:** `memex index rebuild --incremental` (the default; the wrapper adds it itself and `--help` lists only `--full`) and `memex sync --discover/--sync/--status/--apply` (`memex sync --help` lists no options; every argument passes through to `sync_auto_memory.py`).

The session discover/import and backfill-memos wrappers hid their flags before v0.22.0; they now list `--import`, `--exclude`, `--apply`, `-v`, `--all-projects`, `--min-score`, `--triage` and `--obsidian-cli` in their own `--help`.

