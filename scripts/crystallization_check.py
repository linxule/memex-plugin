# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""
Crystallization Check — Analyze vault's unresolved wikilinks for maturation readiness.

Uses Obsidian CLI (1.12.2+) native `aliases` command for alias-aware filtering,
with eval fallback for unresolved link aggregation. Classifies by frequency
and cross-project spread.

Maturation tiers:
  OVERDUE  — 5+ references, should have been crystallized already
  READY    — 3+ references across 2+ projects
  MATURING — 2+ references (single-project or low spread)
  SEEDLING — 1 reference (leave it, may grow)

Requires Obsidian to be running (uses metadataCache for alias-aware resolution).
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _get_vault() -> Path:
    """Resolve vault path: config.json → env var → script location fallback."""
    config_path = Path.home() / ".memex" / "config.json"
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
            if "memex_path" in config:
                return Path(config["memex_path"]).resolve()
        except (json.JSONDecodeError, KeyError):
            pass
    env_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env_root:
        return Path(env_root).resolve()
    return Path(__file__).parent.parent.resolve()


VAULT = _get_vault()
STATE_FILE = Path.home() / ".memex" / "crystallization_state.json"

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
    re.compile(r"^https?://"),    # URLs accidentally wikified
    re.compile(r"^\d+$"),         # bare numbers
]


# ---------------------------------------------------------------------------
# Obsidian interface
# ---------------------------------------------------------------------------

def get_obsidian_cli():
    """Import and return an ObsidianCLI instance."""
    sys.path.insert(0, str(VAULT / "scripts"))
    from obsidian_cli import ObsidianCLI
    return ObsidianCLI()  # defaults to vault="memex"


def get_unresolved_links(cli) -> dict[str, list[str]]:
    """Get unresolved links as {link_text: [source_files]}.

    Uses metadataCache.unresolvedLinks for file-level resolution,
    then applies alias filtering (since neither the CLI nor the
    metadata cache resolves frontmatter aliases).
    """
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
        print(
            "Error: No output from Obsidian eval. Is Obsidian running?",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        return json.loads(result)
    except json.JSONDecodeError:
        print(
            f"Error: Could not parse Obsidian output: {result[:200]}",
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

    # Check Obsidian availability
    cli = get_obsidian_cli()
    if not cli.is_available():
        print(
            "Error: Obsidian CLI not available. Is Obsidian running?",
            file=sys.stderr,
        )
        sys.exit(1)

    # Get unresolved links and alias map
    raw = get_unresolved_links(cli)
    alias_map = get_alias_map(cli)

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
