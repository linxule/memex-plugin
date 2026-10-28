"""
Backfill has_memo frontmatter — match memos to transcripts and update has_memo: false → true.

A transcript with ``has_memo: false`` flips to true when ANY of these holds
(strongest evidence first; each transcript is counted under the first that applies):
  1. stamp        — a memo's frontmatter ``session_id:`` is the transcript's session id
  2. session_ids  — a memo's frontmatter ``session_ids:`` list contains it (a memo
                    reconstructed from several threads)
  3. suffix       — the 8-hex session suffix of the transcript filename equals the
                    suffix of a memo filename (e.g. 20260128-141500-abc123de)
  4. state        — ~/.memex/state.json → processed_sessions has a memo_generated
                    entry whose first 8 characters equal that suffix

(1) and (2) compare FULL session ids (the transcript's own frontmatter ``session_id``
against ``memex.session_memos.memo_session_map``), so a slug-named memo is found
even though its filename carries no id. 8-hex prefixes alone collide across Codex
threads (UUIDv7 ids share their first 8 hex within a minute), so (3) and (4) are the
weaker, older evidence and give way to a contradiction: when the transcript's full
id is known and every memo (or state key) behind the prefix names a different full
id, the transcript is NOT flipped and is reported as a conflict.

Only the transcript's frontmatter is edited; the body is written back byte for byte
(line endings included).

By default the update is a plain frontmatter edit. ``--obsidian-cli`` asks the
Obsidian CLI to set the property first (falls back to the edit if it can't); it is
opt-in because probing Obsidian costs a 5 s timeout on every run when it is closed.

Usage:
  uv run scripts/backfill_has_memo.py                # dry-run (default)
  uv run scripts/backfill_has_memo.py --apply         # actually write changes
  uv run scripts/backfill_has_memo.py --verbose        # show each match
  uv run scripts/backfill_has_memo.py --apply -v       # apply + verbose
  uv run scripts/backfill_has_memo.py --apply --obsidian-cli
"""

import argparse
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from memex.paths import get_memex_path, get_state_dir
from memex.scripts.reconcile_orphans import _frontmatter
from memex.session_memos import memo_session_map, normalize_session_id

# Evidence kinds, strongest first; the report lists them in this order.
KINDS = ("stamp", "session_ids", "suffix", "state")
_KIND_LABEL = {
    "stamp": "memo session_id",
    "session_ids": "memo session_ids",
    "suffix": "memo filename suffix",
    "state": "state.json",
}

# A transcript's frontmatter is a few hundred bytes; one that doesn't close
# within this many bytes is not trusted (and never edited).
_HEAD_BYTES = 64 * 1024

_HAS_MEMO_FALSE = re.compile(rb"has_memo:\s*(?:false|False)\s*$")
_SESSION_ID_LINE = re.compile(rb"session_id:\s*(.*?)\s*$")


# ---------------------------------------------------------------------------
# Suffix extraction
# ---------------------------------------------------------------------------

def extract_session_suffix(filename: str) -> str | None:
    """Extract 8-char session suffix from timestamp-based filename."""
    match = re.match(r"\d{8}-\d{6}-([a-f0-9]{8})", filename)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Evidence collection
# ---------------------------------------------------------------------------

def collect_memo_suffix_map(vault: Path) -> dict[str, list[str]]:
    """8-char filename suffix → vault-relative paths of the memos that carry it."""
    out: dict[str, list[str]] = {}
    for memo_path in sorted(vault.glob("projects/*/memos/**/*.md")):
        suffix = extract_session_suffix(memo_path.stem)
        if suffix:
            out.setdefault(suffix, []).append(str(memo_path.relative_to(vault)))
    return out


def collect_memo_suffixes(vault: Path) -> set[str]:
    """Scan all memo files and return the set of 8-char session suffixes."""
    return set(collect_memo_suffix_map(vault))


