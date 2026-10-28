#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyyaml>=6.0",
# ]
# ///
"""
Claude Memory Plugin - UserPromptSubmit Hook (v0.22.0)

Nudges Claude to save a memo when substantial work has accumulated, judged by
ACTIVITY — the transcript's size on disk — or by the older prompt tiers,
whichever is reached first. A session that runs 60 agents on 9 prompts is
substantial; prompt counts alone never noticed (plan I4).

No nudge when a memo stamped with this session's id exists — unless the
session has grown well past that memo, in which case it asks to extend it.
The vault is scanned (``memo_for_session``, ~0.5 s) only when a nudge is
about to fire; every other prompt costs a stat and a small state write.
Fleet seats (sdk-cli) get silence.

Input (stdin):
{
    "session_id": "abc123",
    "transcript_path": "/path/to/transcript.jsonl",
    "prompt": "user's message",
    "cwd": "/path/to/project"
}

Output (stdout): ``hookSpecificOutput.additionalContext`` when a nudge fires.
"""

import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from memex.paths import get_memex_path, get_state_dir
from memex.scripts.reconcile_orphans import (
    HELPER_ENTRYPOINTS,
    _frontmatter,
    is_helper_session,
    transcript_facts,
)
from memex.session_memos import memo_for_session, memo_stamps, normalize_session_id


def default_state():
    return {"count": 0, "chars": 0, "memo_saved": False, "tier_nudged": 0}


# Prompt tiers (1M context window). Chars are an AND gate so low-substance
# sessions don't trip them.
TIERS = [
    # (min_chars, min_messages, message)
    (4_000, 40,
     "[memex] Substantial session. "
     "Consider `/memex:save` when you reach a natural pause."),
    (8_000, 80,
     "[memex] Long session — worth preserving. "
     "`/memex:save` before context compaction."),
]

# Activity tiers: transcript bytes that reach the same tier as TIERS[i].
# Calibrated 2026-10-28 from file sizes only (4,281 non-temp transcripts in
# ~/.claude/projects): p90 = 3.1 MB, p95 = 8.1 MB; sessions whose memo filename
# carries their id had a median of 10.9 MB and 96% were >= 2 MB. 3 MB nudges
# the top ~10% of sessions; 8 MB comes before a typical first 1M-token
# compaction and before the 8.6 MB / 9-prompt session that was never nudged.
BYTE_TIERS = [3_000_000, 8_000_000]

# Re-nudge an existing memo once the session has at least doubled since it
# was saved AND grown by a first-tier amount.
RENUDGE_MIN_GROWTH = BYTE_TIERS[0]
HISTORY_CAP = 512


def _size(path: str) -> int:
    try:
        return os.stat(path).st_size if path else 0
    except OSError:
        return 0


def current_tier(state: dict, active_bytes: int) -> int:
    tier = 0
    for i, (min_chars, min_messages, _) in enumerate(TIERS, 1):
        if state.get("chars", 0) >= min_chars and state.get("count", 0) >= min_messages:
            tier = i
    for i, threshold in enumerate(BYTE_TIERS, 1):
        if active_bytes >= threshold:
            tier = max(tier, i)
    return tier


def bytes_at(history: list, when: float, current: int) -> int:
    """Transcript size at ``when``, rounded UP to the first prompt recorded at
    or after it — an upper bound, so growth since a memo is never overstated."""
    for entry in history:
        if isinstance(entry, list) and len(entry) == 2 and entry[0] >= when:
            return int(entry[1])
    return current


def _saved_at(mtime: float) -> str:
    when = datetime.fromtimestamp(mtime)
    return when.strftime("%H:%M") if when.date() == date.today() else when.strftime("%Y-%m-%d %H:%M")


def renudge(state: dict, memo: Path, active: int) -> str | None:
    """Ask to extend the memo when the session has grown well past it (once per
    memo version: extending the memo moves its mtime and resets the baseline)."""
    try:
        mtime = memo.stat().st_mtime
    except OSError:
        return None
    if state.get("memo_mtime") != mtime:
        state["memo_mtime"] = mtime
        state["memo_bytes"] = bytes_at(state.get("history", []), mtime, active)
        state.pop("renudged", None)
    base = int(state.get("memo_bytes", active))
    if state.get("renudged") or active < 2 * base or active - base < RENUDGE_MIN_GROWTH:
        return None
    state["renudged"] = True
    grown = f"{active / base:.0f}×" if base > 0 else "a lot"
    return (
        f"[memex] This session's memo was saved {_saved_at(mtime)} (`{memo}`); the session has "
        f"grown {grown} since — extend it? Add a dated section, then re-send the COMPLETE "
        f"observation set in one `memex backfill obs --stdin --replace` call."
    )


