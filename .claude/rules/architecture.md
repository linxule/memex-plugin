---
paths:
  - "scripts/**/*.py"
  - "src/memex/**/*.py"
  - "hooks/**/*.py"
  - "commands/**/*"
  - "skills/**/*"
---

# Architecture

## Memo Generation (Two Layers)

Memos are generated without external API calls — everything runs through Claude Code sessions.

**Layer 1 — Proactive Save (primary, best quality):**
- `UserPromptSubmit` hook judges activity by the transcript's size on disk OR the older prompt tiers, whichever is reached first: 3 MB and 8 MB of transcript (the archive's 90th/95th percentiles), or 40 prompts with 4,000 chars and 80 prompts with 8,000 chars (the char floor is an AND gate, so thin sessions don't trip it). A session that runs dozens of agents on a handful of prompts is substantial; prompt counts alone never noticed
- It nudges Claude: "[memex] Consider /memex:save". No nudge when a memo stamped with this session's id exists, unless the session has doubled since that memo was saved and grown by at least 3 MB ("memo saved HH:MM; the session has grown since — extend it?"). Fleet seats (`sdk-cli`) get silence; a fork's inherited bytes don't count. Known gap: a run with zero user prompts is never nudged (Stop can't add context without blocking) — the compaction path below covers it
- Claude writes memo using `/memex:save` with full experiential context
- This produces the best memos because Claude was *there*, not reconstructing from transcript

**Layer 2 — Background Subagent (safety net):**
- `PreCompact` hook writes signal file to `~/.memex/pending-memos/` — unless the vault already holds a memo stamped with the session's id (then it records the path in `session-state/<sid16>.memo` instead) or the session is a fleet seat (no signal)
- `PostCompact` hook keeps Claude Code's compaction summary at `~/.memex/compact-summaries/<session_id>/<n>.md` — a model-written digest that exists exactly when Layer 2 needs one. Summaries are kept, never pruned
- Post-compaction, `SessionStart` (`source: compact`) states the truth from the vault: a memo exists → it names it and says to extend it (dated section; re-send the COMPLETE observation set in one `--replace` call); only `memex mark-saved` ran (no stamped memo) → extend that memo and add the `session_id:` stamp; none → one short instruction to spawn a background sonnet subagent following the recovery recipe, `skills/memo-writing/recovery.md`
- The recipe reads the compaction summaries first and the transcript second, searches vault for related memos, writes the memo, then extracts 5–15 atomic observations and pipes them to `memex backfill obs --stdin`
- `SessionEnd` raises the same signal for a substantial claude.ai relay worker (≥ 30 tool uses) with no memo, since relays rarely compact
- Decent quality, but reconstructed — only fires when Layer 1 didn't catch it

**Other hosts write their own memos (v0.22.0):** Codex Desktop and Kimi Code have no memex hooks, so there is no Layer 2 for them. The memo-writing skill ("Saving from Codex, Kimi Code") has the agent save a memo stamped with `memex session whoami --json`; claude.ai sessions use the cloud connector's `save_memo`. memex does not convert their sessions into transcripts (owner decision, 2026-10-28)

**Cross-Session Synthesis (periodic, manual):**
- Run cross-session synthesis periodically via the garden-tending skill
- Finds: patterns across projects, contradictions, semantic drift, compression candidates
- Updates `_project.md` overviews with condensed project knowledge
- For large vaults: use a dedicated CLI session with `claude --resume <analyst-id> --model sonnet`

## Session Lifecycle

1. **SessionStart** → Decides from the vault, not `state.json`: a session has a memo when one in the vault is stamped with its id (`memex.session_memos`). By `source`:
   - `compact` → the truth about this session's memo (see Layer 2)
   - `startup` / `clear` / `resume` → pending-memo signals for this cwd (or every signal, in the vault) that no stamped memo covers; covered and `sdk-cli` signals are cleared, never announced (positive evidence only)
   - `startup` / `clear` / `fork` → the sidecar-rejection notice and, for a project session, the **pointer**: one line (≤ 300 chars) — `memex: <project> — overview condensed <date>, <n> open threads, last memo <date> (<harness>). memex read projects/<p>/_project.md`. A pointer, not content. Not shown in the vault, to fleet seats, or to `agent_type` sessions; off with config `session_start_pointer: false`
   - `fork` → no memo nags: the parent session owns that work
   - in the vault (not `resume`) → the **filing receipts** SessionEnd left since the last look (see 6), shown once as a `systemMessage` for the user and as context
   - fleet seats (`sdk-cli`) → nothing at all
2. **UserPromptSubmit** → Tracks activity, nudges Claude to save when substantial work accumulates (Layer 1)
3. **During session** → Skills guide Claude when to search/save (intent-based); Claude saves memo via `/memex:save`
4. **PreCompact** → Writes the signal file as safety net, or records the stamped memo's path (no API calls)
5. **PostCompact** → Keeps the compaction summary for the recovery recipe
6. **SessionEnd** → Routes the session to a project, archives the full transcript to `projects/<name>/transcripts/`, signals a substantial relay worker with no memo, and appends a **filing receipt** (`~/.memex/last-archives.jsonl`: project + the rule that chose it). The next SessionStart in the vault shows receipts filed by a guess (`leaf` / `fallback`) or an unmapped relay channel, tracked by the cursor `~/.memex/receipts-shown.json` — SessionEnd itself can't show anything (1.5 s budget, `systemMessage` discarded)

