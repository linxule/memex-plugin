#!/usr/bin/env python3
"""Backward-compat shim — delegates to memex.scripts.discover_sessions."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from memex.scripts.discover_sessions import main

if __name__ == "__main__":
    main()
