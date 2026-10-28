"""`memex check --harnesses`: Codex threads with no memo of their own (v0.22.0, read-only).

memex captures Claude Code sessions itself; other agents write their own memos
(owner decision 2026-10-28: "let Codex do these without Claude writing things for
Codex"). This check is the gap report for that arrangement: top-level Codex
Desktop threads, started by the owner or by an agent, that no memo stamps with
the thread id (`session_id:` / `session_ids:`). An automation's thread counts
once a person has taken turns in it. It reads the first records of each rollout
(the adapter's discover) and, for automation threads in the window, the whole
rollout to find a person's turns. It writes nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from memex.harness.codex_desktop import CodexDesktopAdapter
from memex.paths import get_memex_path
from memex.routing import resolve_session_project
from memex.session_memos import memo_session_map, normalize_session_id

# thread_source values that are someone's work (seen 2026-10-28: user 289,
# none 175 — older threads before the field existed, agent_created_thread 27,
# realtime_voice 1; automation 186 is excluded).
WORK_SOURCES = {None, "", "none", "user", "agent_created_thread", "realtime_voice"}
# Skip reasons that mean "could not read", not "deliberately not a work thread".
UNREADABLE_REASONS = {"zstd-unavailable", "corrupt", "unreadable", "parse-error", "no-session-meta"}


def _since(value: str) -> str:
    v = value.strip().lower()
    if v.endswith("d") and v[:-1].isdigit():
        return (date.today() - timedelta(days=int(v[:-1]))).isoformat()
    datetime.strptime(v, "%Y-%m-%d")  # raises on a bad date
    return v


def harness_gaps(vault: Path, *, since: str, root: Path | None = None) -> dict:
    adapter = CodexDesktopAdapter()
    scan = adapter.scan(root=root)
    stamped = memo_session_map(vault)
    rows, covered, automation, unjudged = [], 0, 0, 0
    for ref in scan.refs:
        if (ref.started_at or "")[:10] < since:
            continue
        sid = normalize_session_id(ref.session_id) or ref.session_id
        if ref.thread_source not in WORK_SOURCES and ref.thread_source != "automation":
            automation += 1  # subagent and other machine threads: never work of their own
            continue
        if sid in stamped:  # a memo names it: covered, whatever started it
            covered += 1
            continue
        source = ref.thread_source or "none"
        if ref.thread_source == "automation":
            # An automation's thread is work once a person takes turns in it: an
            # 18-turn design discussion in a daily monitor's thread was not
            # counted (2026-10-28; 8 of 30 such threads in 14 days). Only the
            # turns tell, so these few files are read in full (~5 ms each).
            session, why = adapter.load_explained(ref)
            if session is None:
                if why == "unreadable":  # damaged: a person's turn may be what was lost
                    unjudged += 1
                else:
                    automation += 1
                continue
            source = "automation, continued by a person"
        project = resolve_session_project(ref.cwd).project if ref.cwd else "_uncategorized"
        rows.append({
            "harness": "codex-desktop",
            "session_id": ref.session_id,
            "started": (ref.started_at or "")[:16],
            "source": source,
            "project": project,
            "subagents": ref.subagent_count,
        })
    rows.sort(key=lambda r: r["started"], reverse=True)
    # Files the scan could not read are threads it could not judge. A report
    # that left them out read as all-clear (gauntlet round 1, Kimi lane) — on a
    # machine without a zstd decoder that would be ~90% of Codex rollouts.
    # Only files that could fall inside the window count: one ancient corrupt
    # rollout must not mark every 14-day report incomplete (gauntlet round 2).
    # A file whose name carries no start time is counted (cannot rule it out).
    unreadable: dict[str, int] = {}
    for reason, started in scan.unread:
        if reason in UNREADABLE_REASONS and (started is None or started[:10] >= since):
            unreadable[reason] = unreadable.get(reason, 0) + 1
    return {"since": since, "threads_without_memo": rows, "covered": covered,
            "automation_skipped": automation, "unreadable_files": unreadable,
            "unjudged_automation_threads": unjudged,
            "complete": not unreadable and not unjudged}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since", default="14d", help="ISO date or Nd (default 14d)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--root", type=Path, default=None, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    try:
        since = _since(args.since)
    except ValueError:
        print(f"Error: --since takes YYYY-MM-DD or Nd, not {args.since!r}", file=sys.stderr)
        sys.exit(2)
    report = harness_gaps(get_memex_path(), since=since, root=args.root)
    if args.json:
        print(json.dumps(report, indent=2))
        sys.exit(0 if report["complete"] else 3)
    rows = report["threads_without_memo"]
    print(f"Codex threads since {since}: {len(rows)} without a memo, {report['covered']} with one "
          f"({report['automation_skipped']} automation-only threads not counted).")
    for r in rows:
        extra = f", {r['subagents']} subagents" if r["subagents"] else ""
        print(f"  {r['started']}  {r['project']:<24} {r['session_id']}  ({r['source']}{extra})")
    if report["unreadable_files"]:
        reasons = ", ".join(f"{k} {v}" for k, v in sorted(report["unreadable_files"].items()))
        print(f"INCOMPLETE: {sum(report['unreadable_files'].values())} rollout file(s) could not be read ({reasons});"
              " their threads are not judged above." + (
                  " Install zstd (brew install zstd) or use Python 3.14 to read compressed rollouts."
                  if "zstd-unavailable" in report["unreadable_files"] else ""))
    if report["unjudged_automation_threads"]:
        print(f"INCOMPLETE: {report['unjudged_automation_threads']} automation thread(s) could not be read "
              "to the end, so whether a person took turns in them is unknown.")
    if rows:
        print("Each thread writes its own memo (memo-writing skill, `memex session whoami` for the stamp).")
    if not report["complete"]:
        sys.exit(3)


if __name__ == "__main__":
    main()
