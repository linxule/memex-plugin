---
description: Retry failed memo generations from pending queue
allowed-tools: Read, Write, Bash
argument-hint: "[session_id] - optional specific session to retry"
---

# Retry Command

Retry memo generation for sessions that failed previously.

## Path Resolution (Required First)

**IMPORTANT:** `${CLAUDE_PLUGIN_ROOT}` points to the plugin cache, NOT the vault!

Resolve the vault path:
1. Read `~/.memex/config.json` → use `memex_path` if present
2. Fallback: use the plugin's own location (where `scripts/` lives)

Store this as `$VAULT` for use in paths below.

## Instructions

1. **Check pending memos**:
   ```bash
   ls ~/.memex/pending-memos/*.json 2>/dev/null
   ```

2. **If specific session provided**, retry only that one

3. **If no argument**, list all pending and offer to retry all or select

4. **For each pending memo**:
   - Read the pending info: session_id, transcript_path, project, error
   - Check if transcript still exists
   - Run memo generation:
     ```bash
     cd $VAULT
     echo '{"session_id": "<id>", "transcript_path": "<path>", "cwd": "<project_path>", "trigger": "manual"}' | \
       uv run hooks/pre-compact.py
     ```

5. **Report results**:
   - Success: Memo created, pending marker removed
   - Failure: Show error, keep in queue

## Output

```
🔄 Retrying failed memo generations...

Pending: 2 memos

1. Session abc123... (myproject)
   Error: RateLimitError
   ✅ Retried successfully - memo created

2. Session def456... (another-project)
   Error: AuthenticationError
   ❌ Still failing - check ANTHROPIC_API_KEY

Summary: 1/2 succeeded
Remaining pending: 1
```

## Notes

- AuthenticationError usually means API key issue
- RateLimitError often succeeds on retry
- If transcript is deleted, pending memo is cleared automatically
