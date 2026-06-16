"""
Crystallization Check — Analyze vault's unresolved wikilinks for maturation readiness.

Uses Obsidian CLI (1.12.5+) native `unresolved verbose format=json` for link
aggregation, with eval fallback for older versions. Native `aliases` command
for alias-aware filtering.

Maturation tiers:
  OVERDUE  — 5+ references, should have been crystallized already
  READY    — 3+ references across 2+ projects
  MATURING — 2+ references (single-project or low spread)
  SEEDLING — 1 reference (leave it, may grow)

Prefers Obsidian (most precise — uses its live metadataCache). When Obsidian
is not running, falls back to a filesystem scan that resolves [[links]] against
markdown filename stems + frontmatter aliases (Obsidian's resolution rules,
minus heading/block awareness) — sufficient for concept-level ghost-node
detection and safe for headless/cron use.
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path


from memex.paths import get_memex_path, get_state_dir

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

VAULT: Path | None = None
STATE_FILE: Path | None = None

# Template placeholders and noise to filter out
NOISE_LITERALS = {
    "topic-name-kebab-case",
    "another-topic",
    "existing-topic",
    "suggested-new-concept",
    "new-concept",
    "project-name",
    "detected-project",
    "detected project",
    "related-concept",
    "topic",
    "topic1",
    "topic2",
    "topic-name",
    "iso date",
    "decision",
    "title",
    "link",
    "session-summary",
}

NOISE_REGEXES = [
    re.compile(r"^\?"),           # ?suggested-concept breadcrumbs
    re.compile(r"/"),             # full path refs (projects/foo/bar)
    re.compile(r"^\d{8}"),        # date-prefixed filenames (20260214-...)
    re.compile(r"^\d{4}-\d{2}-\d{2}"),  # ISO-date-prefixed memo links (2026-02-16-...)
    re.compile(r"\.md$"),         # explicit .md file links, not concepts
    re.compile(r"^https?://"),    # URLs accidentally wikified
    re.compile(r"^@"),            # @handles (social, not concepts)
    re.compile(r"^\d+$"),         # bare numbers
    re.compile(r"^[A-Z][A-Z0-9]{0,5}$"),  # short ALL-CAPS acronyms (X, MCP, SSRN); real topics are kebab-case
]


# ---------------------------------------------------------------------------
# Filesystem fallback (used when Obsidian is not running)
# ---------------------------------------------------------------------------

WIKILINK_RE = re.compile(r"\[\[([^\]]+?)\]\]")
_ALIAS_LINE_RE = re.compile(r"^aliases:\s*(.*)$")
_FALLBACK_SKIP_DIRS = ("/.obsidian/", "/.git/", "/node_modules/", "/.venv/")

# Code/escape spans must be stripped before wikilink scanning. The markdown
# fallback otherwise mis-parses TOML ``[[section]]`` headers (wrangler.toml),
# bash ``if [[ ... ]]`` conditionals, and ANSI terminal dumps inside code
# blocks as ghost-node wikilinks. Obsidian's metadataCache parser ignores code
# spans; this mirrors that so the fallback doesn't over-report phantom nodes.
_FENCED_CODE_RE = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _strip_code_spans(text: str) -> str:
    """Remove fenced/inline code and ANSI escapes before wikilink scanning."""
    text = _ANSI_CSI_RE.sub("", text)
    text = _FENCED_CODE_RE.sub("", text)
    text = _INLINE_CODE_RE.sub("", text)
    return text


_STATUS_ARCHIVED_RE = re.compile(r"^status:\s*archived\b", re.MULTILINE | re.IGNORECASE)


def _is_archived(text: str) -> bool:
    """True if the file's YAML frontmatter declares ``status: archived``.

    Archived files remain valid wikilink TARGETS (their stems/aliases stay in
    the resolvable set, mirroring Obsidian — the file still exists on disk) but
    are skipped as link SOURCES, so dead or duplicated notes don't cast
    ghost-node votes for concepts that should not crystallize.
    """
    if not text.startswith("---"):
        return False
    end = text.find("\n---", 3)
    head = text if end == -1 else text[:end]
    return bool(_STATUS_ARCHIVED_RE.search(head))


def _vault_markdown_files(vault: Path) -> list[Path]:
    """All vault markdown files, excluding plumbing/config dirs."""
    return [
        p
        for p in vault.rglob("*.md")
        if not any(skip in str(p) for skip in _FALLBACK_SKIP_DIRS)
    ]


def _read_frontmatter_aliases(path: Path) -> list[str]:
    """Cheap frontmatter alias reader (inline `[a, b]` and block `- a` forms)."""
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    if not lines or lines[0].strip() != "---":
        return []
    out: list[str] = []
    in_block = False
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = _ALIAS_LINE_RE.match(line)
        if m:
            rest = m.group(1).strip()
            if rest.startswith("["):
                out.extend(
                    a.strip().strip("\"'")
                    for a in rest.strip("[]").split(",")
                    if a.strip()
                )
                in_block = False
            else:
                in_block = not rest  # bare `aliases:` → YAML list follows
            continue
        if in_block:
            s = line.strip()
            if s.startswith("- "):
                out.append(s[2:].strip().strip("\"'"))
            elif s and not s.startswith("#"):
                in_block = False
    return [a for a in out if a]


def scan_unresolved_via_markdown(
    vault: Path,
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Filesystem fallback for when Obsidian isn't running.

    Resolves [[links]] against markdown filename stems + frontmatter aliases
    (mirrors Obsidian's wikilink resolution, minus heading/block awareness, minus
    space/underscore normalization, and ignoring frontmatter ``title:``).
    Returns ``(unresolved, alias_map)`` where ``unresolved`` maps each
    unresolved link to its source file list (vault-relative) and already
    excludes filename/alias-resolvable links. Less precise than Obsidian's
    metadataCache but exactly right for concept-level ghost-node detection: every
    unmirrored case over-reports (a real link looks like a ghost), never the
    reverse, so the degraded mode is safe.
    """
    files = _vault_markdown_files(vault)
    stems: set[str] = set()
    alias_map: dict[str, str] = {}
    for f in files:
        stems.add(f.stem.lower())
        for alias in _read_frontmatter_aliases(f):
            alias_map[alias.lower()] = str(f.relative_to(vault))
    resolvable = stems | set(alias_map.keys())

    unresolved: dict[str, list[str]] = {}
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = str(f.relative_to(vault))
        if _is_archived(text):
            continue  # valid TARGET (stem kept above), but not a vote-casting SOURCE
        for match in WIKILINK_RE.finditer(_strip_code_spans(text)):
            target = match.group(1).split("|", 1)[0].split("#", 1)[0].strip()
            if not target or target.lower() in resolvable:
                continue
            sources = unresolved.setdefault(target, [])
            if rel not in sources:
                sources.append(rel)
    return unresolved, alias_map


