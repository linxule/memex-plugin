#!/usr/bin/env python3
"""Batch-dispatch observation extraction across memos missing from the index.

For each memo in the work list, this script:
1. Spawns `claude --print --model sonnet --output-format text` with the
   extraction prompt and read-only tool access.
2. Captures the JSON output (5-15 atomic observations per memo)
3. Pipes it to `memex backfill obs --stdin --doc-path <memo-path>`

Concurrency is bounded by --workers (default 5) to match GEMINI_CONCURRENCY.
Idempotent: skips memos that already have observations via content_hash UNIQUE
constraint at the storage layer.

Vault is resolved from `memex path` — set `MEMEX_VAULT` to override.
The `memex` CLI must be on `$PATH` (install via `uv tool install .`).

Usage:
    uv run scripts/batch_extract_observations.py \\
        --work-list /tmp/missing_obs_memos.txt \\
        --workers 5 \\
        --log-file ~/.memex/logs/batch-obs-extraction.jsonl
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Sample of valid topic slugs that observations can be tagged with. The
# dispatcher reads `topics/*.md` filenames at startup and passes the full
# list to each subagent — keeps subagent prompts independent of vault state.

EXTRACTION_INSTRUCTION = """\
You are extracting atomic observations from a session memo.

The memo file lives at this absolute path:
{memo_abs_path}

Read the file with the `Read` tool, then return ONE THING: a single JSON
array of observation objects. Nothing else — no preamble, no explanation,
no code fence. Just the raw JSON array.

Rules per observation:
- Each observation is a single, self-contained fact understandable without
  surrounding context.
- Use absolute dates (e.g., "2026-03-16" not "today").
- Attribute correctly: if the memo says "we decided X", the observation
  should be "Decision: X was chosen over Y because Z".
- Capture decisions, facts, preferences, constraints, open questions.
- Skip trivial / meta observations about the memo itself.
- Target 5-15 observations.

Each JSON object must have these fields:
- "content": string, the observation text
- "obs_type": one of "explicit" | "deductive" | "contradiction"
- "confidence": one of "high" | "medium" | "low"
- "topics": array of 0-3 topic slugs from this approved list:

{topic_list}

