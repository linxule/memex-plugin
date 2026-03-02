#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""
Memex Plugin - Interactive Setup Wizard

Helps new users configure the memex plugin.

Usage:
    setup.py [--check]
"""

import argparse
import json
import os
import sys
from pathlib import Path


def get_memex_path() -> Path:
    """Get memex vault path, checking config first.

    Resolution order:
    1. ~/.memex/config.json -> memex_path (user override)
    2. CLAUDE_PLUGIN_ROOT env var (set by plugin system)
    3. Script location fallback (assumes scripts are in memex/scripts/)
    """
    # 1. Check config file first
    config_path = Path.home() / ".memex" / "config.json"
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
            if "memex_path" in config:
                path = Path(config["memex_path"]).expanduser()
                if path.exists():
                    return path
        except (json.JSONDecodeError, KeyError):
            pass

    # 2. Check env var
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root:
        return Path(plugin_root)

    # 3. Fallback: scripts/setup.py -> scripts/ -> memex/
    return Path(__file__).parent.parent


def create_state_dir():
    """Create ~/.memex directory structure."""
    state_dir = Path.home() / ".memex"

    dirs = [
        state_dir,
        state_dir / "logs",
        state_dir / "locks",
        state_dir / "pending-memos",
        state_dir / "prompts",
    ]

    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        print(f"  Created: {d}")

    return state_dir


def create_config(state_dir: Path, memex_path: Path, verbosity: str = "standard"):
    """Create default config file."""
    config_path = state_dir / "config.json"

    if config_path.exists():
        print(f"  Config already exists: {config_path}")
        return config_path

    config = {
        "memex_path": str(memex_path),
        "session_context": {
            "verbosity": verbosity
        }
    }

    config_path.write_text(json.dumps(config, indent=2))
    print(f"  Created: {config_path}")

    return config_path


def check_gemini_key() -> bool:
    """Check if Gemini API key is set."""
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))


def check_installation():
    """Check if everything is set up correctly."""
    print("\n=== Memex Installation Check ===\n")

    issues = []

    # Check state directory
    state_dir = Path.home() / ".memex"
    if state_dir.exists():
        print(f"[OK] State directory: {state_dir}")
    else:
        print(f"[!!] State directory missing: {state_dir}")
        issues.append("Run: mkdir -p ~/.memex/{logs,locks,pending-memos,prompts}")

    # Check config
    config_path = state_dir / "config.json"
    if config_path.exists():
        print(f"[OK] Config file: {config_path}")
        try:
            config = json.loads(config_path.read_text())
            if "memex_path" in config:
                print(f"     memex_path: {config['memex_path']}")
        except json.JSONDecodeError:
            print(f"[!!] Config file has invalid JSON")
            issues.append("Fix JSON syntax in ~/.memex/config.json")
    else:
        print(f"[  ] Config file: not created (using defaults)")

    # Check memex vault
    memex = get_memex_path()
    if memex.exists():
        print(f"[OK] Memex vault: {memex}")
    else:
        print(f"[!!] Memex vault not found: {memex}")
        issues.append("Set memex_path in ~/.memex/config.json")

    # Check index
    index_path = memex / "_index.sqlite"
    if index_path.exists():
        size_mb = index_path.stat().st_size / (1024 * 1024)
        print(f"[OK] Search index: {index_path} ({size_mb:.1f} MB)")
    else:
        print(f"[!!] Search index missing")
        issues.append("Run: uv run scripts/index_rebuild.py --full")

    # Check Gemini key
    if check_gemini_key():
        print(f"[OK] Gemini API key: set")
    else:
        print(f"[  ] Gemini API key: not set (semantic search disabled)")

    # Check hooks file
    hooks_path = memex / "hooks" / "hooks.json"
    if hooks_path.exists():
        print(f"[OK] Hooks config: {hooks_path}")
    else:
        print(f"[!!] Hooks config missing")
        issues.append("hooks/hooks.json not found")

    # Check plugin.json
    plugin_path = memex / ".claude-plugin" / "plugin.json"
    if plugin_path.exists():
        print(f"[OK] Plugin manifest: {plugin_path}")
    else:
        print(f"[!!] Plugin manifest missing")
        issues.append(".claude-plugin/plugin.json not found")

    # Summary
    print("\n" + "=" * 40)
    if issues:
        print(f"\n{len(issues)} issue(s) found:\n")
        for i, issue in enumerate(issues, 1):
            print(f"  {i}. {issue}")
        print("\nRun 'uv run scripts/setup.py' to fix automatically.")
        return False
    else:
        print("\nAll checks passed! Memex is ready to use.")
        print("\nNext steps:")
        print("  1. Restart Claude Code to load hooks")
        print("  2. Run /memex:status to verify")
        print("  3. Run /memex:search to find past sessions")
        return True


def interactive_setup():
    """Run interactive setup wizard."""
    print("\n=== Memex Setup Wizard ===\n")

    memex = get_memex_path()
    print(f"Memex vault location: {memex}\n")

    # Step 1: Create state directory
    print("Step 1: Creating state directory...")
    state_dir = create_state_dir()

    # Step 2: Verbosity preference
    print("\nStep 2: Context verbosity")
    print("  How much context should be loaded at session start?")
    print("  - minimal: Just a hint (~20 tokens)")
    print("  - standard: Project + memo titles (~150 tokens) [recommended]")
    print("  - full: Complete memo content (~500+ tokens)")

    verbosity = input("\n  Enter choice [standard]: ").strip().lower()
    if verbosity not in ("minimal", "standard", "full"):
        verbosity = "standard"

    # Step 3: Create config
    print(f"\nStep 3: Creating config (verbosity={verbosity})...")
    create_config(state_dir, memex, verbosity)

    # Step 4: Check Gemini
    print("\nStep 4: Semantic search")
    if check_gemini_key():
        print("  Gemini API key detected - semantic search enabled")
    else:
        print("  No Gemini API key found")
        print("  To enable semantic search, set GEMINI_API_KEY in your shell profile")
        print("  (Keyword search works without it)")

    # Step 5: Build index
    print("\nStep 5: Search index")
    index_path = memex / "_index.sqlite"
    if index_path.exists():
        print(f"  Index exists: {index_path}")
        rebuild = input("  Rebuild index? [n]: ").strip().lower()
    else:
        print("  Index not found")
        rebuild = input("  Build index now? [y]: ").strip().lower()
        if rebuild == "" or rebuild == "y":
            rebuild = "y"

    if rebuild == "y":
        print("\n  Building index (this may take a moment)...")
        import subprocess
        result = subprocess.run(
            ["uv", "run", str(memex / "scripts" / "index_rebuild.py"), "--full"],
            cwd=memex,
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            print("  Index built successfully!")
        else:
            print(f"  Index build failed: {result.stderr}")

    # Done
    print("\n" + "=" * 40)
    print("\nSetup complete!")
    print("\nNext steps:")
    print("  1. Restart Claude Code to load hooks")
    print("  2. Run /memex:status to verify installation")
    print("  3. Use /memex:search to find past sessions")
    print("\nFor full documentation, see CLAUDE.md")


def main():
    parser = argparse.ArgumentParser(description="Memex setup wizard")
    parser.add_argument("--check", action="store_true",
                        help="Check installation status only")

    args = parser.parse_args()

    if args.check:
        success = check_installation()
        sys.exit(0 if success else 1)
    else:
        interactive_setup()


if __name__ == "__main__":
    main()
