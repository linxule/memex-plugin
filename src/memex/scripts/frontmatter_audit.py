"""Frontmatter lint — detect the YAML-frontmatter damage class.

Read-only. Walks curated vault content (``projects/**/memos/*.md``,
``projects/**/_project.md``, ``topics/*.md``) and flags, per file:

  - **merged-line** — two frontmatter keys collapsed onto one physical line,
    e.g. ``status: archivedtitle: "..."`` (the v0.16 migration bug: ``status``
    and ``title`` glued). The literal symptom that motivated this lint: a
    ``status`` value that does not equal ``archived`` silently keeps the doc in
    the index AND loses its title. The simple ``key, _, value = partition(":")``
    parser swallows it without error, so only a dedicated check catches it.
  - **no-closing-delimiter** — a ``---`` opener with no closing ``---`` in the
    head, which traps the body inside the YAML block (the documented
    silent-empty-skill failure mode).
  - **missing-title** — a memo / topic / trail / project with no identity field
    (``title`` or ``name``).
  - **no-frontmatter** — a curated file with no ``---`` block at all.
  - **bad-harness**, **bad-session-ids**, **stamps-ignored** — the v0.22.0
    capture keys: ``harness`` must be one of ``memex.harness.HARNESSES``;
    ``session_ids`` must be a list of full UUIDs; a valid session stamp must also
    be one that the strict YAML reader (what hooks, ``backfill memos`` and
    ``reconcile-orphans`` use) sees. A stamp that does not normalize
    (``memex.session_memos.normalize_session_id``) or that the reader never gets
    to is never evidence, so the memo silently looks unlinked to its session —
    the 2026-10-28 failure class.

Findings come in two tiers. **Errors** (``issues``) are damage and fail the exit
code. **Warnings** (``warnings``) are judgement calls and never do:

  - **unknown-topic** / **project-slug-topic** — a memo ``topics:`` slug that
    matches no ``topics/*.md`` file and no topic ``aliases:`` entry (the phantom
    topics seen on 2026-10-28); a slug that is a project folder name gets its own
    message, since that is the usual cause.
  - **session-id-not-uuid** — a ``session_id`` that is not one full UUID (legacy
    8-hex prefixes exist in old memos; they never match a session).

It deliberately does NOT enforce a ``status`` vocabulary: the vault uses a rich,
intentional set (``evergreen``, ``stub``, ``developing``, ``superseded``,
``pre-writing``, …), so an enum check is pure noise. The damage class — two keys
glued onto one line, a body trapped by a missing delimiter — is the signal.

Never mutates anything. Exposed as ``memex check --validate`` (``--json`` for
agents, with ``issues`` = errors and ``warnings``; exits non-zero when any ERROR
is found, for CI/launchd — warnings never change the exit code).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

from memex.harness import HARNESSES
from memex.paths import get_memex_path
from memex.scripts.reconcile_orphans import _UNTRUSTED, _frontmatter, _StrictLoader
from memex.session_memos import memo_stamps, normalize_session_id

# Frontmatter keys used across the vault schema (see .claude/rules/architecture.md).
_KNOWN_KEYS = {
    "type", "title", "project", "projects", "date", "topics", "status", "manual",
    "aliases", "redirect_to", "source_cwd", "session_id", "name", "created",
    "condensed", "memos_digested", "related_memos", "tags", "concept", "started",
    "last_extended", "volatile", "source", "source_hash", "synced", "messages",
    "has_memo", "models", "commits", "duration_minutes", "input_tokens",
    "output_tokens", "cache_read_tokens", "updated",
    # v0.22.0 (harness-aware capture): a glued "<key>:" must read as a merge, and
    # these keys must never be mistaken for unknown ones.
    "harness", "session_ids", "written_by", "relay_channel", "relay_coordinator",
    "parent_session_id", "subagents",
}
_KEY_ALT = "|".join(sorted(_KNOWN_KEYS, key=len, reverse=True))
# A known key ANYWHERE in a value (even glued: "archivedtitle:"). Only applied to
# colon-free scalar/enum keys, whose legitimate values are single tokens — so any
# embedded "<known-key>:" is necessarily a merge, never a mid-word substring hit.
_ANY_KEY_RE = re.compile(rf"(?:{_KEY_ALT}):")
# A known key at value start or after whitespace — word-boundary-clean, so it
# never fires on a key name embedded mid-word ("subtitle", "myproject:8080").
_DELIM_KEY_RE = re.compile(rf"(?:^|\s)(?:{_KEY_ALT}):")
# A known key at the very start of a value ("title: status: archived").
_START_KEY_RE = re.compile(rf"^\s*(?:{_KEY_ALT}):")

# title is free text (colons are legitimate: 'title: "Notes: a study"', or even
# unquoted 'title: My manual: a guide'); source/redirect_to can hold URLs/paths
# with colons. Everything else is a colon-free scalar/list/enum value — the
# v0.22.0 prose fields (written_by, relay_coordinator) included: an unquoted
# 'written_by: x (harness: y)' is a YAML error that makes hooks ignore the memo's
# session stamps, so it SHOULD read as a merge.
_FREE_TEXT_KEYS = {"title"}
_URL_KEYS = {"source", "redirect_to"}


def _dequote(value: str) -> str:
    """Strip well-formed quoted spans so colons *inside* a value
    (``title: "A: B"``) don't masquerade as a second key. Unmatched quotes are
    left intact — the bounded ``[^"]*`` cannot cross a closing quote, so it never
    over-consumes to end-of-line."""
    value = re.sub(r'"[^"]*"', "", value)
    value = re.sub(r"'[^']*'", "", value)
    return value


def _is_merged(key: str, value: str) -> bool:
    """True if ``value`` carries a second ``known_key:`` (two keys glued onto one
    physical line). Per-key, to stay high-precision (a lint that cries wolf is
    worse than none):
      - free-text ``title``: flag only a key at the very START of the value
        (``title: status: x``); embedded prose colons (``a manual: guide``) are fine;
      - URL-ish / unknown keys: a whitespace/start-delimited key only, so
        ``http://h:8080`` and custom ``my-x:`` don't fire;
      - everything else (colon-free scalar/enum: ``status``/``project``/``date`` …):
        ANY embedded ``known_key:`` — this is what catches the glued
        ``status: archivedtitle: "..."`` production bug.

    Known limitation (precision > recall, by choice): a glued merge whose FIRST
    key is itself unlisted and whose second key is *glued* mid-value (no
    delimiter) can slip through. Relaxing it would risk false positives on
    custom free-text keys; the real damage class always starts at a known key.
    """
    dq = _dequote(value)
    if key in _FREE_TEXT_KEYS:
        return bool(_START_KEY_RE.search(dq))
    if key in _URL_KEYS or key not in _KNOWN_KEYS:
        return bool(_DELIM_KEY_RE.search(dq))
    return bool(_ANY_KEY_RE.search(dq))


def _frontmatter_block(text: str) -> tuple[list[str] | None, bool]:
    """Return (frontmatter_lines, closed). ``None`` lines = no opening ``---``.
    ``closed`` reflects a closing ``---`` ANYWHERE below the opener (full scan, so
    a long-but-valid ``aliases``/``messages`` block is never mis-flagged as
    unclosed). Curated files (memos/topics/_project) are small; transcripts and
    auto-memory are excluded upstream."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None, True
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return lines[1:i], True
    return lines[1:], False


def _parse(fm_lines: list[str]) -> dict:
    """Light key→value parse of frontmatter lines (first colon wins)."""
    out: dict[str, str] = {}
    for line in fm_lines:
        if ":" not in line or line.lstrip().startswith("#") or line.lstrip().startswith("-"):
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _wants_identity(path: Path, fm: dict) -> bool:
    """Whether this file should carry an identity field (``title`` or ``name``).
    Memos are keyed by PATH (``/memos/``), not by a ``type:`` field — an
    empty-frontmatter memo (``---\\n---``) still needs a title."""
    posix = path.as_posix()
    if "/memos/" in posix:
        return True
    typ = fm.get("type", "")
    if path.name == "_project.md" or typ == "project":
        return True
    if typ in ("concept", "trail"):
        return True
    # topics/ files (except curator-infra meta notes) want a title
    return "topics/" in posix and typ != "meta"


# --- reading single keys out of possibly-damaged frontmatter ------------------

def _strip_comment(text: str) -> str:
    """Drop a trailing ``# comment`` (``#`` at value start or after whitespace,
    outside quotes: ``manual: true   # note``). A quote only opens at a token
    start, so prose like ``don't #x`` is not mistaken for a quoted span."""
    quote = ""
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'" and (i == 0 or text[i - 1] in " \t[,"):
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i]
    return text


