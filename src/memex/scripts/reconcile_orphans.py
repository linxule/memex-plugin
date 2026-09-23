"""Reconcile orphan pending-memo signals against existing Layer-1 memos.

A PreCompact hook writes a signal to ``~/.memex/pending-memos/<session>.json``
as a safety net so a memo can be regenerated post-compaction. But if the
session was already saved by Layer 1 (``/memex:save`` wrote a memo), the signal
is stale — and it persists forever because SessionStart only nudges, never
clears. The 2026-06-09 garden-tending pass found 3 such stale signals whose
Layer-1 memos already existed.

This command detects stale signals and, with ``--apply``, deletes the covered
ones. Uncovered signals are left alone (they're genuine retries).

**Exact (cleared by ``--apply``):** a memo whose YAML frontmatter has a
top-level ``session_id:`` equal to the signal's session — stamped by
``/memex:save``, the memo-writing skill and the post-compaction recovery
prompt since v0.20.0. Positive evidence: a parsing quirk would have to CREATE
this session's full UUID.

**Likely (reported; cleared only with ``--apply --trust-window``):**
- *transcript* — the session's JSONL or a subagent's (``<session>/subagents/
  *.jsonl``) holds a successful in-vault ``Write`` of a memo in the signal's own
  project, dated within ``--window`` days, not stamped by another session
  (followed through ``redirect_to:`` stubs if the folder was consolidated).
  Supporting only: it also needs NEGATIVE evidence (no foreign stamp), and
  eleven review rounds kept finding frontmatter where a line reader and YAML
  disagree — each hid a foreign stamp and would have deleted a genuine retry.
- *window* — any same-project memo dated within ``--window`` days.

Bash commands are never evidence (quoting, heredocs, ``||``, ``bash -n -c``).
With several sessions per project per day, the nearest memo is often another
session's, and deleting a genuine retry signal loses that session's memo for
good (a duplicate memo is recoverable; a lost signal is not).
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime
from pathlib import Path

import yaml

from memex.paths import get_memex_path, get_pending_dir

_DATE_NAME_RE = re.compile(r"(\d{4})-?(\d{2})-?(\d{2})")
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_ISO_DAY_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_SLUG_RE = re.compile(r"[A-Za-z0-9_.-]+")
_MAX_REDIRECT_HOPS = 4


_UNTRUSTED = object()  # frontmatter present but unusable: never evidence


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate mapping keys (PyYAML keeps the last one
    silently, so `session_id: <other>` then `session_id: <own>` would read as own)."""


def _construct_unique_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    seen: set = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(None, None, f"duplicate key {key!r}", key_node.start_mark)
        seen.add(key)
    return loader.construct_mapping(node, deep)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)
_MAX_FRONTMATTER_BYTES = 64 * 1024
_EXOTIC_BREAKS = ("\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")


def _frontmatter(path: Path):
    """The memo's frontmatter as YAML defines it.

    Returns a dict, ``None`` when the file has no frontmatter, or
    ``_UNTRUSTED`` when the file can't be read, the block never closes, is
    larger than 64 KB, doesn't parse (including duplicate keys and any
    exception raised while constructing values), or isn't a mapping. Every
    failure is untrusted, never "no frontmatter": an unreadable memo must not
    be able to hide a foreign stamp. Parsed with a strict ``SafeLoader``
    because every line-based reading lost to some YAML construct in review (a
    stamp past line 50, an indented ``---`` inside a block scalar, a quoted
    multi-line description containing a ``session_id:`` line,
    ``session_id: >-`` with the value on the next line, a quoted key).
    Delimiters are ``---`` at column 0, YAML's document marker. ~1.4% of this
    vault's memos don't parse (old hand-written frontmatter); they fall
    through to the report-only tier."""
    try:
        # newline="": no universal-newline translation, or a lone "\r" would
        # silently become a line break before the checks below can see it
        with open(path, encoding="utf-8", errors="ignore", newline="") as fh:
            # bounded: frontmatter over _MAX_FRONTMATTER_BYTES is untrusted anyway,
            # so a block that doesn't close within this read is unclosed
            text = fh.read(2 * _MAX_FRONTMATTER_BYTES)
    except OSError:
        return _UNTRUSTED
    # Split on "\n" only: str.splitlines() also breaks on \v, \f, \x1c-\x1e,
    # \x85, \u2028, \u2029 and could manufacture a closing `---` that YAML
    # never sees. A UTF-8 BOM is not content.
    lines = [ln.removesuffix("\r") for ln in text.removeprefix("\ufeff").split("\n")]
    if not lines or lines[0] != "---":
        return None
    end = next((i for i in range(1, len(lines)) if lines[i] == "---"), None)
    if end is None:
        return _UNTRUSTED
    if any(ch in "\n".join(lines[: end + 1]) for ch in _EXOTIC_BREAKS):
        return _UNTRUSTED  # separators other than \n: this reader and YAML could disagree
    block = "\n".join(lines[1:end])
    if len(block.encode("utf-8")) > _MAX_FRONTMATTER_BYTES:
        return _UNTRUSTED
    try:
        data = yaml.load(block, Loader=_StrictLoader)  # noqa: S506 — SafeLoader subclass
    except Exception:  # YAMLError, RecursionError, ValueError from bad timestamps, …
        return _UNTRUSTED
    if data is None:
        return {}
    return data if isinstance(data, dict) else _UNTRUSTED


