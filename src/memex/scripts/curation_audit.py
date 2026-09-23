"""Curation backlog audit — the two scans every tending pass re-derived by hand.

``memex check --signals``
    Open ``## Recent signals`` bullets per topic. Closed-section-aware: a
    ``## Recent signals (closed — migrated …)`` header, a section opening with a
    ``> **Closed …`` blockquote, and any topic with ``status: archived`` or
    ``redirect_to:`` are audit trail, not backlog. The naive grep counts all of
    them — 2026-08-05, 08-25, 09-08 and 09-23 each re-learned that (on 09-23 the
    naive count was 218 bullets / 83 topics vs 216 / 81 open, and it could not
    tell which signals were new since the last fold).

``memex check --condense``
    Projects whose ``memos/`` hold memos dated after the overview's
    ``condensed:`` date, or more memos than ``memos_digested:`` records. Memo
    dates come from the filename via ``extract_date_from_filename`` (both
    ``YYYY-MM-DD-slug`` and legacy ``YYYYMMDD-HHMM-slug``), falling back to the
    frontmatter ``date:``. A raw string compare sorts every legacy
    ``2026MMDD-…`` name after ``2026-…`` — on 09-23 that inflated the backlog
    from ~60 to ~350 "new" memos.

Both are read-only reports (exit 0). ``--json`` for agents.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

from memex.paths import get_memex_path
from memex.scripts.temporal_scan import extract_date_from_filename

_SIGNALS_HEADER = re.compile(r"^## Recent signals\b(.*)$")
_SECTION_END = re.compile(r"^#{1,2} ")
# Closure is a convention, not a keyword hunt: the header's parenthetical STARTS
# with it — `## Recent signals (closed — migrated …)` — or the section opens with
# a `> **Closed YYYY-MM-DD.**` blockquote. "> Not yet absorbed" is not closed.
_CLOSED_HEADER = re.compile(r"^\s*\(\s*(?:closed|migrated|absorbed)\b", re.I)
_CLOSED_QUOTE = re.compile(r"^>\s*\**\s*closed\b", re.I)
_INLINE_COMMENT = re.compile(r"\s+#")
_QUOTED = re.compile(r"""^(["'])(.*?)\1(?:\s+#.*)?\s*$""")
_YAML_NULLS = {"null", "~"}
_BULLET = re.compile(r"^- ")
_BULLET_DATE = re.compile(r"^- (\d{4}-\d{2}-\d{2})\b")
_ISO_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_MAINTAINED_LINES = 40  # same threshold garden-tending uses for "MAINTAINED"


# ── shared helpers ────────────────────────────────────────────────────


def _split_frontmatter(text: str) -> tuple[dict[str, str], list[str]]:
    """Return (flat key→value frontmatter, body lines). No YAML dependency:
    only top-level scalar keys are needed here (status, redirect_to, type,
    condensed, memos_digested, date, updated)."""
    lines = text.split("\n")
    if not lines or lines[0].rstrip() != "---":
        return {}, lines
    for i in range(1, len(lines)):
        if lines[i].rstrip() == "---":  # column 0 only: indented `---` is scalar content
            fm: dict[str, str] = {}
            for line in lines[1:i]:
                if ":" not in line or line[:1].isspace() or line.lstrip().startswith(("#", "-")):
                    continue
                key, _, value = line.partition(":")
                value = value.strip()
                quoted = _QUOTED.match(value)
                if quoted:
                    value = quoted.group(2)  # `status: "archived"  # old` → archived
                elif value.startswith("#"):
                    value = ""  # `redirect_to: # none` — comment only, no value
                else:
                    value = _INLINE_COMMENT.split(value, 1)[0].strip()  # `memos_digested: 12  # note`
                fm[key.strip()] = "" if value.lower() in _YAML_NULLS else value
            return fm, lines[i + 1:]
    return {}, lines


def _parse_iso(value: str | None) -> date | None:
    if not value:
        return None
    m = _ISO_DATE.search(value)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _is_retired(fm: dict[str, str]) -> bool:
    return fm.get("status", "").lower() == "archived" or bool(fm.get("redirect_to"))


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


# ── --signals ─────────────────────────────────────────────────────────


def _signal_sections(body: list[str]) -> list[tuple[bool, list[str]]]:
    """Every ``## Recent signals`` section as (is_closed, bullet_lines)."""
    sections: list[tuple[bool, list[str]]] = []
    i = 0
    while i < len(body):
        m = _SIGNALS_HEADER.match(body[i])
        if not m:
            i += 1
            continue
        closed = bool(_CLOSED_HEADER.match(m.group(1)))
        bullets: list[str] = []
        first_content: str | None = None
        i += 1
        while i < len(body) and not _SECTION_END.match(body[i]):
            line = body[i]
            if first_content is None and line.strip():
                first_content = line.strip()
            if _BULLET.match(line):
                bullets.append(line)
            i += 1
        if first_content and _CLOSED_QUOTE.match(first_content):
            closed = True
        sections.append((closed, bullets))
    return sections


def audit_signals(vault: Path) -> dict:
    """Open Recent-signals backlog per topic (no mutation)."""
    topics: list[dict] = []
    retired_with_signals = 0
    closed_sections = 0
    topics_dir = vault / "topics"
    for path in sorted(topics_dir.glob("*.md")) if topics_dir.is_dir() else []:
        text = _read(path)
        if text is None or "## Recent signals" not in text:
            continue
        fm, body = _split_frontmatter(text)
        sections = _signal_sections(body)
        if not sections:
            continue
        if _is_retired(fm):
            if any(b for _, b in sections):
                retired_with_signals += 1
            continue
        open_bullets: list[str] = []
        for closed, bullets in sections:
            if closed:
                closed_sections += 1
            else:
                open_bullets.extend(bullets)
        if not open_bullets:
            continue
        dates = sorted(
            d for d in (_parse_iso(m.group(1)) for m in
                        (_BULLET_DATE.match(b) for b in open_bullets) if m) if d
        )
        updated = _parse_iso(fm.get("updated"))
        topics.append({
            "slug": path.stem,
            "type": fm.get("type", ""),
            "open": len(open_bullets),
            "since_updated": sum(1 for d in dates if updated and d > updated) if updated else None,
            "oldest": dates[0].isoformat() if dates else None,
            "newest": dates[-1].isoformat() if dates else None,
            "updated": updated.isoformat() if updated else None,
            "lines": text.count("\n"),
        })
    topics.sort(key=lambda t: (-t["open"], t["slug"]))
    return {
        "total_open": sum(t["open"] for t in topics),
        "topics": topics,
        "closed_sections": closed_sections,
        "retired_topics_skipped": retired_with_signals,
    }


def _print_signals(report: dict) -> None:
    topics = report["topics"]
    if not topics:
        print("✓ No open Recent-signals across topics/.")
        return
    print(f"Open topic signals: {report['total_open']} across {len(topics)} topic(s)  "
          f"(skipped: {report['closed_sections']} closed section(s), "
          f"{report['retired_topics_skipped']} archived/redirect topic(s))\n")
    print(f"  {'OPEN':>4}  {'OLDEST':10}  {'UPDATED':10}  TOPIC")
    for t in topics:
        trail = "  (trail — extend, don't rewrite)" if t["type"] == "trail" else ""
        print(f"  {t['open']:>4}  {t['oldest'] or '-':10}  {t['updated'] or '-':10}  "
              f"{t['slug']}{trail}")
    heavy = sum(1 for t in topics if t["open"] >= 3)
    print(f"\n  {heavy} topic(s) at 3+ signals — fold these first; singletons can ride "
          "along with a related fold. Read each memo before folding: counts include "
          "duplicates and already-integrated pointers.")


# ── --condense ────────────────────────────────────────────────────────


def _memo_date(path: Path) -> date | None:
    d = extract_date_from_filename(path.name)
    if d:
        return d
    text = _read(path)
    if text is None:
        return None
    fm, _ = _split_frontmatter(text)
    return _parse_iso(fm.get("date"))


def audit_condense(vault: Path) -> dict:
    """Projects whose overview lags their memos (no mutation)."""
    rows: list[dict] = []
    stamp_drift: list[dict] = []
    projects_dir = vault / "projects"
    for pdir in sorted(p for p in projects_dir.iterdir() if p.is_dir()) if projects_dir.is_dir() else []:
        overview = pdir / "_project.md"
        memos_dir = pdir / "memos"
        memos = sorted(memos_dir.glob("*.md")) if memos_dir.is_dir() else []
        if not memos:
            continue
        text = _read(overview) if overview.exists() else ""
        fm, _ = _split_frontmatter(text or "")
        if _is_retired(fm):
            continue
        condensed = _parse_iso(fm.get("condensed"))
        basis = "condensed"
        if condensed is None and fm.get("updated"):
            condensed, basis = _parse_iso(fm.get("updated")), "updated"
        digested_raw = fm.get("memos_digested", "")
        digested = int(digested_raw) if digested_raw.isdigit() else None
        lines = (text or "").count("\n")
        dated = [(m, _memo_date(m)) for m in memos]
        if condensed:
            newer = [m for m, d in dated if d and d > condensed]
        else:
            newer = [m for m, _ in dated]
            # A substantial overview with no stamps is maintained-but-unstamped,
            # not never-condensed: stamp it, don't re-condense from scratch.
            basis = "unstamped" if lines > _MAINTAINED_LINES else "never"
        gap = len(memos) - digested if digested is not None else None
        if gap is not None and gap < 0 and not newer:
            # stamp exceeds memos/: memos moved out, or counted from elsewhere
            stamp_drift.append({"project": pdir.name, "memos_digested": digested, "memos": len(memos)})
            continue
        if not newer and not (gap and gap > 0):
            continue
        newest = max((d for _, d in dated if d), default=None)
        rows.append({
            "project": pdir.name,
            "condensed": condensed.isoformat() if condensed else None,
            "basis": basis,
            "memos": len(memos),
            "newer": len(newer),
            "digested_gap": gap,
            "newest": newest.isoformat() if newest else None,
            "overview_lines": lines,
            "newer_memos": [m.name for m in newer],
        })
    rows.sort(key=lambda r: (-r["newer"], -(r["digested_gap"] or 0), r["project"]))
    return {"projects": rows, "stamp_drift": stamp_drift}


def _print_condense(report: dict) -> None:
    rows = report["projects"]
    drift = report.get("stamp_drift", [])
    if not rows:
        print("✓ Every project overview is current with its memos.")
        _print_stamp_drift(drift)
        return
    print(f"Projects with undigested memos: {len(rows)}\n")
    print(f"  {'NEW':>4}  {'GAP':>4}  {'CONDENSED':10}  {'MEMOS':>5}  {'NEWEST':10}  PROJECT")
    for r in rows:
        gap = r["digested_gap"]
        gap_s = str(gap) if gap is not None else "-"
        when = r["condensed"] or r["basis"]
        note = {"updated": "  (no condensed: — dated by updated:)",
                "unstamped": "  (maintained overview, unstamped — add condensed:/memos_digested:, "
                             "don't re-condense from scratch)"}.get(r["basis"], "")
        print(f"  {r['newer']:>4}  {gap_s:>4}  {when:10}  "
              f"{r['memos']:>5}  {r['newest'] or '-':10}  {r['project']}{note}")
    print("\n  NEW = memos dated after `condensed:` (same-day memos count as digested); "
          "GAP = memos − `memos_digested:` (catches same-day and moved-in memos). "
          "5+ NEW or a never-condensed overview is worth a pass; 1–2 is a quick integration.")
    _print_stamp_drift(drift)


def _print_stamp_drift(drift: list[dict]) -> None:
    if drift:
        items = ", ".join(f"{d['project']} ({d['memos_digested']}>{d['memos']})" for d in drift)
        print(f"\n  Stamp drift — memos_digested exceeds memos/ (moved out, or counted from "
              f"elsewhere; re-stamp if stale): {items}")


# ── entry points ──────────────────────────────────────────────────────


def run_curation_audit(kind: str, json_out: bool = False) -> int:
    vault = get_memex_path()
    if kind == "signals":
        report = audit_signals(vault)
        printer = _print_signals
    elif kind == "condense":
        report = audit_condense(vault)
        printer = _print_condense
    else:
        raise ValueError(f"unknown curation audit: {kind!r}")
    if json_out:
        print(json.dumps(report, indent=2))
    else:
        printer(report)
    return 0


def main():
    parser = argparse.ArgumentParser(description="Curation backlog audit (topic signals / condensation)")
    parser.add_argument("kind", choices=["signals", "condense"])
    parser.add_argument("--json", action="store_true", help="JSON output for agents")
    args = parser.parse_args()
    sys.exit(run_curation_audit(args.kind, json_out=args.json))


if __name__ == "__main__":
    main()
