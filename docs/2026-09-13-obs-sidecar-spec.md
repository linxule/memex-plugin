# Spec — vault-backed observations via `.obs.jsonl` sidecars (linxule/memex#1)

Repo: `/Users/xulelin/Documents/Apps/memex` (Python 3.11+, `uv`, pytest; run tests with `uv run pytest -q`).
Read first: `.claude/rules/python-patterns.md`, `src/memex/observations.py`, `src/memex/extract.py`,
`src/memex/scripts/index_rebuild.py` (rebuild_full / rebuild_incremental / `_OBS_PRESERVATION_TABLES`),
`src/memex/dreamer.py` (`_dream_locked`, `_merge_duplicate_observations`), `src/memex/cli.py` (`obs_app`),
`src/memex/scrub.py` (`scrub_text`, `_atomic_write`, `safe_write_text`).

## Principle

The vault (markdown) is truth; `~/.memex/_index.sqlite` is a per-machine cache. Today observations
(LLM-extracted, ~17.8k rows) exist ONLY in the DB, so a second machine sharing the vault via iCloud
has none, and two machines drift. Fix: every document `D.md` that has observations gets a sidecar
`D.obs.jsonl` next to it. Three rules:

1. **Write-through.** Every DB mutation of a doc's observations rewrites that doc's sidecar *from DB
   state* (one primitive: `write_sidecar(conn, vault, doc_path)`). No per-mutation sidecar editing.
2. **Ingest on rebuild.** `memex index rebuild` (full and incremental) diffs each changed sidecar into
   the DB — insert/delete/update, never wipe-and-reload — so existing rows keep their ids and vectors.
3. **Sidecars are never deleted automatically** except when a *local explicit mutation* leaves a doc
   with zero observations. A memo missing on disk (possibly mid-iCloud-sync) must NOT cause its sidecar
   to be removed or its rows to be ingested; a sidecar arriving before its memo is simply retried next run.

## Sidecar format

Path: `sidecar_path(vault, doc_path)` — strip trailing `.md`, append `.obs.jsonl`
(`projects/p/memos/x.md` → `projects/p/memos/x.obs.jsonl`; `projects/p/_project.md` →
`projects/p/_project.obs.jsonl`; `topics/t.md` → `topics/t.obs.jsonl`). `doc_path` must be
vault-relative; an absolute doc_path (a handful of legacy rows exist) → return `None`, caller warns.
Resolved path must stay under vault (`relative_to` in try/except, per python-patterns).

Content: one JSON object per line, `json.dumps(obj, ensure_ascii=False, sort_keys=True)`, ordered by
`observations.id` ASC, trailing newline. Keys:

```
content        str  (verbatim)
content_hash   str  sha256(content) — same as observations.content_hash
obs_type       str  explicit | deductive | contradiction
confidence     str
topics         list[str], sorted
source_obs     list[str] — content_hashes of the source observations (row ids are per-machine and
                            must not appear); unresolvable ids are dropped at render time
created_at     str  DB value verbatim (SQLite CURRENT_TIMESTAMP text)
```

No `doc_path` field — location implies it, so a file move IS a reassign. Zero observations → the
sidecar is removed, never written empty.

