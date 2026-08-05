#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "filelock>=3.0",
# ]
# ///
"""
Claude Memory Plugin - PostToolUse Hook (scrub gate)

Deterministic secret-scrub gate that fires after Write/Edit operations on
memo files. The triggering incident (2026-05-25, kimi-plugin-cc) was a
subagent that ignored "don't transcribe secrets" instructions and surfaced
three API keys into a transcript. The lesson: instruction-based controls
fail in exactly the cases they're meant to catch. This hook is the
deterministic layer that doesn't depend on subagent compliance.

Matches and scrubs writes targeting:
  - `projects/<name>/memos/*.md`        (memos — committed to repo)
  - `projects/<name>/auto-memory/*.md`  (auto-memory — synced from Claude)

Other writes pass through untouched. Same scrubber as `memex scrub` (curated
high-precision regex, idempotent, atomic-write). Errors are logged but never
block the write — a broken scrub must not break the user's workflow.

Input (stdin):
{
    "session_id": "abc123",
    "tool_name": "Write" | "Edit" | "MultiEdit",
    "tool_input": {
        "file_path": "/abs/path/to/memo.md",
        ...
    },
    "tool_response": { ... },
    "hook_event_name": "PostToolUse",
    ...
}

Actions:
1. Skip non-Write/Edit tools — exit 0
2. Skip writes outside `projects/*/memos/` and `projects/*/auto-memory/` — exit 0
3. Scrub the file in place (idempotent — no-op when nothing matches)
4. Log + exit 0
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from memex.scripts.utils import read_hook_input, log_info, log_warning  # noqa: E402
from memex.scrub import scrub_file  # noqa: E402


# Only act on Write-class tools. MultiEdit fires once per file group; the
# final on-disk state contains the merged result, so a single scrub is enough.
WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit"})

# Vault paths that warrant scrubbing. Pattern is "matches `/projects/<name>/<bucket>/`
# anywhere in the absolute path", which works regardless of where the vault
# lives on the user's filesystem.
GATED_PATH_RE = re.compile(r"/projects/[^/]+/(?:memos|auto-memory)/")


def main() -> None:
    input_data = read_hook_input()

    tool_name = input_data.get("tool_name", "")
    if tool_name not in WRITE_TOOLS:
        # Read, Bash, Grep, etc. — not our concern.
        sys.exit(0)

    tool_input = input_data.get("tool_input", {}) or {}
    file_path_str = tool_input.get("file_path", "")
    if not file_path_str:
        sys.exit(0)

    # Normalize and gate. Use forward-slash matching (POSIX paths in vault);
    # Windows users would need a different pattern but the vault is POSIX-only.
    if not GATED_PATH_RE.search(file_path_str):
        sys.exit(0)

    file_path = Path(file_path_str)
    if not file_path.exists() or not file_path.is_file():
        # Write may have failed or the file vanished — nothing to do.
        sys.exit(0)

    # Scrub in place. Idempotent — re-running on already-scrubbed content
    # is a no-op (matches list is empty, no rewrite). Skips non-UTF8 files
    # silently.
    try:
        result = scrub_file(file_path, apply=True)
    except OSError as exc:
        # Permission denied, disk full, etc. Log and exit clean — we will
        # never block the user's write on a scrub failure, but the warning
        # gives them a paper trail to investigate.
        log_warning(f"Scrub failed on {file_path}: {type(exc).__name__}: {exc}")
        sys.exit(0)

    if result.applied:
        providers = sorted({m.provider for m in result.matches})
        log_info(
            f"Scrubbed {len(result.matches)} secret(s) "
            f"({', '.join(providers)}) from {file_path.name}"
        )
    # else: clean file, no log needed

    sys.exit(0)


if __name__ == "__main__":
    main()
