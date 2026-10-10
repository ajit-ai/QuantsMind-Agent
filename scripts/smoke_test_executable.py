"""Smoke-test a packaged QuantsMind-Agent executable.

Headless by design: it drives ``--check-config``, which loads configuration,
creates the application directories, registers the tools and reports readiness
without opening a window. That exercises the whole frozen bundle -- Qt plugin
discovery, Python imports, keyring backend, path resolution -- and then exits,
so a broken bundle fails here rather than on a user's desktop.

This does **not** replace desktop acceptance. Opening a real window, driving the
approval dialog, and closing the application still need a human or a full
desktop automation stack; see ``docs/source/testing/manual-acceptance.rst``.

Usage::

    python scripts/smoke_test_executable.py [--distpath dist]

Exit code 0 means every check passed.
"""

from __future__ import annotations

import argparse
import json
import locale
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: Executable name per platform, relative to the dist directory.
LAYOUT = {
    "Windows": ("QuantsMindAgent", "QuantsMindAgent.exe"),
    "Linux": ("QuantsMindAgent", "QuantsMindAgent"),
    "Darwin": ("QuantsMindAgent.app", "Contents/MacOS/QuantsMindAgent"),
}

#: The executable's file name, used for the discovery fallback.
EXE_NAME = {
    "Windows": "QuantsMindAgent.exe",
    "Linux": "QuantsMindAgent",
    "Darwin": "QuantsMindAgent",
}

#: Qt platform plugin that must ship for the app to start at all.
PLATFORM_PLUGIN = {"Windows": "qwindows", "Linux": "libqxcb", "Darwin": "libqcocoa"}

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, PASS if ok else FAIL, detail))
    print(f"[{PASS if ok else FAIL}] {name}" + (f" -- {detail}" if detail else ""), flush=True)


def locate(distpath: Path) -> Path | None:
    """Find the packaged executable for the current platform.

    The documented layout is tried first. If it is not there, the dist
    directory is searched for the executable by name: PyInstaller's exact
    output nesting differs between platforms and between its onedir and
    onefile modes, and a smoke test that hard-codes one layout reports "no
    executable" on a perfectly good build.
    """

    system = platform.system()
    if system not in LAYOUT:
        return None

    root, exe = LAYOUT[system]
    candidate = distpath / root / exe
    if candidate.exists():
        return candidate

    wanted = EXE_NAME[system]
    matches = sorted(p for p in distpath.rglob(wanted) if p.is_file())
    if not matches:
        return None
    # Prefer the shallowest match: the real bundle is less deeply nested than
    # anything copied inside it.
    return min(matches, key=lambda p: len(p.parts))