def _stamp(fm: dict):
    """``session_id`` as a lowercase full UUID, ``None`` if absent, ``_UNTRUSTED``
    if present but not a single full UUID string."""
    if "session_id" not in fm:
        return None
    v = fm["session_id"]
    if isinstance(v, str) and _UUID_RE.fullmatch(v.strip()):
        return v.strip().lower()
    return _UNTRUSTED


def _fm_date(fm: dict):
    """``date`` as a ``date``, ``None`` if absent, ``_UNTRUSTED`` if unparseable."""
    if "date" not in fm:
        return None
    v = fm["date"]
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        m = _ISO_DAY_RE.match(v.strip())
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return _UNTRUSTED
    return _UNTRUSTED


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
    fm = _frontmatter(path)
    d = _fm_date(fm) if isinstance(fm, dict) else None
    return d if isinstance(d, date) else _date_from_name(path.name)


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


def _memo_session_map(vault: Path) -> dict[str, str]:
    """session_id → vault-relative memo path, from each memo's parsed frontmatter
    (a top-level ``session_id`` key holding one complete UUID)."""
    out: dict[str, str] = {}
    for f in sorted(vault.glob("projects/*/memos/*.md")):
        fm = _frontmatter(f)
        if not isinstance(fm, dict):
            continue
        sid = _stamp(fm)
        if isinstance(sid, str):
            out.setdefault(sid, str(f.relative_to(vault)))
    return out


