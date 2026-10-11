"""Measure the frequency of the intermittent GUI test-process abort.

The abort has been seen in CI on ubuntu CPython 3.11 and 3.12, with a bare
``Fatal Python error`` banner or ``Aborted (core dumped)``, and no test named.
Re-running the identical tree usually passes, which is why it is hard to
characterise: one green run proves nothing.

This harness runs the same test suite repeatedly in one configuration and
records what each run actually did, distinguishing the outcomes that matter:

* ``0``   -- the suite passed
* ``1``   -- the suite ran and reported a test failure (a real, reproducible
              problem, and a *different* problem from an abort)
* ``134`` -- SIGABRT, the abort under investigation
* ``139`` -- SIGSEGV, a segmentation fault

It does not retry a failure, does not suppress anything, and exits non-zero if
any run aborted, so it cannot be mistaken for a green suite. Its purpose is to
turn "it fails sometimes" into a number that can be compared between
configurations.

Usage::

    python scripts/diagnose_gui_abort.py --iterations 10
    python scripts/diagnose_gui_abort.py --iterations 10 --label "no-warnings" -- -p no:warnings
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: pytest's exit code when tests fail. Deliberately *not* treated as an abort:
#: a genuine failure is a different defect and must not be counted as this bug.
EXIT_TEST_FAILURE = 1

#: pytest's exit code for a usage or collection error.
EXIT_COLLECTION_ERROR = 2

#: Outcomes that mean the suite genuinely ran. Anything else -- a collection
#: error, an unknown exit code -- means the process never got far enough to
#: produce a sample, and counting it as one would let a completely misconfigured
#: environment report a confident 0% abort rate from zero real runs.
EXECUTED_OUTCOMES = frozenset({"pass", "test-failure", "abort", "segfault"})


def classify(returncode: int) -> str:
    """Map a process exit code onto the outcome this harness cares about."""

    if returncode == 0:
        return "pass"
    if returncode == EXIT_TEST_FAILURE:
        return "test-failure"
    if returncode == EXIT_COLLECTION_ERROR:
        return "collection-error"
    if returncode in (-6, 134):
        return "abort"
    if returncode in (-11, 139):
        return "segfault"
    return f"other({returncode})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--label", default="baseline")
    parser.add_argument("--target", default="tests/gui")
    parser.add_argument(
        "--summary",
        default=None,
        help="write a JSON summary here (for CI job summaries)",
    )
    parser.add_argument(
        "--log-dir",
        default=None,
        help="write each run's full output to this directory (recommended in CI)",
    )
    parser.add_argument(
        "pytest_args",
        nargs="*",
        help="extra arguments passed through to pytest, after a bare --",
    )
    args = parser.parse_args(argv)

    extra = [a for a in args.pytest_args if a != "--"]
    log_dir = Path(args.log_dir) if args.log_dir else None
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)

    command = [sys.executable, "scripts/run_qt_tests.py", args.target, "-q", *extra]

    # Each run gets its own basetemp. pytest's default is a single shared
    # directory that it rewrites per run, so back-to-back runs -- or a stale
    # directory some other process still holds open -- can make every run fail
    # with "PermissionError: ... pytest-of-<user>" and look like a real test
    # failure. Isolating them keeps a measurement measuring what it claims to.
    basetemp_root = Path(tempfile.gettempdir()) / f"qm-gui-abort-{args.label}"

    env_report = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "qt_qpa_platform": os.environ.get("QT_QPA_PLATFORM", "(unset)"),
        "pyside6": _pyside6_version(),
        "pytest": _pytest_version(),
    }

    print(f"configuration: {args.label}")
    for key, value in env_report.items():
        print(f"  {key:16} {value}")
    print(f"  {'command':16} {' '.join(command)}")
    print(f"  {'iterations':16} {args.iterations}\n", flush=True)

    outcomes: Counter[str] = Counter()
    codes: list[int] = []
    started = time.monotonic()

    for index in range(1, args.iterations + 1):
        run_started = time.monotonic()
        run_basetemp = basetemp_root / f"run{index:02d}"
        shutil.rmtree(run_basetemp, ignore_errors=True)
        run_basetemp.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            [*command, "--basetemp", str(run_basetemp)],
            cwd=REPO,
            capture_output=True,
            env=dict(os.environ),
            check=False,
        )
        elapsed = time.monotonic() - run_started
        tail = (completed.stdout + completed.stderr).decode("utf-8", "replace")
        outcome = classify(completed.returncode)
        outcomes[outcome] += 1
        codes.append(completed.returncode)
        banner = "clean" if outcome == "pass" else outcome.upper()
        print(
            f"  run {index:>3}/{args.iterations}: exit={completed.returncode:<4} "
            f"{banner:<16} {elapsed:6.1f}s",
            flush=True,
        )
        if outcome != "pass":
            if log_dir is not None:
                log_path = log_dir / f"{args.label}-run{index:02d}.log"
                log_path.write_text(tail, encoding="utf-8")
                print(f"        full output: {log_path}", flush=True)
            # Only the failure banner; a full suite log would bury it.
            for line in tail.splitlines():
                if (
                    "Fatal Python error" in line
                    or "Aborted" in line
                    or "Segmentation" in line
                    or "cannot open shared object file" in line
                ):
                    print(f"        > {line.strip()}", flush=True)

    total = args.iterations
    aborts = outcomes["abort"] + outcomes["segfault"]
    collection_errors = outcomes["collection-error"]

    # A run that died in collection never executed the suite. Counting it as a
    # sample would dilute the abort rate and, worse, make a completely
    # misconfigured environment look like a clean result.
    executed = sum(outcomes[name] for name in EXECUTED_OUTCOMES)
    summary = {
        "label": args.label,
        "environment": env_report,
        "command": command,
        "iterations": total,
        "executed_runs": executed,
        "collection_errors": collection_errors,
        "outcomes": dict(outcomes),
        "exit_codes": codes,
        "process_aborts": aborts,
        "abort_rate": (round(aborts / executed, 3) if executed else None),
        "wall_seconds": round(time.monotonic() - started, 1),
    }

    print("\nsummary")
    for outcome, count in sorted(outcomes.items()):
        print(f"  {outcome:<16} {count}/{total}")
    if executed:
        print(f"  abort rate      {aborts}/{executed} executed runs ({aborts / executed:.0%})")
    else:
        print(f"  abort rate      NOT MEASURED -- 0 of {total} runs executed the suite")

    if args.summary:
        Path(args.summary).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {args.summary}")

    if executed != total:
        print(
            f"\nERROR: {total - executed}/{total} runs did not execute the suite, so the "
            "abort rate above is not a measurement.\n"
            "Outcomes seen: "
            + ", ".join(
                f"{k}={v}" for k, v in sorted(outcomes.items()) if k not in EXECUTED_OUTCOMES
            )
            + "\nMost often this means the Qt system libraries are missing on this\n"
            "runner (for example libEGL.so.1). Install them and re-run; see the step\n"
            "'Install Qt system libraries' in .github/workflows/diagnose-gui-abort.yml.",
            file=sys.stderr,
        )

    # Any run that failed to produce a sample invalidates the measurement, not
    # just the ones we recognise by name. An abort is a failure of this
    # harness's purpose; a suite that never ran is not a pass either.
    return 1 if aborts or executed != total else 0


def _pyside6_version() -> str:
    try:
        import PySide6

        return str(PySide6.__version__)
    except Exception:  # pragma: no cover - diagnostic only
        return "(unavailable)"


def _pytest_version() -> str:
    try:
        import pytest

        return str(pytest.__version__)
    except Exception:  # pragma: no cover - diagnostic only
        return "(unavailable)"


if __name__ == "__main__":
    raise SystemExit(main())