# ---------------------------------------------------------------------------
# Obsidian interface
# ---------------------------------------------------------------------------

def get_obsidian_cli():
    """Import and return an ObsidianCLI instance."""
    vault = get_memex_path()
    sys.path.insert(0, str(vault / "scripts"))
    from obsidian_cli import ObsidianCLI

    return ObsidianCLI()  # defaults to vault="memex"


def _parse_native_unresolved(lines: list[str]) -> dict[str, list[str]] | None:
    """Parse native `unresolved verbose format=json` output.

    Expected format: JSON array of {"link": "...", "count": "...", "sources": "path1\\npath2"}.
    Returns {link_text: [source_files]} or None if parsing fails.
    """
    if not lines:
        return None
    raw = "\n".join(lines).strip()
    if not raw:
        return None
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(entries, list) or not entries:
        return None
    result: dict[str, list[str]] = {}
    for entry in entries:
        link = entry.get("link", "")
        sources = entry.get("sources", "")
        if not link:
            continue
        source_list = [s for s in sources.split("\n") if s.strip()]
        if source_list:
            result[link] = source_list
    return result


def _get_unresolved_via_eval(cli) -> dict[str, list[str]] | None:
    """Fallback: get unresolved links via metadataCache eval."""
    code = (
        "JSON.stringify("
        "Object.entries(app.metadataCache.unresolvedLinks)"
        ".reduce((acc,[f,links])=>{"
        "Object.keys(links).forEach(l=>{"
        "if(!acc[l])acc[l]=[];"
        "acc[l].push(f)"
        "});return acc},{}))"
    )
    result = cli.eval_js(code)
    if not result:
        return None
    try:
        return json.loads(result)
    except json.JSONDecodeError:
        return None