# A column-0 `key:` line: where one key's own lines end and the next key begins.
_KEY_LINE_RE = re.compile(r"[A-Za-z_][\w.\-]*[ \t]*:(?:[ \t]|$)")


class _Unreadable:
    """Marker: the key is present but its own lines are not valid YAML."""

    def __repr__(self) -> str:
        return "<unreadable>"


_UNREADABLE = _Unreadable()


def _fragment(fm_lines: list[str], key: str) -> tuple[int, object] | None:
    """Locate top-level ``key:`` (column 0) and parse ONLY its lines with YAML →
    ``(line_no, value)``, or ``None`` when the key is absent. ``value`` is what
    YAML says (``None`` for a bare key, a list for flow/block lists, a string for
    ``>-`` block scalars, …) or ``_UNREADABLE`` when the fragment does not parse.

    The key's own lines are the unit (the key line plus everything under it, up
    to the next column-0 ``key:`` line; a flow list may legally continue at
    column 0), so damage elsewhere in the frontmatter (a glued line, an unquoted
    colon) neither hides this key nor gets blamed on it, and an unterminated flow
    list cannot swallow its neighbours. Hand-parsing quotes, flow lists and block
    scalars was tried first and lost to each of them in review.
    """
    prefix = f"{key}:"
    for i, line in enumerate(fm_lines):
        if line.startswith(prefix):
            break
    else:
        return None
    frag = [line]
    for nxt in fm_lines[i + 1:]:
        if _KEY_LINE_RE.match(nxt):
            break
        frag.append(nxt)
    try:
        data = yaml.safe_load("\n".join(frag))
    except (yaml.YAMLError, ValueError):
        return i + 2, _UNREADABLE  # fm_lines[0] is file line 2 (line 1 is the opening '---')
    if isinstance(data, dict) and key in data:
        return i + 2, data[key]
    return i + 2, _UNREADABLE