Return the JSON array as your ENTIRE response. The orchestrator will pipe
it directly to a parser — extra prose breaks the pipeline.
"""


def load_topic_slugs(vault: Path) -> list[str]:
    """Read all topic filenames from topics/ as slugs."""
    topics_dir = vault / "topics"
    slugs = []
    for p in sorted(topics_dir.glob("*.md")):
        slugs.append(p.stem)
    return slugs


def already_extracted(vault: Path, memo_rel: str) -> bool:
    """Check if observations already exist for this memo in the index."""
    import sqlite3
    idx = vault / "_index.sqlite"
    if not idx.exists():
        return False
    conn = sqlite3.connect(idx)
    try:
        row = conn.execute(
            "SELECT 1 FROM observations WHERE doc_path = ? LIMIT 1",
            (memo_rel,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def parse_json_array(text: str) -> list[dict]:
    """Pull a JSON array out of the model output, tolerating prose noise.

    The instruction asks for a bare array, but models sometimes wrap it in
    a code fence or add a one-line preface. Scan for the first `[` and the
    last `]`; parse the slice.
    """
    text = text.strip()
    if not text:
        raise ValueError("empty model output")
    # Strip common fence wrappers
    if text.startswith("```"):
        # Skip until first newline
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text[: -3].rstrip()
        # Strip an optional "json\n" language tag at the very top
        if text.startswith("json\n"):
            text = text[5:]
    # Find array bounds
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        raise ValueError(f"no JSON array found in output: {text[:200]!r}")
    arr = json.loads(text[start : end + 1])
    if not isinstance(arr, list):
        raise ValueError(f"expected array, got {type(arr).__name__}")
    return arr


async def extract_one(
    memo_rel: str,
    vault: Path,
    topic_list_str: str,
    sem: asyncio.Semaphore,
    log_fn,
) -> dict:
    """Run extraction + storage for a single memo. Returns a result dict."""
    async with sem:
        memo_abs = vault / memo_rel
        if not memo_abs.exists():
            return {
                "memo": memo_rel,
                "status": "missing",
                "error": "file does not exist",
            }
        if already_extracted(vault, memo_rel):
            return {"memo": memo_rel, "status": "skipped", "reason": "already has obs"}

        prompt = EXTRACTION_INSTRUCTION.format(
            memo_abs_path=str(memo_abs),
            topic_list=topic_list_str,
        )

        # Spawn claude --print in subprocess
        t0 = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                "claude",
                "--print",
                "--model",
                "sonnet",
                "--output-format",
                "text",
                "--add-dir",
                str(vault),
                "--allowedTools",
                "Read",
                "--dangerously-skip-permissions",
                prompt,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=180
            )
        except asyncio.TimeoutError:
            return {
                "memo": memo_rel,
                "status": "timeout",
                "elapsed": time.monotonic() - t0,
            }

        elapsed = time.monotonic() - t0
        if proc.returncode != 0:
            return {
                "memo": memo_rel,
                "status": "claude_error",
                "returncode": proc.returncode,
                "stderr": stderr.decode(errors="replace")[:500],
                "elapsed": elapsed,
            }

        out = stdout.decode(errors="replace")
        try:
            obs_list = parse_json_array(out)
        except ValueError as e:
            return {
                "memo": memo_rel,
                "status": "parse_error",
                "error": str(e),
                "raw_head": out[:300],
                "elapsed": elapsed,
            }

        # Pipe JSON to memex backfill obs --stdin --doc-path <memo_rel>.
        # Use the `memex` CLI from PATH (installed via `uv tool install`).
        # The vault path is resolved by `memex` itself via ~/.memex/config.json.
        try:
            store = subprocess.run(
                # --replace is explicit: this script only targets memos with NO existing
                # observations, so replace is correct, and stating it now means a
                # future release that requires an explicit mode is a no-op here.
                ["memex", "backfill", "obs", "--stdin", "--replace",
                 "--doc-path", memo_rel],
                input=json.dumps(obs_list).encode(),
                capture_output=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            return {
                "memo": memo_rel,
                "status": "store_timeout",
                "elapsed": elapsed,
                "obs_count": len(obs_list),
            }

        if store.returncode == 0:
            return {
                "memo": memo_rel,
                "status": "ok",
                "obs_count": len(obs_list),
                "elapsed": elapsed,
            }
        return {
            "memo": memo_rel,
            "status": "store_failed",
            "returncode": store.returncode,
            "stderr": store.stderr.decode(errors="replace")[:500],
            "obs_count": len(obs_list),
            "elapsed": elapsed,
        }


async def main_async(args):
    vault_env = os.environ.get("MEMEX_VAULT")
    if not vault_env:
        # Try resolving via `memex path` (requires `memex` CLI on PATH).
        try:
            result = subprocess.run(
                ["memex", "path"], capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0 and result.stdout.strip():
                vault_env = result.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    if not vault_env:
        print(
            "Error: vault path not resolved. Set MEMEX_VAULT or ensure "
            "the `memex` CLI is on PATH (install via `uv tool install .`).",
            file=sys.stderr,
        )
        return 1
    vault = Path(vault_env)
    if not vault.exists():
        print(f"Vault path does not exist: {vault}", file=sys.stderr)
        return 1

    work_list_path = Path(args.work_list)
    if not work_list_path.exists():
        print(f"Work list not found: {work_list_path}", file=sys.stderr)
        return 1
    memos = [
        line.strip() for line in work_list_path.read_text().splitlines() if line.strip()
    ]
    if args.limit:
        memos = memos[: args.limit]
    print(f"Loaded {len(memos)} memos from {work_list_path}", file=sys.stderr)

    topic_slugs = load_topic_slugs(vault)
    topic_list_str = "\n".join(f"- {s}" for s in topic_slugs)
    print(f"Loaded {len(topic_slugs)} topic slugs", file=sys.stderr)

    log_path = Path(args.log_file).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_f = log_path.open("a")
    start_ts = datetime.now().isoformat()
    log_f.write(json.dumps({"event": "batch_start", "ts": start_ts, "memos": len(memos)}) + "\n")
    log_f.flush()

    sem = asyncio.Semaphore(args.workers)
    tasks = [
        extract_one(m, vault, topic_list_str, sem, log_f)
        for m in memos
    ]

    completed = 0
    counts = {"ok": 0, "skipped": 0, "missing": 0, "claude_error": 0,
              "parse_error": 0, "timeout": 0, "store_failed": 0, "store_timeout": 0}
    total_obs = 0
    t0 = time.monotonic()
    for fut in asyncio.as_completed(tasks):
        result = await fut
        completed += 1
        status = result.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1
        total_obs += result.get("obs_count", 0) or 0
        log_f.write(json.dumps(result) + "\n")
        log_f.flush()
        if completed % 10 == 0 or completed == len(tasks):
            elapsed = time.monotonic() - t0
            rate = completed / elapsed if elapsed > 0 else 0
            eta = (len(tasks) - completed) / rate if rate > 0 else 0
            print(
                f"  {completed}/{len(tasks)}  ok={counts['ok']}  "
                f"skipped={counts['skipped']}  errors={sum(v for k, v in counts.items() if k not in ('ok', 'skipped'))}  "
                f"obs={total_obs}  rate={rate:.2f}/s  eta={eta/60:.1f}min",
                file=sys.stderr,
            )

    log_f.write(
        json.dumps(
            {
                "event": "batch_end",
                "ts": datetime.now().isoformat(),
                "elapsed_seconds": time.monotonic() - t0,
                "counts": counts,
                "total_obs": total_obs,
            }
        )
        + "\n"
    )
    log_f.close()
    print(f"Done. Counts: {counts}. Total obs stored: {total_obs}", file=sys.stderr)
    # Exit-code contract (matches v0.11.0 `memex backfill obs` semantics):
    #   0 = every memo was ok-or-skipped (clean run, automation can trust it)
    #   1 = zero successes (nothing landed — likely a configuration/auth failure)
    #   2 = partial failure (some ok, some errored — visible to cron/scripts so
    #       data loss doesn't hide behind an "exit 0 with one success" report)
    errors = sum(v for k, v in counts.items() if k not in ("ok", "skipped"))
    if counts.get("ok", 0) == 0:
        print(
            "Exit 1: no successful extractions. Check claude CLI auth / "
            "vault path / topic list. See per-memo results in the log file.",
            file=sys.stderr,
        )
        return 1
    if errors > 0:
        # Break down the failures so the operator knows which class to chase.
        breakdown = ", ".join(
            f"{k}={v}" for k, v in sorted(counts.items())
            if k not in ("ok", "skipped") and v > 0
        )
        print(
            f"Exit 2: partial failure — {errors} memo(s) errored ({breakdown}). "
            f"Re-run on the failed entries from the log file.",
            file=sys.stderr,
        )
        return 2
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--work-list", required=True, help="Path to file with memo paths (one per line)")
    p.add_argument("--workers", type=int, default=5, help="Concurrent claude subprocesses")
    p.add_argument(
        "--log-file",
        default=os.path.expanduser("~/.memex/logs/batch-obs-extraction.jsonl"),
        help="Per-memo result log (JSONL)",
    )
    p.add_argument("--limit", type=int, default=0, help="Process only first N memos (0 = all)")
    args = p.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