def _memos_written_by(transcript: Path, vault: Path) -> list[tuple[str, str]]:
    """(project, basename) of memos the session wrote with the ``Write`` tool.

    Structured evidence only: a ``Write`` whose ``file_path`` resolves inside the
    vault at ``projects/<p>/memos/<f>.md``, counted once a matching
    ``tool_result`` came back without ``is_error`` (a denied write saved
    nothing). Bash is deliberately NOT evidence — three review rounds showed
    that inferring "this command saved a memo" from shell text (quoting,
    heredocs, ``||``, ``bash -n -c``, re-extracting an old memo's observations)
    keeps producing false clears, and a false clear loses a session's memo for
    good. A memo drafted elsewhere and copied in falls through to the
    report-only date window; the ``session_id:`` stamp makes it exact. Project
    is taken from the path AS WRITTEN (the memo may have moved since)."""
    pending: dict[str, list[tuple[str, str]]] = {}
    ok: set[str] = set()
    try:
        vault_real = vault.resolve()
    except OSError:
        vault_real = vault
    try:
        fh = transcript.open(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    with fh:
        for line in fh:
            if "tool_result" in line:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    obj = None
                for item in _content_items(obj):
                    use_id = str(item.get("tool_use_id", "") or "")
                    if item.get("type") == "tool_result" and use_id and not _is_error(item.get("is_error")):
                        ok.add(use_id)
            # POSIX paths only (Claude Code on macOS/Linux); a Windows transcript
            # misses here, which is the safe direction (signal kept)
            if "/memos/" not in line or '"Write"' not in line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            for item in _content_items(obj):
                if item.get("type") != "tool_use":
                    continue
                inp = item.get("input")
                if not isinstance(inp, dict):
                    continue
                if item.get("name") != "Write":
                    continue
                use_id = str(item.get("id", "") or "")
                hit = _vault_memo_path(str(inp.get("file_path", "")), vault_real)
                if hit and use_id:
                    pending.setdefault(use_id, []).append(hit)
    return [hit for use_id, hits in pending.items() if use_id in ok for hit in hits]


def _is_error(flag: object) -> bool:
    """``is_error`` as recorded: normally a bool or absent; be strict about variants."""
    return flag is True or (isinstance(flag, str) and flag.strip().lower() == "true") or flag == 1


def _content_items(obj: object) -> list[dict]:
    msg = obj.get("message") if isinstance(obj, dict) else None
    content = msg.get("content") if isinstance(msg, dict) else None
    return [c for c in content if isinstance(c, dict)] if isinstance(content, list) else []


def _vault_memo_path(file_path: str, vault_real: Path) -> tuple[str, str] | None:
    """(project, basename) if ``file_path`` is a memo inside the vault."""
    if not file_path:
        return None
    try:
        rel = Path(file_path).resolve().relative_to(vault_real)
    except (OSError, ValueError):
        return None
    parts = rel.parts
    if len(parts) == 4 and parts[0] == "projects" and parts[2] == "memos" and parts[3].endswith(".md"):
        return parts[1], parts[3]
    return None


def _dated_near(memo: Path, sig_date: date | None, window: int) -> bool:
    """Every date the memo carries (filename AND frontmatter ``date``) is within
    ``window`` days of the signal — a mismatched pair can't smuggle an old memo
    in; unusable frontmatter is never near."""
    if sig_date is None:
        return False
    fm = _frontmatter(memo)
    if not isinstance(fm, dict):
        return False  # evidence needs a parsed frontmatter mapping (every memo has one)
    fm_date = _fm_date(fm)
    if fm_date is _UNTRUSTED:
        return False
    dates = [d for d in (_date_from_name(memo.name), fm_date) if d is not None]
    return bool(dates) and all(abs((d - sig_date).days) <= window for d in dates)


def _stamped_by_other_session(memo: Path, session_id: str) -> bool:
    """The memo is stamped by a different session — or it has no usable
    frontmatter or stamp, so a foreign stamp can't be ruled out ("no
    frontmatter" can be manufactured, e.g. by a leading BOM or blank line)."""
    fm = _frontmatter(memo)
    if not isinstance(fm, dict):
        return True
    stamp = _stamp(fm)
    return stamp is not None and stamp != session_id


def _redirect_target(vault: Path, project: str) -> str | None:
    fm = _frontmatter(vault / "projects" / project / "_project.md")
    if not isinstance(fm, dict):
        return None  # no (or unusable) frontmatter → no redirect; body text is not metadata
    v = fm.get("redirect_to")
    return v.strip() if isinstance(v, str) and _SLUG_RE.fullmatch(v.strip()) else None


def _session_stamp_memo(sig: dict, session_map: dict[str, str]) -> str | None:
    """The memo stamped with this signal's session — the ONLY exact evidence.

    Positive evidence is robust: a parsing quirk would have to CREATE this
    session's full UUID in some memo's frontmatter. The transcript ``Write``
    check below needs negative evidence too ("the memo carries no OTHER
    session's stamp"), and eleven review rounds kept finding frontmatter a
    line-based reader and YAML disagree on — each one hid a foreign stamp and
    produced a false clear. So a transcript Write is reported, never trusted."""
    sid = str(sig.get("session_id", "") or "").lower()
    return session_map.get(sid) if sid else None


def _transcript_hint(vault: Path, sig: dict, window: int = 2) -> str | None:
    """A memo this session provably wrote (successful in-vault ``Write`` to its
    own project, main or subagent transcript), dated near the signal and not
    stamped by another session — shown as supporting evidence in the report;
    cleared only with ``--trust-window``."""
    sid = str(sig.get("session_id", "") or "").lower()
    tp = sig.get("transcript_path")
    project = str(sig.get("project", "") or "")
    sig_date = _signal_date(sig.get("timestamp", ""))
    if not (isinstance(tp, str) and tp and project and sig_date is not None):
        return None
    main_jsonl = Path(tp)
    sources = [main_jsonl, *sorted(main_jsonl.with_suffix("").glob("subagents/*.jsonl"))]
    for src in sources:
        for written_project, name in _memos_written_by(src, vault):
            if written_project != project:
                continue
            where, hops = written_project, 0
            while where and hops <= _MAX_REDIRECT_HOPS:
                memo = vault / "projects" / where / "memos" / name
                if memo.is_file():
                    if _dated_near(memo, sig_date, window) and not _stamped_by_other_session(memo, sid):
                        return str(memo.relative_to(vault))
                    break
                where, hops = _redirect_target(vault, where), hops + 1
    return None


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
    parser.add_argument(
        "--trust-window",
        action="store_true",
        help="With --apply, also clear likely matches (transcript Write or date window).",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    args = parser.parse_args()

    vault = get_memex_path()
    pending_dir = get_pending_dir()
    signals = sorted(pending_dir.glob("*.json"))

    session_map = _memo_session_map(vault) if signals else {}
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
        covered_by = _session_stamp_memo(sig, session_map)
        evidence = "session_id" if covered_by else None
        if not covered_by:
            covered_by = _transcript_hint(vault, sig, args.window)
            evidence = "transcript" if covered_by else None
        if not covered_by:
            sig_date = _signal_date(sig.get("timestamp", ""))
            covered_by = _covering_memo(vault, project, sig_date, args.window)
            evidence = "window" if covered_by else None
        rows.append(
            {
                "file": sig_file.name,
                "session": str(sig.get("session_id", "") or "")[:8],
                "project": str(project or ""),
                "timestamp": str(sig.get("timestamp", "") or ""),
                "covered_by": covered_by,
                "evidence": evidence,
            }
        )
        clearable = evidence == "session_id" or (evidence in ("transcript", "window") and args.trust_window)
        if clearable and args.apply:
            try:
                sig_file.unlink()
                rows[-1]["cleared"] = True
            except OSError as e:
                rows[-1]["cleared"] = False
                rows[-1]["error"] = str(e)

    # classification is by evidence TYPE; --trust-window only changes what --apply clears
    covered = [r for r in rows if r.get("evidence") == "session_id"]
    likely = [r for r in rows if r.get("evidence") in ("transcript", "window")]
    uncovered = [r for r in rows if not r.get("covered_by") and "error" not in r]
    failed = [r for r in rows if r.get("cleared") is False]
    errors = [r for r in rows if "error" in r]

    if args.json:
        print(json.dumps({"total": len(rows), "covered": covered, "likely": likely,
                          "uncovered": uncovered, "errors": errors,
                          "trust_window": args.trust_window}, indent=2))
        return

    if not signals:
        print("No pending-memo signals — nothing to reconcile.")
        return

    print(f"Pending signals: {len(rows)}  (covered={len(covered)}, likely={len(likely)}, "
          f"uncovered={len(uncovered)})")
    print()
    for r in rows:
        if r.get("cleared") is False:
            print(f"  FAILED   {r['project']:24} {r['timestamp'][:10]}  could not delete signal: {r.get('error')}")
        elif r.get("evidence") == "session_id":
            verb = "CLEARED " if r.get("cleared") else ("STALE   " if args.apply else "stale   ")
            print(f"  {verb} {r['project']:24} {r['timestamp'][:10]}  ← covered by {r['covered_by']} "
                  f"[{r['evidence']}]")
        elif r.get("evidence") in ("transcript", "window"):
            why = ("session wrote this memo (transcript Write)" if r["evidence"] == "transcript"
                   else "date window only")
            if r.get("cleared"):
                print(f"  CLEARED  {r['project']:24} {r['timestamp'][:10]}  ~ {r['covered_by']} "
                      f"[{why}, --trust-window]")
            else:
                hint = " (would clear with --apply)" if args.trust_window else ""
                print(f"  likely   {r['project']:24} {r['timestamp'][:10]}  ~ {r['covered_by']} "
                      f"[{why} — verify it is this session's memo]{hint}")
        elif "error" in r:
            print(f"  ERROR   {r['file']}: {r['error']}")
        else:
            print(f"  keep    {r['project']:24} {r['timestamp'][:10]}  (no covering memo — genuine retry)")
    print()
    if covered and not args.apply:
        print(f"Re-run with --apply to delete {len(covered)} stale signal(s).")
    elif args.apply:
        cleared = sum(1 for r in rows if r.get("cleared"))
        print(f"Cleared {cleared} signal(s); {len(uncovered)} genuine retry(ies) kept.")
    if failed:
        print(f"{len(failed)} signal(s) could not be deleted — see FAILED rows.")
    if likely and not (args.apply and args.trust_window):
        print(f"{len(likely)} signal(s) have supporting but not exact evidence — kept. Check "
              "each memo belongs to that session, then --apply --trust-window. (Memos stamped "
              "with session_id: match exactly.)")


if __name__ == "__main__":
    main()