Writes: scrub via `memex.scrub.scrub_text(..., apply=True)` (hook-bypass rule, see `safe_write_text`
docstring) and write atomically (temp file in the same directory + `os.replace`) so iCloud never
syncs a half-written file. Do not create missing parent directories — if the parent dir is absent
(typo'd doc_path), warn to stderr and skip.

Reading: blank lines skipped. Any malformed line (bad JSON, non-dict, empty/non-str `content`,
`content_hash` ≠ sha256(content), wrong types) → raise `SidecarError` for the WHOLE file; the ingest
loop reports it, skips the file, and does NOT record its hash (so it is retried next run). A
partially broken file is more likely a sync artifact than intent, and a partial ingest would delete
DB rows. Missing `obs_type`/`confidence` default to `explicit`/`high`; missing `topics`/`source_obs`
default to `[]`; missing `created_at` → `None` (insert with CURRENT_TIMESTAMP).

iCloud conflict copies are named like `x.obs 2.jsonl` — the `*.obs.jsonl` glob must not match them;
`find_sidecar_conflicts(vault)` lists them for the health command.

## New module `src/memex/sidecars.py`

- `SIDECAR_SUFFIX`, `SidecarRecord` dataclass, `SidecarError`.
- `sidecar_path(vault, doc_path) -> Path | None`, `doc_path_for_sidecar(vault, path) -> str`.
- `find_sidecars(vault) -> list[Path]` (globs `projects/**/*.obs.jsonl`, `topics/*.obs.jsonl`;
  skip `/_templates/`, `/_views/`), `find_sidecar_conflicts(vault) -> list[Path]`.
- `render_sidecar(conn, doc_path) -> str` (from DB; `""` when no rows).
- `write_sidecar(conn, vault, doc_path) -> Path | None`: render → write or unlink; then record
  `(doc_path, sha256 of the bytes written)` in `obs_sidecars` (delete the row on unlink). Does NOT
  commit — caller owns the transaction, same as everything else in `observations.py`.
- `read_sidecar(path) -> list[SidecarRecord]`.
- `file_hash(path) -> str`.
- `ingest_sidecar(conn, vault, doc_path, path) -> dict`: diff against the doc's DB rows keyed by
  `content_hash`:
  - in DB, not in file → `delete_observation_ids` (the ONLY sanctioned deleter).
  - in file, not in DB → if the hash already exists under a *different* doc_path, skip and count
    `skipped_foreign` (hash is globally unique; mirrors `store_observations`); else INSERT
    `observations` (with `created_at` from the record when present) + `fts_observations(rowid=id, …)`
    + `observation_topics`. No vector — the existing gap-heal (`count_embedding_gaps` /
    `reembed_missing` / `memex index embed-missing`) embeds it later.
  - in both → UPDATE `obs_type`/`confidence` (and `fts_observations.obs_type`) if changed; set-diff
    `observation_topics`.
  - Returns `{inserted, deleted, updated, retagged, skipped_foreign, pending_sources}` where
    `pending_sources` is a list of `(obs_id, [source hashes])` for records with non-empty `source_obs`
    — resolved by the caller in a second phase (below), because a deduction in `_project.obs.jsonl`
    may reference hashes in a sidecar not yet ingested.
  - Records the file hash in `obs_sidecars` on success. No commit.
- `resolve_pending_sources(conn, pending) -> dict`: map hashes → ids (`WHERE content_hash IN (...)`,
  chunked ≤900 params like `_chunked` in observations.py), write `source_obs_ids` (file order;
  NULL when none resolve) only when it differs. Returns `{resolved, unresolved}`.
- `ingest_all_sidecars(conn, vault, *, indexed_paths: set[str] | None, force=False) -> dict`:
  for each sidecar: derive doc_path; if `indexed_paths` is given and doc_path ∉ it → count
  `skipped_no_doc`, do not record hash; if not `force` and `file_hash == obs_sidecars.content_hash`
  → `unchanged`; else `SAVEPOINT sidecar` → `ingest_sidecar` → `RELEASE`; on exception
  `_rollback_savepoint_or_die` + `_release_savepoint_if_exists` (import from index_rebuild or move
  those two helpers into `db_utils` — your call, keep existing imports working), print to stderr,
  count `errors`. Then phase 2 `resolve_pending_sources` over all pending. Aggregate and return
  counts (`files`, `ingested`, `unchanged`, `skipped_no_doc`, `errors`, plus the per-file sums).
- `export_sidecars(conn, vault, *, apply: bool, force: bool) -> dict`: for each distinct
  `observations.doc_path`: absolute / escapes vault → `unportable` (list them); sidecar exists and
  `not force` → compare rendered text with the file: identical → `current`, differs → `conflict`
  (list them; never overwrite without `--force`, because on a machine whose DB is a stale copy —
  m5 — exporting would clobber sidecars synced from the machine of record); else `written` (or
  `would_write` when dry-run). Records hashes on write. Caller commits.

New table, created in `init_observation_schema`:
```
CREATE TABLE IF NOT EXISTS obs_sidecars (
    doc_path TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    ingested_at TEXT DEFAULT CURRENT_TIMESTAMP
)
```
Add it to `_OBS_PRESERVATION_TABLES` in index_rebuild.py (rows whose doc_path is in
`main.fts_content`), so `test_preservation_registry_covers_init_schema` stays green and a full
rebuild with a prior index can skip unchanged sidecars.

## Write-through call sites

1. `extract.store_observations(index_path, memo_path, observations, pipeline, *, mode, vault)` —
   add REQUIRED keyword-only `vault: Path | None`. `None` = explicitly no sidecar (tests, `--index`
   callers without a vault). This is the same "unstateable-by-omission" convention as `mode`; update
   the docstring in that spirit (briefly). After the row writes and before `conn.commit()`, if
   `vault` is not None: `write_sidecar(conn, vault, memo_path)`. Add `"sidecar": str|None` to the
   returned dict and to `main()`'s JSON output. `extract.main` passes `get_memex_path()`.
   Update every caller (dreamer, tests).
2. `dreamer._dream_locked`: pass `vault=vault_path` to `store_observations`; in
   `_merge_duplicate_observations(dry_run=False)` collect the distinct doc_paths of `drop_ids`
   BEFORE deleting and `write_sidecar` each afterwards (it has `conn`; needs `vault_path` — thread it
   through).
3. `memex obs retag`: collect distinct doc_paths tagged `old` before `retag_topic`, rewrite each
   sidecar after. Add a small `observations.doc_paths_for_topic(conn, slug)` helper.
4. `memex obs reassign --apply`: before the UPDATE, collect distinct matching old doc_paths; after
   the UPDATE + invariant check: for each, `write_sidecar(conn, vault, new_path)` and, if the OLD
   sidecar file still exists, unlink it and delete its `obs_sidecars` row (the user may have already
   `git mv`'d the folder, sidecars included — then there is nothing to unlink). Report counts.
5. `rebuild_incremental` deleted-doc branch: keep `delete_observations_for_doc`; additionally
   `DELETE FROM obs_sidecars WHERE doc_path = ?` so a returning memo re-ingests. Do NOT touch the
   sidecar file (rule 3).
6. `_rebuild_full`: after the doc loop and after `_preserve_obs_tables` (or right after the doc
   loop when there was no prior index), call
   `ingest_all_sidecars(conn, memex, indexed_paths={paths in main.fts_content})` inside the same
   transaction before commit/swap. Add `sidecars` (the returned dict) to `stats` and surface
   ingested/errors in `format_rebuild_stats`. A per-sidecar error does not abort the swap (the
   savepoint isolates it and the hash isn't recorded).
7. `rebuild_incremental`: after the doc loop and the deletion loop, `ingest_all_sidecars(conn,
   memex, indexed_paths=indexed_paths)`; add to stats. The existing end-of-run gap-heal already
   embeds newly inserted observations when a key is present.
8. `memex obs orphans --apply` touches only mirror tables — no sidecar change.

## CLI additions (`memex obs …`, typer, in cli.py)

- `memex obs export-sidecars [--apply] [--force] [--json]` — one-off migration; dry-run by default
  (repo convention, cf. `reassign`, `scrub`). Print counts and list `unportable` + `conflict` paths.
  Holds `writer_lock`.
- `memex obs ingest-sidecars [--force] [--json]` — ingest without a rebuild (after an iCloud sync).
  Holds `writer_lock`; commits; prints a hint to run `memex index embed-missing` when `inserted > 0`.
- `memex obs sidecars [--json]` — health report: sidecar count; docs with DB observations but no
  sidecar (`missing`); sidecars whose document is not indexed / not on disk (`orphan`); sidecars whose
  file hash ≠ `obs_sidecars` (`stale`, pending ingest); conflict copies; unportable doc_paths. Exit 0.
- `memex index status` / `format_status`: one line — `Sidecars: N on disk, M pending ingest, K docs
  without sidecar` (compute from `find_sidecars` + `obs_sidecars` + distinct observation doc_paths).

## Docs to update (keep terse, match the existing voice)

- `CLAUDE.md`: folder-structure tree (`<memo>.obs.jsonl` line), CLI list (three obs commands).
- `.claude/rules/architecture.md`: new "Observations" section — sidecar is truth, index is cache,
  write-through, ingest on rebuild, hand-editing a sidecar is allowed and picked up by
  `ingest-sidecars`/rebuild, sidecars are committed with memos.
- `.claude/rules/python-patterns.md`: bullet — any code that mutates a doc's observations must
  `write_sidecar` for every affected doc_path; `store_observations` requires `vault=` (pass `None`
  only in tests / index-only tools).
- `.claude/rules/maintenance.md`: "Multi-machine" subsection — after iCloud sync run
  `memex index rebuild --incremental` (or `memex obs ingest-sidecars`) then `memex index
  embed-missing`; one-time migration `memex obs export-sidecars --apply` on the machine of record
  only; what a conflict copy looks like and how to resolve (pick one, delete the other, ingest).
- `commands/save.md` and `skills/memo-writing/**` wherever `backfill obs` is described: note that it
  also writes `<memo>.obs.jsonl` beside the memo — commit it with the memo.
- Do NOT bump versions or touch CHANGELOG — the orchestrator does the release.

## Tests (pytest, `tmp_path` vaults + `tmp_path` index; never the real vault or `~/.memex`)

`tests/test_sidecars.py`:
- path derivation (memo, `_project.md`, topic, absolute → None, escape → None), inverse mapping.
- render/write/read round-trip is byte-stable and ordered by id; `source_obs` renders hashes and
  drops unresolvable ids; zero rows removes the file and its `obs_sidecars` row; parent-dir-missing
  warns and skips; atomic write leaves no temp file.
- read: malformed line / hash mismatch → `SidecarError`; defaults applied; blank lines skipped.
- ingest diff: insert (fts + topics rows, `created_at` preserved), delete (mirrors gone — assert via
  `count_orphaned_observation_rows` == 0), update type/confidence/topics, unchanged rows keep id and
  a pre-existing `vec_observations` row (use `init_observation_schema(conn, 8)` and insert an 8-dim
  blob; if sqlite-vec can't load in CI, monkeypatch as existing tests do), `skipped_foreign`,
  pending sources resolved across two files in phase 2, unresolved dropped.
- ingest_all: unchanged hash skipped; changed re-ingested; sidecar without doc skipped and NOT
  hash-recorded; broken file → error counted, hash not recorded, other files still ingested; `force`.
- export: dry-run writes nothing; apply writes + records; existing identical → `current`; differing
  → `conflict` untouched without `--force`, overwritten with it; absolute doc_path → `unportable`.
- conflict-copy glob: `x.obs 2.jsonl` is not a sidecar but is listed by `find_sidecar_conflicts`.

Extend existing files:
- `tests/test_extract.py` (or wherever `store_observations` is tested): `vault=` writes/rewrites
  the sidecar in replace and append modes; `vault=None` writes nothing; result has `sidecar`.
- `tests/test_index_rebuild.py`: incremental ingests a new sidecar for an unchanged memo (memo hash
  unchanged, sidecar new → obs appear); sidecar-only edit re-ingests; deleted memo clears
  `obs_sidecars` row but leaves the file; full rebuild from a vault with sidecars and NO prior index
  yields the obs; full atomic rebuild with a prior index keeps vec rows for unchanged obs and applies
  a sidecar diff; `test_preservation_registry_covers_init_schema` still green with `obs_sidecars`.
- `tests/test_obs_reassign.py` / `tests/test_cli.py`: reassign --apply moves the sidecar; retag
  rewrites affected sidecars; export/ingest/sidecars commands run via `typer.testing.CliRunner`
  against a tmp vault (`--index`/config override as existing CLI tests do).
- `tests/test_dreamer.py`: merge-duplicates rewrites the affected sidecars.

## Constraints

- All existing tests stay green (`uv run pytest -q`, ~640). Run ruff on touched files.
- Follow `.claude/rules/python-patterns.md`: `connect_index`, helpers never commit, SAVEPOINT
  pattern, parameterized SQL, `uv run python` never bare `python3`, chunk IN-lists ≤900.
- NEVER run `export-sidecars`, `ingest-sidecars`, or any rebuild against the real vault
  (`~/Documents/Apps/memex/projects`, `topics`) or the real index (`~/.memex/_index.sqlite`).
  Do not modify anything under `projects/`, `topics/`, `_meta/`, `_views/`, `_templates/`.
- Comments: this codebase writes rationale comments at invariants and incident-driven decisions;
  match that, don't pad. No version bump, no CHANGELOG, no git commits — leave the working tree for
  review.
- When done, write a short summary to `IMPLEMENTATION-NOTES.md` in the repo root: what changed per
  file, any deviation from this spec and why, test counts before/after, anything you were unsure of.

## Addendum A — after adversarial review (2026-09-13, Kimi)

These amend the sections above; where they conflict, the addendum wins.

A1. **An empty sidecar file is never authoritative.** `write_sidecar` unlinks on zero rows and never
writes an empty file, so a 0-byte `*.obs.jsonl` on disk is always a sync placeholder or a hand
artifact — never intent. `ingest_all_sidecars` treats a zero-byte / whitespace-only file as
`empty`: skip it, do not record its hash, do not delete anything. `memex obs sidecars` lists them.
(Without this, an iCloud mid-transfer file would diff as "zero observations" and wipe the doc's rows.)

A2. **Ingest runs BEFORE the deleted-doc loop in `rebuild_incremental`, and adopts orphaned rows.**
When a record's `content_hash` already exists under a *different* doc_path:
  - if that other doc_path is NOT in `indexed_paths` (its memo is gone from disk — typically a rename
    whose new memo+sidecar arrived via sync), **adopt** the row: `UPDATE observations SET doc_path=?`
    (ids, fts, vec, topics all survive; then apply the type/confidence/topics diff as for an
    "in both" row). Count `adopted`.
  - otherwise it is a genuine cross-doc duplicate: count `skipped_foreign` as before, and do NOT
    record the file's hash in `obs_sidecars` (so it re-surfaces every run) — list such files in
    `memex obs sidecars` as `foreign_conflicts` with the two doc_paths.
Ordering: doc loop → `ingest_all_sidecars` → deleted-doc loop. In `_rebuild_full` there is no
deleted-doc loop; `indexed_paths` = main.fts_content paths as before.

A3. **Unresolved `source_obs` are remembered and retried every run.** New table
`obs_pending_sources(observation_id INTEGER NOT NULL, source_hash TEXT NOT NULL,
PRIMARY KEY (observation_id, source_hash))`, created in `init_observation_schema`, added to
`_OBS_PRESERVATION_TABLES` (join on main.observations like `observation_topics`), and added to
`_OBS_MIRROR_TABLES` so `delete_observation_ids` clears it. `ingest_sidecar` writes every
(obs_id, hash) it cannot resolve there; `resolve_pending_sources` runs at the end of EVERY
`ingest_all_sidecars` call (even when every file was `unchanged`) against the whole pending table,
appends newly resolved ids to `source_obs_ids` (preserving file order where known; otherwise
append), and deletes resolved pairs. Wrap phase 2 in its own SAVEPOINT. Health report shows the
pending count. (Before this, a deduction ingested before its sources' sidecar synced lost its
provenance permanently because its file hash never changed again.)

A4. **First-export warning.** `export-sidecars` prints a loud stderr warning when the vault currently
has zero sidecars: "first export — run this only on the machine of record (m4max)". maintenance.md
names m4max as the machine of record and states the operating assumption that observation-mutating
commands (backfill obs, dreamer, retag, reassign, merge-duplicates) run from one machine at a time —
there is no cross-machine lock; iCloud is last-write-wins on a sidecar.

A5. **Cross-machine rename note** in maintenance.md: after a folder rename on one Mac, let iCloud
settle before running a rebuild on the other; A2's adoption keeps ids/vectors when the new sidecar
has arrived, and the deleted-doc loop only removes what nothing adopted.

A6. `.gitignore`: add `*.obs *.jsonl` (iCloud numbered conflict copies) so one is never committed.

A7. Stats/health: `ingest_all_sidecars` returns additionally `empty`, `adopted`, `foreign_conflicts`
(list), `pending_sources` (count after phase 2); `memex obs sidecars` surfaces all of them.

## Addendum B — review round on the diff (2026-09-13; Kimi challenge + Opus code-reviewer)

Applied, with tests: (B1) secrets inside an observation's *own* text are scrubbed per row in the DB
(content + hash + FTS) before render — a whole-file scrub after render left `content_hash` stale and
would have wedged that doc's sidecar on every other machine; (B2) `memex obs retag` holds
`writer_lock`; (B3) each sidecar is read once per run — emptiness, the unchanged check, the parse and
the recorded hash all come from the same bytes; a `UnicodeDecodeError` (truncated multi-byte char
mid-transfer) is a per-file error, listed as `unreadable` by the health report; (B4) `reassign
--apply` keeps the old sidecar when the new one cannot be written; (B5) `export-sidecars --apply`
records the hash on the `current` branch too; (B6) `render_sidecar` includes hashes still pending in
`obs_pending_sources`, so a local write-through cannot drop provenance the vault already carried.
Documented as intended: a hand-move of a line between two *live* docs is a conflict until the source
sidecar drops it, then delete+insert (one re-embed) — never a guess about ownership.
Implementation log: `docs/2026-09-13-obs-sidecar-implementation-notes.md`.
