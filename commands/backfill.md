---
description: Batch extract observations from memos that don't have them yet
allowed-tools: Read, Bash, Agent, Glob, Write
argument-hint: "[--limit N] [--project NAME]"
effort: low
---

# Observation Backfill Command

Extract atomic observations from memos missing them, using parallel Claude Code subagents.

## Instructions

### 1. Find Pending Docs

```bash
cd $(memex path) && uv run python -c "
import sqlite3, sys
sys.path.insert(0, 'src')
from memex.paths import get_index_path
from memex.observations import init_observation_schema
conn = sqlite3.connect(get_index_path())
init_observation_schema(conn)
rows = conn.execute('''
    SELECT f.path, f.title, f.project
    FROM fts_content f
    WHERE NOT EXISTS (SELECT 1 FROM observations o WHERE o.doc_path = f.path)
      AND f.type IN (\"memo\", \"concept\", \"topic\", \"project-overview\")
    ORDER BY f.date DESC
    LIMIT 25  -- replace with --limit arg value
''').fetchall()
for r in rows: print(f'{r[0]}|{r[1]}|{r[2]}')
print(f'---')
total = conn.execute('''
    SELECT count(*) FROM fts_content f
    WHERE NOT EXISTS (SELECT 1 FROM observations o WHERE o.doc_path = f.path)
      AND f.type IN (\"memo\", \"concept\", \"topic\", \"project-overview\")
''').fetchone()[0]
print(f'total_remaining:{total}')
conn.close()
"
```

Replace `LIMIT_VALUE` with the `--limit` argument (default: 25).
If `--project` is specified, add `AND f.project = ?` to both queries.

Report: "Found N docs needing observation extraction (M total remaining)"

### 2. Launch Subagent Waves

**Critical design: ONE memo per subagent.** Multi-memo agents are unreliable at executing the store step for every memo. One memo per agent ensures each extraction is stored.

Split pending docs into waves of **5 parallel agents**. For each wave, launch all 5 as **background agents** in a single message (one Agent tool call per memo).

**Subagent configuration:**
- `subagent_type`: `general-purpose`
- `model`: `haiku`
- `run_in_background`: `true`

**Subagent prompt template** (fill in `VAULT_PATH`, `DOC_PATH` and `UNIQUE_ID` per memo — get `VAULT_PATH` from `memex path`):

```
Extract atomic observations from a memo and store them. You MUST complete all 4 steps.

**Step 1 — Read:** Read the memo at VAULT_PATH/DOC_PATH

**Step 2 — Extract:** Generate 5-15 atomic observations as a JSON array. Rules:
- Each observation must be independently understandable without context
- Use absolute dates (e.g., "2026-03-13" not "today" or "yesterday")
- Attribute correctly: "we decided X" → "Decision: X was chosen over Y because Z"
- Capture: decisions, facts, preferences, constraints, open questions
- Do NOT extract meta-observations about the memo itself
- Do NOT extract obvious/trivial facts
- Types: "explicit" (directly stated) or "deductive" (follows from combining facts)
- Confidence: "high", "medium", or "low"

**Step 3 — Pipe JSON and store (MANDATORY):** Pipe the observations directly to the store command:
```bash
echo '[{"content": "...", "obs_type": "explicit", "confidence": "high"}, ...]' | memex backfill obs --stdin --doc-path "DOC_PATH"
```

You are NOT done until Step 4 completes and you see the JSON output with "stored" count.
Report the stored count.
```

Use `UNIQUE_ID` = wave number + index (e.g., `w3_01`, `w3_02`).

### 3. Between Waves

After each wave of 5 completes:

1. **Verify storage** — run a quick count:
```bash
cd $(memex path) && uv run python -c "
import sqlite3
conn = sqlite3.connect('_index.sqlite')
print(conn.execute('SELECT count(*) FROM observations').fetchone()[0], 'total observations')
print(conn.execute('SELECT count(DISTINCT doc_path) FROM observations').fetchone()[0], 'docs with observations')
conn.close()
"
```

2. **Report progress:**
```
Wave N complete: X/5 succeeded
Running total: Y docs processed, Z observations stored
Remaining: R docs
```

3. **Launch next wave** with the next 5 docs.

### 4. Final Summary

```
Backfill complete:
- Docs processed: X
- Total observations: Y
- Remaining: Z
```

## Key Lessons (from production experience)

1. **ONE memo per agent** — multi-memo agents extract correctly but skip the store step for some memos. The "Step 4 (MANDATORY)" emphasis + single-memo scope fixes this.
2. **haiku is sufficient** — fast, cheap, quality extraction (12-16 observations per memo typical).
3. **5 parallel agents per wave** — safe concurrency level. Don't increase beyond this.
4. **Verify between waves** — always check stored count before launching the next wave.
5. **content_hash dedup** — running backfill twice on the same memo is safe (duplicates are skipped by hash).
6. **Embeddings happen at store time** — observations are embedded via Gemini when GEMINI_API_KEY is set. No separate embedding step needed.
