# Changelog

All notable changes to the memex plugin. Dates in YYYY-MM-DD.

## [0.16.1] — 2026-07-21

### Fixed

- **Deleting an observation now removes every row that mirrors it.**
  `observations` is mirrored by three tables keyed to observation id —
  `fts_observations` (rowid), `vec_observations` (rowid), and
  `observation_topics` (observation_id). Two delete paths each maintained
  their own list of those tables and disagreed:
  `dreamer._merge_duplicate_observations` cleaned two and never
  `observation_topics`, so every duplicate merge left tag rows pointing at
  deleted observations.

  The damage was invisible by construction. Every JOIN-ing read path
  discards orphans silently, so search results were never wrong — the only
  symptoms were a counter reporting observations the vault could not return,
  and orphaned vectors consuming KNN result slots before the join dropped
  them. A live vault carried 515 orphaned tag rows and 187 orphaned rows in
  each virtual table.

  The mirror list is now a single `_OBS_MIRROR_TABLES` registry that both
  paths route through via `delete_observation_ids`, with a coverage test that
  fails at CI if a table is added to `init_observation_schema` without being
  registered. Fixing only the dreamer would have left two hand-maintained
  lists free to drift again.

- **`memex obs stats` counted tag rows whose observation no longer existed.**
  `topic_observation_counts` and `retag_topic` now JOIN `observations`, so
  their counts agree with `fetch_observations_by_topic`, which always joined.
  (`memex obs topic <slug>` was unaffected — it already joined.)

- **`delete_observations_for_doc` deletes by observed id, not by `doc_path`.**
  `backfill obs` holds a SHARED advisory lock, so a concurrent writer can
  insert for the same document between the id SELECT and the DELETE. A
  `doc_path`-scoped DELETE would remove that row while leaving its mirror
  rows behind — manufacturing the orphans this release removes. The
  concurrent writer's row now survives intact.

- **Large deletes are chunked below SQLite's host-parameter limit.** That
  ceiling is 32,766 since SQLite 3.32 and 999 before it, so a single-statement
  prune of more than 999 ids passed on a modern runtime and raised
  `too many SQL variables` on an older one.

### Added

- **`memex obs orphans`** — reports index rows whose parent observation is
  gone, per mirror table. Read-only; `--apply` prunes, `--json` for scripts.
  Exits 1 when orphans exist and nothing was applied, and 2 when a mirror
  table could not be checked at all — a table that does not exist is omitted
  from `tables` and listed under `unchecked` on BOTH the human-readable and
  `--json` paths, so "not measured" never renders as "measured clean" on the
  surface a script consumes.

### Security / correctness notes