def _finding(path: Path, kind: str, line: int, detail: str, severity: str = "error") -> dict:
    return {"path": str(path), "kind": kind, "line": line, "detail": detail, "severity": severity}


# --- v0.22.0 schema checks (E1) ---------------------------------------------

def _capture_key_issues(path: Path, fm_lines: list[str], skip_lines: set[int]) -> list[dict]:
    """Findings for ``harness`` / ``session_id`` / ``session_ids``. Lines already
    reported as merged are skipped: the value is two keys glued together, so
    judging it would double-report one defect."""
    out: list[dict] = []

    found = _fragment(fm_lines, "harness")
    if found and found[0] not in skip_lines:
        line_no, value = found
        if value is _UNREADABLE:
            detail = "harness line is not valid YAML"
        elif isinstance(value, (list, dict)):
            detail = f"harness must be a single value, not a {'list' if isinstance(value, list) else 'mapping'}"
        elif value not in HARNESSES:
            shown = "empty" if value is None or value == "" else repr(value)
            detail = f"unknown harness {shown} (expected one of: {', '.join(HARNESSES)})"
        else:
            detail = ""
        if detail:
            out.append(_finding(path, "bad-harness", line_no, detail))

    found = _fragment(fm_lines, "session_id")
    if found and found[0] not in skip_lines:
        line_no, value = found
        if isinstance(value, (list, dict)):
            # not legacy damage: the reader ignores a collection, so the memo is unlinked
            out.append(_finding(path, "session-id-not-uuid", line_no,
                                "session_id must be one UUID, not a collection (use session_ids for several)"))
        elif value is not None and normalize_session_id(value) is None:
            shown = "unreadable" if value is _UNREADABLE else f"{value!r}"
            out.append(_finding(path, "session-id-not-uuid", line_no,
                                f"session_id is {shown}, not one full UUID (it can never match a session)",
                                "warning"))

    found = _fragment(fm_lines, "session_ids")
    if found and found[0] not in skip_lines:
        line_no, value = found
        detail = ""
        if value is _UNREADABLE:
            detail = "session_ids line is not valid YAML"
        elif isinstance(value, list):
            bad = [str(x) for x in value if normalize_session_id(x) is None]
            if bad:
                detail = "session_ids entries that are not full UUIDs: " + ", ".join(repr(x) for x in bad[:3])
                if len(bad) > 3:
                    detail += f" (+{len(bad) - 3} more)"
        elif value is not None:  # a bare `session_ids:` / `null` claims no stamps
            detail = f"session_ids must be a list of full UUIDs, got the scalar {value!r}"
        if detail:
            out.append(_finding(path, "bad-session-ids", line_no, detail))
    return out


