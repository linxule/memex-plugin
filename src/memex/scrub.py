"""Regex-based secret scrubber for the memex vault.

Detects API keys and other credentials in vault files and either reports them
(dry-run, default) or redacts them in place (`--apply`). Curated high-precision
patterns — biased toward false negatives over false positives so legit memo
content doesn't get corrupted.

The CLI scans a path (file or directory) and writes findings to stdout. With
`--apply`, replaces each match with `<REDACTED:provider>` in place. The
replacement format is itself a no-match against every pattern, so re-running
the scrubber on already-redacted content is a true no-op (idempotent).

Library usage:

    from memex.scrub import scrub_text
    cleaned, matches = scrub_text(content, apply=True)

Add new providers by appending to `PATTERNS`. Order matters: list specific
patterns before generic ones — overlapping spans resolve to the earlier entry
in the list.
"""

from __future__ import annotations

import argparse
import json as jsonlib
import os
import re
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


# ── Pattern catalog ─────────────────────────────────────────────────────────
#
# Ordered most-specific first. Within a span overlap, the earlier pattern wins.
# Each pattern should match real secrets and reject most prose. When in doubt,
# prefer false negative (miss a key) over false positive (corrupt prose) — the
# scrubber is one layer of defense, not the only one.
#
# Why curated regex instead of `detect-secrets` / `trufflehog` / `gitleaks`:
# the memex vault is prose (memos + transcripts), not code. Entropy-based
# heuristics that work well on source repos flag SHA hashes, base64 blobs in
# auto-memory, embedding IDs, and JWT-shaped examples — false positives that
# would force every memo to grow an allowlist annotation, or train users to
# disable the scrubber entirely. The 0-FP-on-prose property here (see
# TestNoFalsePositives) is more valuable than the marginal recall lift from
# adding entropy detection. If a new high-confidence provider needs coverage,
# add it to this list — don't reach for the general-purpose library.

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Anthropic — sk-ant-(api|admin|sid)NN-<base62-ish>
    ("anthropic", re.compile(r"sk-ant-(?:api|admin|sid)\d{2}-[A-Za-z0-9_\-]{40,}")),
    # OpenAI project / service-account keys (specific prefixes)
    ("openai-project", re.compile(r"sk-proj-[A-Za-z0-9_\-]{40,}")),
    ("openai-service", re.compile(r"sk-svcacct-[A-Za-z0-9_\-]{40,}")),
    # GitHub PATs / OAuth tokens
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b")),
    # Google API keys (Firebase, Maps, GCP, Gemini — all share AIza prefix)
    ("google-api", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    # AWS access key ID (paired secret would be next to it; we only catch the ID)
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # Slack bot/user/legacy tokens
    ("slack", re.compile(r"\bxox[abprs]-[0-9]{10,13}-[0-9]{10,13}-[A-Za-z0-9]{24,}\b")),
    # Hugging Face tokens — hf_<34+ alphanumeric>. User-access tokens are 37 chars total;
    # fine-grained tokens can be longer. Unique prefix → low FP risk.
    ("huggingface", re.compile(r"\bhf_[A-Za-z0-9]{34,}\b")),
    # Stripe live/test secret + publishable + restricted keys.
    # Uses `_` (not `-`) so this never overlaps with `sk-ant-*` / `sk-proj-*` / generic-sk above.
    ("stripe", re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{24,}\b")),
    # Notion integration secrets — `secret_<43 alphanumeric>` (50 chars total).
    # The 43-char alphanumeric run after `secret_` is shape-distinctive enough
    # to avoid prose collisions like "secret_password".
    ("notion", re.compile(r"\bsecret_[A-Za-z0-9]{43}\b")),
    # Sentry DSN — `https://<32hex>[:<32hex>]@<host with `sentry` substring>/<numeric id>`.
    # Substring `sentry` in the host is the discriminator that keeps this from matching
    # any prose URL that happens to lead with a long hex string.
    (
        "sentry-dsn",
        re.compile(
            r"\bhttps?://[a-f0-9]{32}(?::[a-f0-9]{32})?@[a-z0-9.-]*sentry[a-z0-9.-]*/[0-9]+\b"
        ),
    ),
    # JWT tokens (three base64url segments)
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{16,}\.eyJ[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}\b")),
    # Private key blocks (RSA/EC/OPENSSH/DSA/PGP/generic)
    (
        "private-key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----"
            r"[\s\S]+?"
            r"-----END (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----"
        ),
    ),
    # Generic sk-* (catches OpenAI legacy, Moonshot, DeepSeek, Together, many others).
    # MUST come after the specific sk-ant / sk-proj / sk-svcacct patterns so they win on overlap.
    # Length floor of 32 base62-ish chars discourages matching "sk-something" prose.
    ("generic-sk", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    # Generic sk-<vendor>-<token> shape (e.g. sk-moonshot-..., sk-openrouter-...)
    ("generic-sk-vendor", re.compile(r"\bsk-[a-z]{3,16}-[A-Za-z0-9_\-]{32,}\b")),
]

# Default file-name allowlist for directory scans. Memo, project doc, topic,
# auto-memory, and transcript files are all in scope by default. Add suffixes
# here, not regex escapes.
DEFAULT_EXTENSIONS = {".md", ".jsonl", ".json", ".txt"}

# Directories to always skip during recursive scans. `tests` is included to
# prevent the scrubber from rewriting its own test fixtures (which contain
# literal sk-* / ghp_* / AIza* strings on purpose).
SKIP_DIR_NAMES = {
    ".git",
    ".venv",
    "__pycache__",
    ".obsidian",
    "node_modules",
    ".pytest_cache",
    "tests",
}


@dataclass
class Match:
    """Single scrubber hit, with enough metadata to act on."""

    provider: str
    line: int
    col: int
    text: str  # raw matched secret
    replacement: str  # what it became (or would become with --apply)


@dataclass
class FileResult:
    """Per-file outcome from a scrub run."""

    path: str
    matches: list[Match]
    applied: bool  # True if --apply rewrote the file


def _replacement_for(provider: str) -> str:
    return f"<REDACTED:{provider}>"


def scrub_text(text: str, apply: bool = False) -> tuple[str, list[Match]]:
    """Scan text for secrets. Returns (possibly-redacted text, list of matches).

    `apply=False` (default): text is returned unchanged; matches list reports
    what would be redacted. `apply=True`: each match span in text is replaced
    with `<REDACTED:provider>` and the new text is returned.

    Overlap handling: when two patterns claim overlapping spans, the more
    specific one (lower index in `PATTERNS`) always wins — even when the
    less-specific pattern would start at an earlier offset. Matches are
    returned in file order.
    """
    # Collect candidate spans from every pattern.
    raw_hits: list[tuple[int, int, int, str]] = []  # (start, end, pattern_idx, provider)
    for idx, (provider, pat) in enumerate(PATTERNS):
        for m in pat.finditer(text):
            raw_hits.append((m.start(), m.end(), idx, provider))

    if not raw_hits:
        return text, []

    # Resolve overlaps specificity-first: process hits in pattern-index order
    # so the most-specific patterns claim their spans before less-specific
    # ones get a chance. A new hit is accepted only if its span doesn't
    # overlap any already-accepted span. Within the same pattern_idx, sort by
    # start so multiple hits from one pattern are processed in file order.
    raw_hits.sort(key=lambda h: (h[2], h[0]))
    accepted: list[tuple[int, int, str]] = []  # (start, end, provider)
    for start, end, _idx, provider in raw_hits:
        # Half-open intervals: overlap iff start < other_end AND end > other_start.
        if any(start < other_end and end > other_start for other_start, other_end, _ in accepted):
            continue
        accepted.append((start, end, provider))

    # Reorder accepted spans by file position for downstream output.
    accepted.sort(key=lambda h: h[0])

    # Build the Match list with line/col for reporting.
    matches: list[Match] = []
    for start, end, provider in accepted:
        line_no = text.count("\n", 0, start) + 1
        line_start = text.rfind("\n", 0, start) + 1
        col = start - line_start + 1
        matches.append(
            Match(
                provider=provider,
                line=line_no,
                col=col,
                text=text[start:end],
                replacement=_replacement_for(provider),
            )
        )

    if not apply:
        return text, matches

    # Rebuild text with replacements.
    out: list[str] = []
    cursor = 0
    for start, end, provider in accepted:
        out.append(text[cursor:start])
        out.append(_replacement_for(provider))
        cursor = end
    out.append(text[cursor:])
    return "".join(out), matches


def iter_files(root: Path, extensions: set[str] = DEFAULT_EXTENSIONS) -> Iterable[Path]:
    """Walk root, yielding files with allowed extensions, skipping noise dirs."""
    if root.is_file():
        if root.suffix in extensions:
            yield root
        return
    for path in root.rglob("*"):
        if path.is_dir():
            continue
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        if path.suffix in extensions:
            yield path


def _atomic_write(path: Path, text: str) -> None:
    """Write `text` to `path` atomically, preserving line endings.

    Writes to a sibling temp file in the same directory (so it's on the same
    filesystem as the destination), fsyncs, then `os.replace`s into place.
    `os.replace` is atomic on POSIX and Windows for same-filesystem renames.
    Eliminates the failure mode where KeyboardInterrupt / OOM / disk-full
    between truncate and write leaves a partially-written memo on disk.

    Binary-mode write preserves CRLF / CR / mixed line endings byte-for-byte —
    text-mode `os.fdopen(..., "w")` would translate `\\n` to the platform
    newline and discard original line endings, silently mutating Windows-style
    transcripts. Callers should pass `text` that already has the desired line
    endings (e.g., the value returned by `scrub_text`, which preserves them).

    Long-filename safety: the temp file's prefix is derived from `path.name`
    but truncated so the full temp name (`.<truncated>.XXXXXX.tmp`) fits well
    within typical 255-byte filesystem limits, even on names like a 200-char
    memo title.
    """
    parent = path.parent
    # Reserve room for the dot prefix, `mkstemp`'s 6-char random suffix, and
    # the `.tmp` suffix. 200 chars of original name is plenty for readability
    # while leaving headroom below the common NAME_MAX of 255 on ext4/APFS/NTFS.
    safe_name = path.name[:200]
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{safe_name}.", suffix=".tmp", dir=parent
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        # Clean up the temp file on any failure (including KeyboardInterrupt).
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def safe_write_text(path: Path, content: str) -> int:
    """Pre-scrub `content` for secrets, then write to `path`.

    The shared write-gate for any Python code path that writes prose to disk
    without going through Claude Code's `Write`/`Edit`/`MultiEdit` tools (the
    boundary the PostToolUse hook in `hooks/post-tool-use.py` covers). Each
    such caller is a hook-bypass that needs its own scrub step; sharing a
    helper makes adding the gate to new callers a one-liner and gives us a
    single audit point.

    Returns the number of redacted secrets (0 means clean — call still
    writes). Raises `OSError` on write failure (caller decides whether to
    swallow or propagate).

    Use this instead of `path.write_text(content)` anywhere the content is
    user-generated prose (memos, transcripts, auto-memory). Skip for purely
    structural content (state files, frontmatter-only edits where the body
    is preserved unchanged) where the scrub is pure overhead with no
    realistic catch.
    """
    new_content, matches = scrub_text(content, apply=True)
    path.write_text(new_content)
    return len(matches)


def scrub_file(path: Path, apply: bool = False) -> FileResult:
    """Scrub a single file. Returns FileResult; writes file only when apply=True
    and matches are non-empty.

    Raises `OSError` if a write fails (caller decides whether to abort or
    accumulate errors across a batch).

    Read uses binary mode + explicit UTF-8 decode so line endings survive
    intact through scrub + atomic write. `Path.read_text(encoding="utf-8")`
    would normalize CR / CRLF to `\\n` and the round-trip would silently
    mutate Windows-style transcripts even when no secrets were found.
    """
    try:
        original = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        # Binary or non-UTF8 file — skip silently.
        return FileResult(path=str(path), matches=[], applied=False)

    new_text, matches = scrub_text(original, apply=apply)
    applied = False
    if apply and matches and new_text != original:
        _atomic_write(path, new_text)
        applied = True
    return FileResult(path=str(path), matches=matches, applied=applied)


def scan_path(root: Path, apply: bool = False) -> tuple[list[FileResult], list[tuple[str, str]]]:
    """Walk root and scrub every eligible file.

    Returns `(results, errors)`:
    - `results`: per-file FileResult for files with at least one match
    - `errors`: list of `(path, error_message)` for files that failed to
      read or write. Read errors yield no FileResult; write errors yield a
      FileResult with `applied=False` plus an entry here.
    """
    results: list[FileResult] = []
    errors: list[tuple[str, str]] = []
    for path in iter_files(root):
        try:
            result = scrub_file(path, apply=apply)
        except OSError as exc:
            errors.append((str(path), f"{type(exc).__name__}: {exc}"))
            continue
        if result.matches:
            results.append(result)
    return results, errors


# ── CLI ─────────────────────────────────────────────────────────────────────


def _format_text(results: list[FileResult], errors: list[tuple[str, str]], apply: bool) -> str:
    if not results and not errors:
        return "No secrets found." if not apply else "No secrets found. Nothing changed."

    lines: list[str] = []
    total = 0
    for r in results:
        total += len(r.matches)
        verb = "redacted" if r.applied else "would redact"
        lines.append(f"\n{r.path}  ({len(r.matches)} {verb})")
        for m in r.matches:
            # Truncate match preview for readability.
            preview = m.text if len(m.text) <= 24 else m.text[:21] + "..."
            lines.append(f"  L{m.line}:C{m.col}  [{m.provider}]  {preview}  →  {m.replacement}")

    if errors:
        lines.append("\nErrors:")
        for path, msg in errors:
            lines.append(f"  {path}  {msg}")

    summary_parts = [f"\n\n{total} match(es) across {len(results)} file(s)."]
    if errors:
        summary_parts.append(f"{len(errors)} file(s) errored.")
    if not apply:
        summary_parts.append("Run with --apply to redact in place.")
    else:
        summary_parts.append("Files rewritten.")
    return "\n".join(lines) + " " + " ".join(summary_parts).strip()


def _format_json(results: list[FileResult], errors: list[tuple[str, str]], apply: bool) -> str:
    payload = {
        "applied": apply,
        "file_count": len(results),
        "match_count": sum(len(r.matches) for r in results),
        "error_count": len(errors),
        "files": [
            {
                "path": r.path,
                "applied": r.applied,
                "matches": [asdict(m) for m in r.matches],
            }
            for r in results
        ],
        "errors": [{"path": p, "message": m} for p, m in errors],
    }
    return jsonlib.dumps(payload, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="memex scrub",
        description="Detect (and optionally redact) API keys and credentials in vault files.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="File or directory to scan (default: current directory)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Rewrite files in place, replacing each match with <REDACTED:provider>.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON instead of human-readable text.",
    )
    args = parser.parse_args()

    root = Path(args.path).resolve()
    if not root.exists():
        print(f"Error: path not found: {root}", file=sys.stderr)
        sys.exit(2)

    results, errors = scan_path(root, apply=args.apply)

    if args.json:
        print(_format_json(results, errors, apply=args.apply))
    else:
        print(_format_text(results, errors, apply=args.apply))

    # Exit codes:
    #   0  no matches found (clean) — also: matches found AND --apply succeeded with no errors
    #   1  dry-run found matches (signal for CI / pre-commit drift detection)
    #   2  CLI error (bad path) — also: --apply ran with at least one file-level write error
    if errors:
        sys.exit(2)
    if results and not args.apply:
        sys.exit(1)


if __name__ == "__main__":
    main()
