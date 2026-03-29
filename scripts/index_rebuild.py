#!/usr/bin/env python3
"""Backward-compat shim — delegates to memex.scripts.index_rebuild."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from memex.scripts.index_rebuild import main

if __name__ == "__main__":
    main()
