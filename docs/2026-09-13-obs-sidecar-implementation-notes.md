# Implementation notes — vault-backed observations via `.obs.jsonl` sidecars (linxule/memex#1)

Implements `docs/2026-09-13-obs-sidecar-spec.md`. All existing tests stay green, the working
tree is left uncommitted for review, and no run touched the real vault or `~/.memex/_index.sqlite`
(see "Safety incident" below — one accidental write happened mid-implementation and was cleaned up).

## Test counts

- Before: **640 passed**
- After: **686 passed** (46 new: 35 in `tests/test_sidecars.py`, 1 in `tests/test_dreamer.py`,
  5 in `tests/test_index_rebuild.py`, 5 in `tests/test_cli.py`)
- `uv run ruff check` on every touched file: clean except pre-existing issues in files I edited but
  didn't introduce (see "Ruff" below).

## New files

- **`src/memex/sidecars.py`** — the module. `SIDECAR_SUFFIX`, `SidecarRecord`, `SidecarError`,
  `sidecar_path` / `doc_path_for_sidecar`, `find_sidecars` / `find_sidecar_conflicts`,
  `render_sidecar`, `write_sidecar`, `read_sidecar`, `file_hash`, `ingest_sidecar`,
  `resolve_pending_sources`, `ingest_all_sidecars`, `export_sidecars`, and one addition beyond the
  spec's explicit list: `sidecar_health(conn, vault) -> dict`, which backs `memex obs sidecars` (the
  spec described the CLI command's report content but not where the logic should live; putting it in
  `sidecars.py` keeps it testable without spinning up the CLI, same as everything else in the module).
- **`tests/test_sidecars.py`** — 35 tests covering path derivation, render/write/read round-trip
  (ordering, byte-stability, source_obs hash rendering + unresolvable-id dropping, zero-obs removal,
  missing-parent-dir warn-and-skip, atomic-write-no-tempfile-left-behind), `read_sidecar` malformed
  input (bad JSON, non-dict, empty content, hash mismatch, defaults, blank lines), the ingest diff
  (insert/delete/update/unchanged-keeps-vector/skipped_foreign/pending-sources-across-two-files),
  `ingest_all_sidecars` (unchanged/changed/skipped_no_doc/broken-file-isolated/force), `export_sidecars`
  (dry-run/apply/current/conflict/force/unportable), the conflict-copy glob, and `sidecar_health`.
- **`docs/2026-09-13-obs-sidecar-spec.md`** — the spec itself (already present when this run started;
  listed here only because `git status` shows it untracked).

## Modified files

- **`src/memex/observations.py`**: `obs_sidecars` table added to `init_observation_schema` (doc
  comment explains the invariant it backs). New `doc_paths_for_topic(conn, slug)` helper for `memex
  obs retag`'s write-through.
- **`src/memex/db_utils.py`**: moved `_rollback_savepoint_or_die` / `_release_savepoint_if_exists`
  here from `index_rebuild.py` (public names, no leading underscore) so `sidecars.py` can share them
  without a circular import (`index_rebuild` imports `sidecars` for `ingest_all_sidecars`; the
  reverse would have been circular). `index_rebuild.py` re-imports them under their old
  underscore-prefixed names for full backward compatibility — no call site there changed.
