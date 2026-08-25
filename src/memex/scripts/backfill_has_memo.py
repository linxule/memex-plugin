"""
Backfill has_memo frontmatter — match memos to transcripts and update has_memo: false → true.

Matching strategy:
  1. Extract 8-char hex session suffix from filenames (e.g., 20260128-141500-abc123de → abc123de)
  2. Build a set of suffixes from all memo filenames across projects
  3. Also check ~/.memex/state.json → processed_sessions for memo_generated entries
  4. For each transcript with has_memo: false, check if its suffix matches either source

Usage:
  uv run scripts/backfill_has_memo.py                # dry-run (default)
  uv run scripts/backfill_has_memo.py --apply         # actually write changes
  uv run scripts/backfill_has_memo.py --verbose        # show each match
  uv run scripts/backfill_has_memo.py --apply -v       # apply + verbose
"""

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path


from memex.paths import get_memex_path, get_state_dir

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

VAULT: Path | None = None
STATE_FILE: Path | None = None


# ---------------------------------------------------------------------------
# Suffix extraction
# ---------------------------------------------------------------------------

def extract_session_suffix(filename: str) -> str | None:
    """Extract 8-char session suffix from timestamp-based filename."""
    match = re.match(r"\d{8}-\d{6}-([a-f0-9]{8})", filename)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Suffix collection
# ---------------------------------------------------------------------------

def collect_memo_suffixes() -> set[str]:
    """Scan all memo files and return the set of 8-char session suffixes."""
    suffixes: set[str] = set()
    for memo_path in VAULT.glob("projects/*/memos/*.md"):
        suffix = extract_session_suffix(memo_path.stem)
        if suffix:
            suffixes.add(suffix)
    return suffixes


def collect_state_suffixes() -> set[str]:
    """Read state.json and return 8-char prefixes of sessions with memo_generated_at."""
    if not STATE_FILE.exists():
        return set()
    try:
        data = json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, ValueError):
        return set()

    suffixes: set[str] = set()
    for session_id, info in data.get("processed_sessions", {}).items():
        if "memo_generated_at" in info:
            # State keys are full UUIDs or other IDs; take first 8 chars
            prefix = session_id[:8]
            if re.fullmatch(r"[a-f0-9]{8}", prefix):
                suffixes.add(prefix)
    return suffixes


# ---------------------------------------------------------------------------
# Transcript scanning
# ---------------------------------------------------------------------------

def has_memo_false_in_frontmatter(path: Path) -> bool:
    """Check if the transcript's YAML frontmatter contains has_memo: false.

    Reads only the frontmatter block for efficiency.
    """
    try:
        with open(path) as f:
            first_line = f.readline()
            if first_line.strip() != "---":
                return False
            for line in f:
                if line.strip() == "---":
                    break
                if re.match(r"has_memo:\s*(false|False)\s*$", line):
                    return True
        return False
    except OSError:
        return False


def _update_via_cli(path: Path, cli, vault: Path) -> bool:
    """Set has_memo: true via Obsidian CLI property:set (preferred).

    Uses vault-relative path and verifies the change took effect.
    """
    try:
        rel_path = str(path.relative_to(vault))
        cli.property_set("has_memo", "true", path=rel_path)
        return not has_memo_false_in_frontmatter(path)
    except Exception:
        return False


def _update_via_regex(path: Path) -> bool:
    """Replace has_memo: false → has_memo: true via regex (fallback)."""
    text = path.read_text()

    if not text.startswith("---"):
        return False

    end = text.find("\n---", 3)
    if end == -1:
        return False

    frontmatter = text[: end + 4]
    body = text[end + 4 :]

    new_frontmatter = re.sub(
        r"has_memo:\s*(false|False)",
        "has_memo: true",
        frontmatter,
    )

    if new_frontmatter == frontmatter:
        return False

    path.write_text(new_frontmatter + body)
    return True


def update_frontmatter(path: Path, cli=None, vault: Path = None) -> bool:
    """Set has_memo: true. Uses Obsidian CLI when available, regex fallback."""
    if cli is not None and vault is not None:
        if _update_via_cli(path, cli, vault):
            return True
    return _update_via_regex(path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _run() -> None:
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
    args = parser.parse_args()

    dry_run = not args.apply

    global VAULT, STATE_FILE
    VAULT = get_memex_path()
    STATE_FILE = get_state_dir() / "state.json"

    # 1. Build suffix sets from memo files and state.json
    memo_suffixes = collect_memo_suffixes()
    state_suffixes = collect_state_suffixes()
    all_suffixes = memo_suffixes | state_suffixes

    if args.verbose:
        print(f"Memo file suffixes: {len(memo_suffixes)}")
        print(f"State.json suffixes: {len(state_suffixes)}")
        print(f"Combined unique: {len(all_suffixes)}")
        print()

    # Check CLI availability once, reuse instance for all updates
    obs_cli = None
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
            obs_cli = _cli
        if args.verbose:
            print(f"Obsidian CLI: {'available' if obs_cli else 'unavailable (regex fallback)'}")
    except Exception:
        if args.verbose:
            print("Obsidian CLI: unavailable (regex fallback)")

    # 2. Walk all transcripts and check for matches
    transcripts = sorted(VAULT.glob("projects/*/transcripts/*.md"))

    updated = 0
    already_correct = 0
    unmatched = 0
    skipped_no_suffix = 0

    for tx_path in transcripts:
        suffix = extract_session_suffix(tx_path.name)
        if not suffix:
            skipped_no_suffix += 1
            continue

        if not has_memo_false_in_frontmatter(tx_path):
            already_correct += 1
            continue

        if suffix in all_suffixes:
            source = "memo file" if suffix in memo_suffixes else "state.json"
            if args.verbose:
                rel = tx_path.relative_to(VAULT)
                print(f"  MATCH  {rel}  (suffix={suffix}, via {source})")

            if not dry_run:
                update_frontmatter(tx_path, cli=obs_cli, vault=VAULT)

            updated += 1
        else:
            unmatched += 1
            if args.verbose:
                rel = tx_path.relative_to(VAULT)
                print(f"  SKIP   {rel}  (suffix={suffix}, no matching memo)")

    action = "Would update" if dry_run else "Updated"
    print(f"\n{action}: {updated}")
    print(f"Already correct: {already_correct}")
    print(f"Unmatched: {unmatched}")
    print(f"No suffix: {skipped_no_suffix}")

    if dry_run and updated > 0:
        print("\nRun with --apply to write changes.")


def main():
    try:
        _run()
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