- `delete_observation_ids` distinguishes a mirror table that does not exist
  (skip) from one that exists but cannot be read — e.g. a `vec0` table on a
  connection that never loaded sqlite-vec, or a locked database. Both raise
  the same `OperationalError` and mean opposite things; treating the second
  as absence would skip that mirror's DELETE and remove the parent anyway.
  Existence is settled via `sqlite_master`; anything else aborts the delete
  with a message naming the fix. Mirrors are deleted before parents, so a
  partial failure leaves an unsearchable observation (recoverable) rather
  than an orphan (not detectable without this release's tooling).
- `memex obs orphans --apply` takes the shared `writer_lock`, so a concurrent
  `index rebuild --full` cannot swap the index file mid-prune and silently
  discard the transaction.
- The prune identifies and deletes orphans in a single statement.
  `observations.id` has no `AUTOINCREMENT`, so SQLite reuses freed ids; a
  collect-then-delete prune could destroy a legitimate tag belonging to a
  concurrent writer that had been granted a reused id.

## [0.16.0] — 2026-07-21

### Changed — BREAKING

- **`memex backfill obs` now REQUIRES an explicit `--replace` or `--append`.**
  Omitting both is an argparse error (exit 2) and nothing is written. The
  command deletes a document's existing observations in `--replace` mode, and
  for its whole life that was the silent default — so the destructive choice
  could be made by omission, which is how 12 observations were destroyed on
  2026-07-21 by a call that printed `{"stored": 5}`.

  **This flip is a no-op for every shipped caller.** v0.15.12 deliberately
  shipped these flags as optional and updated `commands/save.md`,
  `skills/memo-writing/SKILL.md`, `hooks/session-start.py`, and the batch
  extractor to pass `--replace`, so the plugin cache had already rolled before
  the requirement landed. What breaks is an ad-hoc or external caller that
  omits the flag — precisely the caller this protects against.

- **`store_observations(...)` requires a keyword-only `mode` argument.** There
  is no default, and it cannot be passed positionally. The 2026-03-16 incident
  — `store_observations` called twice, the second call wiping the first — was
  a *Python* incident, so leaving a silent default in the Python API would
  have closed only half the historical hole. Callers passing `mode="replace"`
  or `mode="append"` are unaffected; callers omitting it now fail loudly with
  a `TypeError` at the call site rather than silently destroying rows.

### Why this shipped as two releases

The behaviour could have been made mandatory in v0.15.12. It was staged
instead because the `memex` CLI runs live from source while skills, commands,
and hooks ship through the plugin cache — requiring a flag before the cache
had rolled would have broken `/memex:save` and the post-compaction memo path
for every user between the two events. Optional-flags-then-required is the
sequence that makes a breaking change land as a no-op.

## [0.15.12] — 2026-07-21

### Added

- **`memex backfill obs` now reports what it destroyed.** The command has
  always been REPLACE-all-for-document — `store_observations` deletes the
  document's existing observations before inserting — but it reported only
  what it stored. Extracting 12 observations for a memo and later extracting
  5 more for the same memo printed `{"stored": 5, "total": 5}` and left you
  with 5, not 17. A true statement about a call that had just deleted twelve
  rows, and invisible unless you diffed `memex obs stats` against an expected
  number. The output now carries `replaced` (rows this call destroyed),
  `skipped_duplicate`, and `mode`.

- **`--append` mode, and `--replace` to state the default explicitly.** The
  two are mutually exclusive. `--replace` remains the default so nothing
  breaks, but every shipped caller now passes it, which makes a future
  release that *requires* an explicit mode a no-op for them.

- **A stderr warning on net row loss only** (`replaced > inserted`). Routine
  re-extraction that grows a document's set stays silent by design: a warning
  that fires on every normal run is one operators learn to ignore, which is
  how the same data loss survived a "be careful at the call site" fix in
  March 2026 and recurred four months later.

### Fixed

- **Globally-deduplicated observations are no longer silently dropped.**
  `content_hash` is unique across the whole table, not per document, so an
  observation whose text already exists under a *different* document — or
  repeated twice within one batch — was skipped without incrementing any
  counter. The only symptom was `stored` < `total` with no stated reason.
  Now counted and reported as `skipped_duplicate`.

- **`delete_observations_for_doc` returns the parent `DELETE`'s `rowcount`**
  rather than the length of a preceding `SELECT`. `backfill obs` holds a
  SHARED advisory lock, so a concurrent writer for the same document can
  insert between those two statements, making the old count under-report what
  was actually destroyed. Callers surface this number to operators as a
  measured fact, and a guess presented as a measurement is the defect class
  v0.15.11 was spent removing.

## [0.15.11] — 2026-07-21

### Fixed

- **`memex index rebuild` no longer reports a preservation result on runs where
  no preservation happened.** An incremental rebuild printed
  `Preserved across atomic swap: 0/0/0/0 obs/topic-tags/fts/vec rows (no prior
  observations)` — two false claims at once, on a vault holding 15,682
  observations: no atomic swap occurs on an incremental run, and the vault was
  not observation-less. `rebuild_full` set the four `*_preserved` keys
  unconditionally, `rebuild_incremental` never set them, and the formatter read
  them with `stats.get(key, 0)` — silently converting "this run never reported"
  into "this run measured zero". The line reads exactly like the silent
  mass-data-loss failure mode it exists to rule out, on the one command where
  that failure is realistic; an operator could plausibly react by restoring a
  backup over a healthy index, turning a false alarm into real loss.

  The reporting now models four states and distinguishes them by key
  *presence*, never by value: preservation ran and carried rows (counts);
  ran and the prior index was genuinely empty (counts + note); ran and threw
  (`Preservation FAILED`); or never ran — silence on incremental and
  first-ever full rebuilds.

- **`--full --no-atomic` over an existing index silently destroyed
  observations while reporting success.** That path unlinks the live index
  outright, with no `ATTACH` and no carry-over, then printed the same
  reassuring `0/0/0/0 … (no prior observations)` line — asserting the vault
  had no observations on the exact run that deleted them. It now emits
  `⚠️  Observation preservation SKIPPED`, naming how many observations were
  destroyed (or admitting the count could not be taken) plus recovery steps.

- **All-zero preservation counts no longer claim the prior index was empty.**
  The `*_preserved` counts are measured *after* the dangling-reference filter,
  so they count rows carried over, not rows the old index held. All-zero has
  two causes: a genuinely empty prior index, or a mass folder rename/archive
  done without `memex obs reassign` in which every observation was dropped.
  A new `prior_observations` census (taken inside the existing `BEGIN
  IMMEDIATE` + `ATTACH` snapshot) separates them, and the drop case now warns
  instead of reassuring. A partial-drop warning was added for the same reason.

- **`memex index status` no longer renders failed counts as zeros.** Each
  `except sqlite3.OperationalError` branch in `get_index_status` recorded `0`,
  so a missing table or schema drift printed `Observations: 0` on a vault
  holding thousands. Failed counts now yield `None` and render as
  `unknown (query failed)`; an absent key renders as `not reported`. Relatedly,
  `embedded_chunks` no longer substitutes `total_chunks` as a "proxy" when
  sqlite-vec is unavailable — that asserted every chunk was embedded, the most
  reassuring possible answer, on precisely the setup where semantic search is
  most likely broken. It now reports unknown.

## [0.15.10] — 2026-07-15

### Added

- **`memex check` now reports curator-artifact freshness.** The output ends
  with a `--- Curator artifacts ---` section that reads the curator dashboard's
  `updated:` date (`_meta/curator-dashboard.md`) and the curator log's newest
  `## YYYY-MM-DD` heading (`_meta/curator-log.md`), compares both against the
  newest **non-archived** `topics/*.md` `updated:` date, and prints a
  `⚠ Nd behind newest topic edit` nudge when either trails by more than 7 days
  (`CURATOR_STALE_DAYS`). This catches the failure mode where the curator
  artifacts silently drift stale while the vault is actively tended in dev
  sessions that edit topics but never touch the dashboard/log. Design notes:
  the baseline is topic edits (not the wall clock, so a quiet vault doesn't
  nag); archived topics are excluded so an archive's `updated:` bump can't fake
  freshness; the log uses `max()` over its headings because entries aren't
  strictly chronological; "behind" clamps at 0. The section is hidden on vaults
  with no curator dashboard, and `memex check --json` gains an additive
  `curator_artifacts` key. No new flag — the section is intrinsic to `check`.

## [0.15.9] — 2026-07-08

### Fixed

- **Bare `[[project-name]]` references no longer surface as false-positive
  ghost nodes in `memex check`.** The crystallization/ghost-node detector
  resolved unresolved `[[wikilinks]]` against markdown filename stems +
  frontmatter aliases only. A project overview's file stem is `_project`, not
  the project slug, so a routine cross-project reference like
  `[[llm-org-cognition]]` or `[[duality-paper]]` from a sibling project's memo
  had nothing to resolve against and contaminated the OVERDUE/READY tiers. New
  `_project_folder_slugs()` maps every `projects/<slug>/_project.md` overview to
  its slug and merges it into the alias map on both the filesystem-fallback and
  Obsidian-native paths (a valid link target, just not a vote-casting source —
  mirroring the existing `_is_archived` pattern). A bare link to a
  not-yet-consolidated drift/fragment folder (no overview file) still correctly
  surfaces as a ghost. 5 regression tests added.

  *(This entry was backfilled during the 0.15.10 release; the 0.15.9 code
  shipped and synced on 2026-07-08 but its CHANGELOG entry was missed.)*

## [0.15.8] — 2026-07-07

### Changed

- **Memo-generation subagents are now explicitly pinned to Sonnet on every
  fallback path.** The automated Layer 2 (post-compaction) subagent already
  spawned with `model='sonnet'`; this release closes the gap on the
  **orphan/pending-memo retry** path. The `SessionStart` nudges (startup and
  resume) previously just said "Ask Claude to retry them" with no model, so a
  retry driven from a heavier main model could run memo generation on that
  model. All four nudges now instruct spawning **one background
  `model='sonnet'` subagent per pending memo** — memo generation is a
  sonnet-tier task and shouldn't burn a heavier model.
- **`/memex:save` gained a "Model guidance" note.** The inline Layer 1 flow
  remains primary and best-quality (the main agent writes the memo with full
  lived context — do *not* delegate to a subagent just to change models). But if
  you *do* delegate memo generation (to conserve a heavier main model or batch a
  backlog), pin the subagent to `model='sonnet'` to stay consistent with the
  automated fallback.

### Fixed

- **Docs said the Layer 2 subagent was Haiku; it has always been Sonnet.**
  `CLAUDE.md`'s memo-generation section is corrected to describe the background
  fallback as a Sonnet subagent, matching the actual hook behavior.

## [0.15.7] — 2026-06-22

### Added

- **`memex check --validate`** — a read-only frontmatter lint that catches the
  YAML-damage class: two keys glued onto one physical line (e.g.
  `status: archivedtitle: "..."`, which silently keeps a doc indexed *and* drops
  its title), a dangling `---` that traps the body inside the YAML block, a
  missing identity field (`title`/`name`), and files with no frontmatter at all.
  It deliberately does **not** enforce a `status` vocabulary — the vault uses a
  rich intentional set (`evergreen`, `stub`, `developing`, `superseded`, …), so
  an enum check would be pure noise; the damage class is the signal. `--json` for
  agents; exits non-zero when any issue is found (CI/launchd).

## [0.15.6] — 2026-06-22

### Added

- **`memex check --folders`** — a read-only audit that detects project-folder
  drift: cwd-fragment-shaped names (e.g. `Apps-arena`), duplicate/subset folders
  (one project's content scattered across two folders), and name≠canonical
  mismatches. Prints the exact `obs reassign` plan to consolidate (by subprefix,
  so it can't collide on `_project.md`); `--json` for agents; exits non-zero when
  high-confidence drift is found.
- **`project-consolidation` skill** — the safe SOP for merging drifted/duplicate
  project folders (confirm duplication → `obs reassign` preserving embeddings →
  verify the obs-count invariant), so this is self-service rather than tribal
  knowledge.

### Fixed

- **`memex session import` could still create `Apps-*` fragment folders.**
  `discover_sessions.py` used the lossy Claude-dir slug parser (the same gap
  v0.15.5 fixed for `memex sync`); it now uses the canonical `detect_project`
  via the true session cwd. Canonical project detection is centralized in one
  place (`utils`) and shared by sync + import, with a tripwire that warns when a
  non-canonical folder would be created.

## [0.15.5] — 2026-06-22

### Fixed

- **`memex sync` no longer re-fragments the vault with `Apps-*` folders.** Auto-memory
  sync derived vault folder names from the lossy Claude project-dir slug (e.g.
  `-Users-you-Documents-Apps-arena` → `Apps-arena`), ignoring `project_mappings`
  and git remote. It now reads the true `cwd` from a session transcript —
  validated against the dir's `/`→`-` encoding so a stray path can't mis-map —
  and feeds the canonical `detect_project()`, the same identity memos use. Folder
  names now match the rest of the vault (`arena`, not `Apps-arena`).
- **`project_mappings` from `~/.memex/config.json` now actually applies in CLI
  context.** `get_config()` returns the pydantic settings dump, but `Settings`
  had no `project_mappings` field, so `extra="ignore"` silently dropped it —
  `detect_project()`'s explicit-mapping check was a no-op outside the hook
  raw-json path (split-brain detection). Added the field.

Reviewed by a three-reviewer fan-out (codex + kimi + in-house). +8 tests.

## [0.15.4] — 2026-06-22

### Fixed

- **Graph stats no longer over-count broken links from transcripts and
  auto-memory.** `extract_wikilinks` — the indexer that populates the `wikilinks`
  graph table — now skips raw `projects/*/transcripts/` and `projects/*/auto-memory/`
  files as link *sources* and strips fenced/inline code + ANSI before scanning,
  matching the v0.15.3 crystallization checker. Roughly 61% of previously-reported
  broken links came from transcript phantoms (e.g. `[[$MEMO_PATH]]`). Transcripts
  remain valid link *targets*; only their vote-casting as sources is removed. Fully
  materializes after a `memex index rebuild --full`.

### Changed

- **New `memex.scripts.wikilink_filters` module** is now the single source of truth
  for wikilink noise-filtering (`strip_code_spans`, `is_noncurated_source`), shared
  by the indexer, the deep-retrieval graph expansion (`ask`), and the crystallization
  checker — so the graph table and the checker can never drift again. `strip_code_spans`
  is newline-preserving, keeping per-link `line_number` accurate in the graph table.

## [0.15.3] — 2026-06-22

### Fixed

- **`memex check` no longer launches Obsidian.** The availability probe invoked
  the Obsidian binary to check it, which *opened the app* (on whatever vault was
  last used) when Obsidian wasn't already running — disruptive during a headless
  `memex check`. It now checks for a live Obsidian process first and falls back
  to the filesystem scan without launching anything. `ensure_running()` remains
  the explicit way to launch.
- **Crystallization checker ignores raw transcripts as link sources.** Raw
  conversation/terminal dumps in `projects/*/transcripts/` (and `auto-memory/`)
  emitted wikilink-shaped fragments (`[[$MEMO_PATH]]`, `[[%s]]`, etc.) that
  survived code-span stripping via fenced-block edge cases and dominated the
  ghost-node "OVERDUE" tier. These folders are now excluded as vote-casting
  *sources* (still valid link *targets*), mirroring the existing `status:
  archived` skip. Real cross-project concepts still surface on curated votes;
  only transcript-only phantoms drop (OVERDUE 30→11 on the author's vault).

## [0.15.2] — 2026-06-17

### Fixed

- **`memex ask --depth thorough` hung for minutes on a real-size index.** The
  thorough-mode chunk vector search joined `fts_content` (an FTS5 table with no
  `path` index) *inside* the KNN query, so SQLite scanned every doc as the outer
  loop and re-ran the vec match per doc — O(docs × KNN). Rewrote it KNN-first
  (matching `memex search`): run the KNN on `vec_chunks`, push the project filter
  into the KNN via the v0.15.0 metadata column, then enrich titles/dates with a
  single batched lookup. ~1.4s now instead of hanging.

### Changed

- **Query embeddings are no longer pre-truncated.** `embed_query` returns the
  model's native-dimension vector; each search path truncates it to whatever its
  vec table actually stores. This keeps vector search working during the window
  after you set a lower `index_dimensions` but before running
  `memex index migrate-vec` — previously the query/stored dimensions mismatched
  and search silently fell back to keyword-only.

### Added

- **`memex index vacuum`** — reclaims the disk space left behind after
  `migrate-vec` drops the old larger-dimension vec tables (`migrate-vec` now
  prints a hint pointing at it). Runs `VACUUM` plus a WAL TRUNCATE checkpoint, so
  the `.sqlite` file actually shrinks (it's in WAL mode, where a plain VACUUM
  leaves the freed pages in the sidecar). Needs free disk roughly equal to the
  current index size.

## [0.15.1] — 2026-06-17

### Fixed

- **Silent batch-embedding under-population.** `embed_content(contents=[str, str,
  …])` is interpreted by the google-genai SDK (both 1.x and 2.x) as the *parts of
  a single Content*, so the API returns **one** embedding for the whole list and
  the rest land as `None` — an N-text batch silently produced 1 vector + N−1
  gaps. An older SDK auto-wrapped bare strings as separate contents, so batch
  embedding worked when indexes were built and regressed on a later SDK; only
  *new* embeds were affected. Fix: each text is now wrapped as its own
  `types.Content`, yielding one embedding per text (verified on the live API).
  Symptom this resolves: `memex index embed-missing` embedding ~1 item per call.
  Regression test: `tests/test_embedding_batch_contents.py`. If you ran rebuilds
  or `backfill obs` on an affected SDK, run `memex index embed-missing` once to
  fill any gaps.

## [0.15.0] — 2026-06-17

Combined vector-index upgrade: Matryoshka dimensionality truncation + vec0
metadata filter-pushdown. Opt-in and fully reversible — the embedding cache
keeps full-fidelity vectors, so any dimension can be regenerated without
re-embedding. **No behavior change unless you opt in** (see `index_dimensions`).

### Added

- **`index_dimensions` config** (`embeddings.index_dimensions`). Matryoshka-
  truncate the vectors stored in the index and used for queries to a smaller
  dimension (e.g. 768) while the API + cache keep the native `dimensions`
  (3072). 768d is ~4× smaller vector storage for ~0.26% retrieval-quality loss
  (Gemini Embedding 2 is MRL-trained). Omit it for no truncation — the default.
- **vec0 metadata columns + KNN filter-pushdown.** `vec_chunks` /
  `vec_observations` now carry `doc_project` / `doc_type` / `doc_date`, and
  `search` pushes `--type` / project / `--since` / `--before` filters *inside*
  the KNN. This fixes recall-collapse: a narrow `--since` no longer discards the
  whole semantic candidate window before filtering.
- **`memex index migrate-vec`** — migrate an existing index in place to the
  configured `index_dimensions` + metadata columns. Truncates stored vectors
  (no re-embed, no API calls) and populates metadata, with an atomic per-table
  swap. Run after setting `index_dimensions`.

### Changed

- **`google-genai` >= 2.0** (verified on 2.8.0). The embedding API surface
  (`Client.models.embed_content`, `EmbedContentConfig`) is unchanged from 1.x;
  the floor is raised to match where fresh installs already resolve.

### Notes

- Pre-0.15.0 indexes keep working unchanged: bare vec tables (no metadata,
  native dim) fall back to bare KNN + post-filter automatically. To adopt the
  new features, set `index_dimensions` (optional) then run
  `memex index migrate-vec`. Do NOT run `memex index rebuild` before migrating —
  a dimension-mismatch guard warns and skips rather than re-embedding.

## [0.14.3] — 2026-06-17

Ergonomics + reliability pass from an external-dependency audit (Claude Code
2.1.x, Obsidian 1.13.1, embedding stack). No breaking changes; no re-embed.

### Added

- **`displayName`** in the plugin manifest ("Memex — Personal Knowledge Base"),
  shown in the `/plugin` picker (Claude Code 2.1.143+).
- **`outline` subcommand** for the Obsidian CLI wrapper. The `outline()` method
  (heading structure, `--format tree|md|json`) was implemented but had no
  argparse subcommand, so it was unreachable from the CLI — now wired. Useful
  for inspecting structure before condensing.
- **`garden-tending` skill frontmatter** — an `argument-hint`
  (`diagnose|condense|connect|crystallize|grow|maintain`) and `effort: xhigh`
  so vault-wide tending runs at full reasoning effort.

### Changed

- **sqlite-vec 0.1.6 → 0.1.9.** Picks up proper DELETE space-reclamation
  (0.1.7+): incremental rebuilds and garden-tending archival churn now reclaim
  vector space once a chunk's worth of vectors is deleted, instead of leaving
  dead space in the index. No re-embed required (same 3072d float32 format);
  smoke-tested against the live index (hybrid + vector search verified).
- **Obsidian CLI wrapper reliability.** `is_available(deep=True)` now runs a
  real liveness query (`vault`) after the version check, so a *wedged* renderer
  (still accepts connections but returns empty for every real query) is detected
  and callers fall back to the SQLite graph queries instead of silently getting
  empty results. CLI calls are also serialized through a best-effort
  cross-process lock so a burst of concurrent calls (e.g. parallel
  garden-tending agents) can't dogpile and wedge the single renderer.

### Fixed

- **Atomic hook state writes.** PreCompact's pending-memo signal and
  UserPromptSubmit's nudge state now write via temp-file + `os.replace` (atomic
  on POSIX), so a hook killed mid-write can't leave a truncated file — which
  PreCompact's reader would skip as corrupt JSON, orphaning the pending memo.
- **Doc accuracy.** The running Obsidian app is 1.13.1 (installer 1.12.4), not
  1.12.5 — corrected across the rules docs, with the runtime-vs-installer
  dual-version scheme and a fan-out wedge-hazard note. Native Obsidian search
  and vault-wide `tasks` re-confirmed broken at 1.13.1 (async-IPC race), so FTS
  stays canonical. Clarified that Gemini Embedding 2 reached GA in April 2026
  (distinct from the local 2026-05-07 config flip) and is the latest model.

## [0.14.2] — 2026-06-16

Further precision tuning for the crystallization checker (follow-on to 0.14.1).

### Fixed

- **Archived files no longer cast ghost-node votes.** The markdown fallback now
  skips `status: archived` files as link *sources* (they remain valid link
  *targets* — their filename stems and aliases stay in the resolvable set,
  mirroring Obsidian, since the file still exists on disk). Dead or duplicated
  notes (e.g. archived cwd-fragment memos) no longer inflate ref counts for
  concepts that should not crystallize.
- **Tighter noise filtering.** `NOISE_REGEXES` now also drops ISO-date-prefixed
  memo links (`2026-02-16-…`), explicit `.md` file links, `@handles`, and short
  ALL-CAPS acronyms (`X`, `MCP`, `SSRN`) — real topics are kebab-case. Combined
  with the archived-source skip, this took the author's vault from 39 to ~31
  actionable ghost nodes with no loss of genuine candidates. Three regression
  tests added.

## [0.14.1] — 2026-06-16

A bug-fix release for the Obsidian-less crystallization fallback.

### Fixed

- **`memex check` filesystem fallback no longer mis-parses code as ghost nodes.**
  The v0.14.0 markdown fallback (`scan_unresolved_via_markdown`) ran the wikilink
  regex over raw file text including fenced code, so TOML `[[section]]` headers
  (e.g. wrangler.toml `[[d1_databases]]`), bash `if [[ ... ]]` conditionals, and
  ANSI terminal escapes inside code blocks were reported as actionable ghost
  nodes. A new `_strip_code_spans()` helper removes fenced/inline code and ANSI
  CSI sequences before scanning — mirroring how Obsidian's metadataCache parser
  ignores code spans. On the author's vault this dropped actionable ghost nodes
  from 129 to 39 (pure false-positive elimination). Two regression tests added.

## [0.14.0] — 2026-06-09

A garden-tending quirk-fix release: four fixes surfaced while running a full
vault-maintenance pass end-to-end (which doubles as an integration test of the
plugin).

### Added

- **`memex session reconcile-orphans [--apply]`** — clears stale pending-memo
  signals whose session already has a Layer-1 memo. A `PreCompact` signal
  persists until cleared; if `/memex:save` already wrote a memo, the signal is
  stale. A signal is "covered" when a memo exists for the same project dated
  within `--window` days (default 2). Dry-run by default; `--apply` deletes the
  covered ones, leaving genuine retries.
- **`memex check` filesystem fallback** — when Obsidian isn't running,
  `crystallization_check` now degrades to a markdown scan (resolves `[[links]]`
  against filename stems + frontmatter aliases) instead of erroring out after a
  15s timeout. Makes the check usable headless/cron. All fidelity gaps over-
  report (a real link looks like a ghost), never the reverse — safe degraded mode.

### Fixed

- **Skill `!`command`` dynamic-context injection no longer clobbers `awk`
  positional fields.** The harness applies slash-command argument substitution
  (`$1`/`$2`/`$ARGUMENTS`) to `!`command`` bodies before execution, so
  `awk '{print $2}'` became `awk '{print }'`. Replaced with `cut -d: -f2`.
  Rule: never use `$<digit>` inside a bang-command injection.
- **Garden-tending diagnostic** no longer false-flags a substantial overview
  that lacks `memos_digested` frontmatter as "never condensed" — it now reports
  `MAINTAINED` (add the frontmatter; don't re-condense).

### Notes

- Documented the stale-`.dist-info` venv-churn cleanup as a release-SOP step.

## [0.13.0] — 2026-05-25

First 0.13.x feature release. Promotes a manual SQL UPDATE pattern (used
during the 2026-05-25 `Apps-pi-proxy/` → `pi-proxy/` folder migration that
preserved 68 obs + 249 chunks across the rename, vs the morning's
video-production migration that lost 43 obs to cascade-delete) to a
first-class CLI command with safety guarantees the manual pattern lacked.

### Added

- **`memex obs reassign --from-prefix X --to-prefix Y`** — rewrite
  `doc_path` prefix on observations + chunks in a single atomic
  transaction. Dry-run by default; `--apply` to commit; `--json` for
  scripting. Operates on the two `doc_path`-holding tables; sister
  tables (`fts_observations`, `vec_observations`, `vec_chunks`,
  `observation_topics`) join by rowid / `observation_id`, not `doc_path`,
  so a `doc_path` UPDATE preserves all index mirror state automatically.

- **`reassign_doc_path_prefix()` helper** in `src/memex/observations.py`
  for direct programmatic use (caller owns transaction boundaries, matches
  `index_document` / `embed_chunks` convention).

### Safety guarantees

- **Prefix-only rewrite** via `WHERE SUBSTR(doc_path, 1, ?) = ?` +
  `SET doc_path = ? || SUBSTR(doc_path, ? + 1)`. Equivalent to the
  manual `REPLACE()` pattern on clean paths but strictly safer when a
  folder name appears later in the path
  (e.g. `projects/Apps-X/memos/Apps-X-backup.md`), and treats SQL
  wildcards (`%`, `_`) inside the prefix as literal characters rather
  than patterns.

- **Re-run footgun guard**: rejects `to_prefix.startswith(from_prefix)`
  (e.g. `projects/X` → `projects/X-old`) which would compound the
  prefix on re-run by matching already-renamed rows.

- **Invariant check on `--apply`**: `matched == updated` for both tables
  before commit, ROLLBACK + exit 2 otherwise.

- **UNIQUE collision**: `chunks` has `UNIQUE(doc_path, chunk_index)`.
  Collisions raise `IntegrityError` → ROLLBACK + exit 3 (data loss is
  worse than a failed migration).

- **`writer_lock`** wrapping prevents loss during concurrent
  `memex index rebuild --full` (matches `extract.py::main` and
  `memex.dreamer` convention).

- Rejects empty `from_prefix` and identical `from_prefix == to_prefix`
  before any SQL runs.

### Tests

11 regression tests in `tests/test_obs_reassign.py` covering dry-run,
apply, total-counts-preserved, prefix-only-substr, SQL-wildcard literals,
re-run footgun, empty/identical-prefix rejection, UNIQUE collision, and
no-implicit-commit. 233 total passing (was 222).

### Documentation

`.claude/rules/plugin-authoring.md` Vault Operations section rewritten to
call the CLI (with full 6-step folder-rename SOP), plus rationale for
`SUBSTR` vs `REPLACE` and the UNIQUE collision exit-3 path.

### Release process

Two review rounds (in-house code-reviewer + plugin-validator) before tag.
Round 1 caught 2 HIGH (`LIKE` wildcard injection, `marketplace.json`
version drift) + 3 MEDIUM (re-run footgun, missing `writer_lock`,
schema-init DDL in dry-run) — all fixed in commit `f4943a9`. Round 2
verified the fix-set as correct, no wrong-fixes.

## [0.12.2] — 2026-05-25

Wrap-up release closing four open threads from v0.12.1. Two real bug
fixes (mark-saved cross-contamination, an unscrubbed write path), one
helper extraction that consolidates the v0.12.1 scrub-gate pattern into
a shared utility for future write-paths, and a doc-tracking cleanup.

### Fixed

- **`memex mark-saved` no longer cross-contaminates sessions.** The
  selection heuristic (newest state file by mtime, no project filter)
  would mark a session in project B's signal when invoked from project A
  if B's state file was the most recently touched. Now prefers
  `CLAUDE_CODE_SESSION_ID` (Claude Code 2.1+ exposes this in env to any
  tool/CLI invoked from a session) for unambiguous selection, and falls
  back to newest-by-mtime only when the env var is absent. When the env
  var points at a session with no state file (config drift), warn to
  stderr and fall back to mtime rather than silently fixing the wrong
  session. Real-world trigger: 2026-05-25 afternoon `memex mark-saved`
  invoked from the memex cwd cleared the kimi-plugin-cc session's
  signal instead of memex's own.
- **`extract.py::append_contradictions_to_memo` now scrubs before
  write.** The contradictions-frontmatter rewrite path bypassed the
  PostToolUse hook (Python writes via `Path.write_text`, not via
  Claude's Write tool). Body content is normally already-scrubbed
  (post-v0.12.0 writes or retroactive sweep), but a v0.11.x-era memo
  touched here for the first time would have escaped. Now uses the new
  shared `safe_write_text` helper.

### Added

- **`memex.scrub.safe_write_text(path, content) -> int`** — shared
  write-gate for any Python code path that writes prose to disk without
  going through Claude Code's Write/Edit/MultiEdit tools (the boundary
  the PostToolUse hook covers). Pre-scrubs content via `scrub_text`,
  writes via `path.write_text`, returns the redaction count. Single
  audit point — anywhere `Path.write_text` is called on user-prose
  content, prefer this helper.

### Changed

- `sync_auto_memory.py::sync_file` refactored to use `safe_write_text`
  (the v0.12.1 fix is functionally identical, just shares the helper
  now instead of inlining the scrub call).

### Tests

- 4 new tests in `tests/test_mark_saved.py`: env-var-driven selection
  picks the right session even when another session's state file is
  newer; mtime fallback when env var absent; warning when env var
  points at a missing state file; pending-memo signal cleanup.
- 4 new tests in `tests/test_scrub.py::TestSafeWriteText`: clean
  content unchanged, single-secret redaction, multi-secret redaction,
  OSError propagation on bad parent path.
- 222 total passing (up from 214 in v0.12.1).

## [0.12.1] — 2026-05-25

Pattern-catalog expansion and one real gap closure. The v0.12.0 catalog
covered the providers most likely to leak via subagent transcripts
(Anthropic, OpenAI, Slack, GitHub, Google, AWS, JWT, PEM); v0.12.1
extends to four more high-confidence-shape providers, and patches the
`memex sync` path that previously bypassed the PostToolUse hook because
it writes via `Path.write_text` (not via the Claude `Write` tool).

### Added

- **HuggingFace tokens** (`hf_[A-Za-z0-9]{34,}`) — covers user-access
  tokens and fine-grained tokens. Unique prefix; low false-positive risk.
- **Stripe keys** (`(sk|pk|rk)_(live|test)_[A-Za-z0-9]{24,}`) — covers
  secret, publishable, and restricted keys in both live and test mode.
  Uses `_` (not `-`) so this never overlaps with the existing `sk-ant-*`
  / `sk-proj-*` / generic-sk patterns above it.
- **Notion integration secrets** (`secret_[A-Za-z0-9]{43}`) — 50-char
  total format. The 43-char alphanumeric run after `secret_` is
  shape-distinctive enough to avoid prose collisions with phrases like
  `secret_password`.
- **Sentry DSNs** (`https?://<32hex>(:<32hex>)?@*sentry*/<id>`) — covers
  both the modern (public-only) and legacy (public+secret) DSN forms;
  matches SaaS hosts (`*.ingest.sentry.io`) and self-hosted Sentry
  installs that include `sentry` in the host name.

### Fixed

- **`memex sync --apply` now scrubs auto-memory content before disk.**
  The PostToolUse hook from v0.12.0 only gates Claude's
  `Write`/`Edit`/`MultiEdit` tool invocations. `memex sync` writes via
  `Path.write_text`, bypassing the hook entirely — so secrets in
  `~/.claude/projects/<project>/memory/*.md` files (which Claude's
  memory system writes without any scrub gate of its own) would
  round-trip into the vault un-redacted and become discoverable via
  search. `sync_file()` now pre-scrubs the assembled content via
  `scrub_text(content, apply=True)` before the disk write, and surfaces
  the redaction count in verbose output (`[scrubbed: N]`).

### Tests

- 13 new scrubber tests: 8 pattern-detection tests covering each new
  provider (using runtime-concatenation fixtures so source bytes don't
  trip GitHub push protection on Stripe/Sentry shapes), plus 5 new
  prose-FP tests guarding against false matches on phrases like
  `secret_password`, `hf_dataset = load_dataset(...)`, `sk_live_demo`,
  and the Sentry docs URL.
- 4 new sync-scrub tests covering: scrub on a single secret, scrub on
  multiple secrets, clean-content has no `scrubbed` field in the result
  dict, and dry-run does neither.
- 214 total passing (up from 201 baseline).

### Retroactive sweep

Targeted scan (memos + topics + `_meta` + auto-memory + transcripts)
found two `pk_live_*` Stripe-shape matches in a single research-starter
transcript (an embedded HuggingFace publishable key from a captured
API response). Publishable Stripe keys are low-risk by definition, but
the scrubber is shape-based, not value-based — both occurrences were
redacted to `<REDACTED:stripe>` for consistency with the v0.12.0 policy.
No other vault content matched the new patterns.

## [0.12.0] — 2026-05-25

First feature release in the 0.12.x line. Introduces a secret-scrubber
CLI + library + deterministic write-time hook, triggered by an incident
in which a subagent probe sequence read a local config file and surfaced
three API keys into an on-disk transcript. The lesson — instruction-
based controls fail in exactly the cases they're meant to catch — drove
the architectural choice to make a PostToolUse hook (not subagent
instructions) the primary defense.

### Added

- **`memex scrub <path>`** CLI + library at `src/memex/scrub.py` with
  shims at `src/memex/scripts/scrub.py` and `scripts/scrub.py`. Detects
  API keys, tokens, and PEM private-key blocks via curated high-precision
  regex (Anthropic, OpenAI variants, generic-sk + generic-sk-vendor,
  GitHub PATs, Google API, AWS access keys, Slack tokens, JWT, private-
  key blocks). Specificity-first overlap resolution. Idempotent
  (`<REDACTED:provider>` markers don't match any pattern, so re-scrub
  is a no-op). Atomic write via tempfile + `os.replace` + fsync;
  preserves CRLF line endings byte-for-byte; safe on long filenames
  (truncates `mkstemp` prefix to leave NAME_MAX headroom). Exit codes:
  `0` clean / `1` dry-run with matches / `2` apply error.
- **`hooks/post-tool-use.py` PostToolUse gate.** Auto-scrubs every
  `Write` / `Edit` / `MultiEdit` operation targeting
  `projects/<name>/memos/**` or `projects/<name>/auto-memory/**`. The
  deterministic primary defense — doesn't depend on subagent compliance.
  Other paths (transcripts, topics, etc.) pass through untouched.
  Errors log but never block the user's write.
- New step in **`commands/save.md`** (4b — "Scrub for Secrets") and
  **`skills/memo-writing/SKILL.md`** (After Saving step 2) calling
  `memex scrub --apply` before observation extraction. These are the
  belt-and-suspenders layer; the PostToolUse hook is the primary control.
- L2 subagent prompt in **`hooks/session-start.py`** now includes
  explicit "do not transcribe API keys..." guidance plus a
  `memex scrub` invocation as a final guard. Path arguments are
  shell-quoted and the Python f-string escapes single quotes in
  `transcript_path` / `project` to prevent literal-corruption hazards.
- `memex scrub` documented in CLAUDE.md's command table.

### Quality

- 54 new tests (46 for the scrubber + 8 for the PostToolUse hook).
  Regression tests for the overlap-algorithm specificity invariant,
  CRLF preservation on read+apply round-trip, long-filename mkstemp
  prefix, atomic-write temp cleanup on rename failure, and idempotency
  of `<REDACTED:provider>` markers.
- Three review rounds: in-house Claude code-reviewer (4 issues — 2 HIGH
  overlap algorithm + self-test FP, 2 MEDIUM atomic write + per-file
  errors — all addressed), Codex correctness audit (3 MEDIUM ship-
  blockers — long-filename mkstemp prefix, CRLF normalization on apply,
  unquoted memo-path in L2 prompt — all addressed), Claude design
  challenge (architectural pushback addressed by adding the PostToolUse
  hook), plugin-validator (PASS).

### Migration

Existing memos and auto-memory files are not auto-scrubbed by the hook
installation alone (the hook only fires on new writes). Backfill once:

```bash
memex scrub "$(memex path)" --apply
```

This runs against the full vault — memos, auto-memory, project docs,
topics, and transcripts. Idempotent and safe to re-run. Transcripts
are typically gitignored but still readable on disk; scrubbing them
closes the local-disk exposure window.

### Notes on what the scrubber does NOT catch

Line-wrapped keys (e.g. word-wrapped inside a markdown table), keys
inside base64-wrapped JSON payloads, low-entropy custom-format tokens,
secrets in commit messages (out of vault scope), and providers not in
the catalog (HuggingFace `hf_*`, Stripe `sk_live_*` / `pk_live_*`,
Notion `secret_*`, Sentry DSN URLs — slated for v0.12.1). Treat the
scrubber as one layer of defense, not the only one. If you're saving
a memo that intentionally discusses a secret, redact it manually
rather than relying on the scrubber.

## [0.11.6] — 2026-05-25

One-character bug-fix release closing a latent flaw in the v0.11.5
idempotency guard.

### Fixed

- **`grep -Fxq` flag-parsing in the topic-signal dedup.** The v0.11.5
  guard `grep -Fxq "$SIGNAL_LINE" "$TOPIC_FILE"` silently failed when
  `$SIGNAL_LINE` started with `- ` (the bullet prefix grep parsed as a
  flag). The dedup never fired and duplicate signal lines accumulated
  on touched topics whenever the title or bullet content contained
  anything grep tried to interpret as an option. Fix: add `--` to
  terminate option parsing in both grep calls in `commands/save.md`
  (the signal-line dedup AND the adjacent `## Recent signals`
  section-existence check, hardened belt-and-suspenders even though
  the literal pattern doesn't start with `-` today).
- **Skill prose pointer mirrors the requirement.**
  `skills/memo-writing/SKILL.md` step 3 now mentions `grep -Fxq --`
  explicitly so future readers transcribing the bash loop don't
  reintroduce the bug.

### Migration

None. Existing duplicate signal lines created during the v0.11.5
window are cosmetic; the user manually deduped or left them for the
next garden-tending pass. Future saves no longer hit the bug.

## [0.11.5] — 2026-05-17

Bug-fix release. The `/memex:save` topic-stamping step in
`commands/save.md` was not idempotent: when a memo's frontmatter listed
multiple slugs that all redirect to the same canonical topic (e.g.
`claude-code-plugins`, `plugin-architecture`, `plugin-development` all
→ `Claude-Code-Plugins`), the bash loop appended the same signal line
once per input slug. One save could leave two- or three-times duplicates
on the canonical topic file. The same pattern hit
`multi-agent-code-review` → `multi-agent-review`. No data loss — just
visual noise in `## Recent signals` sections.

### Fixed

- **Idempotent topic stamping.** The bash loop in
  `commands/save.md` step 6 now wraps the append in
  `grep -Fxq "$SIGNAL_LINE" "$TOPIC_FILE"` so the same line is never
  written twice to the same topic file. `-F` treats `[[`, `|`, `]])`,
  `.` as literal (no regex surprises); `-x` requires whole-line match
  (no substring false positives). Also covers the case where
  `/memex:save` re-runs on an existing memo across sessions.
- **Skill instruction mirrors the requirement.**
  `skills/memo-writing/SKILL.md` step 3 now explicitly states the
  dedup requirement so out-of-band invocations (skill triggered
  without going through the slash command) inherit the same behavior.

### Migration

None. Existing duplicate signal lines in topic files are cosmetic;
clean them by hand when convenient or let the next garden-tending
pass handle them. Future saves will no longer create new duplicates.

## [0.11.4] — 2026-05-13

Hotfix for v0.11.3 across four rounds of multi-reviewer audit (3+codex
on the v0.11.3 ship, then 4 on the fix-set, then 1 narrow gate on the
final delta — 22 + 10 findings total). The audit caught two HIGH-severity
data-loss paths in v0.11.3 and several class-of-bug repeats; this release
hardens all of them. No schema changes — drop-in upgrade.

### Fixed

- **Partial observation preservation no longer ships a half-baked index
  (HIGH).** v0.11.3's `rebuild_full --atomic` caught `sqlite3.OperationalError`
  during ATTACH-old preservation and only logged to stderr, then the
  `finally` block committed and the atomic swap proceeded. A disk-full
  partway through the four INSERT…SELECT statements would have produced
  a "successful" rebuild with broken observation FTS/vector search and
  no error signal — strictly worse than the May 7 wipe (which at least
  failed loudly). Preservation now runs inside `SAVEPOINT obs_preserve`;
  any exception triggers ROLLBACK, sets `stats["preservation_error"]`,
  raises `RuntimeError` with a stable prefix, and the CLI exits 4 with
  a clear message instead of falling through to the swap.
- **Fresh-install slash commands no longer break out of the box (HIGH).**
  README/SETUP Quick Start used to install the plugin before the `memex`
  CLI, so the first `/memex:status` after install failed with
  `memex: command not found`. Quick Start is reordered: `uv tool install`
  is now Step 1, plugin install is Step 2.
- **`redirect_to:` resolver handles cross-namespace targets (P0).** The
  bash resolver in `commands/save.md` hardcoded `$VAULT/topics/$slug.md`,
  so real-world redirects like `topics/bloom.md → projects/clawd-world/_project.md`
  silently dropped signals. Resolver extracted to a Python module +
  `memex topic resolve` CLI subcommand. Bare slugs resolve under
  `topics/`; targets containing `/` resolve as vault-relative paths
  (with auto `.md` suffix). Path traversal (`../../etc/passwd`) is blocked
  via `Path.resolve()` + containment check.
- **Cycle detection in redirect chains (P0).** The 5-hop limit is now a
  fallback, not the primary guard. The resolver tracks a visited-set
  and reports cycles as `WARN: redirect cycle detected: A -> B -> A`
  with the actual chain — not the previous generic
  "exceeded 5 hops" message.
- **CHANGELOG migration note for v0.11.3 was factually inverted.**
  v0.11.3's text said terminal-archive (`status: archived` without
  `redirect_to:`) would "silently land on the archived stub". Actual
  behavior is "skip with stderr warning". The note is rewritten to
  describe both archive shapes correctly.
- **`batch_extract_observations.py` exit code reflects partial failure.**
  Previously exited 0 if any single memo succeeded — a run of 1-ok +
  99-store-failed exited 0, hiding data loss from automation. Now
  exits 0 only when all results are ok-or-skipped, 1 when no success,
  2 when partial failure (matches the v0.11.0 `backfill obs` convention).
- **`commands/status.md` no longer hardcodes `~/.memex/pending-memos`.**
  The path resolves through `state_dir` config, which the hardcoded
  pipeline ignored. Replaced with `memex context` lookup.
- **`init_observation_schema` no longer commits internally.** Restores
  the v0.11.1 "callers own transactions" convention. All call sites
  audited; each already commits downstream.
- **`config.json.example` cleanup**: prior fix mistakenly claimed
  `project_mappings` was an unused phantom field. It's actively read by
  `detect_project()` in `src/memex/scripts/utils.py` as priority-1
  project name override. Example block restored with accurate comment.

### Changed

- **Observation preservation refactored to registry + helper.** Module
  constant `_OBS_PRESERVATION_TABLES` lists every preserved table.
  Test `test_preservation_registry_covers_init_schema` enforces that
  any new table added to `init_observation_schema` either appears in
  the registry or in the documented FTS/vec special-case branches —
  converts the v0.11.3 fts_observations bug class from
  runtime-symptom to CI-time discovery.
- **`memex topic resolve <slug>`** is a first-class CLI subcommand
  (registered under the new `topic` Typer group). Used internally by
  `/memex:save` and available standalone for testing or scripting
  redirect-aware tools.

### Added

- **Advisory file lock at `~/.memex/locks/full-rebuild.lock`** prevents
  `memex backfill obs` and `memex.dreamer` from racing against an
  in-progress `memex index rebuild --full`. New helper
  `memex.db_utils.writer_lock()` (context manager, `LOCK_SH | LOCK_NB`)
  wraps observation-write call sites in `extract.py` and `dreamer.py`.
  `--full` rebuild acquires `LOCK_EX | LOCK_NB`; contention exits 3
  with a clear message. Also: `BEGIN IMMEDIATE` on the old DB before
  ATTACH belt-and-suspenders against stragglers that haven't yet hit
  the lock check.
- **`tests/test_topic_resolve.py`** — 24 tests covering the redirect
  resolver: slug/path resolution, cross-namespace targets, cycle
  detection, quoted/comment/whitespace edge cases, path traversal
  blocked, binary-target graceful handling, empty frontmatter
  fall-through, auto-`.md` suffix on path-form targets.
- **`tests/test_batch_extract_observations.py`** — 13 tests for
  `parse_json_array` (fenced JSON, prose preamble, trailing prose,
  nested arrays, empty input, whitespace-only).
- **Additional tests in `tests/test_index_rebuild.py`**: registry
  coverage, vec_observations preservation across atomic swap,
  observation_topics broken-ref filter, preservation-failure aborts
  swap, CLI exits 4 on preservation failure, CLI re-raises unrelated
  RuntimeErrors.
- **FTS5 rowid invariant documented in code** at both the
  `INSERT INTO fts_observations` site (`extract.py`) and the
  `CREATE VIRTUAL TABLE` site (`observations.py`). Future maintainers
  who change either side know the preservation path depends on the
  equality `fts_observations.rowid == observations.id`.
- **`memex index status` listed in CLAUDE.md CLI commands table** (was
  documented in prose only).

### Migration notes

- Existing installs: no action required. The fixes are internal
  hardening + UX tightening. A future `memex index rebuild --full`
  will exercise the new SAVEPOINT-protected preservation path.
- If running automation that invokes `memex backfill obs` or
  `memex.dreamer` while a rebuild may be in progress: the new lock
  causes the backfill/dreamer to exit 3 on contention rather than
  silently lose writes. Wrap automation in retry-with-backoff if you
  need transparent recovery.

---

## [0.11.3] — 2026-05-13

Three fixes addressing curator-tending findings: redirect-aware signal
routing, fast `memex status` on large indexes, and observation preservation
across atomic full rebuilds. No schema changes — drop-in upgrade.

### Fixed

- **`memex status` no longer hangs at 100% CPU on large indexes.**
  `count_embedding_gaps` was running `LEFT JOIN chunks × vec_chunks`
  against the sqlite-vec virtual table — the join can't use an index, so
  it probed per-row (~107s on 136K chunks). Replaced with a
  `COUNT(chunks) - COUNT(vec_chunks)` delta — ~2000× faster (107s → 53ms).
  The per-doc DISTINCT query only runs when a gap actually exists (rare
  + actionable). Observations LEFT JOIN stays (low cardinality).
- **`rebuild_full --atomic` preserves observations across the swap.**
  Background: observations are extracted by sonnet subagents
  (`memex backfill obs --stdin`), not derived from documents at index
  time. The May 7, 2026 incident on the maintainer's vault wiped ~3265
  observations because the atomic swap installed a fresh empty DB.
  Recovery cost ~$10-20 in subagent time. Now `rebuild_full` runs
  `ATTACH DATABASE old` → `INSERT...SELECT` for observations +
  `observation_topics` + `vec_observations`, filtered to `doc_paths`
  still present in the new `fts_content` table. Dangling refs (memos
  deleted between rebuilds) are intentionally dropped. Defensive:
  handles pre-0.11 schemas without obs tables, commits before DETACH
  so the unlock succeeds.

### Changed

- **`/memex:save` now follows `redirect_to:` chains when appending
  "Recent signals" to wikilinked topics.** When archiving a topic
  whose content was absorbed into a canonical replacement, set
  `redirect_to: <target-slug>` in frontmatter (alongside
  `status: archived`). Signals from new memos will route to the target
  instead of accumulating on the dead-end stub. The resolver walks up
  to 5 hops, then bails with a warning. Topics archived without
  `redirect_to:` (typical for project-target archives where content
  belongs in a `_project.md`) get a `WARN: ... skipping signal`
  message — by design, not a bug.
- `skills/memo-writing/SKILL.md` step 3 updated to match: "Resolve
  `redirect_to:` in frontmatter first so archived topics route to
  their canonical replacement."

### Added

- **`scripts/batch_extract_observations.py`** — async dispatcher that
  fans up to 5 concurrent `claude --print --model sonnet` subprocesses
  across a list of memos to populate the observations table. Useful
  for users who imported existing memos before observations existed,
  or who need to refresh observations en masse. Idempotent: skips
  memos that already have observations. Logs per-memo JSON results
  to `~/.memex/logs/batch-obs-extraction.jsonl`. Rate ~0.12 memos/s
  at 5-way concurrency.
- **4 new tests in `tests/test_index_rebuild.py`** (98 tests total,
  97 pass + 1 skipped):
  - `count_embedding_gaps` fast path: asserts no LEFT JOIN against
    vec_chunks when total == embedded.
  - `count_embedding_gaps` fallback: per-doc query DOES run when gap > 0.
  - `rebuild_full` preserves observations across atomic swap.
  - `rebuild_full` drops obs for deleted memos (filter correctness).

### Migration notes

- Existing installs: nothing required. The `redirect_to:` convention is
  opt-in, but the two archive shapes now behave differently:
  - **Redirected archive** (`status: archived` + `redirect_to: <slug>`)
    — the resolver follows the chain (up to 5 hops) and the signal
    lands on the canonical replacement.
  - **Terminal archive** (`status: archived`, no `redirect_to:`) —
    the resolver emits a stderr warning
    (`WARN: archived with no redirect_to — skipping signal`) and the
    signal is **dropped**, not silently landed on the archived stub.
  To preserve signals for archives whose content moved elsewhere, add
  `redirect_to:` to the archive's frontmatter as you encounter them
  during garden-tending.
- `memex status` speedup is automatic.
- Observation preservation is automatic on the next `--full` rebuild.
  No more "I just rebuilt and now `memex obs stats` is empty" surprises.

---

## [0.11.2] — 2026-05-07

Throughput pass on the embedding pipeline. No schema changes, no breaking
API changes — drop-in upgrade. After installing, no rebuild required;
your next `memex index rebuild --incremental` (and the nightly job) will
just be much faster.

### Changed

- **Default Gemini model flipped from `gemini-embedding-2-preview` to
  the GA `gemini-embedding-2`.** Both names produce 3072-dim unit-norm
  vectors and are interchangeable — the GA name is just the documented
  stable identifier. Existing embeddings keep working; new embeddings
  come from GA. If your `~/.memex/config.json` pinned the `-preview`
  name, it still works; switching is optional.

### Added

- **Async/concurrent embedding dispatch.** Sub-batches now run in
  parallel under `asyncio.Semaphore(GEMINI_CONCURRENCY=5)` instead of
  sequentially with a 1-second inter-batch sleep. Gemini Tier 2 paid
  is TPM-bound at 5M tokens/min; with 8K-token batches the realistic
  ceiling is ~625 RPM, so 5 concurrent ≈ 48% utilization with
  headroom. Expect order-of-magnitude speedup on `--full` rebuilds
  and noticeably faster nightly `--incremental` runs.
- **±20% jitter on retry backoff.** `GEMINI_BACKOFF_SCHEDULE` is now
  randomized per attempt to prevent thundering-herd retries when
  multiple concurrent batches all hit 429 at the same instant.
- **Running-event-loop fallback for the sync `embed_texts` API.** If
  invoked from inside an async context (the way `scripts/mcp_server.py`
  tool handlers would call it), `embed_texts` punts to a worker
  thread with its own loop instead of raising
  `RuntimeError: asyncio.run() cannot be called from a running event loop`.
- **`threading.Lock` around `_get_client`.** The env-var stash dance
  that suppresses the SDK's "both keys set" warning is now serialized
  across threads.
- **Two new regression scenarios in `scripts/verify_embedding_retry.py`:**
  `concurrent_multi_batch` (force 3 sub-batches; assert order
  preservation + zero inter-batch sleeps) and `inside_running_loop`
  (the MCP-server regression we caught and fixed).

### Behavior change worth knowing

- **Partial failure is more granular.** Pre-0.11.2, a single bad
  sub-batch would None-pad itself *and every subsequent batch*. In
  0.11.2, parallel batches launch all at once; one batch's failure
  no longer kills its siblings — only the failing batch's slots
  become None, successful batches' vectors survive in the result
  list. `PartialEmbeddingFailure` is still raised so callers know
  something failed; positional alignment to input texts is preserved.

### Internal

- `GeminiProvider.embed_texts` now wraps `_aembed_texts` (async). The
  public API stays sync.
- The async path uses `await asyncio.to_thread(client.models.embed_content, ...)`
  rather than `client.aio.models.embed_content` directly, because the
  SDK's aio HTTP client binds to whichever loop first touched it and
  goes stale across our running-loop fallback's loop boundaries.

---

## [0.11.1] — 2026-04-21

Hardening pass on top of 0.11.0. Critical transcript-chunking fix.

### Fixed

- **Critical: transcript chunking slice bug.** `chunk_transcript_turns` used
  `parts[1::2]` against a non-capturing regex. `re.split` with a
  non-capturing pattern returns `[pre, body_1, ..., body_N]` — no
  interleaved delimiters. The old slice silently dropped every other turn
  and paired surviving bodies with the wrong headers. Net impact: every
  multi-turn transcript indexed before this fix had ~half its turns
  missing from the semantic search index, with mismatched headers on the
  rest. FTS5 was unaffected (tokenized raw content independently), which
  is why nothing surfaced as an error. **Existing installs must run
  `memex index rebuild --full` after upgrading** to re-chunk every
  transcript correctly.
- Rollback test in `test_index_rebuild.py` was vacuous — monkeypatched
  the wrong function. Now monkeypatches `index_document` (which runs
  after FTS is written) and asserts the FTS row stays at v1 content.
- `enable_load_extension(True)` no longer leaves the connection in a
  privileged state if sqlite-vec fails to load (try/finally).

### Added

- **Unit-norm invariant on embedding provider output.** Both
  `GeminiProvider` and `LMStudioProvider` call `_assert_unit_norm` at
  the end of `embed_texts`. Samples head+mid+tail of each batch,
  raises `ValueError` if `||v||²` drifts more than ±0.02.
  sqlite-vec uses L2 distance by default; only monotonic with cosine
  when inputs are normalized. New providers must call this helper.
- **`memex.db_utils` module.** `connect_index(path)` applies
  `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=10s` so
  writers don't collide with parallel `memex backfill obs --stdin`.
  `load_vec_extension(conn)` loads sqlite-vec and clears the
  privileged state in `finally`. Use this everywhere instead of bare
  `sqlite3.connect(path)`.
- **Per-doc SAVEPOINT pattern in rebuild loops.** `rebuild_full` and
  `rebuild_incremental` wrap each doc in `SAVEPOINT doc` → work →
  `RELEASE SAVEPOINT doc`, with `ROLLBACK TO SAVEPOINT doc` on
  exception. Split into `_rollback_savepoint_or_die` (propagates any
  ROLLBACK error) and `_release_savepoint_if_exists` (tolerant only
  of "no such savepoint" substring). Prevents partial commits on
  mid-doc exceptions while ensuring real SAVEPOINT bugs still surface.
- **Nightly rebuild support.** `scripts/nightly-rebuild.sh` handles
  incremental rebuild + `embed-missing` retry, sources `~/.secrets`,
  survives malformed secrets with a clear error. See SETUP.md for a
  launchd/cron snippet.
- Hermetic test suite shipped to public repo for the first time
  (11 files, 93 passed + 1 skipped on a fresh install).

### Changed

- `index_document` and `embed_chunks` no longer call `conn.commit()` —
  transaction boundaries belong to the caller. Single-file CLI callers
  commit explicitly; batch callers use the SAVEPOINT pattern above.

## [0.11.0] — 2026-04-20

### Added

- **`memex index embed-missing`** command. Idempotent retry path for
  when rebuild completes but the embed step failed (expired API key,
  rate limit). Finds chunks/observations in the index but missing from
  `vec_chunks`/`vec_observations` via LEFT JOIN, re-embeds through the
  pipeline. Exits non-zero if any remain unembedded.
- `embedding_gaps` surfaced in `format_rebuild_stats` + `format_status`.
- Dual Gemini/Google API key stderr-noise handling (env stash/restore
  in `_get_client`).
- `PartialEmbeddingFailure` typed exception. After 4 attempts
  (initial + 3 retries with 10s/30s/90s backoff) on 429/500/503,
  `GeminiProvider.embed_texts` raises this with partial results
  attached. `EmbeddingPipeline.embed_text` (single-text wrapper)
  catches it and degrades to `None` for query-path resilience;
  `embed_chunks` caches partial success and logs failures to stderr.

### Removed

- **`pending_embeddings.jsonl` queue.** `enqueue_embedding_job` /
  `dequeue_embedding_jobs` / `get_embedding_queue_count` were dead
  code (zero call sites) and duplicated `embed-missing`. Removed.
- **`memex context --full` and `--compact`.** The rich context injection
  surface backed by `src/memex/context.py` was removed by design.
  `recall` skill DEEP mode replaces it. The `memex context` CLI
  command is preserved but trimmed to project-detection + pending
  memo status.

## [0.9.0 – 0.10.0] — 2026-04 (consolidated)

### Added

- **Observation-to-topic clustering.** `observation_topics` junction
  table links observations to topic slugs (many-to-many). Enables
  bounded complete retrieval: `memex obs topic <slug>`, `memex obs
  stats`, `memex obs retag <old> <new>`, `memex obs untagged`.
- **`memex backfill topic-tags`** propagates memo-frontmatter topics
  to `observation_topics`.
- **Gemini Embedding 2 support** (3072d). Dimension migration
  auto-drops `vec_chunks` on provider switch (1024d LM Studio ↔
  3072d Gemini). Preview model `gemini-embedding-2-preview` does NOT
  support `task_type` — `_build_embed_config` strips it.
- **Scoped observation search** and **similarity detection**
  (`memex similarity`).

## [0.7.0 – 0.8.0] — 2026-04 (consolidated)

### Added

- **Typer-based unified CLI.** Subcommand groups: `index` (rebuild,
  status, embed-missing), `obs` (topic, stats, retag, untagged),
  `backfill` (obs, tokens, memos, topic-tags), `session` (discover,
  import), `graph` (backlinks, orphans, tags, stats).
- `memex read`, `memex path`, `memex check`, `memex context`.

### Changed

- **Primary interface shifted to CLI + skills.** `scripts/mcp_server.py`
  still ships as an optional MCP integration, but the recommended path
  is now the `memex` CLI (shell access) plus intent-based skills
  (`recall`, `garden-tending`, `memo-writing`, `curator-practice`) for
  in-session use. New features land in the CLI first.

### Removed

- `scripts/batch_generate_memos.py` (the only `import anthropic` in
  the codebase, contradicted the "no external API calls" architecture).

## Breaking Changes Summary (0.6.0 → 0.11.1)

If you're upgrading from 0.6.0, expect these user-visible changes:

1. **Slash commands removed**: `/ask`, `/backfill`, `/load`,
   `/maintain`, `/merge`, `/retry`, `/search`, `/synthesize`,
   `/timeline`. Use the equivalent CLI (`memex ask`, `memex search`,
   `memex timeline`, `memex backfill`) or skill-based flows
   (`garden-tending` handles the former `synthesize` and `merge`
   behavior). Kept slash commands: `/memex:save`, `/memex:status`,
   `/memex:open`.

2. **`memex context --full` and `--compact` removed.** Rich context
   injection is no longer in the CLI. The `recall` skill (DEEP mode)
   covers cross-session synthesis.

3. **`ask-memex` skill removed** (absorbed into `recall`).

4. **Transcript re-chunking required**: after upgrading, run
   `memex index rebuild --full` once to fix the chunking slice bug
   (see 0.11.1 "Fixed").

5. **Embedding provider change**: if you were on LM Studio (1024d) and
   switch to Gemini (3072d), the migration path auto-drops the
   `vec_chunks` table on first rebuild. Same in reverse.