def get_unresolved_links(cli) -> dict[str, list[str]]:
    """Get unresolved links as {link_text: [source_files]}.

    Tries native `unresolved verbose format=json` first (1.12.5+),
    falls back to eval-based metadataCache approach.
    """
    # Try native CLI first — faster, no eval injection
    native = _parse_native_unresolved(cli.unresolved(verbose=True, fmt="json"))
    if native:
        return native

    # Fallback to eval-based approach
    fallback = _get_unresolved_via_eval(cli)
    if fallback:
        return fallback

    print(
        "Error: Could not get unresolved links from Obsidian. Is Obsidian running?",
        file=sys.stderr,
    )
    sys.exit(1)


def get_alias_map(cli) -> dict[str, str]:
    """Build a map of {alias_lowercase: file_path} from all vault files.

    Uses native `aliases verbose` command (1.12.2+) which returns
    alias→file_path mappings. Falls back to eval if native command
    returns empty. Also includes filename stems since Obsidian resolves
    wikilinks against both aliases and filenames.
    """
    # Try native command first (faster, no injection risk)
    mapping = cli.alias_map()
    if mapping:
        return mapping

    # Fallback to eval for older versions
    code = (
        "JSON.stringify("
        "app.vault.getMarkdownFiles().reduce((acc,f)=>{"
        "const a=app.metadataCache.getCache(f.path)?.frontmatter?.aliases||[];"
        "a.forEach(x=>{acc[String(x).toLowerCase()]=f.path});"
        "acc[f.basename.replace('.md','').toLowerCase()]=f.path;"
        "return acc},{}))"
    )
    result = cli.eval_js(code)
    if not result:
        return {}
    try:
        return json.loads(result)
    except json.JSONDecodeError:
        return {}


def filter_alias_resolved(
    unresolved: dict[str, list[str]], alias_map: dict[str, str]
) -> tuple[dict[str, list[str]], int]:
    """Remove links that resolve via aliases. Returns (filtered, removed_count)."""
    filtered = {}
    removed = 0
    for link, files in unresolved.items():
        if link.lower() in alias_map:
            removed += 1
        else:
            filtered[link] = files
    return filtered, removed


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def is_noise(link: str) -> bool:
    """Check if a link is a template placeholder or noise."""
    if link.lower().strip() in NOISE_LITERALS:
        return True
    return any(p.search(link) for p in NOISE_REGEXES)


def extract_project(file_path: str) -> str:
    """Extract project name from a vault-relative file path."""
    if file_path.startswith("projects/"):
        parts = file_path.split("/")
        if len(parts) >= 2:
            return parts[1]
    if file_path.startswith("topics/"):
        return "_topics"
    return "_vault"


def classify(ref_count: int, projects: set[str]) -> str:
    """Classify a ghost node into a maturation tier.

    For cross-project spread, only real projects count (not _topics, _vault).
    """
    real_projects = {p for p in projects if not p.startswith("_")}
    cross_project = len(real_projects) >= 2
    if ref_count >= 5:
        return "OVERDUE"
    if ref_count >= 3 and cross_project:
        return "READY"
    if ref_count >= 2:
        return "MATURING"
    return "SEEDLING"


TIER_ORDER = {"OVERDUE": 0, "READY": 1, "MATURING": 2, "SEEDLING": 3}


