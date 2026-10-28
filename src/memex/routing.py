"""Which project a session belongs to, and why (v0.22.0, plan items C2 and I6).

``detect_project`` answers from the cwd alone. That misfiles every session whose
identity is not its folder: a claude.ai relay worker opened in a catch-all cwd
belongs to its claude.ai project (``<project id="chan_…">``), not to
``~/Documents``. ``resolve_session_project`` is the single entry point
for SessionEnd, ``session import`` and the harness importers: identity rules
first (relay channel), then the cwd rules.

The returned ``rule`` says which rule decided, so SessionEnd can leave a filing
receipt and the next SessionStart can tell the user about guesses
(``leaf``/``fallback``) and unmapped channels.

Filing receipts (plan I6) live here too, writer and reader side by side, so the
format has one definition. ``<state_dir>/last-archives.jsonl`` holds one JSON
object per archived session, oldest first::

    {"ts": "2026-10-28T11:48:02", "session_id": "<full uuid>", "project": "notes",
     "rule": "detect", "unmapped_channel": "chan_…" or null,
     "archived": "projects/notes/transcripts/20261028-114802-83b7533e.md",
     "cwd": "/Users/…/Documents/notes"}

``rule`` is one of ``RULES``; ``archived`` is vault-relative. The file is state,
not vault content (never indexed or pushed), and is trimmed to the newest
``RECEIPTS_KEEP`` lines once it passes ``RECEIPTS_TRIM_BYTES``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from filelock import FileLock, Timeout

from memex.scripts.utils import detect_project_explained, get_config, sanitize_project_name

# Rules a route can report. "detect" is the coarse answer until
# detect_project_explained reports the finer cwd rules (plan item B).
RULES = (
    "channel",      # relay_channel found in config channel_mappings
    "pin",          # project_mappings substring
    "scratchpad",   # session scratchpad → owning session's cwd
    "temp-prefix",  # mkdtemp/tmp leaf starting with an existing project name (B1)
    "remembered",   # cwd → project recorded on an earlier archive (B2)
    "git-remote",
    "git-root",
    "leaf",         # cwd folder name: a guess
    "fallback",     # _uncategorized
    "detect",       # detect_project without detail
)
GUESS_RULES = frozenset({"leaf", "fallback"})


@dataclass(frozen=True)
class ProjectRoute:
    project: str
    rule: str
    # The relay channel the session carried when no mapping matched it: one
    # decision per claude.ai project, surfaced instead of silently filed.
    unmapped_channel: str | None = None

    @property
    def is_guess(self) -> bool:
        return self.rule in GUESS_RULES or self.unmapped_channel is not None


def resolve_session_project(
    cwd: str,
    metadata: Mapping[str, object] | None = None,
    *,
    config: Mapping[str, object] | None = None,
) -> ProjectRoute:
    """Route a session to a project folder.

    ``metadata`` is the transcript's parsed metadata; the key read here is
    ``relay_channel`` (``chan_…``). ``config`` defaults to ``get_config()``.
    """
    cfg = get_config() if config is None else config
    channel = (metadata or {}).get("relay_channel")
    unmapped = None
    if isinstance(channel, str) and channel:
        mappings = cfg.get("channel_mappings") or {}
        mapped = mappings.get(channel) if isinstance(mappings, Mapping) else None
        if isinstance(mapped, str) and mapped.strip():
            return ProjectRoute(sanitize_project_name(mapped), "channel")
        unmapped = channel
    if not cwd:
        return ProjectRoute("_uncategorized", "fallback", unmapped)
    project, rule = detect_project_explained(cwd)
    return ProjectRoute(project, rule, unmapped)


# --- filing receipts (I6) ----------------------------------------------------

RECEIPTS_FILE = "last-archives.jsonl"
RECEIPTS_KEEP = 500
RECEIPTS_TRIM_BYTES = 256 * 1024
# SessionEnd's whole budget is 1.5 s: a receipt is never worth waiting for.
_RECEIPT_LOCK_TIMEOUT = 0.5


def filing_receipt(
    route: ProjectRoute,
    *,
    session_id: str,
    archived: str,
    cwd: str,
    ts: str | None = None,
) -> dict:
    """One receipt line (format in the module docstring)."""
    return {
        "ts": ts or datetime.now().isoformat(timespec="seconds"),
        "session_id": session_id,
        "project": route.project,
        "rule": route.rule,
        "unmapped_channel": route.unmapped_channel,
        "archived": archived,
        "cwd": cwd,
    }


def append_filing_receipt(state_dir: Path, receipt: Mapping[str, object]) -> Path | None:
    """Append ``receipt`` to ``<state_dir>/last-archives.jsonl``.

    ``None`` when the lock is busy (a concurrent SessionEnd): the receipt is
    dropped, never waited for. A structural state write (no prose body), so it
    is not routed through ``safe_write_text``.
    """
    path = Path(state_dir) / RECEIPTS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(dict(receipt), ensure_ascii=False) + "\n"
    try:
        with FileLock(str(path) + ".lock", timeout=_RECEIPT_LOCK_TIMEOUT):
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
            if path.stat().st_size > RECEIPTS_TRIM_BYTES:
                _trim_receipts(path)
    except Timeout:
        return None
    return path


def _trim_receipts(path: Path) -> None:
    """Keep the newest receipts: at most ``RECEIPTS_KEEP`` lines and half of
    ``RECEIPTS_TRIM_BYTES``, so a trim buys ~128 KB of appends before the next
    one however long the lines are (caller holds the lock)."""
    kept: list[str] = []
    size = 0
    for line in reversed(path.read_text(encoding="utf-8").splitlines(keepends=True)):
        size += len(line.encode("utf-8"))
        if kept and (len(kept) >= RECEIPTS_KEEP or size > RECEIPTS_TRIM_BYTES // 2):
            break
        kept.append(line)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("".join(reversed(kept)), encoding="utf-8")
    os.replace(tmp, path)


def read_filing_receipts(state_dir: Path, *, limit: int | None = None) -> list[dict]:
    """Receipts oldest first (only the newest ``limit`` when given); malformed lines skipped."""
    path = Path(state_dir) / RECEIPTS_FILE
    if limit is not None and limit <= 0:
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for raw in lines if limit is None else lines[-limit:]:
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out
