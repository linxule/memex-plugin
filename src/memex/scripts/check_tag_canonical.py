#!/usr/bin/env python3
"""Scan topic tags against `_meta/tag-taxonomy.md` for canonical-tag drift.

Replaces the inline `uv run --with pyyaml python <<'PYEOF'` heredoc used four
times across the 2026-05-14 tag-normalization sweep. Reads the taxonomy file,
extracts both canonical tags and the provisional → canonical mapping table,
then scans every topic's frontmatter tags and reports drift.

Exit codes:
    0 — all topic tags are canonical
    1 — at least one tag has no mapping (escalation needed)
    2 — at least one tag is a non-canonical mapping (mechanical normalization possible)
    3 — taxonomy file not readable or no canonical tags detected

Usage:
    memex check-tags                     # scan vault topics/
    memex check-tags --json              # machine-readable output
    memex check-tags --topics-dir <dir>  # scan a different topics directory

Designed to be reusable as a pre-commit hook (point at staged topics).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterable

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "error: PyYAML required. Run via `uv run --with pyyaml python -m memex.scripts.check_tag_canonical`\n"
    )
    sys.exit(3)

# ── taxonomy parsing ─────────────────────────────────────────────────


def _vault_root() -> Path:
    """Resolve the vault root by walking up from this file."""
    return Path(__file__).resolve().parent.parent.parent.parent


def parse_taxonomy(taxonomy_path: Path) -> tuple[set[str], dict[str, str]]:
    """Return (canonical_tags, mapping) where mapping[provisional] = canonical.

    Canonical tags come from the `## Canonical Tags` section's table rows.
    Mappings come from the `## Provisional → Canonical Mapping Reference` table.
    """
    if not taxonomy_path.is_file():
        raise FileNotFoundError(f"Taxonomy file not found: {taxonomy_path}")

    text = taxonomy_path.read_text()

    canonical: set[str] = set()
    mapping: dict[str, str] = {}

    # Find ## Canonical Tags section
    canonical_section = re.search(
        r"## Canonical Tags\s*\n(.*?)(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if canonical_section:
        # Each row: `| \`tag-name\` | ... |`
        for m in re.finditer(r"^\|\s*`([a-z][a-z0-9-]*)`\s*\|", canonical_section.group(1), re.MULTILINE):
            canonical.add(m.group(1))

    # Find ## Provisional → Canonical Mapping Reference section (or similar header)
    mapping_section = re.search(
        r"## .*?(?:Mapping|Tag Mapping).*?\n(.*?)(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if mapping_section:
        # Each row: `| \`prov-tag\` | \`canonical-tag\` |`  (also `(remove ...)` for drops)
        for m in re.finditer(
            r"^\|\s*`([a-z][a-z0-9-]*)`\s*\|\s*(?:`([a-z][a-z0-9-]*)`|\(([^)]+)\))\s*\|",
            mapping_section.group(1),
            re.MULTILINE,
        ):
            prov, canon, drop_note = m.group(1), m.group(2), m.group(3)
            if canon:
                mapping[prov] = canon
            elif drop_note and "remove" in drop_note.lower():
                mapping[prov] = "__DROP__"

    return canonical, mapping


# ── topic scanning ───────────────────────────────────────────────────


def _read_frontmatter(path: Path) -> dict | None:
    """Extract frontmatter as a dict; return None if absent or invalid."""
    try:
        text = path.read_text()
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    end_marker = text.find("\n---", 4)
    if end_marker == -1:
        return None
    try:
        data = yaml.safe_load(text[4:end_marker])
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def iter_topic_files(topics_dir: Path) -> Iterable[Path]:
    """Yield every .md file in topics_dir, recursively."""
    if not topics_dir.is_dir():
        return
    yield from sorted(topics_dir.rglob("*.md"))


def scan_topics(
    topics_dir: Path,
    canonical: set[str],
    mapping: dict[str, str],
) -> dict:
    """Scan topic files; return a report dict.

    Report shape:
        {
            "clean": [(path, [tags])],            # all tags canonical
            "mappable": [(path, [(tag, canon)])], # has provisional → canonical mappings
            "unmapped": [(path, [tag])],          # tags with no canonical mapping
            "archived": [path],                   # status: archived (excluded from check)
            "no_tags": [path],                    # no tags field
            "totals": {...},
        }
    """
    clean: list[tuple[str, list[str]]] = []
    mappable: list[tuple[str, list[tuple[str, str]]]] = []
    unmapped: list[tuple[str, list[str]]] = []
    archived: list[str] = []
    no_tags: list[str] = []

    for path in iter_topic_files(topics_dir):
        rel = str(path.relative_to(topics_dir.parent))
        fm = _read_frontmatter(path)
        if fm is None:
            no_tags.append(rel)
            continue
        if fm.get("status") == "archived":
            archived.append(rel)
            continue
        tags = fm.get("tags") or []
        if not isinstance(tags, list) or not tags:
            no_tags.append(rel)
            continue

        bad_unmapped = []
        bad_mapped = []
        for t in tags:
            if not isinstance(t, str):
                continue
            if t in canonical:
                continue
            if t in mapping and mapping[t] != "__DROP__":
                bad_mapped.append((t, mapping[t]))
            elif t in mapping and mapping[t] == "__DROP__":
                bad_unmapped.append(t)  # explicit drop → unmapped (needs human review)
            else:
                bad_unmapped.append(t)

        if bad_unmapped:
            unmapped.append((rel, bad_unmapped))
        elif bad_mapped:
            mappable.append((rel, bad_mapped))
        else:
            clean.append((rel, list(tags)))

    return {
        "clean": clean,
        "mappable": mappable,
        "unmapped": unmapped,
        "archived": archived,
        "no_tags": no_tags,
        "totals": {
            "topics_scanned": len(clean) + len(mappable) + len(unmapped) + len(no_tags),
            "clean": len(clean),
            "mappable": len(mappable),
            "unmapped": len(unmapped),
            "archived": len(archived),
            "no_tags": len(no_tags),
        },
    }


# ── output rendering ─────────────────────────────────────────────────


def render_text(report: dict) -> str:
    out = []
    t = report["totals"]
    out.append(
        f"Scanned {t['topics_scanned']} topics: "
        f"{t['clean']} clean, {t['mappable']} mappable, {t['unmapped']} unmapped, "
        f"{t['no_tags']} no-tags, {t['archived']} archived (skipped)"
    )
    out.append("")
    if report["unmapped"]:
        out.append("UNMAPPED (no canonical mapping — needs human triage):")
        for path, tags in report["unmapped"]:
            out.append(f"  {path}")
            for t in tags:
                out.append(f"    - {t}")
        out.append("")
    if report["mappable"]:
        out.append("MAPPABLE (mechanical normalization possible):")
        for path, pairs in report["mappable"]:
            out.append(f"  {path}")
            for prov, canon in pairs:
                out.append(f"    - {prov} → {canon}")
        out.append("")
    if not report["unmapped"] and not report["mappable"]:
        out.append("ALL CLEAN — every topic tag is canonical.")
    return "\n".join(out)


def render_json(report: dict) -> str:
    return json.dumps(report, indent=2, default=str)


# ── entrypoint ───────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--taxonomy",
        type=Path,
        default=_vault_root() / "_meta" / "tag-taxonomy.md",
        help="Path to tag taxonomy file (default: <vault>/_meta/tag-taxonomy.md)",
    )
    parser.add_argument(
        "--topics-dir",
        type=Path,
        default=_vault_root() / "topics",
        help="Path to topics directory (default: <vault>/topics)",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    args = parser.parse_args(argv)

    try:
        canonical, mapping = parse_taxonomy(args.taxonomy)
    except FileNotFoundError as e:
        sys.stderr.write(f"error: {e}\n")
        return 3

    if not canonical:
        sys.stderr.write(
            f"error: no canonical tags parsed from {args.taxonomy}. "
            "Check the `## Canonical Tags` section format.\n"
        )
        return 3

    report = scan_topics(args.topics_dir, canonical, mapping)

    if args.json:
        print(render_json(report))
    else:
        print(render_text(report))

    if report["totals"]["unmapped"]:
        return 1
    if report["totals"]["mappable"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
