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
import os
import re
import subprocess
import sys
import unicodedata
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


def _git_add_dates(vault: Path) -> tuple[dict[Path, date] | None, str]:
    """(resolved memo path → date the file currently on disk was added to git, reason).

    "Added" = the NEWEST add of that path (git lists commits newest-first and
    ``setdefault`` keeps the first seen), so a deleted-and-re-added memo dates
    from its re-add.

    The dict is ``None`` when the vault is not a git checkout, the checkout
    is shallow, git is unavailable or times out; the reason names which, and
    the ARR column then prints ``-`` with that reason in the legend (a skill
    preamble discards stderr, so a silent ``-`` would blame the wrong cause). One ``git log`` for
    the whole vault — a per-file call would be ~1,500 subprocesses; 0.04 s for
    3,446 paths on the reference vault. ``--no-renames`` makes a memo
    consolidated in from another folder an *add* at its new path, so it
    "arrives" on the day it was moved, whatever its filename date; the newest
    add wins, so a memo deleted and re-added arrives on its re-add. Committer
    date, not author date: a cherry-pick keeps the old author date. ``-z`` keeps
    quotes, tabs and non-ASCII bytes in paths literal (it disables git's
    quoting); both sides are NFC-normalised (macOS stores NFD). The 2026-09-30
    stamp audit found 8 of 9 "count-mismatch" overviews hid exactly such a
    moved-in memo: NEW compares the memo's own date with ``condensed:``, and a
    moved-in memo is older than the stamp by construction.
    """
    # A caller inside another repo's hook exports GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE;
    # those would point these calls at that repo instead of the vault's.
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")}

    def _why(proc: subprocess.CompletedProcess, default: str) -> str:
        err = (proc.stderr or b"").decode("utf-8", "replace") if isinstance(proc.stderr, bytes) else (proc.stderr or "")
        lines = [ln.strip() for ln in err.strip().splitlines() if ln.strip()]
        # the first `fatal:` line is the reason; later lines are git's remedy advice
        pick = next((ln for ln in lines if ln.startswith("fatal:")), lines[-1] if lines else "")
        return f"{default}: {pick}" if pick else default

    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel", "--is-shallow-repository"],
                             cwd=vault, capture_output=True, text=True, timeout=10, env=env)
        if top.returncode != 0:
            return None, _why(top, "not a git checkout")
        toplevel, _, shallow = top.stdout.strip().partition("\n")
        if not toplevel:
            return None, "not a git checkout"
        if shallow.strip() == "true":
            return None, "shallow clone (no add history)"
        # NFC on the root too: an NFD parent directory (macOS, accented names)
        # would otherwise miss every key and read as "tracks none of the memos".
        root = Path(unicodedata.normalize("NFC", str(Path(toplevel).resolve())))
        log = subprocess.run(
            ["git", "log", "-z", "--diff-filter=A", "--no-renames",
             "--format=%x01%cd", "--date=short", "--name-only", "--", "projects"],
            cwd=vault, capture_output=True, timeout=10, env=env)
        if log.returncode != 0:
            return None, _why(log, "git log failed")
    except subprocess.TimeoutExpired:
        return None, "git timed out (10 s)"
    except FileNotFoundError:
        return None, "git unavailable"
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"git unavailable: {exc.__class__.__name__}"
    first: dict[Path, date] = {}
    current: date | None = None
    for raw in log.stdout.split(b"\x00"):
        token = raw.decode("utf-8", errors="surrogateescape")
        if token.startswith("\n"):
            token = token[1:]  # -z separates a commit header from the previous block with one newline
        if not token:
            continue
        if token.startswith("\x01"):
            current = _parse_iso(token[1:].strip())
            continue
        if current:
            key = Path(unicodedata.normalize("NFC", str(root / token)))
            first.setdefault(key, current)  # newest first: the add that put today's file there
    return first, "git"


def _memo_key(m: Path) -> Path:
    """Lookup key into ``_git_add_dates``: parent resolved (symlinked vault), NFC (git stores NFC).

    Only the directory is resolved — a memo that is itself a symlink should be
    looked up at its own path, which is what git tracks.
    """
    r = m.parent.resolve() / m.name
    return Path(unicodedata.normalize("NFC", str(r)))


