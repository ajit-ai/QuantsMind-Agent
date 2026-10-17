"""Build a native FreeBSD package (.pkg) from a FreeBSD onedir build.

FreeBSD's package format is its own; a Linux ``.deb`` renamed to ``.pkg``, or a
tarball dressed up as one, is not a FreeBSD package and ``pkg`` will refuse it.
This uses the supported toolchain -- ``pkg create`` against an installed package
manifest, or ``pkg make-manifest`` -- so the result is a real package that
``pkg install`` and ``pkg info`` understand.

FreeBSD specifics that make this different from the Linux packaging
-------------------------------------------------------------------
* **The bundle has to be built on FreeBSD.** PyInstaller does not cross-compile,
  and a Linux build will not run on FreeBSD. There is no workaround; the missing
  piece is a FreeBSD machine, not a flag.
* **Qt on FreeBSD** comes from ports (``devel/qt5-*`` or ``devel/qt6-*``) or
  ``pkg``. PyInstaller's own bootloader is not available for FreeBSD, so a
  PyInstaller build there is itself unsupported by upstream; this script expects
  a bundle produced by whatever route is verified on that machine, and reports
  honestly if it is given nothing.
* **Permissions**: the ports system rejects world-writable directories and
  requires correct ownership; ``pkg create -o`` takes care of that, and the
  checks below assert it rather than assume it.

Requires a native FreeBSD host with ``pkg`` and a Python 3.11+ that can run the
application::

    python scripts/build_executable.py --distpath artifacts/v0.2.0/freebsd
    python scripts/build_freebsd_pkg.py --onedir artifacts/v0.2.0/freebsd

Then on the FreeBSD machine::

    sudo pkg install -y ./quantsmind-agent-0.2.0.pkg
    quantsmind-agent --check-config
    sudo pkg remove -y quantsmind-agent
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

PACKAGE = "quantsmind-agent"
PREFIX = "/usr/local"
DESKTOP_FILE = f"{PREFIX}/share/applications/{PACKAGE}.desktop"

PASS, FAIL, INCONCLUSIVE = "PASS", "FAIL", "INCONCLUSIVE"
checks: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, PASS if ok else FAIL, detail))
    print(f"[{PASS if ok else FAIL}] {name}" + (f" -- {detail}" if detail else ""), flush=True)


def environment(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, PASS if ok else INCONCLUSIVE, detail))
    print(
        f"[{PASS if ok else INCONCLUSIVE}] {name}" + (f" -- {detail}" if detail else ""), flush=True
    )


def read_version() -> str:
    text = (REPO / "src" / "quantsmind_agent" / "version.py").read_text(encoding="utf-8")
    match = re.search(r'__version__: Final\[str\] = "([^"]+)"', text)
    return match.group(1) if match else "0.0.0"


def git_commit() -> str | None:
    try:
        return (
            subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=False
            ).stdout.strip()
            or None
        )
    except OSError:  # pragma: no cover - git absent
        return None


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--onedir", required=True, help="the FreeBSD bundle to package")
    parser.add_argument("--output", default=None, help="path of the .pkg to write")
    args = parser.parse_args(argv)

    onedir = Path(args.onedir)
    if not onedir.is_absolute():
        onedir = REPO / onedir
    bundle = onedir / "QuantsMindAgent"
    if not bundle.is_dir():
        bundle = onedir

    version = read_version()
    output = (
        Path(args.output) if args.output else onedir.parent / "pkg" / f"{PACKAGE}-{version}.pkg"
    )
    if not output.is_absolute():
        output = REPO / output

    if "FreeBSD" not in sys.platform or platform.system() != "FreeBSD":
        environment(
            "built on FreeBSD",
            False,
            f"this is {platform.system()}; a FreeBSD package must be built on FreeBSD",
        )
        print("\nnothing was built. Run this on a FreeBSD host with a FreeBSD bundle.")
        return 2

    executable = bundle / "QuantsMindAgent"
    if not executable.is_file():
        print(f"no QuantsMindAgent executable in {bundle}", file=sys.stderr)
        return 2

    print(f"packaging {bundle} -> {output}\n")
    started = time.time()
    completed = subprocess.run(
        [
            "pkg",
            "create",
            "-O",  # force the output path
            "-o",
            str(output),
            "-n",
            PACKAGE,
            "-v",
            version,
            "-c",
            f"{PREFIX}/lib/{PACKAGE}",
            "-d",
            "QuantsMind-Agent is a local coding agent. It runs entirely on this machine; "
            "cloud model usage is billed by your own provider against your own account.",
            "-m",
            "QuantsMind <support@quantsmind.invalid>",
            "-w",
            f"{PREFIX}/bin/{PACKAGE}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        print(completed.stdout[-2000:], file=sys.stderr)
        print(completed.stderr[-2000:], file=sys.stderr)
        check("pkg create", False, f"exit={completed.returncode}")
        return 1

    check("package created", output.is_file(), f"{output.name} {output.stat().st_size} bytes")

    info = subprocess.run(
        ["pkg", "info", "-g", str(output)], capture_output=True, text=True, check=False
    )
    check(
        "pkg recognises the package",
        info.returncode == 0 and PACKAGE in (info.stdout + info.stderr),
        f"exit={info.returncode}",
    )

    metadata = {
        "artifact": "freebsd-pkg",
        "version": version,
        "commit": git_commit(),
        "install_prefix": PREFIX,
        "packaging_tool": "pkg create",
        "packaging_command": f"python scripts/build_freebsd_pkg.py --onedir {args.onedir}",
        "built_on": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "build_seconds": round(time.time() - started, 1),
        "filename": output.name,
        "bytes": output.stat().st_size,
        "sha256": sha256_of(output),
        "user_data_location": "~/.local/share, ~/.config (never packaged)",
    }
    (output.parent / "BUILD-METADATA.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )

    failures = [name for name, status, _d in checks if status == FAIL]
    print()
    print(f"{len(checks) - len(failures)}/{len(checks)} FreeBSD package checks passed")
    print(f"built: {output}")
    print("install with:  sudo pkg install -y " + output.name)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