- **`src/memex/scripts/index_rebuild.py`**: `obs_sidecars` added to `_OBS_PRESERVATION_TABLES`
  (filtered by `doc_path IN (SELECT DISTINCT path FROM main.fts_content)`, same shape as
  `observations`). `_rebuild_full` calls `ingest_all_sidecars(conn, memex, indexed_paths=...)` right
  before the metadata/commit block (after the doc loop and after `_preserve_obs_tables`, whether or
  not there was a prior index — matches the spec). `rebuild_incremental` calls it after the deletion
  loop, reusing the `indexed_paths` set already built by the doc loop; the deleted-doc branch also
  clears the `obs_sidecars` DB row (never touches the file, per rule 3). `get_index_status` computes
  an on-disk/pending/without-sidecar summary (best-effort, swallows `OperationalError`/`OSError` so a
  pre-sidecar index or an unreadable vault glob doesn't break `status`); `format_status` renders it as
  one `Sidecars: N on disk, M pending ingest, K docs without sidecar` line. `format_rebuild_stats`
  gained a `Sidecars: ...` summary line sourced from the `stats["sidecars"]` dict, placed so it does
  not disturb the exact `"Preserved across atomic swap: N/N/N/N"` string several existing tests pin.
- **`src/memex/extract.py`**: `store_observations` gained a REQUIRED keyword-only `vault: Path |
  None` (same "unstateable by omission" pattern as `mode`, added in v0.16.0 for the same reason).
  Writes the sidecar via `write_sidecar` after the row loop, before `conn.commit()`, when `vault` is
  not `None`. Return dict gained `"sidecar"` (path string or `None`). `main()` gained `--vault`
  (defaults to `get_memex_path()` — see the "CLI vault default" deviation below) and passes it
  through; `main()`'s JSON output gained `"sidecar"`.
- **`src/memex/dreamer.py`**: `_dream_locked` passes `vault=vault_path` to `store_observations` for
  derived-observation writes. `_merge_duplicate_observations` gained a keyword-only `vault_path:
  Path | None = None`; when set, it collects the distinct doc_paths of `drop_ids` BEFORE deleting and
  calls `write_sidecar` for each afterward. Default `None` keeps every existing caller/test
  unaffected (dry-run path never needed it either).
- **`src/memex/cli.py`**: `obs retag` collects `doc_paths_for_topic(conn, old)` before retagging,
  rewrites each afterward. `obs reassign --apply` collects old doc_paths (via the same
  `SUBSTR(doc_path,1,?)=?` predicate `reassign_doc_path_prefix` uses internally) before the UPDATE,
  then after the invariant check computes each new doc_path client-side
  (`to_prefix + old[len(from_prefix):]`), writes its sidecar, and unlinks the old sidecar file +
  `obs_sidecars` row if still present (a no-op if the user already `git mv`'d it). Three new commands:
  `obs export-sidecars [--apply] [--force] [--json]`, `obs ingest-sidecars [--force] [--json]`
  (hints `memex index embed-missing` to stderr when anything was inserted, exits 1 on any per-file
  error), `obs sidecars [--json]` (health report, always exits 0).
- **Docs**: `CLAUDE.md` (folder tree gets the `.obs.jsonl` line; CLI table gets the three new
  commands), `.claude/rules/architecture.md` (new "Observations" section), `.claude/rules/
  python-patterns.md` (bullet on the `write_sidecar` write-through obligation + `vault=` being
  required), `.claude/rules/maintenance.md` (new "Multi-machine (obs sidecars)" subsection: rebuild
  vs. `ingest-sidecars`, the one-time `export-sidecars --apply` migration and why it's
  machine-of-record-only, conflict-copy resolution), `commands/save.md` and `skills/memo-writing/
  SKILL.md` (note that `backfill obs` also writes the sidecar; commit it with the memo).
- **Test-suite fixups required by the new required `vault=` kwarg** (not new coverage, just keeping
  the suite green): `tests/test_extract.py` (every `store_observations(...)` call site got
  `vault=None`; the two exact-dict-equality assertions got `"sidecar": None`; `_run_cli`'s argv now
  always includes `--vault <tmp_path>` — see the safety incident below for why this one is
  load-bearing, not cosmetic); `tests/test_review_followups.py` (a monkeypatched
  `_merge_duplicate_observations` lambda widened to accept `vault_path=None`);
  `tests/test_observation_orphans.py` and `tests/test_index_rebuild.py` (both had a hardcoded
  whitelist of "every real table `init_observation_schema` creates" — one for the mirror-table
  registry, one for the preservation registry — and both needed `obs_sidecars` added with a comment
  explaining why it's *not* an id-keyed observation mirror even though it's a new real table).

## Deviations from the spec, and why

1. **`extract.py`'s CLI gained a `--vault` override the spec didn't ask for.** The spec says only
   "`extract.main` passes `get_memex_path()`." Taken literally, every existing CLI-level test in
   `tests/test_extract.py` that drives `extract.main()` end-to-end would have called the REAL
   `get_memex_path()` on this developer machine — which resolves to the actual live vault at
   `/Users/xulelin/Documents/Apps/memex` — for its `vault=` argument, because those tests never
   configured `~/.memex/config.json` and this machine already has one pointing at the real vault. I
   added `--vault` (default: `get_memex_path()`, unchanged production behavior) purely so tests can
   redirect it, matching the existing `--index` override on the same command. This is additive, not
   a spec violation, and it's what closed the safety hole below.
2. **`obs_sidecars` preservation counts aren't surfaced in the human-readable
   `"Preserved across atomic swap: N/N/N/N"` line.** Several existing tests pin that exact 4-number
   string (`obs`/`topic-tags`/`fts`/`vec`). Adding a 5th number would have broken them for a
   cosmetic gain; `obs_sidecars` preservation still runs (registry-driven, verified via
   `test_full_atomic_rebuild_with_prior_index_keeps_vec_and_applies_sidecar_diff` and the
   `EXPECTED_REAL`-set update to `test_preservation_registry_covers_init_schema`), it's just not
   individually itemized in that one line. `format_rebuild_stats` does get a *separate* new line
   (`Sidecars: N ingested, ...`) from the `ingest_all_sidecars` stats dict, which is the
   operationally interesting number anyway (obs_sidecars preservation is invisible plumbing that lets
   ingest skip unchanged sidecars after a full rebuild; ingest counts are what an operator wants to
   see).
3. **Savepoint helper relocation.** The spec offered a choice ("import from index_rebuild or move
   those two helpers into `db_utils` — your call, keep existing imports working"). I moved them to
   `db_utils.py` (public names) and re-export under the old underscore names from `index_rebuild.py`,
   because the alternative — `sidecars.py` importing from `index_rebuild.py` — would be circular
   (`index_rebuild.py` imports `sidecars.py` for `ingest_all_sidecars`). Existing imports/monkeypatches
   in `index_rebuild.py`'s own call sites are untouched.

## Safety incident (caught and fixed during this run, not shipped)

Before I added the `--vault` CLI override (deviation #1), I ran the full suite once with `extract.py`
already changed to require `vault=` end-to-end. Five `_run_cli`-driven CLI tests in
`tests/test_extract.py` don't pass `--vault`, so `extract.main()` fell back to `get_memex_path()` —
which on THIS machine resolves to the real vault — while `--index` still correctly pointed at a
`tmp_path` sqlite file. `write_sidecar` then found the real vault's `projects/memex/memos/` directory
already exists and wrote a real file:
`/Users/xulelin/Documents/Apps/memex/projects/memex/memos/2026-03-16-existing.obs.jsonl` (contents
were the test fixtures' literal `"a"`/`"b"`/`"c"` observations — obviously not real vault data). I
caught this via a post-implementation vault-cleanliness check (`find projects/ topics/ -name
'*.obs.jsonl'`), deleted the stray file, added `--vault <tmp_path>` to `_run_cli`'s argv, and reran
the full suite plus the same cleanliness check — clean. The real `~/.memex/_index.sqlite` was never
touched (all tests use tmp_path indexes); only the vault side was at risk, and only because
`get_memex_path()` on a real developer checkout doesn't know it's running under pytest. **Flagging
this explicitly** because it's exactly the failure mode the task's hard constraints exist to prevent,
and the fix (an explicit override, not a mock or an environment check) is the same shape as the
`--index` override that already existed for the same reason — it should probably be considered for
adoption more broadly if other CLI entry points have the same "no vault override" gap.

Unrelated observation: `topics/single-source-of-truth.md` shows a new uncommitted line (a
2026-09-09 LTAC signal) that wasn't there when this session started and that none of my code or
tests reference. That's some other concurrent process editing the live vault, not this
implementation — left untouched.

## Open doubts

- **`obs reassign --apply`'s new-sidecar write happens inside the same transaction as the UPDATE,
  before `conn.commit()`.** This means `write_sidecar`'s read of `observations` (via
  `render_sidecar`) sees the just-updated `doc_path` correctly (same-connection reads see pending
  writes), but if the process dies between the UPDATE and the sidecar write, the DB commit and the
  sidecar write are not atomic with each other at the OS level — a crash there leaves the DB
  correctly reassigned but the sidecar still at the old path (or absent) until the next
  `ingest-sidecars`/rebuild. This mirrors the pre-existing risk profile of `write_sidecar` everywhere
  else (it's not itself transactional with the filesystem), so I didn't treat it as a defect, but
  it's worth naming.
- **`sidecar_health`'s `orphan` vs. `stale` split trusts `fts_content`'s absence as "can't tell,
  don't flag."** A minimal/legacy index with no `fts_content` table will never report a sidecar as
  orphaned for being "unindexed" — only for the doc actually missing from disk. This seemed like the
  right conservative default (a health report should never manufacture false alarms it can't back
  up), but it does mean the orphan check is weaker on such indexes.
- **`ingest_sidecar`'s foreign-hash check is a single `SELECT` per new record**, not batched. For a
  sidecar with many brand-new observations this is O(n) round-trips inside one SAVEPOINT. Given
  sidecars are per-doc and typically small (single-digit to low-tens of observations), I judged the
  simplicity worth it over prefetching all hashes up front; flagging in case a very large `_project.md`
  sidecar (aggregated deductions) makes this measurably slow in practice.
- I did not add a `sidecar_health`-equivalent JSON-schema doc anywhere; its shape is only documented
  in the CLI's own docstring and the function's docstring in `sidecars.py`. If this becomes a
  scripted/chained command (like `embed-missing`'s exit-code contract), it may deserve the same
  treatment.

## Addendum A — 2026-09-13 adversarial review (Kimi), applied same day

Implements A1–A7 appended to `docs/2026-09-13-obs-sidecar-spec.md`. Test count: **692 passed**
(up from 686 before this addendum; 6 new tests — 5 in `tests/test_sidecars.py`, 1 in
`tests/test_index_rebuild.py` — plus one existing test in `tests/test_observation_orphans.py`
extended to cover the new mirror table). `uv run ruff check` on every touched file: clean except
the same four pre-existing findings already noted above (an unrelated unused `Path` import in
`observations.py`, a mid-file import in `index_rebuild.py`, two unused test locals in
`test_index_rebuild.py`) — confirmed via `git diff` that none fall inside a hunk this addendum
touched.

**A1 — empty sidecar is never authoritative.** `ingest_all_sidecars` now reads each sidecar's text
before anything else; a zero-byte or whitespace-only file is counted under a new `empty` stat and
skipped entirely — no hash recorded, no diff attempted, so an existing doc's observations survive an
iCloud mid-transfer placeholder untouched. `sidecar_health` gained a matching `empty` list. Test:
`test_ingest_all_skips_empty_sidecar_and_does_not_record_hash` (covers both zero-byte and
whitespace-only, and asserts a doc's existing DB rows survive).

**A2 — ingest before the deleted-doc loop; adopt orphaned rows.** In `rebuild_incremental`,
`ingest_all_sidecars` now runs immediately after the doc loop and before the "remove deleted
documents" loop (previously it ran after). `ingest_sidecar` gained a keyword-only `indexed_paths`
parameter: when a record's `content_hash` already exists under a different doc_path, and that
doc_path is *not* in `indexed_paths` (the memo is gone from disk this run — the shape of an
already-arrived rename), the row is **adopted**: `UPDATE observations SET doc_path = ?` by id, then
the same obs_type/confidence/topics diff as an "in both" row (factored into a shared
`_sync_type_confidence_topics` helper so the adopt and in-both paths can't drift). fts/vec rows are
untouched because they key on the unchanged id. Counted under a new `adopted` stat. When the foreign
doc_path *is* still in `indexed_paths` (a genuinely live duplicate), behavior is unchanged
(`skipped_foreign`) except the file's hash is now deliberately **not** recorded — previously
`_record_sidecar_hash` ran unconditionally at the end of `ingest_sidecar`, which would have let a
real duplicate go stale (silently skipped forever) after its first report. New `foreign_conflicts`
list (in both `ingest_all_sidecars`'s return and `sidecar_health`) names both doc_paths so it's
diagnosable without reading stderr. `sidecar_health`'s version of this check is read-only (no
`indexed_paths` concept — it just flags any cross-doc content_hash collision as a candidate,
without attempting the adopt/skip distinction; an actual ingest is authoritative). `indexed_paths=None`
(the `memex obs ingest-sidecars` CLI path, which has no per-run on-disk snapshot to consult) always
treats a foreign hash as live — never adopts — matching the conservative default.
Tests: `test_ingest_adopts_orphaned_row_across_rename_keeps_id_and_vector` (id + pre-existing
`vec_observations` row + topics all survive),
`test_ingest_foreign_conflict_with_live_doc_does_not_adopt`,
`test_ingest_all_foreign_conflict_resurfaces_every_run` (same conflict reported on two consecutive
calls, hash never recorded), and an end-to-end
`test_incremental_adopts_orphaned_row_across_rename_before_deleting` in `test_index_rebuild.py`
exercising the actual `rebuild_incremental` wiring (old memo + sidecar removed from disk, new
memo + sidecar with identical content arrives, one `rebuild_incremental` call adopts rather than
delete-then-reinsert).

**A3 — `obs_pending_sources` table; phase 2 always sweeps the whole table.** New table added to
`init_observation_schema` (id-keyed like `observation_topics`), to `_OBS_MIRROR_TABLES` in
`observations.py` (so `delete_observation_ids` clears it — extended
`test_delete_observation_ids_clears_all_mirrors` to assert this), and to `_OBS_PRESERVATION_TABLES`
in `index_rebuild.py` with the same `JOIN main.observations` shape as `observation_topics` (extended
`EXPECTED_REAL` in `test_preservation_registry_covers_init_schema`; the mirror-table whitelist test
needed no change since it asserts a subset relation against `_OBS_MIRROR_TABLES` directly).
`ingest_sidecar` now writes a pending row for *every* `source_obs` hash a record references — not
only ones it fails to resolve immediately — via a new `_write_pending_sources` helper; this is
simpler than tracking "did this hash resolve at insert time" and costs nothing extra, since
`resolve_pending_sources` deletes whichever rows resolve regardless of how soon. Rewrote
`resolve_pending_sources` as two passes: pass 1 resolves this run's freshly-ingested records in file
order (as before — "file order where known"); pass 2 unconditionally sweeps the *entire*
`obs_pending_sources` table, including rows left over from a prior run whose referencing sidecar is
unchanged this run (and so was never re-parsed, never reaching pass 1) — newly resolved ids are
*appended* to whatever `source_obs_ids` already holds, since order isn't recoverable for those.
`ingest_all_sidecars` now calls `resolve_pending_sources` unconditionally (previously it happened
to run every time anyway since it wasn't gated, but now it's documented as load-bearing and wrapped
in its own `SAVEPOINT sidecar_pending` per the spec, isolated from the per-file savepoints). New
`pending_sources` stat = the outstanding count after phase 2 (distinct from the pre-existing
`pending_sources_resolved`/`pending_sources_unresolved`, kept for backward compatibility with the
CLI's existing output). `sidecar_health` gained a `pending_sources` count (`SELECT COUNT(*) FROM
obs_pending_sources`, defensive against a pre-A3 schema via `OperationalError` catch). Test:
`test_pending_source_resolved_on_later_run_with_no_file_changes` — a deduction's sidecar is ingested
once (source unresolved, persisted to the table), then a second run adds only the *source's* sidecar
and re-runs `ingest_all_sidecars`; the deduction's own sidecar file is never touched, yet its
`source_obs_ids` gets populated and the pending row is cleared — this is the scenario A3 exists for.

**A4 — first-export warning + machine of record.** `export_sidecars_cmd` in `cli.py` now checks
`find_sidecars(vault)` before calling `export_sidecars` and prints a stderr warning naming m4max as
the machine of record when the vault currently has zero sidecars on disk. `.claude/rules/
maintenance.md` gained a paragraph naming m4max as the machine of record and stating the
one-machine-at-a-time assumption for every observation-mutating command (backfill obs, dreamer,
retag, reassign, export-sidecars) — no cross-machine lock exists; iCloud is last-write-wins on a
sidecar file.

**A5 — cross-machine rename note.** Added to the same maintenance.md section: after a rename on one
machine, let iCloud settle before rebuilding on the other; explains in plain terms what A2's
adoption now does (keeps id/vector when the new sidecar has arrived) versus what happens if you
rebuild too early (old rows removed this run, adopted on a later one once the new sidecar syncs in)
— nothing is silently lost either way, syncing first just avoids the extra round trip.

**A6 — `.gitignore`.** Added `*.obs *.jsonl` (the iCloud numbered-conflict-copy shape, e.g.
`x.obs 2.jsonl`) with a comment pointing at `find_sidecar_conflicts`/`memex obs sidecars`.

**A7 — stats/health surfacing.** `ingest_all_sidecars` returns `empty`, `adopted`,
`foreign_conflicts` (list), and `pending_sources` (int, outstanding count after phase 2) alongside
the pre-existing keys. `sidecar_health` returns the same four (as `empty`, `foreign_conflicts`,
`pending_sources`, plus the pre-existing `stale`/`orphan`/`missing`/`conflicts`/`unportable`).
Surfaced in three CLI text-output paths: `memex obs sidecars` (new lines for empty/foreign
conflicts/pending), `memex obs ingest-sidecars` (adopted count added to the observations summary
line; empty/foreign-conflict lines added), and `format_rebuild_stats` in `index_rebuild.py` (new
lines, each gated on non-zero so a clean run's summary doesn't grow noisier).

**Deviation from the addendum's literal text:** A2 says ingest_sidecar's foreign-hash branch should
distinguish adopt vs. conflict, but doesn't specify what a *read-only* health check (no on-disk
snapshot of `indexed_paths` to consult) should do with the same collision. I had `sidecar_health`
report every cross-doc content_hash collision under `foreign_conflicts` without attempting the
adopt/skip distinction — it's necessarily a weaker signal than an actual ingest's decision (it can't
tell "this will adopt cleanly next run" from "this is a real duplicate"), documented as such in the
function's docstring. This seemed better than omitting the check from the health report entirely,
since A2 explicitly asks for `memex obs sidecars` to list these.

**Safety re-check:** re-ran the same vault-cleanliness check as the original implementation
(`find projects topics -name '*.obs*.jsonl'`) after this addendum's full test run — prints nothing,
confirming no test touched the real vault.