def _yaml_problem(fm_lines: list[str]) -> tuple[int | None, str]:
    """Why the strict reader rejects this frontmatter → ``(file_line, text)``."""
    try:
        yaml.load("\n".join(fm_lines), Loader=_StrictLoader)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
        problem = getattr(exc, "problem", None) or str(exc).splitlines()[0]
        if mark:
            return mark.line + 2, f"line {mark.line + 2}: {problem}"
        return None, str(problem)
    except Exception as exc:  # noqa: BLE001 — same breadth as the reader being explained
        return None, f"{type(exc).__name__}: {exc}"
    return None, ("the strict reader still rejects the block (a '---' line with extra spaces, a BOM, "
                  "an oversized block or unusual line separators)")


def _stamps_ignored(path: Path, fm_lines: list[str], merged_lines: set[int]) -> list[dict]:
    """Error when the line-based reading finds a valid session stamp that the
    strict reader (the one hooks, ``backfill memos`` and ``reconcile-orphans``
    use, via ``memo_stamps``) does not: the memo then silently looks unlinked to
    its session. ~1.4% of old memos have frontmatter that is not valid YAML (an
    unquoted ``title: a: b``); without this check they pass the lint while their
    stamps do nothing. Silent when the YAML error sits on a line already reported
    as merged (one defect, one finding)."""
    expected: set[str] = set()
    for key in ("session_id", "session_ids"):
        found = _fragment(fm_lines, key)
        if not found:
            continue
        # same shapes memo_stamps honours: one scalar id, or a list of ids
        value = found[1]
        candidates = (value if isinstance(value, list) else []) if key == "session_ids" else [value]
        expected.update(sid for sid in map(normalize_session_id, candidates) if sid)
    if not expected:
        return []
    strict = _frontmatter(path)
    if expected <= memo_stamps(strict):
        return []
    if strict is _UNTRUSTED:
        line, problem = _yaml_problem(fm_lines)
        if line in merged_lines:
            return []
        reason = f"frontmatter is not valid YAML as a whole; {problem}"
    else:
        reason = "the YAML reading of the frontmatter disagrees with its lines"
    return [_finding(path, "stamps-ignored", 1,
                     f"session stamp(s) here are never matched to their session: {reason}")]


# --- topic slug lint (E2, warning tier) -------------------------------------

def _slug_key(raw: str) -> str:
    """Comparison key for a topic slug: case, ``[[wikilink]]`` brackets, a
    ``|label``, a ``#heading``, a ``topics/`` folder prefix and ``.md`` ignored."""
    text = raw.strip().removeprefix("[[").removesuffix("]]").split("|")[0].split("#")[0].strip()
    return text.removeprefix("topics/").removesuffix(".md").strip().lower()


def _strings(value: object) -> list[str]:
    """The scalar entries of a YAML list (or a lone scalar) as strings. Nested
    lists are flattened (an unquoted ``[[a]]`` parses as ``[["a"]]``); nulls,
    mappings and unreadable fragments are skipped."""
    out: list[str] = []
    for x in value if isinstance(value, list) else [value]:
        if isinstance(x, list):
            out.extend(_strings(x))
        elif isinstance(x, (str, int, float)) and not isinstance(x, bool):
            out.append(str(x))
    return out