def find_memo(session_id: str) -> Path | None:
    try:
        vault = get_memex_path()
    except ValueError:
        return None
    try:
        return memo_for_session(vault, session_id)
    except OSError:
        return None


def recorded_memo(session_id: str) -> str | None:
    """The memo path PreCompact recorded in ``session-state/<sid16>.memo``."""
    try:
        data = json.loads((get_state_dir() / "session-state" / f"{session_id[:16]}.memo").read_text())
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and data.get("session_id") == session_id and isinstance(data.get("memo_path"), str):
        return data["memo_path"]
    return None


def known_memo(state: dict, session_id: str) -> Path | None:
    raw = state.get("memo_path") or recorded_memo(session_id)
    if not raw:
        return None
    memo = Path(str(raw))
    state["memo_path"] = str(memo)
    if memo.is_file():
        return memo
    forget_memo(state)  # moved or deleted: look again when a nudge is due
    return None


def forget_memo(state: dict) -> None:
    for key in ("memo_path", "memo_mtime", "memo_bytes", "renudged"):
        state.pop(key, None)


def decide(state: dict, session_id: str, transcript_path: str, prompt: str,
           *, now: float | None = None) -> str | None:
    """Update ``state`` for one prompt; the nudge to show, if any."""
    now = time.time() if now is None else now
    state["count"] = state.get("count", 0) + 1
    state["chars"] = state.get("chars", 0) + len(prompt)

    if "facts" not in state and transcript_path:
        facts = transcript_facts(transcript_path)
        if facts["settled"]:  # a conversation record exists: the answer is final
            state["facts"] = {
                "entrypoint": facts["entrypoint"],
                # A fork starts with its parent's records copied in: what is on
                # disk at its first prompt is the parent's activity (an upper
                # bound, so the fork is never nudged early for it).
                "fork_base": _size(transcript_path) if facts["forked_from"] else 0,
            }
    facts = state.get("facts") if isinstance(state.get("facts"), dict) else {}
    if is_helper_session(facts):
        return None

    active = max(0, _size(transcript_path) - int(facts.get("fork_base") or 0))
    if not isinstance(state.get("history"), list):
        state["history"] = []
    history = state["history"]
    history.append([now, active])
    del history[:-HISTORY_CAP]

    memo = known_memo(state, session_id)
    tier = current_tier(state, active)
    message = None
    if memo is None and tier > state.get("tier_nudged", 0):
        state["tier_nudged"] = tier
        memo = find_memo(session_id)
        if memo is not None:
            state["memo_path"] = str(memo)
        elif not state.get("memo_saved"):  # `memex mark-saved` also counts
            message = TIERS[tier - 1][2]
    if memo is not None:
        message = renudge(state, memo, active)
        if message and normalize_session_id(session_id) not in memo_stamps(_frontmatter(memo)):
            forget_memo(state)  # re-stamped or rewritten since: not this session's memo any more
            message = None
    return message


def main():
    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)
    if not isinstance(input_data, dict):
        sys.exit(0)

    session_id = input_data.get("session_id", "")
    if not session_id:
        sys.exit(0)
    if os.environ.get("CLAUDE_CODE_ENTRYPOINT", "").strip() in HELPER_ENTRYPOINTS:
        sys.exit(0)  # helpers get silence

    state_dir = get_state_dir() / "session-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / f"{session_id[:16]}.json"

    try:
        state = json.loads(state_file.read_text()) if state_file.exists() else default_state()
        if not isinstance(state, dict):
            state = default_state()
    except (json.JSONDecodeError, OSError, ValueError):
        state = default_state()

    try:
        message = decide(state, session_id, str(input_data.get("transcript_path", "") or ""),
                         str(input_data.get("prompt", "") or ""))
    except (TypeError, ValueError, AttributeError):  # a hand-mangled state file self-heals
        state, message = default_state(), None
    if message:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit", "additionalContext": message}}))

    # Atomic write: temp + os.replace so a killed hook can't leave a truncated
    # state file. Non-critical (reader self-heals via default_state on JSON
    # error), but cheap to do right and avoids spurious nudge resets.
    try:
        tmp_file = state_file.parent / (state_file.name + ".tmp")
        tmp_file.write_text(json.dumps(state))
        os.replace(tmp_file, state_file)
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
