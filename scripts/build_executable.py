"""Build a distributable QuantsMind-Agent executable with PyInstaller.

Runs on a *native* runner for the target OS: PyInstaller does not cross-compile,
so a Linux binary must be built on Linux and a macOS bundle on macOS.

Usage::

    python scripts/build_executable.py                 # onedir (default)
    python scripts/build_executable.py --onefile      # single self-extracting file
    python scripts/build_executable.py --clean        # wipe dist/ and build/

The build writes ``dist/BUILD-METADATA.json`` recording what was produced and on
what, so an artifact can be traced back to a platform and commit without guessing
from a file name.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SPEC = REPO / "packaging" / "quantsmind-agent.spec"
DIST = REPO / "dist"
BUILD = REPO / "build"


def read_version() -> str:
    """Read the version without importing the package."""

    import re

    text = (REPO / "src" / "quantsmind_agent" / "version.py").read_text(encoding="utf-8")
    match = re.search(r'__version__: Final\[str\] = "([^"]+)"', text)
    return match.group(1) if match else "0.0.0"


def git_commit() -> str | None:
    try:
        return (
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=REPO,
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            or None
        )
    except OSError:  # pragma: no cover - git absent
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--onefile", action="store_true", help="single self-extracting file")
    parser.add_argument("--clean", action="store_true", help="remove dist/ and build/ first")
    parser.add_argument(
        "--distpath",
        default=None,
        help="write artifacts here instead of dist/ (used by CI matrices)",
    )
    args = parser.parse_args(argv)

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print(
            "PyInstaller is not installed. Install it with:\n"
            '    python -m pip install -e ".[packaging]"',
            file=sys.stderr,
        )
        return 2

    if args.clean:
        for path in (DIST, BUILD):
            shutil.rmtree(path, ignore_errors=True)

    distpath = Path(args.distpath) if args.distpath else DIST
    version = read_version()
    started = time.time()

    env = dict(os.environ)
    env["QM_ONEFILE"] = "1" if args.onefile else "0"

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(distpath),
        "--workpath",
        str(BUILD),
        str(SPEC),
    ]
    print(f"building {version} on {platform.system()} ({platform.machine()}) -> {distpath}")
    completed = subprocess.run(command, cwd=REPO, env=env, check=False)

    metadata = {
        "version": version,
        "commit": git_commit(),
        "layout": "onefile" if args.onefile else "onedir",
        "built_on": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "build_seconds": round(time.time() - started, 1),
        "returncode": completed.returncode,
        "distpath": str(distpath),
    }

    distpath.mkdir(parents=True, exist_ok=True)
    (distpath / "BUILD-METADATA.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )

    if completed.returncode != 0:
        print(f"build FAILED (exit {completed.returncode})", file=sys.stderr)
        return completed.returncode

    print(f"build OK in {metadata['build_seconds']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
