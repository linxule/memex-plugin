#!/usr/bin/env python3
"""Backward-compat shim — re-exports from memex.scripts.wikilink_filters."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from memex.scripts.wikilink_filters import *  # noqa: F401,F403
