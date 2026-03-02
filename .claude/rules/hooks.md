---
paths:
  - "hooks/**/*.py"
---

# Hook Development

## Gotchas

- **SessionStart context leaks to Haiku autocomplete** - Hook-injected context (memo nudges, open threads) gives Haiku enough situational awareness to suggest `/memex:save` at appropriate moments. Keep injected context actionable — it influences prompt suggestions, not just the main agent
- **hooks.json schema** - Don't reference in plugin.json (auto-discovered). Schema: `{"hooks": {"EventName": [{"matcher": "*", "hooks": [{...}]}]}}`
- **Hook concurrency** - Hooks can run in parallel; use FileLock with short timeouts (1-2s) around SQLite writes and per-session state changes
