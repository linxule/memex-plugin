---
name: project-consolidation
description: Safely consolidate drifted/duplicate project folders in the memex vault — cwd-fragment folders (e.g. `Apps-arena`, `GitHub-loom`), duplicate/split projects (one project's content scattered across two folders), and name-mismatch folders. Trigger on "consolidate project folders", "merge duplicate projects", "fix project detection drift", "deduplicate Apps-* folders", "clean up fragment folders", or after `memex check --folders` reports drift. Preserves observations + embeddings via `memex obs reassign` (never naive `git mv` + rebuild, which cascade-deletes obs).
argument-hint: "[audit|consolidate <from> <to>]"
allowed-tools: Read, Bash, Grep, Glob
---

# Project-Folder Consolidation

Drifted project folders happen when session→project detection produced a
non-canonical name: a cwd-path fragment (`Apps-arena` instead of `arena`), or a
project split across folders (content under both `personal-website` and
`linxule_com`). The detection bugs that caused this were fixed in v0.15.5/v0.15.6
(`detect_project` via true session `cwd` + restored `project_mappings`), so **no
new drift forms** — this skill cleans up existing debt.

## The cardinal rule

**Never run `memex obs reassign` on a folder until you have confirmed it is a
duplicate or strict subset of the canonical target.** A wrong merge collapses two
distinct projects. And **never** consolidate via `git mv` + `memex index rebuild`
— the incremental rebuild detects the old paths as deleted and **cascade-deletes
the observations and their embeddings**. `obs reassign` is the only safe primitive
(it `UPDATE`s `doc_path` in `observations` + `chunks`, preserving embeddings).

## Step 1 — Audit

```bash
memex check --folders            # human report: high-confidence drift + review list
memex check --folders --json     # machine-readable, for planning
```

- **High-confidence drift** = cwd-fragment-shaped names, or content ⊆ another folder. Safe to act on after confirming duplication.
- **Review (lower confidence)** = `name≠canonical` / canonical collisions. The git remote or a memo's recorded `cwd` can legitimately differ from a deliberately-chosen folder name (e.g. a real project whose folder ≠ its repo name). **Verify manually; do not auto-act.**

## Step 2 — Confirm duplication (before touching anything)

Pick the canonical target — prefer the folder that already holds the most
observations and matches `detect_project` / `project_mappings`. Then prove the
source is redundant:

```bash
# Memo/transcript filename overlap (a strict subset is a clean duplicate):
comm -23 <(ls projects/<from>/memos/ | sort) <(ls projects/<to>/memos/ | sort)        # files UNIQUE to <from>
comm -23 <(ls projects/<from>/transcripts/ | sed 's/\.\(md\|jsonl\)$//' | sort -u) \
         <(ls projects/<to>/transcripts/ | sed 's/\.\(md\|jsonl\)$//' | sort -u)
# If a memo exists in both, diff it — often only the `project:` frontmatter line differs:
diff projects/<from>/memos/<file>.md projects/<to>/memos/<file>.md
```

If everything in `<from>` is already in `<to>` (only `project:` differs), `<from>`
is a pure duplicate → delete it (Step 4b). If `<from>` has UNIQUE content → migrate
it (Step 3). Auto-memory under a fragment is almost always a re-syncable duplicate
(source of truth is `~/.claude/projects/*/memory/`); transcripts/memos with no
canonical twin are unique and must be moved, not deleted.

## Step 3 — Migrate unique content (preserves obs + embeddings)

```bash
# Record the invariant BEFORE:
memex obs stats          # or: total-obs count — must be unchanged at the end

# 3a. Move files on disk (git mv for tracked memos/_project; mv for gitignored transcripts):
mkdir -p projects/<to>/memos projects/<to>/transcripts
git mv projects/<from>/memos/*.md projects/<to>/memos/
mv    projects/<from>/transcripts/*  projects/<to>/transcripts/    2>/dev/null

# 3b. Update the project: frontmatter on moved memos:
for m in projects/<to>/memos/<moved>*.md; do
  sed -i.bak 's/^project: <from>$/project: <to>/' "$m" && rm -f "$m.bak"
done

# 3c. Reassign obs + chunks by SUBPREFIX (avoids the _project.md collision):
memex obs reassign --from-prefix "projects/<from>/memos/"       --to-prefix "projects/<to>/memos/"          # dry-run
memex obs reassign --from-prefix "projects/<from>/memos/"       --to-prefix "projects/<to>/memos/" --apply
memex obs reassign --from-prefix "projects/<from>/transcripts/" --to-prefix "projects/<to>/transcripts/" --apply
```

Reassign reports `obs_updated` / `chunks_updated` and rolls back on a UNIQUE
collision (same `doc_path` already at the target — a sign the file is a true
duplicate, not a unique migration; delete the source copy instead).

## Step 4 — Finish

```bash
# 4a. Archive the fragment _project.md as a redirect stub (audit trail), OR remove it
#     if the canonical _project.md is authoritative.
# 4b. Remove the now-empty / pure-duplicate fragment folder:
rm -rf projects/<from>
# 4c. Incremental rebuild (no-op on moved content since disk paths now match the index):
memex index rebuild --incremental
```

## Step 5 — Verify the invariant + prevent recurrence

```bash
memex check --folders        # the consolidated folder should be gone
memex obs stats              # TOTAL observations unchanged from Step 3 (the invariant)
```

- If total obs **dropped**, a cascade-delete happened — restore from git/backup and re-do via `obs reassign`, not `git mv`+rebuild.
- For a project whose cwd should always map to a specific name, pin it: add `"<cwd-substring>": "<canonical>"` to `~/.memex/config.json` → `project_mappings` (now honored in CLI as of v0.15.5).

## When to stop and ask

- The "review" (lower-confidence) findings — `name≠canonical` without content overlap — are usually **legitimate distinct projects**, not drift. Confirm with the user before merging.
- Two folders with substantial obs on **both** sides and only partial overlap = a genuine split needing human judgment on the canonical home, not a mechanical reassign.