def build_topic_index(vault: Path) -> dict[str, set[str]]:
    """What a memo ``topics:`` slug can legitimately resolve to: topic file stems
    (archived ones count: the file exists), topic ``aliases:`` entries, and project
    folder names (kept apart, only to word the message)."""
    topics: set[str] = set()
    aliases: set[str] = set()
    tdir = vault / "topics"
    if tdir.is_dir():
        for f in tdir.glob("*.md"):
            topics.add(f.stem.lower())
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            fm_lines, closed = _frontmatter_block(text)
            found = _fragment(fm_lines, "aliases") if fm_lines and closed else None
            if found and found[1] is not _UNREADABLE:
                aliases.update(_slug_key(a) for a in _strings(found[1]))
    pdir = vault / "projects"
    projects = {p.name.lower() for p in pdir.iterdir() if p.is_dir()} if pdir.is_dir() else set()
    return {"topics": topics, "aliases": aliases, "projects": projects}


def _topic_warnings(path: Path, fm_lines: list[str], index: dict[str, set[str]]) -> list[dict]:
    found = _fragment(fm_lines, "topics")
    if not found or not isinstance(found[1], list):
        return []  # absent, null, a scalar, or unparseable: not judged
    line_no, value = found
    out: list[dict] = []
    seen: set[str] = set()
    for raw in _strings(value):
        key = _slug_key(raw)
        if not key or key in seen:
            continue
        seen.add(key)
        if key in index["topics"] or key in index["aliases"]:
            continue
        if key in index["projects"]:
            out.append(_finding(path, "project-slug-topic", line_no,
                                f"project slug used as topic: {raw!r} (projects/{key}/ exists; "
                                f"no topics/{key}.md or alias)", "warning"))
        else:
            out.append(_finding(path, "unknown-topic", line_no,
                                f"topic {raw!r} matches no topics/*.md file and no topic alias", "warning"))
    return out


