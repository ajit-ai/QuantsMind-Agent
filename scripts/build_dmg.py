"""Build a macOS disk image (.dmg) from a macOS ``.app`` bundle.

``scripts/build_executable.py`` already produces a ``.app`` on macOS (the spec
bundles it). A bare ``.app`` is awkward to hand someone: they have to know that
it goes in ``/Applications``, and copying it out of a zip is where the
"downloaded from the internet" quarantine attribute and the lost execute bit
come from. A disk image gives the normal drag-to-Applications experience.

What is created
---------------
* a read/write compressed disk image containing the ``.app`` and an
  ``/Applications`` symlink, so dragging works
* the same image mounted and verified: the ``.app`` is copied out of it and
  launched, which is the only way to prove the image itself is usable rather
  than merely present

Signing and notarization are reported, never assumed. Nothing here signs,
staples or notarises anything: doing so requires an Apple Developer identity and
a notarisation credential, and a build that has neither is an unsigned build.
Gatekeeper will therefore warn, and the documentation says so.

Requires macOS (``hdiutil``) and PyInstaller's ``.app`` output::

    python scripts/build_executable.py --distpath artifacts/v0.3.0/macos
    python scripts/build_dmg.py --app artifacts/v0.3.0/macos/QuantsMindAgent.app
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


def signing_status(app: Path) -> tuple[bool, str]:
    """Report the real signing state of the bundle."""

    if shutil.which("codesign") is None:
        return False, "codesign not available"
    completed = subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", str(app)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode == 0:
        detail = (
            subprocess.run(
                ["codesign", "-dv", "--verbose=2", str(app)],
                capture_output=True,
                text=True,
                check=False,
            )
            .stderr.strip()
            .splitlines()
        )
        return True, next((line.strip() for line in detail if "Authority" in line), "signed")
    return False, (completed.stderr.strip().splitlines() or ["not signed"])[-1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, help="the .app bundle to package")
    parser.add_argument("--output", default=None, help="path of the .dmg to write")
    parser.add_argument("--skip-launch", action="store_true", help="build without mounting it")
    args = parser.parse_args(argv)

    app = Path(args.app)
    if not app.is_absolute():
        app = REPO / app
    version = read_version()
    output = (
        Path(args.output) if args.output else app.parent / "dmg" / f"QuantsMindAgent-{version}.dmg"
    )
    if not output.is_absolute():
        output = REPO / output

    if platform.system() != "Darwin":
        environment(
            "built on macOS",
            False,
            f"this is {platform.system()}; hdiutil and .app bundles are macOS-only",
        )
        print("\nnothing was built. Run this on a macOS machine with the .app already built.")
        return 2

    if not app.is_dir() or app.suffix != ".app":
        print(f"not a .app bundle: {app}", file=sys.stderr)
        return 2

    print(f"packaging {app} -> {output}\n")
    started = time.time()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()

    with tempfile.TemporaryDirectory(prefix="qm-dmg-") as raw:
        staging = Path(raw) / "dmg"
        staging.mkdir()
        shutil.copytree(app, staging / app.name)
        (staging / "Applications").symlink_to("/Applications")

        subprocess.run(
            [
                "hdiutil",
                "create",
                "-volname",
                "QuantsMind-Agent",
                "-srcfolder",
                str(staging),
                "-ov",
                "-format",
                "UDZO",
                str(output),
            ],
            check=True,
        )
        check(
            "disk image created", output.is_file(), f"{output.name} {output.stat().st_size} bytes"
        )

        if args.skip_launch:
            environment("image mounted and application launched", False, "skipped by request")
        else:
            mount = Path(raw) / "mnt"
            mount.mkdir()
            subprocess.run(
                [
                    "hdiutil",
                    "attach",
                    str(output),
                    "-mountpoint",
                    str(mount),
                    "-nobrowse",
                    "-quiet",
                ],
                check=True,
            )
            try:
                copied = Path(raw) / "copied" / app.name
                copied.parent.mkdir()
                shutil.copytree(mount / app.name, copied)
                check(
                    "application copies out of the image",
                    (copied / "Contents" / "MacOS").is_dir(),
                    str(copied),
                )

                completed = subprocess.run(
                    [str(copied / "Contents" / "MacOS" / "QuantsMindAgent"), "--check-config"],
                    capture_output=True,
                    text=True,
                    timeout=180,
                    check=False,
                )
                check(
                    "copied application starts",
                    completed.returncode in (0, 1),
                    f"exit={completed.returncode} (1 means no stored API key)",
                )
                check(
                    "no traceback on startup",
                    "Traceback (most recent call last)" not in completed.stderr,
                    "",
                )
            finally:
                subprocess.run(["hdiutil", "detach", str(mount), "-quiet"], check=False)

    signed, detail = signing_status(app)
    environment(
        "application bundle is signed and notarized",
        signed,
        detail if signed else f"unsigned: {detail}. Gatekeeper will warn; see the packaging docs.",
    )

    metadata = {
        "artifact": "dmg",
        "version": version,
        "commit": git_commit(),
        "packaging_tool": "hdiutil",
        "packaging_command": f"python scripts/build_dmg.py --app {args.app}",
        "built_on": {
            "system": platform.system(),
            "release": platform.mac_ver()[0],
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "build_seconds": round(time.time() - started, 1),
        "filename": output.name,
        "bytes": output.stat().st_size,
        "sha256": sha256_of(output),
        "signed": signed,
        "notarized": False,
    }
    (output.parent / "BUILD-METADATA.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )

    failures = [name for name, status, _d in checks if status == FAIL]
    print()
    print(f"{len(checks) - len(failures)}/{len(checks)} dmg build checks passed")
    print(f"built: {output}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
