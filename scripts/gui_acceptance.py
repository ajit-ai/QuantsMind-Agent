"""Launch a packaged QuantsMind-Agent executable against a real display and
verify that it actually renders a window.

This is deliberately *not* a headless check. ``--check-config`` and the smoke
suite prove the bundle imports Qt and finds its platform plugin; they never open
a window, so they cannot catch a blank window, a failed window handle, or a
window that appears and is immediately destroyed. This script launches the real
executable with no ``QT_QPA_PLATFORM`` override, waits for a titled top-level
window to exist, and confirms the process is still alive.

What it verifies
----------------
* the executable starts on a real display server
* a top-level window with the expected title is created
* the process survives long enough to own that window (no startup crash)
* it can be closed, and the exit is observed

What it does **not** verify
--------------------------
* what is inside the window (no screenshot comparison is done here)
* the approval dialog, or Allow/Deny behaviour -- reaching those needs a live
  model to request a tool, which CI has no credential for
* a real human desktop session; see ``docs/source/testing/manual-acceptance.rst``

Usage::

    python scripts/gui_acceptance.py --distpath dist --workspace ./some-project

Requires a display: DISPLAY on Linux, an interactive session on Windows, a
logged-in session on macOS.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

WINDOW_TITLE_PREFIX = "QuantsMind-Agent"
PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, PASS if ok else FAIL, detail))
    print(f"[{PASS if ok else FAIL}] {name}" + (f" -- {detail}" if detail else ""), flush=True)


#: Packaged layout per platform: (bundle root, executable path within it).
LAYOUTS = {
    "Windows": ("QuantsMindAgent", "QuantsMindAgent.exe"),
    "Linux": ("QuantsMindAgent", "QuantsMindAgent"),
    "Darwin": ("QuantsMindAgent.app", "Contents/MacOS/QuantsMindAgent"),
}


def find_executable(distpath: Path) -> Path | None:
    """Locate the packaged executable, preferring the documented layout."""

    system = platform.system()
    if system not in LAYOUTS:
        return None
    root, exe = LAYOUTS[system]
    candidate = distpath / root / exe
    if candidate.exists():
        return candidate
    matches = sorted(p for p in distpath.rglob(Path(exe).name) if p.is_file())
    return matches[0] if matches else None


def window_titles_windows(pid: int) -> list[str]:
    """Title of the main top-level window owned by ``pid``.

    ``Get-Process`` reads ``MainWindowTitle``, which the window manager populates
    for a visible top-level window. This is used rather than an EnumWindows
    delegate because the delegate form needs an Add-Type delegate signature that
    is fragile to inline and silently returned nothing when it failed -- which
    would have looked exactly like "the app renders no window".
    """

    try:
        out = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"$p = Get-Process -Id {pid} -ErrorAction SilentlyContinue;"
                "if ($p) { $p.MainWindowTitle }",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return []
    return [line for line in out.splitlines() if line.strip()]


def window_titles_x11(pid: int) -> list[str]:
    """Titles of X11 windows owned by ``pid``, via xdotool."""

    if not shutil.which("xdotool"):
        return []
    try:
        out = subprocess.run(
            ["xdotool", "search", "--pid", str(pid), "getwindowname", "%@"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [line for line in out.stdout.splitlines() if line.strip()]


def window_titles_macos(pid: int) -> list[str]:
    """Titles of windows owned by ``pid``, via System Events."""

    try:
        out = subprocess.run(
            [
                "osascript",
                "-e",
                f'tell application "System Events" to tell (first process whose unix id is {pid})'
                " to get name of every window",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in out.stdout.split(",") if line.strip()]


def probe(pid: int) -> list[str]:
    system = platform.system()
    if system == "Windows":
        return window_titles_windows(pid)
    if system == "Linux":
        return window_titles_x11(pid)
    if system == "Darwin":
        return window_titles_macos(pid)
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distpath", default="dist")
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--config-dir", default=None)
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args(argv)

    distpath = Path(args.distpath)
    if not distpath.is_absolute():
        distpath = REPO / distpath

    system = platform.system()
    print(f"platform : {system} {platform.machine()}")
    print(f"display  : DISPLAY={os.environ.get('DISPLAY', '(unset)')}")
    print(f"QPA      : QT_QPA_PLATFORM={os.environ.get('QT_QPA_PLATFORM', '(unset)')}\n")

    exe = find_executable(distpath)
    if exe is None:
        print(f"no packaged executable under {distpath}", file=sys.stderr)
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="qm-guiacc-"))
    workspace = Path(args.workspace) if args.workspace else workdir / "project"
    if not args.workspace:
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "README.md").write_text("# GUI acceptance\n", encoding="utf-8")
    config_dir = Path(args.config_dir) if args.config_dir else workdir / "config-root"
    config_dir.mkdir(parents=True, exist_ok=True)

    command = [str(exe), "--workspace", str(workspace), "--config-dir", str(config_dir)]
    print(f"launching: {command[0]}\n")

    log = workdir / "app.log"
    log_handle = log.open("wb")
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        cwd=str(workdir),
    )

    titles: list[str] = []
    try:
        deadline = started + args.timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            titles = probe(process.pid)
            if any(WINDOW_TITLE_PREFIX in title for title in titles):
                break
            time.sleep(2)
        elapsed = time.monotonic() - started

        alive = process.poll() is None
        check(
            "executable is still running after startup",
            alive,
            f"exit={process.poll()} after {elapsed:.1f}s"
            if not alive
            else f"alive after {elapsed:.1f}s",
        )
        check(
            "a top-level window was created",
            bool(titles),
            f"titles={titles}",
        )
        check(
            "window title identifies the application",
            any(WINDOW_TITLE_PREFIX in title for title in titles),
            f"expected prefix {WINDOW_TITLE_PREFIX!r}, got {titles}",
        )
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        log_handle.close()

    shutdown = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    check(
        "no traceback during the session", "Traceback (most recent call last)" not in shutdown, ""
    )
    if args.config_dir is None:
        shutil.rmtree(workdir, ignore_errors=True)

    print()
    failures = [name for name, status, _d in results if status == FAIL]
    print(f"{len(results) - len(failures)}/{len(results)} GUI acceptance checks passed on {system}")
    if failures:
        print("failing: " + ", ".join(failures))
        if log.exists():
            print("\n--- application output ---")
            print(shutdown[-2000:])
        return 1
    print("note: this proves a window is created and survives. It does not verify")
    print("      what is drawn inside it, nor the approval dialog.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