def decode_output(raw: bytes) -> str:
    """Decode a child process's stdout the way that child encoded it.

    A windowed PyInstaller build writes stdout in the machine's legacy ANSI code
    page (cp1252 on Windows) and ignores both PYTHONIOENCODING and PYTHONUTF8 --
    verified against a real build. Decoding it as UTF-8 turns every non-ASCII
    character into U+FFFD, which would make the non-ASCII workspace check measure
    the console encoding rather than the application's path handling.

    UTF-8 is tried first, because that is what a console-mode build and the
    Linux/macOS runners produce; the locale encoding is the fallback.
    """

    for encoding in ("utf-8", locale.getpreferredencoding(False), "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def run(exe: Path, args: list[str], cwd: Path) -> tuple[int, str, str]:
    """Run the executable and capture its output."""

    completed = subprocess.run(
        [str(exe), *args],
        cwd=str(cwd),
        capture_output=True,
        timeout=180,
        check=False,
    )
    return (
        completed.returncode,
        decode_output(completed.stdout or b""),
        decode_output(completed.stderr or b""),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distpath", default="dist")
    args = parser.parse_args(argv)
    distpath = Path(args.distpath)
    if not distpath.is_absolute():
        distpath = REPO / distpath

    system = platform.system()
    exe = locate(distpath)
    if exe is None:
        print(f"no packaged executable found under {distpath} for {system}", file=sys.stderr)
        return 2

    print(f"smoke-testing {exe}\n")
    check("executable exists", exe.is_file(), str(exe))

    # -- the bundle must ship Qt's platform plugin ------------------------
    plugin = PLATFORM_PLUGIN.get(system)
    if plugin:
        found = any(distpath.rglob(f"{plugin}*"))
        check("Qt platform plugin is bundled", found, f"expected {plugin}*")

    # -- nothing user-specific may be baked in ----------------------------
    leaked = [
        str(p.relative_to(distpath))
        for p in distpath.rglob("*")
        if p.is_file() and p.name in {"config.toml", "secrets.json"}
    ]
    check("no configuration or credential file is bundled", not leaked, ", ".join(leaked))

    metadata = distpath / "BUILD-METADATA.json"
    if metadata.exists():
        data = json.loads(metadata.read_text(encoding="utf-8"))
        check(
            "build metadata records the platform",
            data.get("built_on", {}).get("system") == system,
            f"{data.get('built_on')} layout={data.get('layout')} commit={data.get('commit')}",
        )
    else:
        check("build metadata present", False, "BUILD-METADATA.json missing")

    # -- startup, directories, readiness ----------------------------------
    workdir = Path(tempfile.mkdtemp(prefix="qm-smoke-"))
    config_dir = workdir / "config-root"
    workspace = workdir / "smoke workspace ünïcode"
    workspace.mkdir(parents=True)
    (workspace / "hello.py").write_text("print('hi')\n", encoding="utf-8")
    (workspace / "notes.md").write_text("# smoke\n", encoding="utf-8")

    try:
        code, out, err = run(exe, ["--check-config", "--config-dir", str(config_dir)], workdir)
        check(
            "starts and reports a not-ready state without a workspace",
            code == 1 and "ready       : no" in out,
            f"exit={code}",
        )
        check(
            "creates config, log, cache and data directories",
            all((config_dir / name).is_dir() for name in ("config", "logs", "cache", "data")),
            str(config_dir),
        )
        check(
            "writes a log file",
            (config_dir / "logs" / "quantsmind-agent.log").is_file(),
            "",
        )
        check(
            "reports an API key only in masked form",
            "api key" in out and ("****" in out or "not set" in out.lower()),
            "no raw credential in output",
        )
        check("no traceback on startup", "Traceback (most recent call last)" not in err, "")

        # -- workspace and unicode path handling --------------------------
        code, out, err = run(
            exe,
            ["--check-config", "--config-dir", str(config_dir), "--workspace", str(workspace)],
            workdir,
        )
        check("opens a workspace with a non-ASCII path", "ready       : yes" in out, f"exit={code}")
        check(
            "registers the full tool set",
            "run_command" in out and "edit_file" in out and "write_file" in out,
            "",
        )
        check(
            "workspace path round-trips unchanged",
            str(workspace) in out,
            str(workspace),
        )

        # -- a bad argument must fail clearly, not silently ---------------
        code, _out, err = run(exe, ["--no-such-flag"], workdir)
        check("rejects an unknown flag with a non-zero exit", code != 0, f"exit={code}")

        # -- a missing workspace must be reported, not crash --------------
        missing = workdir / "does-not-exist"
        code, out, err = run(
            exe,
            ["--check-config", "--config-dir", str(config_dir), "--workspace", str(missing)],
            workdir,
        )
        check(
            "reports a missing workspace without crashing",
            code != 0 and "Traceback (most recent call last)" not in err,
            f"exit={code}",
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print()
    failures = [entry for entry in results if entry[1] == FAIL]
    print(f"{len(results) - len(failures)}/{len(results)} smoke checks passed on {system}")
    if failures:
        print("failing checks: " + ", ".join(name for name, _s, _d in failures))
        return 1
    print("note: this does not verify the GUI, the approval dialog, or desktop")
    print("      behaviour. Those remain manual desktop acceptance checks.")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    raise SystemExit(main())
