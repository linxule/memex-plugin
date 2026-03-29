#!/usr/bin/env python3
"""Legacy compatibility stub for a removed optional integration."""

from __future__ import annotations

import sys


def main() -> None:
    print(
        "This integration was removed in memex 0.6.0. Use the `memex` CLI instead.",
        file=sys.stderr,
    )
    raise SystemExit(1)


if __name__ == "__main__":
    main()
