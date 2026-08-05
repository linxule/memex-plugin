#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["filelock>=3.0"]
# ///
# The header is here because the __main__ block below is a real entry point:
# under `uv run --script` this runs in an isolated env, where the canonical
# module's `from memex.scripts.utils import ...` needs filelock, and
# memex/__init__.py needs tomllib (3.11). Sibling shims omit the header and
# are dishonest about it; do not "harmonize" this one away.
"""Backward-compat shim — re-exports from memex.scripts.transcript_to_md.

Flat importers (`sys.path.insert(scripts_dir)` + `import transcript_to_md`)
and package importers (`from scripts.transcript_to_md import ...`) both land
here and get the canonical module's real names. Until v0.16.3 this file WAS
the canonical module, and its import ladder shadowed four of the five names it
imported from utils — see the canonical module for the fix.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from memex.scripts.transcript_to_md import *  # noqa: F401,F403
from memex.scripts.transcript_to_md import main  # noqa: F401

if __name__ == "__main__":
    main()
