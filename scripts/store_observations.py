#!/usr/bin/env python3
"""Redirect for hallucinated script name.

Claude instances sometimes guess ``store_observations.py`` instead of
``extract_observations.py`` or ``python -m memex.extract``.  This thin
wrapper delegates to the real module so those invocations succeed.
"""
import sys
from pathlib import Path

src = Path(__file__).resolve().parent.parent / "src"
if str(src) not in sys.path:
    sys.path.insert(0, str(src))

from memex.extract import main  # noqa: E402

if __name__ == "__main__":
    main()
