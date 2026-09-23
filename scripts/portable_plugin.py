#!/usr/bin/env python3
"""Backward-compat shim — delegates to memex.scripts.portable_plugin."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from memex.scripts.portable_plugin import *  # noqa: F401,F403
from memex.scripts.portable_plugin import main

if __name__ == "__main__":
    main()