## Search Pipeline

1. Query comes in via recall skill or `memex search` CLI
2. FTS5 scores documents by BM25 keyword relevance
3. Vector embeddings score by semantic similarity (Gemini Embedding 2 API)
4. RRF (Reciprocal Rank Fusion, k=60) combines rankings - industry standard
5. Result diversity applied (max 3 chunks per document)
6. Optional `--since` filter for recency (e.g., `--since=7d`)

## Project Detection

A session's project comes from `memex.routing.resolve_session_project(cwd, metadata)` (SessionEnd, `session import`): identity first, then the cwd.

0. **Relay channel** (rule `channel`): a claude.ai relay worker's `relay_channel` (`chan_…`) found in config `channel_mappings` decides, before any cwd rule — its cwd is only a place to stand (Dropbox root, `~/Documents/GitHub`), and a pin can't tell the projects apart. A channel not in the map is reported (receipt, `memex check --channels`) and the cwd rules below decide meanwhile

The cwd rules (`detect_project`, in order):

1. Check explicit mappings in `~/.memex/config.json` (`project_mappings`, substring match) — rule `pin`
2. Session scratchpad (`…/claude-<uid>/<encoded-parent>/<uuid>/scratchpad/…`, v0.20.2): resolve
   on the owning session's true cwd — its transcript `~/.claude/projects/<enc>/<uuid>.jsonl`
   first, validated dir scan as fallback; nested fleets hop again (bounded at 3). Deliberately
   before the git rules: a worker's checkout inside a scratchpad belongs to the session that
   spawned it. An orphaned scratchpad root → `_uncategorized`. Rule `scratchpad`
3. Temp roots (`/private/var/folders/*/T/`, `/private/tmp/`; rule `temp-prefix`): fleet seats run from mkdtemp
   dirs. A leaf that starts with an existing project name plus a separator (`-`, `_`, `.`) routes
   there (longest match, so `paper` never captures `paperb-…`); generic leaves (`repo`, `wt`, `work`,
   `worktree`, `src`, `checkout`, …) walk up; failing that, a git remote naming an existing project, then
   the nearest matching ancestor; else `_uncategorized` — never a new folder
4. Parse git remote URL for repo name (rule `git-remote`)
5. Use git root folder name (rule `git-root`)
6. Fall back to cwd folder name (rule `leaf`, a guess) or `_uncategorized` (rule `fallback`)

**Remembered rule (`remembered`):** the first git-rule resolution of a repo is recorded in `~/.memex/cwd-projects.json` (keyed by git root, so subdirs agree). A later answer from a changed remote (a repo split or rename) returns the remembered project instead of minting a new folder. Pins still win; memory yields again when the remembered folder has left the vault or another checkout now lives at the path.

`detect_project_explained(cwd) -> (project, rule)` returns the deciding rule; `detect_project(cwd)` is its first element. Rules are `memex.routing.RULES` (channel, pin, scratchpad, temp-prefix, remembered, git-remote, git-root, leaf, fallback; `detect` is the coarse stand-in); `leaf` and `fallback` are the guesses that filing receipts flag.

The `~/.claude/projects` slug fallback (sync/discover, transcript-less dirs) recognises the
encoded scratchpad dir name and routes through the owner dir too.

## How the Plugin Works

**Hooks:**
1. **SessionStart** - Minimal injection: the project pointer, uncovered pending memos, the truth about the memo after compaction (and a recovery instruction when there is none), filing receipts in the vault
2. **UserPromptSubmit** - Tracks transcript size and prompt count, nudges Claude to `/memex:save` (3 / 8 MB of transcript, or 40 / 80 prompts)
3. **SessionEnd** - Routes and archives the transcript to `projects/<project>/transcripts/`, writes the filing receipt (1.5 s budget)
4. **PreCompact** - Writes signal file to `~/.memex/pending-memos/` unless a stamped memo exists (no API calls, <100ms)
5. **PostCompact** - Saves `compact_summary` to `~/.memex/compact-summaries/<session_id>/<n>.md`
6. **PostToolUse** (`Write|Edit|MultiEdit`) - Scrub gate on memo and auto-memory writes (see `hooks/post-tool-use.py`)

**Memo generation philosophy:**
- Claude writes memos from full experiential context (Layer 1) — best quality
- Background subagent reads transcript as fallback (Layer 2) — decent quality
- No external API calls — everything uses Claude Code subscription
- The nudge system (UserPromptSubmit) reminds Claude to save before compaction catches us

**Why skills over hooks for search:**
- Skills let Claude decide when to search (judgment-based)
- No timeout pressure (hooks have 5-10s limits)
- Claude can refine queries iteratively
- More transparent to user