def collect_state_sessions(state_file: Path) -> dict[str, set[str]]:
    """8-char prefix → the full session ids behind it, for sessions with memo_generated_at.

    A key that isn't a full UUID (a legacy 16-char prefix key) contributes the
    prefix with no full id, which can't contradict anything.
    """
    if not state_file.exists():
        return {}
    try:
        data = json.loads(state_file.read_text())
    except (json.JSONDecodeError, ValueError, OSError):
        return {}

    processed = data.get("processed_sessions") if isinstance(data, dict) else None
    if not isinstance(processed, dict):
        return {}
    out: dict[str, set[str]] = {}
    for session_id, info in processed.items():
        if isinstance(info, dict) and "memo_generated_at" in info:
            # State keys are full UUIDs or other IDs; take first 8 chars
            prefix = str(session_id)[:8]
            if re.fullmatch(r"[a-f0-9]{8}", prefix):
                full = normalize_session_id(session_id)
                out.setdefault(prefix, set()).update({full} if full else set())
    return out


def collect_state_suffixes(state_file: Path) -> set[str]:
    """Read state.json and return 8-char prefixes of sessions with memo_generated_at."""
    return set(collect_state_sessions(state_file))


def _contradicts(sid: str | None, claimed: Iterable[set[str]]) -> bool:
    """True when ``sid`` is known and every source behind a prefix match names
    full ids, none of them ``sid``. A source that names no full id (a memo with
    no stamp, a legacy state key) can't contradict, so it keeps the match."""
    claims = list(claimed)
    return sid is not None and bool(claims) and all(ids and sid not in ids for ids in claims)


def _stamp_kind(vault: Path, sid: str, memo_paths: list[str]) -> str:
    """``stamp`` when a memo carries ``sid`` as its ``session_id:``, else ``session_ids``.

    ``memo_session_map`` merges both keys; only matched sessions get re-read to
    tell them apart, so a vault-wide pass parses each stamped memo once more at most.
    """
    for rel in memo_paths:
        fm = _frontmatter(vault / rel)
        if isinstance(fm, dict) and normalize_session_id(fm.get("session_id")) == sid:
            return "stamp"
    return "session_ids"


# ---------------------------------------------------------------------------
# Transcript scanning
# ---------------------------------------------------------------------------

def _frontmatter_block(lines: Iterable[bytes]) -> list[tuple[int, bytes]] | None:
    """``(offset, line)`` for each line between the ``---`` delimiters, or ``None``.

    ``lines`` are ``\\n``-terminated byte lines (a binary file or ``BytesIO``);
    each ``line`` keeps its ending. ``None`` when the file has no frontmatter or the
    block never closes within ``_HEAD_BYTES``. A delimiter is a line that is
    ``---`` after stripping whitespace, as the historical reader had it.
    """
    it = iter(lines)
    first = next(it, None)
    if first is None or first.strip() != b"---":
        return None
    out: list[tuple[int, bytes]] = []
    offset = len(first)
    for line in it:
        if line.strip() == b"---":
            return out
        out.append((offset, line))
        offset += len(line)
        if offset > _HEAD_BYTES:
            return None
    return None


@dataclass(frozen=True)
class TranscriptInfo:
    valid: bool  # a readable file with a closed frontmatter block
    has_memo_false: bool
    session_id: str | None  # normalized full UUID, else None


def scan_transcript(path: Path) -> TranscriptInfo:
    """Read only the frontmatter: ``has_memo: false`` and the full session id."""
    try:
        with open(path, "rb") as fh:
            block = _frontmatter_block(fh)
    except OSError:
        block = None
    if block is None:
        return TranscriptInfo(False, False, None)
    false_flag = False
    sid: str | None = None
    for _, line in block:
        stripped = line.rstrip(b"\r\n")
        if _HAS_MEMO_FALSE.match(stripped):
            false_flag = True
        elif sid is None and stripped.startswith(b"session_id:"):
            m = _SESSION_ID_LINE.match(stripped)
            if m:
                raw = m.group(1).decode("utf-8", errors="ignore").strip("\"'")
                sid = normalize_session_id(raw)
    return TranscriptInfo(True, false_flag, sid)


