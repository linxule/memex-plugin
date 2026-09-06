# Code health review — 2026-09-06

This pass reviewed application code with parallel agents covering retrieval,
index integrity, session handling, and CLI/packaging architecture. A separate
review checked integration and data preservation. Existing vault-content edits
were excluded from the commits.

## Improvements

- **Retrieval:** batch vector-hit metadata enrichment instead of scanning the
  FTS table per chunk. The regression fixture with 24 vector hits across two
  documents went from 50 metadata reads to one, with the same documents
  returned. This is a query-count measurement, not an end-to-end speed claim.
  Date filters now agree across keyword/vector search; nearest-neighbor ranks
  survive score clamping. Short acronyms and empty queries have explicit tests.
- **Indexing:** maintain document hashes, chunks, and graph data without an
  embedding provider. FTS failures reach the document transaction boundary.
  Legacy FTS refresh delegates to canonical indexing instead of deleting the
  shared database, preserving stored observations and unchanged vectors.
  Rebuild commands hold a common exclusive lock; a failed atomic rebuild
  preserves the previous index. An unavailable vector extension cannot leave
  old vectors attached to newly reused chunk IDs.
- **Recovery:** stage transcript artifacts until conversion succeeds, keeping
  failed sessions discoverable. The viability check stops after six nonempty
  lines or a tool-use record. Invalid JSON record shapes no longer discard valid
  later conversation. Auto-memory sync recognizes complete frontmatter
  delimiter lines, including titles that contain `---`.
- **Maintainability:** share the CLI delegation context and restore caller
  arguments/working directory on failure as well as success. Share exception-safe
  SQLite extension loading. Separate local document schema initialization from
  optional vector setup.
- **Packaging:** installed wheels resolve versions through distribution
  metadata; source/plugin checkouts retain live pyproject version lookup. Track
  the uv lockfile, install Ruff with the default dev group, and require the
  pydantic-settings version that supports the configuration API in use. See
  [development instructions](../DEVELOPMENT.md).

## Validation

The baseline was 459 passing tests and three failing offline-index tests. New
regressions use temporary databases/vaults and synthetic providers; they cover
actual FTS5/sqlite-vec behavior, transaction failures, lifecycle recovery, and
  installed-package version lookup. CLI smoke tests now use a fixture index rather
  than the user's live index.

Final validation: **578 tests passed** with `uv run --locked pytest -q`.
The built wheel imported and ran CLI help from an isolated directory outside the
checkout. The lockfile check and scoped whitespace checks passed. A comparison
of Ruff diagnostics against the previous code found **no new findings** in
changed/new Python files; the repository-wide lint pass still reports 55 existing
findings. The independent integration reviewer verified both unavailable-vector
rollback and exclusive rebuild locking with isolated reproductions.

## Follow-up candidates

- Consolidate vault resolution in `paths.py` and `scripts/utils.py`. The legacy
  script-location fallback still assumes the pre-`src` layout, and plugin-root
  fallback behavior disagrees with the configuration guide. Resolve the intended
  first-run/plugin-cache contract before changing precedence already covered by
  configuration tests.
- Audit the UserPromptSubmit and `mark-saved` read/modify/write lifecycle under
  concurrency. Static review found updates to shared session state without one
  common transaction lock; a deterministic race reproduction is still needed.
- Existing Ruff findings remain outside this functional cleanup. Prefer fixing
  them in the modules being changed and extracting further modules around clear
  responsibilities, with existing import/CLI contracts preserved.
- Legacy vector indexes without metadata columns still filter after a bounded
  nearest-neighbor query, which can reduce recall. Migrated indexes support
  filtering inside the vector query; this pass tested both layouts.

## Credential setup and CLI follow-up

Added [Gemini credential setup](gemini-credentials.md): missing-key guidance
offers an explicit 1Password wrapper, and `memex auth set-key` optionally saves
a local owner-only key for automatic loading. Status reports the source without
printing keys or contacting Gemini. Tests use fake keys only.

Claude's reported piped-input failure came from an `echo` command with unescaped
apostrophes inside a single-quoted JSON literal. Valid pipes and file redirects
both work. Input validation now reports empty/malformed/invalid-schema JSON before
provider initialization or database changes. The save command's example now uses
a quoted heredoc instead of teaching the failing pattern.

Current `memex search --mode=fts` works from another directory against an isolated
index. The historical traceback was truncated before its final exception; it
occurred between the CLI and index/search edits, so an intermediate source state
is plausible but the exact exception cannot be recovered. A separate local
editable-install issue remains: macOS marks `.pth` files hidden, causing Python
to skip them. Clearing the flags restored direct `.venv/bin/memex` temporarily,
but they returned. The normal `bin/memex` wrapper's explicit `PYTHONPATH` continues
to work; no durable repair of the host metadata issue is claimed.

Follow-up validation: **605 tests passed**. The built wheel's credential
save/status/clear lifecycle passed outside the checkout with a fake key, and
changed Python files introduced no new Ruff findings.

## Independent review of this pass (Claude, same day)

Four parallel reviewers re-verified the five commits with throwaway indexes
and fake providers. Findings, all fixed in v0.18.0 with regression tests in
`tests/test_review_followups.py`:

- **Regression:** offline incremental runs (no key in the environment — the
  normal case inside a Claude Code session) deleted a changed document's
  vectors *and* refreshed its hash, so a later keyed `--incremental` skipped
  it and the gap never closed. A keyed incremental now runs the
  `embed-missing` pass when gaps exist, and the offline path warns.
- An aborted atomic `--full` left the multi-GB `.tmp` database on disk.
- `memex index embed-missing` ran without the shared writer lock while chunk
  rowids are reusable.
- Contended locks failed instantly with exit 3; rebuilds and writers now wait
  up to 30 seconds (`LOCK_WAIT_SECONDS`).
- `--before` alone still admitted undated documents on the keyword path
  (`'' < 'YYYY-MM-DD'`), contradicting the "date filters agree" claim above.
- Low: credentials directory not re-tightened when pre-existing; piped
  `set-key` printed getpass fallback noise; `clear-key` looped on a directory;
  a corrupt saved key file was not named in its error; provider `repr` could
  spell the key; `import memex` hard-failed outside the two supported layouts;
  SIGKILLed SessionEnd hooks stranded `.archive-*` staging dirs; BOM'd
  observation JSON was rejected.

Not a code defect but confirmed: the `.venv/bin/memex` failure is iCloud
Drive marking `.venv` contents hidden plus Python 3.13 skipping hidden
`.pth` files (see `DEVELOPMENT.md`).

