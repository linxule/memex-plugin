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

It deliberately does NOT enforce a ``status`` vocabulary: the vault uses a rich,
intentional set (``evergreen``, ``stub``, ``developing``, ``superseded``,
``pre-writing``, …), so an enum check is pure noise. The damage class — two keys
glued onto one line, a body trapped by a missing delimiter — is the signal.

Never mutates anything. Exposed as ``memex check --validate`` (``--json`` for
agents; exits non-zero when any issue is found, for CI/launchd).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from memex.paths import get_memex_path

# Frontmatter keys used across the vault schema (see .claude/rules/architecture.md).
_KNOWN_KEYS = {
    "type", "title", "project", "projects", "date", "topics", "status", "manual",
    "aliases", "redirect_to", "source_cwd", "session_id", "name", "created",
    "condensed", "memos_digested", "related_memos", "tags", "concept", "started",
    "last_extended", "volatile", "source", "source_hash", "synced", "messages",
    "has_memo", "models", "commits", "duration_minutes", "input_tokens",
    "output_tokens", "cache_read_tokens", "updated",
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
# with colons. Everything else is a colon-free scalar/list/enum value.
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


def _scan_file(path: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    fm_lines, closed = _frontmatter_block(text)
    if fm_lines is None:
        return [{"path": str(path), "kind": "no-frontmatter", "line": 1,
                 "detail": "file does not start with a '---' frontmatter block"}]
    issues: list[dict] = []
    if not closed:
        issues.append({"path": str(path), "kind": "no-closing-delimiter", "line": 1,
                       "detail": "opening '---' has no closing '---' (body trapped in YAML)"})
        return issues  # everything below is unreliable without a real block

    for idx, line in enumerate(fm_lines, start=2):
        if ":" not in line or line.lstrip().startswith(("#", "-")):
            continue
        key, _, value = line.partition(":")
        if _is_merged(key.strip(), value):
            issues.append({"path": str(path), "kind": "merged-line", "line": idx,
                           "detail": f"two keys on one line: {line.strip()!r}"})

    fm = _parse(fm_lines)
    if _wants_identity(path, fm) and not (fm.get("title") or fm.get("name")):
        issues.append({"path": str(path), "kind": "missing-title", "line": 1,
                       "detail": "no 'title' or 'name' in frontmatter"})
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
    """Build the frontmatter-lint report (no mutation)."""
    issues: list[dict] = []
    scanned = 0
    for f in _curated_files(vault):
        scanned += 1
        issues.extend(_scan_file(f))
    return {"scanned": scanned, "issues": issues}


def run_frontmatter_audit(json_out: bool = False) -> int:
    """Print the frontmatter-lint report. Returns 1 iff any issue found."""
    vault = get_memex_path()
    report = audit_frontmatter(vault)
    issues = report["issues"]

    if json_out:
        print(json.dumps(report, indent=2))
        return 1 if issues else 0

    if not issues:
        print(f"✓ No frontmatter issues across {report['scanned']} curated files.")
        return 0

    by_kind: dict[str, list[dict]] = {}
    for iss in issues:
        by_kind.setdefault(iss["kind"], []).append(iss)

    print(f"⚠ {len(issues)} frontmatter issue(s) across {report['scanned']} curated files:\n")
    for kind in sorted(by_kind):
        rows = by_kind[kind]
        print(f"  [{kind}] ×{len(rows)}")
        for iss in rows:
            rel = iss["path"]
            print(f"    • {rel}:{iss['line']}  {iss['detail']}")
        print()
    print("  Fix: split merged keys onto separate lines; restore missing titles; "
          "close dangling '---'. Re-run after fixing.")
    return 1


def main():
    parser = argparse.ArgumentParser(description="Lint vault frontmatter for the YAML-damage class")
    parser.add_argument("--json", action="store_true", help="JSON output for agents")
    args = parser.parse_args()
    sys.exit(run_frontmatter_audit(json_out=args.json))


if __name__ == "__main__":
    main()