def has_memo_false_in_frontmatter(path: Path) -> bool:
    """Check if the transcript's YAML frontmatter contains has_memo: false."""
    return scan_transcript(path).has_memo_false


# ---------------------------------------------------------------------------
# Updating
# ---------------------------------------------------------------------------

def _update_via_cli(path: Path, cli, vault: Path) -> bool:
    """Set has_memo: true via Obsidian CLI property:set.

    Uses vault-relative path and verifies the change took effect.
    """
    try:
        rel_path = str(path.relative_to(vault))
        cli.property_set("has_memo", "true", path=rel_path)
        return not has_memo_false_in_frontmatter(path)
    except Exception:
        return False


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    """Replace ``path`` (or the file a symlink there points to) with ``data`` via a
    same-directory temp file; the file mode is kept."""
    target = Path(os.path.realpath(path))
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name[:200]}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        shutil.copymode(target, tmp)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _flip_has_memo_line(path: Path) -> bool:
    """Rewrite the frontmatter's ``has_memo: false`` line as ``has_memo: true``.

    Only that one line changes; every other byte of the file (body, line
    endings) is written back as read. False when there is nothing to change.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return False
    block = _frontmatter_block(io.BytesIO(data))
    if not block:
        return False
    for offset, line in block:
        body = line.rstrip(b"\r\n")
        if _HAS_MEMO_FALSE.match(body):
            new_line = b"has_memo: true" + line[len(body):]
            try:
                _write_bytes_atomic(path, data[:offset] + new_line + data[offset + len(line):])
            except OSError:
                return False
            return True
    return False


def update_frontmatter(path: Path, cli=None, vault: Path = None) -> bool:
    """Set has_memo: true. Uses the Obsidian CLI when one is given, else (or on
    failure) the plain frontmatter edit."""
    if cli is not None and vault is not None:
        if _update_via_cli(path, cli, vault):
            return True
    return _flip_has_memo_line(path)


def _load_obsidian_cli():
    """The Obsidian CLI wrapper, or ``None`` when it can't be used. Opt-in only."""
    try:
        # ObsidianCLI lives ONLY at vault-root scripts/obsidian_cli.py — it is
        # not a package module. Under `memex backfill memos` the bare name is
        # not on sys.path (before v0.16.4 the bare import silently fell to the
        # regex fallback on every packaged invocation), so put the vault's
        # scripts dir on the path first — same pattern as
        # crystallization_check.py.
        sys.path.insert(0, str(get_memex_path() / "scripts"))
        from obsidian_cli import ObsidianCLI
        _cli = ObsidianCLI(vault="memex", timeout=5)
        if _cli.is_available():
            return _cli
    except Exception:
        pass
    print("Obsidian CLI: unavailable (plain frontmatter edit)", file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def backfill(
    vault: Path,
    state_file: Path,
    *,
    apply: bool = False,
    verbose: bool = False,
    use_obsidian_cli: bool = False,
) -> dict:
    """Flip ``has_memo: false`` → true where a memo covers the transcript.

    Returns counts: ``updated`` (would-update on a dry run), ``by_kind`` (per
    evidence kind), ``already_correct`` (``has_memo`` is not false),
    ``unmatched``, ``conflicts`` (a prefix match whose memo/state key names a
    different full id; not flipped), ``no_identifier`` (no filename suffix and
    no usable session id), ``unreadable`` (no readable, closed frontmatter) and
    ``failed`` (apply only: evidence found, file not changed).
    """
    suffix_memos = collect_memo_suffix_map(vault)
    state_sessions = collect_state_sessions(state_file)
    stamps = memo_session_map(vault)
    memo_stamp_ids: dict[str, set[str]] = {}
    for sid, paths in stamps.items():
        for rel in paths:
            memo_stamp_ids.setdefault(rel, set()).add(sid)

    if verbose:
        print(f"Memo session ids (session_id/session_ids): {len(stamps)}")
        print(f"Memo file suffixes: {len(suffix_memos)}")
        print(f"State.json suffixes: {len(state_sessions)}")
        print()

    obs_cli = _load_obsidian_cli() if (use_obsidian_cli and apply) else None

    by_kind: Counter[str] = Counter()
    already_correct = unmatched = conflicts = no_identifier = unreadable = failed = 0

    for tx_path in sorted(vault.glob("projects/*/transcripts/*.md")):
        info = scan_transcript(tx_path)
        if not info.valid:
            unreadable += 1
            continue
        if not info.has_memo_false:
            already_correct += 1
            continue

        sid = info.session_id
        suffix = extract_session_suffix(tx_path.name)
        kind = detail = None
        conflict = False
        if sid and sid in stamps:
            kind = _stamp_kind(vault, sid, stamps[sid])
            detail = f"{sid} in {stamps[sid][0]}"
        if kind is None and suffix and suffix in suffix_memos:
            memos = suffix_memos[suffix]
            if _contradicts(sid, (memo_stamp_ids.get(m, set()) for m in memos)):
                conflict = True
            else:
                kind, detail = "suffix", f"{suffix} in {memos[0]}"
        if kind is None and suffix and suffix in state_sessions:
            if _contradicts(sid, [state_sessions[suffix]]):
                conflict = True
            else:
                kind, detail = "state", suffix

        rel = tx_path.relative_to(vault)
        if kind is None:
            if conflict:
                conflicts += 1
                if verbose:
                    print(f"  CONFLICT {rel}  (suffix {suffix} belongs to a memo for a different session id)")
            elif suffix is None and sid is None:
                no_identifier += 1
            else:
                unmatched += 1
                if verbose:
                    print(f"  SKIP   {rel}  (no matching memo)")
            continue

        if apply and not update_frontmatter(tx_path, cli=obs_cli, vault=vault):
            failed += 1
            print(f"  FAILED {rel}  (has_memo line not rewritten)", file=sys.stderr)
            continue
        by_kind[kind] += 1
        if verbose:
            print(f"  MATCH  {rel}  (via {_KIND_LABEL[kind]}: {detail})")

    return {
        "updated": sum(by_kind.values()),
        "by_kind": {k: by_kind[k] for k in KINDS},
        "already_correct": already_correct,
        "unmatched": unmatched,
        "conflicts": conflicts,
        "no_identifier": no_identifier,
        "unreadable": unreadable,
        "failed": failed,
    }


def _run() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill has_memo in transcript frontmatter by matching memos to transcripts",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write changes (default is dry-run)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print each match found",
    )
    parser.add_argument(
        "--obsidian-cli",
        action="store_true",
        help="With --apply: set the property through the Obsidian CLI first "
        "(opt-in; costs a 5 s timeout per run when Obsidian is closed)",
    )
    args = parser.parse_args()

    result = backfill(
        get_memex_path(),
        get_state_dir() / "state.json",
        apply=args.apply,
        verbose=args.verbose,
        use_obsidian_cli=args.obsidian_cli,
    )

    action = "Updated" if args.apply else "Would update"
    print(f"\n{action}: {result['updated']}")
    for kind in KINDS:
        print(f"  via {_KIND_LABEL[kind]}: {result['by_kind'][kind]}")
    print(f"Already correct: {result['already_correct']}")
    print(f"Unmatched: {result['unmatched']}")
    print(f"Conflicting session id (not flipped): {result['conflicts']}")
    print(f"No identifier: {result['no_identifier']}")
    print(f"Unreadable or no frontmatter: {result['unreadable']}")
    if result["failed"]:
        print(f"Failed: {result['failed']}")

    if not args.apply and result["updated"] > 0:
        print("\nRun with --apply to write changes.")
    return 1 if result["failed"] else 0


def main():
    try:
        code = _run()
        if code:
            sys.exit(code)
    except SystemExit:
        raise
    except FileNotFoundError as exc:
        print(f"Error: {exc}\nFix: Verify the referenced transcript exists and rerun the command.", file=sys.stderr)
        sys.exit(2)
    except sqlite3.OperationalError as exc:
        print(f"Error: {exc}\nFix: memex index rebuild --full", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