**Skill-based Search:**
- The `recall` skill teaches Claude when to search memos (see `skills/recall/SKILL.md`)
- When user asks "why did we...", "remind me...", etc., Claude decides to search
- Claude extracts keywords (not full questions) for effective FTS matching
- Example: "Why did we choose JWT?" → search for `JWT OR authentication`

## Frontmatter Schema

**Memos:** `type: memo`, `project`, `title`, `date`, `topics: []`, `status: active|archived`, `source_cwd`, plus the capture keys (v0.22.0): `harness` (where the session ran — one of `claude-code`, `claude-desktop-relay`, `codex-desktop`, `kimi-code`), `session_id` (the writing session's full UUID; Kimi's `session_<uuid>` is accepted), `session_ids: []` (full UUIDs, when one memo covers several sessions), `written_by` (provenance line on reconstructed memos, e.g. who rebuilt it and from what). The stamps are how hooks, `backfill memos`, `reconcile-orphans` and `check --harnesses` match a memo to its session — read from the vault, not from `state.json`. `memex check --validate` lints them: errors on an unknown `harness`, a `session_ids` that is not a list of full UUIDs, or a stamp the strict YAML reader cannot parse (the memo would silently look unlinked); warns on a memo topic with no topic or alias

**Transcripts:** `type: transcript`, `project`, `session_id`, `date`, `messages`, `has_memo`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `models: []`, `commits: []`, `duration_minutes`, `subagents` (count), `entrypoint` (cli | sdk-cli | claude-desktop, when present in the source JSONL). A claude.ai relay worker (first user turn opens with `<relay from="coordinator" session=…>`, `entrypoint: claude-desktop`) also gets `harness: claude-desktop-relay`, `relay_coordinator: session_…` and, when found, `relay_channel: chan_…` (from `<project id="chan_…">` in the queued wake message before the first user turn). `source_cwd` and `parent_session_id` appear only in markdown rendered by `memex.harness.render_transcript_md` (the Codex/Kimi adapters' library renderer; no command calls it); ordinary Claude Code transcripts carry no `harness` key

**Concepts:** `type: concept`, `title`, `projects: []`, `related_memos: []`

**Trails:** `type: trail`, `title`, `concept`, `projects: []`, `started`, `last_extended`, `status: active`, `aliases: []`, `tags: []`

**Projects:** `type: project`, `name`, `created`, `condensed`, `memos_digested`, `status: active`

**Auto-Memory:** `type: auto-memory`, `title`, `project`, `date`, `source`, `source_hash`, `synced`, `volatile: true|false`, `topics: []`, `status: active`

**Meta:** `type: meta`, `title`, `created`, `updated` — curator infrastructure files in `_meta/` (dashboard, log, tag taxonomy). Not indexed.

**Legacy types (normalized):** `pattern`, `meta-pattern`, `dimension-cluster`, `topic` were used in early vault files and have been normalized to `concept`. `project-overview` normalized to `project`.

## Observations

The vault (markdown) is truth for observations, same as for memos; `~/.memex/_index.sqlite` is a
per-machine cache. Every document that has observations gets a sidecar next to it —
`<doc>.obs.jsonl`, one JSON object per line — so a second machine sharing the vault via iCloud sees
the same observations, not none.

- **Write-through.** Every mutation of a doc's observations (extraction, dreamer deductions, `memex
  obs retag`, `memex obs reassign --apply`) rewrites that doc's sidecar from current DB state
  immediately after. No per-mutation sidecar editing — the sidecar is always a full re-render.
- **Ingest on rebuild.** `memex index rebuild` (full and incremental) diffs every changed sidecar
  into the DB — insert/delete/update by content hash, never wipe-and-reload — so unrelated rows keep
  their ids and vectors. `memex obs ingest-sidecars` does the same without a full rebuild (e.g. right
  after an iCloud sync).
- **Hand-editing a sidecar is supported.** Edit the JSONL directly (fix a typo, retag an
  observation), and the next `ingest-sidecars` or `index rebuild` picks it up — the file, not the DB,
  is authoritative. You can leave `content_hash` as it was: ingest recomputes it from the edited text
  (since v0.21.2), and the edit replaces the old observation (new row, re-embedded by
  `embed-missing`). A file that is truly malformed (bad JSON, wrong types, empty text) is still
  rejected whole; every ingest records it, `memex obs sidecars` lists it under `malformed` with the
  line, and the SessionStart hook warns every new session until it is fixed.
- **Deleting detaches citations (v0.22.0).** `delete_observation_ids` runs `detach_source_refs`: an observation that cites a deleted one in `source_obs_ids` keeps the citation as a pending content hash (`obs_pending_sources`, also written to the sidecar) instead of a dangling id — SQLite reuses freed ids, so a stale id would silently cite a different observation. A dreamer duplicate merge redirects the citations to the survivor, and a `--full` rebuild's preservation detaches the citations of observations it drops
- **Sidecars are committed with their memo.** They live next to the `.md` file and move/rename with
  it; a sidecar has no `doc_path` field, so relocating the pair (`git mv`) IS a reassign.
- Details, format, and the full API live in `src/memex/sidecars.py` and
  `docs/2026-09-13-obs-sidecar-spec.md`.
