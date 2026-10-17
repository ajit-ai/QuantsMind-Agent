"""Build a Debian package (.deb) from a Linux onedir build.

PyInstaller's onedir output is a folder. On Linux that folder is enough to run
the application, but a folder has no version, no dependency declarations, no
launcher and no uninstaller, so ``dpkg`` cannot manage it. This wraps the bundle
in a real Debian package.

Layout
------
* ``/opt/quantsmind-agent/`` -- the application, exactly as built
* ``/usr/bin/quantsmind-agent`` -- a launcher that execs it
* ``/usr/share/applications/com.quantsmind.agent.desktop`` -- the menu entry

Nothing is written to a user's home directory by any maintainer script.
Configuration, credentials, logs and projects belong to the user and live under
``$XDG_CONFIG_HOME``/``$XDG_DATA_HOME``, which this package never touches. The
``postinst`` and ``prerm`` scripts in ``packaging/deb`` exist to make that
explicit and are shipped as files so they can be reviewed rather than generated.

Build requirements (native Linux; this cannot run on Windows or macOS)::

    dpkg-deb
    file          # architecture detection

Usage::

    python scripts/build_deb.py --onedir artifacts/v0.2.0/ubuntu/onedir
    python scripts/build_deb.py --onedir dist --output dist/deb

Then, on a Debian or Ubuntu machine::

    sudo dpkg -i quantsmind-agent_0.2.0_amd64.deb
    quantsmind-agent --check-config     # exit 1 without an API key is expected
    sudo dpkg -r quantsmind-agent
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "packaging" / "deb"

#: Debian package name. Lower case and hyphenated by convention; the upstream
#: distribution keeps its capitalisation, the package index does not.
PACKAGE = "quantsmind-agent"
DESKTOP_ID = "com.quantsmind.agent.desktop"

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


def detect_architecture(executable: Path) -> str:
    """Debian architecture for a native Linux binary."""

    machine = platform.machine().lower()
    mapping = {"x86_64": "amd64", "aarch64": "arm64", "armv7l": "armhf", "i686": "i386"}
    if machine in mapping:
        return mapping[machine]
    if shutil.which("dpkg") and executable.is_file():
        out = subprocess.run(
            ["dpkg", "--print-architecture"], capture_output=True, text=True, check=False
        )
        if out.stdout.strip():
            return out.stdout.strip()
    raise RuntimeError(f"cannot map machine {machine!r} to a Debian architecture")


def build(bundle: Path, output: Path) -> tuple[int, int]:
    """Assemble the package tree and build the .deb. Returns (files, bytes)."""

    version = read_version()
    arch = detect_architecture(bundle / "QuantsMindAgent")

    with tempfile.TemporaryDirectory(prefix="qm-deb-") as raw:
        root = Path(raw) / f"{PACKAGE}_{version}_{arch}"
        app = root / "opt" / PACKAGE
        app.parent.mkdir(parents=True)
        shutil.copytree(bundle, app)

        installed = sum(1 for p in app.rglob("*") if p.is_file())
        installed_kb = max(1, sum(p.stat().st_size for p in app.rglob("*") if p.is_file()) // 1024)

        (root / "DEBIAN").mkdir()
        control = (DATA / "control.in").read_text(encoding="utf-8")
        control = (
            control.replace("@VERSION@", version)
            .replace("@ARCH@", arch)
            .replace("@SIZE@", str(installed_kb))
        )
        (root / "DEBIAN" / "control").write_text(control, encoding="utf-8")

        for script in ("postinst", "prerm"):
            source = DATA / script
            if source.is_file():
                target = root / "DEBIAN" / script
                target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
                target.chmod(0o755)

        launcher = root / "usr" / "bin" / PACKAGE
        launcher.parent.mkdir(parents=True)
        launcher.write_text(
            f'#!/bin/sh\nexec "/opt/{PACKAGE}/QuantsMindAgent" "$@"\n',
            encoding="utf-8",
        )
        launcher.chmod(0o755)

        applications = root / "usr" / "share" / "applications"
        applications.mkdir(parents=True)
        desktop = (DATA / DESKTOP_ID).read_text(encoding="utf-8")
        launcher_name = "QuantsMindAgent"
        desktop = desktop.replace("Exec=quantsmind-agent %f", f"Exec={launcher_name} %f")
        (applications / DESKTOP_ID).write_text(desktop, encoding="utf-8")

        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            output.unlink()
        completed = subprocess.run(
            [
                "dpkg-deb",
                "--root-owner-group",
                "--build",
                str(root),
                str(output),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            print(completed.stdout[-2000:], file=sys.stderr)
            print(completed.stderr[-2000:], file=sys.stderr)
            raise RuntimeError(f"dpkg-deb failed with exit {completed.returncode}")
        return installed, output.stat().st_size


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--onedir", required=True, help="PyInstaller onedir output to package")
    parser.add_argument("--output", default=None, help="path of the .deb to write")
    args = parser.parse_args(argv)

    onedir = Path(args.onedir)
    if not onedir.is_absolute():
        onedir = REPO / onedir
    bundle = onedir / "QuantsMindAgent"
    if not bundle.is_dir():
        bundle = onedir
    if not (bundle / "QuantsMindAgent").is_file():
        print(f"no QuantsMindAgent executable in {bundle}", file=sys.stderr)
        return 2

    version = read_version()
    output = (
        Path(args.output)
        if args.output
        else onedir.parent / "deb" / f"{PACKAGE}_{version}_amd64.deb"
    )
    if not output.is_absolute():
        output = REPO / output

    if platform.system() != "Linux":
        environment(
            "built on a Linux system",
            False,
            f"this is {platform.system()}; a .deb must be built and installed on Linux",
        )
        print("\nnothing was built. See the module docstring for the command to run on Linux.")
        return 2

    print(f"packaging {bundle} -> {output}\n")
    started = time.time()
    try:
        files, size = build(bundle, output)
    except RuntimeError as exc:
        check("dpkg-deb build", False, str(exc))
        return 1

    check("application files packaged", files > 100, f"{files} files")
    check("deb produced", output.is_file(), f"{output.name} {size} bytes")
    with output.open("rb") as handle:
        check("ar archive (the deb container)", handle.read(8) == b"!<arch>\n", "")

    metadata = {
        "artifact": "deb",
        "version": version,
        "commit": git_commit(),
        "install_prefix": f"/opt/{PACKAGE}",
        "packaging_tool": "dpkg-deb",
        "packaging_command": f"python scripts/build_deb.py --onedir {args.onedir}",
        "built_on": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "build_seconds": round(time.time() - started, 1),
        "files": files,
        "filename": output.name,
        "bytes": size,
        "sha256": sha256_of(output),
        "user_data_location": "$XDG_CONFIG_HOME, $XDG_DATA_HOME (never packaged)",
    }
    (output.parent / "BUILD-METADATA.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )

    failures = [name for name, status, _d in checks if status == FAIL]
    print()
    print(f"{len(checks) - len(failures)}/{len(checks)} deb build checks passed")
    print(f"built: {output}")
    print("install with:  sudo dpkg -i " + output.name)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
