---
paths:
  - "hooks/**/*.py"
---

# Hook Development

## Gotchas

- **SessionStart context leaks to Haiku autocomplete** - Hook-injected context (memo nudges, open threads) gives Haiku enough situational awareness to suggest `/memex:save` at appropriate moments. Keep injected context actionable — it influences prompt suggestions, not just the main agent
- **hooks.json schema** - Don't reference in plugin.json (auto-discovered). Schema: `{"hooks": {"EventName": [{"matcher": "*", "hooks": [{...}]}]}}`
- **Hook concurrency** - Hooks can run in parallel; use FileLock with short timeouts (1-2s) around SQLite writes and per-session state changes

## Claude Code constraints (verified 2026-10-28, Claude Code 2.1.312 hooks reference)

- **SessionEnd gets 1.5 s in total, and a plugin's `timeout` does not raise it** — the `timeout: 30` in `hooks.json` is decorative. Interpreter start and imports count against the budget (`session-end.py` starts its clock before importing), so new work at SessionEnd must stay cheap (logs 2026-09-20 to 10-28: all 522 completed archives took ≤ 1 s) and bounded by a deadline — the relay memo scan is. Anything heavy belongs to `session discover`/`import`. SessionEnd also **discards `systemMessage`**: nothing it prints reaches the user. News for the user goes through a state file the next SessionStart reads (the filing receipts, `last-archives.jsonl`)
- **PreCompact and PostCompact cannot inject or show anything** — `systemMessage` and `continue` are discarded for both, so they only write files. PreCompact matches `manual|auto`, and `auto` includes idle compaction (a 200K+ context idle ~54 min), so expect more compactions — and more post-compact messages — than before. PostCompact receives `compact_summary`; `post-compact.py` saves it to `<state_dir>/compact-summaries/<session_id>/<n>.md` (never the vault) and the recovery recipe reads it before the transcript
- **SessionStart `source` is startup | resume | clear | compact | fork** (`/fork`, `/branch`, a session moved to the background). `additionalContext` reaches the model, `systemMessage` the user. Strings are capped at 10,000 characters (beyond that Claude Code keeps a file path plus a 2,000-character preview), so keep injected text far below it. The compact message is the only place the model learns what happened to its memo — it states the truth from the vault, and fork sessions get no memo nags (the parent owns that work)
- **The subagent tool is `Agent`** (renamed from `Task` in 2.1.63; `Task(...)` still works as an alias). Write `Agent(...)` in anything a hook or command tells Claude to run. Plain stdout from UserPromptSubmit is still added as context

## Deciding from the vault

- **Truth check = a stamped memo in the vault, not `state.json`.** `memex.session_memos.memo_for_session(vault, session_id)` returns the memo whose frontmatter stamps `session_id:` or `session_ids:` (memos in `memos/<sub>/` count); `memo_session_map(vault)` is the whole-vault form. A full miss costs ~0.5 s, so call it only when about to act (nudge, write a signal, announce one). PreCompact writes no pending signal when a stamped memo exists — it records the path in `session-state/<sid16>.memo` instead, where UserPromptSubmit and the post-compact SessionStart find it without a scan
- **Fleet seats get silence.** `CLAUDE_CODE_ENTRYPOINT=sdk-cli`, or a transcript whose first `entrypoint` is `sdk-cli` (`is_helper_session` in `reconcile_orphans.py`): no pending signal, nudge, pointer or post-compact message. A signal that does exist for a seat is dismissed by `reconcile-orphans`
- **Forks don't double-count.** A fork's transcript starts with the parent's records; the nudge measures activity from the fork point, not from byte zero

## Dependencies

- **Any import a hook gains goes in its PEP 723 block** — Claude Code runs each hook with `uv run --script`, so only the block's packages exist, while the test interpreter has every project dependency. That includes imports pulled in transitively: a hook importing `memex.session_memos` (or `reconcile_orphans`) loads `yaml`, so it must declare `pyyaml>=6.0`; the same hook without it dies at session start, silently
- **`tests/test_hooks_run_for_real.py` is the guard.** It runs every hook with `uv run --exact --script` (`--exact` drops packages the header no longer lists) against a throwaway HOME and vault (`hook_env` in `tests/hook_helpers.py`) and fails on a missing dependency or a traceback. A new hook must get an entry in its `_payloads`. Hooks don't see the pydantic-backed `MEMEX_*` settings (`MEMEX_MEMEX_PATH`, `MEMEX_STATE_DIR`: there is no pydantic in their environment, so `get_config()` falls back to raw `~/.memex/config.json`) — hook tests set HOME for the subprocess instead; never pipe a payload into a hook with the real HOME