def _scan_file(path: Path, topic_index: dict[str, set[str]] | None = None) -> list[dict]:
    """Lint one file. Each finding carries ``severity`` (``error`` | ``warning``).
    ``topic_index`` (from ``build_topic_index``) switches on the memo ``topics:``
    lint; without it only per-file checks run."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    fm_lines, closed = _frontmatter_block(text)

    if fm_lines is None:
        return [_finding(path, "no-frontmatter", 1, "file does not start with a '---' frontmatter block")]
    if not closed:
        # everything below is unreliable without a real block
        return [_finding(path, "no-closing-delimiter", 1,
                         "opening '---' has no closing '---' (body trapped in YAML)")]

    issues: list[dict] = []
    merged_lines: set[int] = set()
    for idx, line in enumerate(fm_lines, start=2):
        if ":" not in line or line.lstrip().startswith(("#", "-")):
            continue
        key, _, value = line.partition(":")
        if _is_merged(key.strip(), _strip_comment(value)):
            merged_lines.add(idx)
            issues.append(_finding(path, "merged-line", idx, f"two keys on one line: {line.strip()!r}"))

    fm = _parse(fm_lines)
    if _wants_identity(path, fm) and not (fm.get("title") or fm.get("name")):
        issues.append(_finding(path, "missing-title", 1, "no 'title' or 'name' in frontmatter"))

    issues.extend(_capture_key_issues(path, fm_lines, merged_lines))
    is_memo = "/memos/" in path.as_posix()
    if is_memo:
        issues.extend(_stamps_ignored(path, fm_lines, merged_lines))
    if is_memo and topic_index is not None and topic_index["topics"]:
        # no topics/ at all (fresh vault, sparse checkout) → every slug would be "phantom"
        issues.extend(w for w in _topic_warnings(path, fm_lines, topic_index)
                      if w["line"] not in merged_lines)
    return issues


def _curated_files(vault: Path):
    """Curated, hand-editable content where the damage class appears. Excludes
    transcripts/ and auto-memory/ (machine-written, high-volume)."""
    proj = vault / "projects"
    if proj.is_dir():
        for pd in sorted(p for p in proj.iterdir() if p.is_dir()):
            memos = pd / "memos"
            if memos.is_dir():
                yield from sorted(memos.glob("*.md"))
            pf = pd / "_project.md"
            if pf.exists():
                yield pf
    topics = vault / "topics"
    if topics.is_dir():
        yield from sorted(topics.glob("*.md"))


def audit_frontmatter(vault: Path) -> dict:
    """Build the frontmatter-lint report (no mutation). ``issues`` = errors,
    ``warnings`` = judgement calls; only errors fail ``memex check --validate``."""
    errors: list[dict] = []
    warnings: list[dict] = []
    scanned = 0
    topic_index = build_topic_index(vault)
    for f in _curated_files(vault):
        scanned += 1
        for finding in _scan_file(f, topic_index):
            (warnings if finding["severity"] == "warning" else errors).append(finding)
    return {"scanned": scanned, "issues": errors, "warnings": warnings}


# Text output lists at most this many rows per warning kind (a vault can carry
# hundreds of phantom topics); --json always carries every row.
_WARNING_ROWS_SHOWN = 25


_NAME_DATE_RE = re.compile(r"(\d{4})-?(\d{2})-?(\d{2})")


def _name_date(path: str) -> str:
    """``YYYYMMDD`` from a memo filename (``2026-10-28-…`` and ``20261028-…`` both
    occur in a vault); ``""`` for undated names, which sort as the oldest."""
    m = _NAME_DATE_RE.match(Path(path).name)
    return "".join(m.groups()) if m and "2000" <= m.group(1) <= "2100" else ""


def _print_tier(rows: list[dict], limit: int | None) -> None:
    by_kind: dict[str, list[dict]] = {}
    for row in rows:
        by_kind.setdefault(row["kind"], []).append(row)
    for kind in sorted(by_kind):
        group = by_kind[kind]
        if limit is not None:
            # capped list: newest memos first, so the rows shown are the actionable
            # ones, not the oldest free-form tags
            group = sorted(group, key=lambda r: (_name_date(r["path"]), r["path"]), reverse=True)
        print(f"  [{kind}] ×{len(group)}")
        for row in group if limit is None else group[:limit]:
            print(f"    • {row['path']}:{row['line']}  {row['detail']}")
        if limit is not None and len(group) > limit:
            print(f"    … and {len(group) - limit} more (--json lists all)")
        print()


def run_frontmatter_audit(json_out: bool = False) -> int:
    """Print the frontmatter-lint report. Returns 1 iff any ERROR found;
    warnings are reported but never change the exit code."""
    vault = get_memex_path()
    report = audit_frontmatter(vault)
    issues, warnings = report["issues"], report["warnings"]

    if json_out:
        print(json.dumps(report, indent=2))
        return 1 if issues else 0

    scanned = report["scanned"]
    if not issues and not warnings:
        print(f"✓ No frontmatter issues across {scanned} curated files.")
        return 0

    if issues:
        print(f"⚠ {len(issues)} frontmatter issue(s) across {scanned} curated files:\n")
        _print_tier(issues, None)
        print("  Fix: split merged keys onto separate lines; restore missing titles; "
              "close dangling '---'; give harness/session_ids valid values. Re-run after fixing.")
    else:
        print(f"✓ No frontmatter errors across {scanned} curated files.")
    if warnings:
        print(f"\nℹ {len(warnings)} warning(s) (judgement calls; they do not fail the check):\n")
        _print_tier(warnings, _WARNING_ROWS_SHOWN)
        print("  Warnings: give a phantom topic a topics/<slug>.md or an alias, or retag the memo; "
              "a project slug is not a topic.")
    return 1 if issues else 0


def main():
    parser = argparse.ArgumentParser(description="Lint vault frontmatter for the YAML-damage class")
    parser.add_argument("--json", action="store_true", help="JSON output for agents")
    args = parser.parse_args()
    sys.exit(run_frontmatter_audit(json_out=args.json))


if __name__ == "__main__":
    main()