def audit_condense(vault: Path) -> dict:
    """Projects whose overview lags their memos (no mutation)."""
    rows: list[dict] = []
    stamp_drift: list[dict] = []
    projects_dir = vault / "projects"
    first_added, arrivals_basis = _git_add_dates(vault)
    if first_added is not None and not any(
            _memo_key(m) in first_added for m in projects_dir.glob("*/memos/*.md")):
        # git tracks none of these memos (projects/ ignored, vault nested in an
        # unrelated repo, path-case mismatch): no arrival evidence, not "all arrived".
        first_added, arrivals_basis = None, "git tracks none of the memos"
    today = date.today()
    for pdir in sorted(p for p in projects_dir.iterdir() if p.is_dir()) if projects_dir.is_dir() else []:
        overview = pdir / "_project.md"
        memos_dir = pdir / "memos"
        memos = sorted(memos_dir.glob("*.md")) if memos_dir.is_dir() else []
        if not memos:
            continue
        # memos/<sub>/ (e.g. memos/archive/) is a curator's deliberate set-aside:
        # not counted against the stamp, but reported so the next pass doesn't
        # re-audit the "drift" (alcor, 2026-09-30: stamp 45 vs 25 on disk = 27 archived).
        archived = sum(1 for _ in memos_dir.rglob("*.md")) - len(memos)
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
        arrived: list[Path] = []
        # Per project, not per vault: a gitignored or nested-repo project folder
        # has no add history of its own, and "absent → arrived today" would flag
        # every memo in it (Kimi round 2). Tracked = git knows this project.
        tracked = first_added is not None and (
            _memo_key(overview) in first_added or any(_memo_key(m) in first_added for m in memos))
        if condensed:
            newer = [m for m, d in dated if d and d > condensed]
            if tracked:
                # Added to git after the stamp although dated before it: moved in by
                # a consolidation, or written with a back-dated name. An untracked
                # memo has no add record and counts as arrived today.
                newer_set = set(newer)
                arrived = [m for m in memos if m not in newer_set
                           and first_added.get(_memo_key(m), today) > condensed]
        else:
            newer = [m for m, _ in dated]
            # A substantial overview with no stamps is maintained-but-unstamped,
            # not never-condensed: stamp it, don't re-condense from scratch.
            basis = "unstamped" if lines > _MAINTAINED_LINES else "never"
        gap = len(memos) - digested if digested is not None else None
        if gap is not None and gap < 0 and not newer and not arrived:
            # stamp exceeds memos/: memos moved out, or counted from elsewhere
            stamp_drift.append({"project": pdir.name, "memos_digested": digested,
                                "memos": len(memos), "archived": archived})
            continue
        if not newer and not arrived and not (gap and gap > 0):
            continue
        newest = max((d for _, d in dated if d), default=None)
        rows.append({
            "project": pdir.name,
            "condensed": condensed.isoformat() if condensed else None,
            "basis": basis,
            "memos": len(memos),
            "newer": len(newer),
            "arrived": len(arrived) if tracked else None,
            "digested_gap": gap,
            "archived": archived,
            "newest": newest.isoformat() if newest else None,
            "overview_lines": lines,
            "newer_memos": [m.name for m in newer],
            "arrived_memos": [m.name for m in arrived],
        })
    rows.sort(key=lambda r: (-r["newer"], -(r["arrived"] or 0), -(r["digested_gap"] or 0), r["project"]))
    return {"projects": rows, "stamp_drift": stamp_drift, "arrivals_basis": arrivals_basis}


def _print_condense(report: dict) -> None:
    rows = report["projects"]
    drift = report.get("stamp_drift", [])
    basis = report.get("arrivals_basis", "git")
    if not rows:
        suffix = "" if basis == "git" else f" (arrivals not checked: {basis})"
        print(f"✓ Every project overview is current with its memos.{suffix}")
        _print_stamp_drift(drift)
        return
    print(f"Projects with undigested memos: {len(rows)}\n")
    print(f"  {'NEW':>4}  {'ARR':>4}  {'GAP':>4}  {'CONDENSED':10}  {'MEMOS':>5}  {'NEWEST':10}  PROJECT")
    for r in rows:
        gap = r["digested_gap"]
        gap_s = str(gap) if gap is not None else "-"
        arr = r.get("arrived")
        arr_s = str(arr) if arr is not None else "-"
        when = r["condensed"] or r["basis"]
        note = {"updated": "  (no condensed: — dated by updated:)",
                "unstamped": "  (maintained overview, unstamped — add condensed:/memos_digested:, "
                             "don't re-condense from scratch)"}.get(r["basis"], "")
        if r.get("archived"):
            note += f"  (+{r['archived']} under memos/*/, not counted)"
        print(f"  {r['newer']:>4}  {arr_s:>4}  {gap_s:>4}  {when:10}  "
              f"{r['memos']:>5}  {r['newest'] or '-':10}  {r['project']}{note}")
        arrived_names = r.get("arrived_memos", [])
        for name in arrived_names[:5]:
            print(f"{'':51}↳ arrived: {name}")
        if len(arrived_names) > 5:
            print(f"{'':51}↳ +{len(arrived_names) - 5} more (--json)")
    arr_note = ("" if basis == "git" else f" — `-` here: {basis}")
    print("\n  NEW = memos dated after `condensed:` (same-day memos count as digested); "
          "ARR = memos added to git after `condensed:` but dated on or before it — moved in by a "
          "consolidation or back-dated; NEW cannot see them (untracked memos count as arrived "
          f"today{arr_note}); "
          "GAP = memos − `memos_digested:` (catches same-day and moved-in memos). "
          "5+ NEW or a never-condensed overview is worth a pass; 1–2 is a quick integration.")
    _print_stamp_drift(drift)


def _print_stamp_drift(drift: list[dict]) -> None:
    if drift:
        items = ", ".join(
            f"{d['project']} ({d['memos_digested']}>{d['memos']}"
            + (f", +{d['archived']} under memos/*/" if d.get("archived") else "") + ")"
            for d in drift)
        print(f"\n  Stamp drift — memos_digested exceeds memos/ (moved out, counted from "
              f"elsewhere, or set aside under memos/*/; re-stamp if stale): {items}")


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
