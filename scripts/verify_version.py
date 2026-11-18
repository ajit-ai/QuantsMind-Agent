"""Verify the version a built wheel actually reports.

Run as part of the packaging gate::

    python scripts/verify_version.py dist/quantsmind_agent-0.9.0-py3-none-any.whl

The version is read from the wheel's ``METADATA`` rather than by importing the
package, because the build job installs only ``build`` -- not the project. That
way the check asserts on the artifact that would actually be published.

Exits 0 when the wheel's version matches the source version, 1 otherwise.
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VERSION_PATH = REPO / "src" / "quantsmind_agent" / "version.py"


def source_version() -> str:
    text = VERSION_PATH.read_text(encoding="utf-8")
    match = re.search(r'__version__: Final\[str\] = "([^"]+)"', text)
    if match is None:
        raise SystemExit(f"{VERSION_PATH}: no __version__ assignment found")
    return match.group(1)


EXPECTED_VERSION = source_version()


def wheel_version(wheel: Path) -> str:
    """Return the ``Version`` recorded in the wheel's METADATA."""

    with zipfile.ZipFile(wheel) as archive:
        candidates = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if not candidates:
            raise SystemExit(f"{wheel.name}: no .dist-info/METADATA found")
        metadata = archive.read(candidates[0]).decode("utf-8", errors="replace")
    match = re.search(r"^Version: (.+)$", metadata, re.MULTILINE)
    if match is None:
        raise SystemExit(f"{wheel.name}: METADATA has no Version field")
    return match.group(1).strip()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print(f"usage: {Path(sys.argv[0]).name} <wheel>", file=sys.stderr)
        return 2

    wheel = Path(args[0])
    if not wheel.is_file():
        print(f"{wheel}: not found. Run 'python -m build' first.", file=sys.stderr)
        return 2

    found = wheel_version(wheel)
    if found != EXPECTED_VERSION:
        print(
            f"FAIL: {wheel.name} reports version {found}, expected {EXPECTED_VERSION}",
            file=sys.stderr,
        )
        return 1
    print(f"OK: {wheel.name} reports version {found}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
