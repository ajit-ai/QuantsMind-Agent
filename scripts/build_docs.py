"""Build the QuantsMind-Agent HTML documentation.

Usage::

    python scripts/build_docs.py               # strict build
    python scripts/build_docs.py --allow-warnings
    python scripts/build_docs.py --clean

Exits non-zero on any Sphinx warning unless ``--allow-warnings`` is given, so
CI fails on documentation errors.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPO_ROOT / "docs" / "source"
BUILD = REPO_ROOT / "docs" / "build"
OUTPUT = BUILD / "html"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-warnings",
        action="store_true",
        help="Do not fail the build on Sphinx warnings.",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove docs/build before building.",
    )
    args = parser.parse_args(argv)

    if args.clean and BUILD.exists():
        shutil.rmtree(BUILD)

    if str(REPO_ROOT / "src") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "src"))

    try:
        from sphinx.application import Sphinx
    except ImportError:
        print(
            'Sphinx is not installed. Install the docs extra:\n    pip install -e ".[docs]"',
            file=sys.stderr,
        )
        return 2

    OUTPUT.mkdir(parents=True, exist_ok=True)
    app = Sphinx(
        srcdir=str(SOURCE),
        confdir=str(SOURCE),
        outdir=str(OUTPUT),
        doctreedir=str(BUILD / "doctrees"),
        buildername="html",
        status=sys.stdout,
        warning=sys.stderr,
        freshenv=True,
        warningiserror=not args.allow_warnings,
        keep_going=True,
    )
    app.build(force_all=True)
    # Sphinx >= 7 reports the outcome on the application; older versions
    # returned it from build().
    result = app.statuscode
    if result == 0:
        index = OUTPUT / "index.html"
        print(f"Documentation built: {index}")
        print(f"Open it with: start {index}")
    else:
        print(
            f"Documentation build reported problems (exit {result}). "
            "Fix the warnings above, or pass --allow-warnings.",
            file=sys.stderr,
        )
    return int(result)


if __name__ == "__main__":
    raise SystemExit(main())
