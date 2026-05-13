"""Resolve redirect_to chains for topic signal routing.

Topics can declare ``redirect_to:`` in frontmatter to route signals to a
canonical replacement (after a merge or archive). The target may be:

* A bare topic slug (e.g. ``embedding-models``) — resolved as
  ``topics/<slug>.md``.
* A vault-relative path with ``/`` (e.g.
  ``projects/clawd-world/_project.md``) — resolved as ``<vault>/<path>``
  directly.

``resolve`` returns the destination as a vault-relative identifier
(without the trailing ``.md``) — a bare topic slug when the destination
lives in ``topics/`` and a vault-relative path otherwise. Returns
``None`` when the chain terminates at a non-redirecting archive, hits a
missing file mid-chain, exceeds ``max_hops``, or contains a cycle.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional

MAX_HOPS = 5

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
_REDIRECT_RE = re.compile(r"^redirect_to:\s*(.+?)\s*$", re.MULTILINE)
_STATUS_ARCHIVED_RE = re.compile(r"^status:\s*archived\s*$", re.MULTILINE)


def _clean_target(raw: str) -> str:
    """Strip quotes and inline comments from a redirect_to value.

    Order matters: strip surrounding quotes BEFORE stripping inline
    comments, so a quoted target containing ``#`` (e.g. ``"foo#bar"``)
    survives intact. The inline-comment pattern requires leading
    whitespace (``\\s+#``) so slugs that legitimately contain ``#``
    (anchor-style) aren't truncated.
    """
    target = raw.strip()
    # 1. Strip matching surrounding quotes (single or double)
    if len(target) >= 2 and target[0] == target[-1] and target[0] in ('"', "'"):
        target = target[1:-1]
    # 2. Strip inline `  # comment` — require leading whitespace
    target = re.sub(r"\s+#.*$", "", target).strip()
    return target


def _resolve_path(vault: Path, target: str) -> Optional[Path]:
    """Resolve a redirect target to an absolute path INSIDE the vault.

    Returns ``None`` if the resolved path escapes the vault root
    (path traversal) or cannot be resolved.
    """
    if "/" in target:
        # Vault-relative path; auto-append .md if missing (mirror slug behavior).
        if not target.endswith(".md"):
            target += ".md"
        candidate = vault / target
    else:
        candidate = vault / "topics" / f"{target}.md"
    # Containment check: resolved path must be within vault root.
    try:
        resolved = candidate.resolve()
        vault_resolved = vault.resolve()
        resolved.relative_to(vault_resolved)  # raises ValueError if escapes
    except (ValueError, OSError):
        return None
    return candidate


def _read_frontmatter(path: Path) -> Optional[str]:
    """Return frontmatter content (between ``---`` delimiters), or ``None``
    when the file is missing or unreadable. Returns an empty string when
    the file exists but has no frontmatter."""
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return ""
    return m.group(1)


def resolve(
    vault: Path,
    slug_or_path: str,
    *,
    max_hops: int = MAX_HOPS,
) -> Optional[str]:
    """Follow a ``redirect_to`` chain starting at ``slug_or_path``.

    Returns the final destination as a vault-relative identifier without
    the ``.md`` suffix (a bare slug when the destination is in
    ``topics/``, otherwise the path). Returns ``None`` on any failure
    mode and emits a stderr warning for cycles, missing mid-chain
    targets, archived terminal stubs, and over-length chains.
    """
    visited: list[str] = []
    current = _clean_target(slug_or_path)

    for _ in range(max_hops + 1):
        path = _resolve_path(vault, current)
        if path is None:
            print(
                f"WARN: redirect target escapes vault or unresolvable: "
                f"{current}",
                file=sys.stderr,
            )
            return None
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key in visited:
            chain = " -> ".join(visited + [key])
            print(f"WARN: redirect cycle detected: {chain}", file=sys.stderr)
            return None
        visited.append(key)

        fm = _read_frontmatter(path)
        if fm is None:
            if len(visited) == 1:
                # First hop: caller passed a slug that doesn't exist.
                # Silently signal "skip" — the bash caller treats empty
                # stdout the same as a non-existent topic file.
                return None
            print(
                f"WARN: redirect target missing: {current} "
                f"(chain: {' -> '.join(visited)})",
                file=sys.stderr,
            )
            return None

        m = _REDIRECT_RE.search(fm)
        if m:
            next_target = _clean_target(m.group(1))
            if next_target:
                current = next_target
                continue
            # Empty redirect_to value — fall through to terminal handling.

        if _STATUS_ARCHIVED_RE.search(fm):
            print(
                f"WARN: topic '{current}' is archived with no redirect_to "
                f"— skipping signal",
                file=sys.stderr,
            )
            return None

        # Terminal: a real destination. Return as a bare slug when the
        # file lives under topics/<slug>.md; otherwise as a vault-relative
        # path without the .md suffix.
        try:
            rel = path.relative_to(vault)
        except ValueError:
            return current
        parts = rel.parts
        if len(parts) == 2 and parts[0] == "topics" and rel.suffix == ".md":
            return rel.stem
        if rel.suffix == ".md":
            return str(rel.with_suffix(""))
        return str(rel)

    print(
        f"WARN: redirect chain exceeded {max_hops} hops starting at "
        f"{slug_or_path}",
        file=sys.stderr,
    )
    return None


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Resolve redirect_to chains for topic signal routing.",
    )
    parser.add_argument(
        "slug",
        help="Topic slug or vault-relative path to resolve",
    )
    parser.add_argument(
        "--vault",
        default=None,
        help="Vault path (default: resolved from memex config)",
    )
    args = parser.parse_args()

    if args.vault:
        vault = Path(args.vault).expanduser().resolve()
    else:
        try:
            from memex.paths import get_memex_path

            vault = get_memex_path()
        except Exception as exc:
            print(f"Error: vault path not resolved ({exc})", file=sys.stderr)
            sys.exit(1)

    if not vault or not vault.exists():
        print(f"Error: vault path does not exist: {vault}", file=sys.stderr)
        sys.exit(1)

    result = resolve(vault, args.slug)
    if result is None:
        sys.exit(1)
    print(result)
    sys.exit(0)


if __name__ == "__main__":
    main()
