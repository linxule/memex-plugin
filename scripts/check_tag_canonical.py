#!/usr/bin/env python3
"""Shim for src/memex/scripts/check_tag_canonical.py.

See src/memex/scripts/check_tag_canonical.py for the implementation.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from memex.scripts.check_tag_canonical import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
