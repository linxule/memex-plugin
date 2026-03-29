---
description: Show timeline of sessions and memos for a date range
allowed-tools: Read, Bash
argument-hint: "<date-expression> - e.g., 'yesterday', 'last week', '3 days ago'"
effort: low
---

# Timeline Command

Browse memex sessions and memos by date — no keywords needed.

## Instructions

1. **Run the temporal scanner** with the user's date expression:
   ```bash
   memex timeline "<date-expression>"
   ```

2. **With filters** (optional):
   ```bash
   # Filter by project
   memex timeline "yesterday" --project=my-app

   # Filter by type
   memex timeline "last week" --type=memo

   # JSON output for programmatic use
   memex timeline "this week" --json
   ```

## Supported Date Expressions

| Expression | Meaning |
|------------|---------|
| `yesterday` | Yesterday's sessions and memos |
| `today` | Today only |
| `3 days ago` | That specific day |
| `last 5 days` | 5-day range through today |
| `this week` | Monday of this week through today |
| `last week` | Previous Monday through Sunday |
| `last monday` | Most recent Monday |
| `7d`, `2w`, `3m` | Duration shorthand |
| `march 15` | Specific date (this year) |
| `2026-03-15` | Exact ISO date |

## Output Format

```
Timeline: yesterday

MEMOS (2):
  [2026-03-28] my-app    | CLI agent batch orchestration patterns [cli, agents]
  [2026-03-28] memex    | Temporal scan implementation [search, temporal]

TRANSCRIPTS (3):
  [2026-03-28] my-app    | 177 min, 81 turns | 20260328-135914-73c12fe2
  [2026-03-28] memex    |  42 min, 23 turns | 20260328-091500-abc123
  [2026-03-28] domains  |  15 min,  8 turns | 20260328-160000-def456

Total: 2 memo(s), 3 transcript(s)
```

## Tips

- Combine with `/memex:load <path>` to read a specific result in full
- For keyword search within a date range, use `/memex:search` with `--since` and `--before`