def analyze(unresolved: dict[str, list[str]]) -> list[dict]:
    """Analyze unresolved links and return classified entries."""
    entries = []
    for link, files in unresolved.items():
        if is_noise(link):
            continue
        projects = {extract_project(f) for f in files}
        tier = classify(len(files), projects)
        entries.append(
            {
                "link": link,
                "refs": len(files),
                "projects": sorted(projects),
                "tier": tier,
                "files": sorted(files),
            }
        )
    entries.sort(key=lambda e: (TIER_ORDER[e["tier"]], -e["refs"], e["link"]))
    return entries


# ---------------------------------------------------------------------------
# Delta tracking
# ---------------------------------------------------------------------------

def load_previous() -> dict:
    """Load previous crystallization state."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def save_state(entries: list[dict], filtered_noise: int):
    """Save current state for future delta comparison."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tiers: dict[str, int] = {}
    for e in entries:
        tiers[e["tier"]] = tiers.get(e["tier"], 0) + 1
    state = {
        "timestamp": datetime.now().isoformat(),
        "total_raw": len(entries) + filtered_noise,
        "filtered_noise": filtered_noise,
        "actionable": len(entries),
        "links": {e["link"]: e["refs"] for e in entries},
        "tiers": tiers,
    }
    STATE_FILE.write_text(json.dumps(state, indent=2))


