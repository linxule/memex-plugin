# Layer-2 Memo Recovery

The one recipe for writing a memo **after the fact**: a session compacted (or ended) without saving one, so a background subagent rebuilds it. Hooks and `/memex:save` point here instead of carrying their own prompt. If you were in the session and can write the memo yourself, do that instead (Layer 1, `commands/save.md`); a memo from lived context is better than this one.

## Spawning it (the parent session)

Spawn one subagent per session, pinned to sonnet (memo writing is a sonnet-tier task; don't spend a heavier model on it):

```
Agent({
  description: "Recover session memo",
  subagent_type: "general-purpose",
  model: "sonnet",
  prompt: "Follow the memo-recovery recipe at <RECIPE_PATH>. Inputs: TRANSCRIPT_PATH=<...> SUMMARIES_DIR=<...> PROJECT=<...> SESSION_ID=<...> VAULT_PATH=<...>"
})
```

Run it in the background (`run_in_background: true` if your Agent tool has that parameter) and keep working. Fill every value with an absolute path or the literal id; a subagent inherits no shell state, env vars or context.

## Inputs

The spawning message supplies these. Wherever this file writes one of the names in angle brackets, substitute the value. If any is missing, report which and stop.

| Name | Meaning |
|---|---|
| `<TRANSCRIPT_PATH>` | the session's `.jsonl` transcript |
| `<SUMMARIES_DIR>` | directory of compaction summaries, `<state dir>/compact-summaries/<session id>/` (normally `~/.memex/compact-summaries/<session id>/`, written by the PostCompact hook) |
| `<PROJECT>` | project folder name under the vault's `projects/` |
| `<SESSION_ID>` | the session's full UUID |
| `<VAULT_PATH>` | absolute path of the memex vault |

You run unattended: don't ask questions, make the best judgment from the record, and say what you were unsure of in the memo.

## Steps

**1. Check no memo exists.** Never regenerate one that is there:

```bash
find "<VAULT_PATH>/projects" -path '*/memos/*' -name '*.md' -exec grep -l "<SESSION_ID>" {} +
```

Any hit: stop and report its path. (A memo stamped with this session id also clears the pending-memo signal; that is why step 4 writes `session_id`.)

**2. Read the compaction summaries first.** `ls "<SUMMARIES_DIR>"` lists `1.md`, `2.md`, ... one per compaction, oldest first; each has a little frontmatter, then the summary the compacting model wrote with the whole conversation in view. Read all of them in order: they give the spine (what the work was, decisions, state at each compaction). If the directory is missing or empty (the session predates the hook, or a summary came back empty), skip to step 3 and rely on the transcript alone.

**3. Read the transcript second**, for what a digest drops: failed approaches, surprises, the user's own words, disagreement, and everything after the last summary. The transcript is JSONL (one JSON object per line, `type` is `user` or `assistant`) and can be many MB, so never read it whole. Locate with grep, then read slices with offset/limit: the user's messages first (priorities, pushback, quotable lines), then assistant text around decisions and tool errors that preceded a change of approach. Skip tool-output bodies.

**4. Write the memo.** Read `memo-default.md` (same directory as this file) and follow its format and guidelines. Optionally `memex search "<keywords>" --mode=hybrid --format=text` for related memos to wikilink (full path with date prefix: `[[projects/<p>/memos/2026-03-11-slug|label]]`). Save to:

`<VAULT_PATH>/projects/<PROJECT>/memos/<YYYY-MM-DD>-<slug>.md`

`<YYYY-MM-DD>` is the day the work happened (summary timestamps, transcript), `<slug>` the kebab-case title. Frontmatter: `type: memo`, `title`, `project: <PROJECT>`, `date`, `topics` (kebab-case slugs), and `session_id: <SESSION_ID>`. The id lets `memex session reconcile-orphans` match the memo to its pending signal exactly.

**Never transcribe secrets.** No API keys, tokens or passwords, nothing matching `sk-*`, `ghp_*`, `AIza*`, `AKIA*`, `xox*`, or a PEM private-key block, in the memo or the observations. Describe what leaked (provider + leak vector) without reproducing it.

**5. Scrub.** A backstop for surface-pattern secrets that slipped through; idempotent:

```bash
memex scrub "<VAULT_PATH>/projects/<PROJECT>/memos/<filename>.md" --apply
```

Quote the path. Do this before step 6 so nothing secret reaches the observations or the search index.

**6. Extract observations.** Re-read the memo and pull 5-15 atomic observations:

- each independently understandable; absolute dates ("2026-03-16", not "today")
- decisions ("Decision: X was chosen over Y because Z"), facts, preferences, constraints, open questions
- no meta-observations about the memo, nothing trivial
- `obs_type`: `explicit` (directly stated) or `deductive` (follows from combining stated facts); `confidence`: `high`, `medium` or `low`
- `topics`: 0-3 slugs that match files in `<VAULT_PATH>/topics/` (`ls "<VAULT_PATH>/topics/"`); `[]` when none fit. An invalid slug is stored without error but breaks topic clustering.

Send **all of them in ONE call**. `--replace` overwrites every observation the memo already has, so a second call destroys the first:

```bash
memex backfill obs --stdin --replace --doc-path "projects/<PROJECT>/memos/<filename>.md" <<'OBSERVATIONS_JSON'
[
  {"content": "Decision: X was chosen over Y because Z", "obs_type": "explicit", "confidence": "high", "topics": ["relevant-topic"]},
  {"content": "Constraint: A requires B", "obs_type": "explicit", "confidence": "high", "topics": []}
]
OBSERVATIONS_JSON
```

`--doc-path` is relative to the vault. The quoted heredoc keeps apostrophes and JSON backslash escapes intact.

**7. Report** in three lines: the memo path, the observation count, and the one thing you could not establish from the record.
