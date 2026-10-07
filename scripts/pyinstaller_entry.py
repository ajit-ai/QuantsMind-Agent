"""PyInstaller entry point.

Kept separate from :mod:`quantsmind_agent.app` so that PyInstaller has a single,
stable module to import. The application itself starts in :func:`main`.
"""

from __future__ import annotations

import sys


def main() -> int:
    from quantsmind_agent.app import main as run

    return run()


if __name__ == "__main__":
    sys.exit(main())