def compute_delta(entries: list[dict], previous: dict) -> dict:
    """Compute what changed since last check."""
    if not previous or "links" not in previous:
        return {"is_first_run": True}

    prev_links = previous["links"]
    current_links = {e["link"]: e["refs"] for e in entries}

    new_links = [e for e in entries if e["link"] not in prev_links]
    resolved = [link for link in prev_links if link not in current_links]
    grown = [
        e
        for e in entries
        if e["link"] in prev_links and e["refs"] > prev_links[e["link"]]
    ]

    return {
        "is_first_run": False,
        "previous_timestamp": previous.get("timestamp", "unknown"),
        "new": new_links,
        "resolved": resolved,
        "grown": grown,
        "prev_actionable": previous.get("actionable", 0),
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

TIER_LABELS = {
    "OVERDUE": "OVERDUE (5+ refs)",
    "READY": "READY (3+ refs, cross-project)",
    "MATURING": "MATURING (2+ refs)",
    "SEEDLING": "SEEDLING (1 ref)",
}


def print_report(
    entries: list[dict],
    delta: dict,
    tier_filter: str,
    verbose: bool,
    raw_count: int = 0,
    alias_resolved: int = 0,
):
    """Print human-readable crystallization report."""
    total_entries = len(entries)  # Pre-filter count for noise calculation
    if tier_filter != "all":
        entries = [e for e in entries if e["tier"] == tier_filter.upper()]

    tiers: dict[str, int] = {}
    for e in entries:
        tiers[e["tier"]] = tiers.get(e["tier"], 0) + 1

    print("=" * 60)
    print("  Crystallization Readiness Report")
    print("=" * 60)
    print()

    # Filtering summary
    if raw_count > 0:
        noise = raw_count - alias_resolved - total_entries
        print(f"  Raw unresolved: {raw_count}")
        print(f"    Resolved via alias: {alias_resolved}")
        if noise > 0:
            print(f"    Filtered noise: {noise}")
        print()

    # Summary counts
    print(f"  Actionable ghost nodes: {len(entries)}")
    for tier_name in ["OVERDUE", "READY", "MATURING", "SEEDLING"]:
        count = tiers.get(tier_name, 0)
        if count > 0:
            print(f"    {TIER_LABELS[tier_name]}: {count}")
    print()

    # Delta since last check
    if not delta.get("is_first_run"):
        ts = delta["previous_timestamp"][:10]
        print(f"  Since last check ({ts}):")
        changes = False
        if delta["new"]:
            print(f"    + {len(delta['new'])} new unresolved")
            changes = True
        if delta["resolved"]:
            print(f"    - {len(delta['resolved'])} resolved")
            changes = True
        if delta["grown"]:
            print(f"    ^ {len(delta['grown'])} gained references")
            changes = True
        if not changes:
            print("    No changes")
        print()

        # Show promoted links (grew into a higher tier)
        if delta["grown"]:
            promoted = [
                e for e in delta["grown"] if e["tier"] in ("OVERDUE", "READY")
            ]
            if promoted:
                print("  Newly actionable (promoted to READY/OVERDUE):")
                for e in promoted:
                    projects_str = ", ".join(
                        p for p in e["projects"] if not p.startswith("_")
                    )
                    print(f"    [[{e['link']}]]  ({e['refs']} refs in {projects_str})")
                print()
    else:
        print("  First run — baseline established for future comparisons")
        print()

    # Detailed entries by tier (skip seedlings unless requested)
    show_seedlings = tier_filter in ("all", "seedling") and verbose
    for tier_name in ["OVERDUE", "READY", "MATURING", "SEEDLING"]:
        tier_entries = [e for e in entries if e["tier"] == tier_name]
        if not tier_entries:
            continue
        if tier_name == "SEEDLING" and not show_seedlings:
            print(f"  {tiers.get('SEEDLING', 0)} seedlings omitted (use -v to show)")
            print()
            continue

        print(f"--- {TIER_LABELS[tier_name]} ---")
        print()
        for e in tier_entries:
            projects_str = ", ".join(
                p for p in e["projects"] if not p.startswith("_")
            )
            if not projects_str:
                projects_str = ", ".join(e["projects"])
            print(f"  [[{e['link']}]]  ({e['refs']} refs — {projects_str})")
            if verbose:
                for f in e["files"]:
                    print(f"    <- {f}")
        print()


def print_json(entries: list[dict], delta: dict):
    """Print JSON output for programmatic use."""
    summary: dict[str, int] = {}
    for e in entries:
        summary[e["tier"]] = summary.get(e["tier"], 0) + 1

    output: dict = {
        "timestamp": datetime.now().isoformat(),
        "summary": summary,
        "entries": entries,
    }
    if not delta.get("is_first_run"):
        output["delta"] = {
            "previous": delta["previous_timestamp"],
            "new_count": len(delta.get("new", [])),
            "resolved_count": len(delta.get("resolved", [])),
            "grown_count": len(delta.get("grown", [])),
        }
    print(json.dumps(output, indent=2))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Analyze vault's unresolved wikilinks for crystallization readiness",
    )
    parser.add_argument(
        "--tier",
        choices=["overdue", "ready", "maturing", "seedling", "all"],
        default="all",
        help="Show only a specific tier (default: all)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON for programmatic use",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show source files for each link and include seedlings",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="Don't update state file (dry-run delta tracking)",
    )
    args = parser.parse_args()

    global VAULT, STATE_FILE
    VAULT = get_memex_path()
    STATE_FILE = get_state_dir() / "crystallization_state.json"

    # Prefer Obsidian (live metadataCache, most precise); fall back to a
    # filesystem scan when it isn't running so the check stays usable headless.
    cli = get_obsidian_cli()
    if cli.is_available():
        raw = get_unresolved_links(cli)
        alias_map = get_alias_map(cli)
    else:
        print(
            "Note: Obsidian not running — using filesystem fallback "
            "(filename + alias resolution; no heading/block awareness, but "
            "sufficient for ghost-node detection).",
            file=sys.stderr,
        )
        raw, alias_map = scan_unresolved_via_markdown(VAULT)

    # Filter out alias-resolved links (neither CLI nor metadataCache does this)
    after_alias, alias_resolved = filter_alias_resolved(raw, alias_map)

    # Analyze and classify
    entries = analyze(after_alias)
    filtered_noise = len(raw) - len(entries)  # includes both alias-resolved and noise

    # Delta tracking
    previous = load_previous()
    delta = compute_delta(entries, previous)

    if not args.no_save:
        save_state(entries, filtered_noise)

    # Output
    if args.json:
        print_json(entries, delta)
    else:
        print_report(
            entries,
            delta,
            args.tier,
            args.verbose,
            raw_count=len(raw),
            alias_resolved=alias_resolved,
        )


if __name__ == "__main__":
    main()
