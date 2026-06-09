"""Reconcile orphan pending-memo signals against existing Layer-1 memos.

A PreCompact hook writes a signal to ``~/.memex/pending-memos/<session>.json``
as a safety net so a memo can be regenerated post-compaction. But if the
session was already saved by Layer 1 (``/memex:save`` wrote a memo), the signal
is stale — and it persists forever because SessionStart only nudges, never
clears. The 2026-06-09 garden-tending pass found 3 such stale signals whose
Layer-1 memos already existed.

This command detects stale signals — a signal is "covered" when a memo exists
for the same project dated within ``--window`` days of the signal timestamp —
and, with ``--apply``, deletes the covered ones. Uncovered signals are left
alone (they're genuine retries).

Heuristic, deliberately: memos don't store a session id, so same-project +
near-date is the strongest cheap signal. The default dry-run reports the
match so a human can eyeball before ``--apply``.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime
from pathlib import Path

from memex.paths import get_memex_path, get_pending_dir

_DATE_FM_RE = re.compile(r"^date:\s*(\d{4})-(\d{2})-(\d{2})")
_DATE_NAME_RE = re.compile(r"(\d{4})-?(\d{2})-?(\d{2})")


def _date_from_name(name: str) -> date | None:
    if not isinstance(name, str):
        return None
    m = _DATE_NAME_RE.match(name)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _memo_date(path: Path) -> date | None:
    """Memo date from frontmatter ``date:``, falling back to the filename."""
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return _date_from_name(path.name)
    if lines and lines[0].strip() == "---":
        for line in lines[1:50]:
            if line.strip() == "---":
                break
            m = _DATE_FM_RE.match(line.strip())
            if m:
                try:
                    return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                except ValueError:
                    break
    return _date_from_name(path.name)


def _signal_date(ts: str) -> date | None:
    """Parse the signal timestamp (ISO 8601, possibly with microseconds)."""
    if not ts or not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts).date()
    except (ValueError, TypeError):
        return _date_from_name(ts)


def _covering_memo(vault: Path, project: str, sig_date: date | None, window: int) -> str | None:
    """Name of a same-project memo within ``window`` days of the signal, if any."""
    if not project or sig_date is None:
        return None
    memo_dir = vault / "projects" / project / "memos"
    if not memo_dir.is_dir():
        return None
    best: tuple[int, str] | None = None
    for f in sorted(memo_dir.glob("*.md")):
        d = _memo_date(f)
        if d is None:
            continue
        delta = abs((d - sig_date).days)
        if delta <= window and (best is None or delta < best[0]):
            best = (delta, f.name)
    return best[1] if best else None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reconcile orphan pending-memo signals against existing memos.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete signals whose session already has a memo (default: dry-run report).",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=2,
        help="± days a memo's date may differ from the signal to count as covering (default: 2).",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    args = parser.parse_args()

    vault = get_memex_path()
    pending_dir = get_pending_dir()
    signals = sorted(pending_dir.glob("*.json"))

    rows: list[dict] = []
    for sig_file in signals:
        try:
            sig = json.loads(sig_file.read_text())
        except (json.JSONDecodeError, OSError):
            rows.append({"file": sig_file.name, "project": "?", "covered_by": None, "error": "unreadable"})
            continue
        if not isinstance(sig, dict):
            rows.append({"file": sig_file.name, "project": "?", "covered_by": None, "error": "not an object"})
            continue
        project = str(sig.get("project", "") or "")
        sig_date = _signal_date(sig.get("timestamp", ""))
        covered_by = _covering_memo(vault, project, sig_date, args.window)
        rows.append(
            {
                "file": sig_file.name,
                "session": str(sig.get("session_id", "") or "")[:8],
                "project": str(project or ""),
                "timestamp": str(sig.get("timestamp", "") or ""),
                "covered_by": covered_by,
            }
        )
        if covered_by and args.apply:
            try:
                sig_file.unlink()
                rows[-1]["cleared"] = True
            except OSError as e:
                rows[-1]["cleared"] = False
                rows[-1]["error"] = str(e)

    covered = [r for r in rows if r.get("covered_by")]
    uncovered = [r for r in rows if not r.get("covered_by") and "error" not in r]

    if args.json:
        print(json.dumps({"total": len(rows), "covered": covered, "uncovered": uncovered}, indent=2))
        return

    if not signals:
        print("No pending-memo signals — nothing to reconcile.")
        return

    print(f"Pending signals: {len(rows)}  (covered={len(covered)}, uncovered={len(uncovered)})")
    print()
    for r in rows:
        if r.get("covered_by"):
            verb = "CLEARED " if r.get("cleared") else ("STALE   " if args.apply else "stale   ")
            print(f"  {verb} {r['project']:24} {r['timestamp'][:10]}  ← covered by {r['covered_by']}")
        elif "error" in r:
            print(f"  ERROR   {r['file']}: {r['error']}")
        else:
            print(f"  keep    {r['project']:24} {r['timestamp'][:10]}  (no covering memo — genuine retry)")
    print()
    if covered and not args.apply:
        print(f"Re-run with --apply to delete {len(covered)} stale signal(s).")
    elif covered and args.apply:
        cleared = sum(1 for r in covered if r.get("cleared"))
        print(f"Cleared {cleared} stale signal(s); {len(uncovered)} genuine retry(ies) kept.")


if __name__ == "__main__":
    main()
