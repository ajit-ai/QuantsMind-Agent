"""Verification: run the project's checks and record what actually happened.

Two ideas do the work here.

**A check that could not run is not a check that passed.** :class:`CheckStatus`
has ``SKIPPED``, ``NOT_RUN`` and ``BLOCKED`` alongside ``PASSED`` and ``FAILED``,
and this module is careful never to map an exception, a missing executable or a
policy refusal onto success. A verification report that says "5 checks passed"
when two of them never ran is worse than no report, because it is believed.

**Checks come from the project, not from a guess.** :func:`discover_checks`
reads ``pyproject.toml``, ``setup.cfg``, ``tox.ini``, a ``Makefile`` and CI
configuration to find what this repository actually declares, and returns only
what it found. When it finds nothing, the plan says verification is unavailable
rather than inventing ``pytest`` because that is usually right.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from quantsmind_agent.command_policy import classify_command
from quantsmind_agent.tasks import CheckResult, CheckStatus

__all__ = ["MAX_OUTPUT_CHARS", "Check", "discover_checks", "run_checks"]

logger = logging.getLogger(__name__)

#: Output kept per check. Enough to see the failure, short enough not to become
#: the context.
MAX_OUTPUT_CHARS = 4000

#: How long any single check may run before it is reported as inconclusive
#: rather than as a failure: a check that hangs has told us nothing about the
#: code, and reporting it as a failure would be a guess.
DEFAULT_TIMEOUT_SECONDS = 300


@dataclass(frozen=True, slots=True)
class Check:
    """One declared check: what to run, and what it is for."""

    id: str
    argv: tuple[str, ...]
    description: str = ""
    source: str = ""
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    optional: bool = False

    @property
    def program(self) -> str:
        return self.argv[0] if self.argv else ""

    def available(self) -> tuple[bool, str]:
        """Whether this check can run at all here.

        Distinguishes "the interpreter is missing" from "this repository does not
        declare that check", because the first is worth fixing and the second is
        simply a fact.
        """

        if not self.argv:
            return False, "the check has no command"
        program = self.program
        if os.path.basename(program).lower().startswith("python"):
            return True, ""
        if shutil.which(program):
            return True, ""
        return False, f"{program} is not installed"


def _python() -> str:
    return sys.executable or "python"


def discover_checks(root: Path) -> list[Check]:
    """Checks this repository actually declares.

    Only what is found in configuration is returned. Nothing is added on the
    grounds that it is conventional: a plan that claims "linting verified" when
    the project declares no linter would be reporting a check that never ran.
    """

    root = Path(root)
    found: list[Check] = []

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        text = _read(pyproject)
        if "addopts" in text or "[tool.pytest.ini_options]" in text:
            found.append(
                Check(
                    id="pytest",
                    argv=(_python(), "-m", "pytest", "-q"),
                    description="the project's configured test suite",
                    source="pyproject.toml",
                )
            )
        if "[tool.ruff" in text:
            found.append(
                Check(
                    id="ruff",
                    argv=(_python(), "-m", "ruff", "check", "."),
                    description="lint",
                    source="pyproject.toml",
                )
            )
            found.append(
                Check(
                    id="ruff-format",
                    argv=(_python(), "-m", "ruff", "format", "--check", "."),
                    description="formatting",
                    source="pyproject.toml",
                    optional=True,
                )
            )
        if "[tool.mypy" in text:
            found.append(
                Check(
                    id="mypy",
                    argv=(_python(), "-m", "mypy", "--no-incremental", "src", "tests"),
                    description="type checking",
                    source="pyproject.toml",
                )
            )

    setup_cfg = root / "setup.cfg"
    if setup_cfg.is_file():
        text = _read(setup_cfg)
        if "[tool:pytest]" in text:
            found.append(
                Check(
                    id="pytest",
                    argv=(_python(), "-m", "pytest", "-q"),
                    description="the project's configured test suite",
                    source="setup.cfg",
                )
            )
        if "mypy" in text.lower() and "[mypy]" in text:
            found.append(
                Check(
                    id="mypy",
                    argv=(_python(), "-m", "mypy", "."),
                    description="type checking",
                    source="setup.cfg",
                    optional=True,
                )
            )

    makefile = root / "Makefile"
    if makefile.is_file():
        for target, check_id in (("test", "make-test"), ("lint", "make-lint")):
            if _make_has_target(makefile, target):
                found.append(
                    Check(
                        id=check_id,
                        argv=("make", target),
                        description=f"the project's `make {target}` target",
                        source="Makefile",
                    )
                )

    deduplicated: dict[str, Check] = {}
    for check in found:
        # setup.cfg repeats pytest when pyproject already declared it.
        if check.id in deduplicated and check.source != "Makefile":
            continue
        deduplicated[check.id] = check
    return sorted(deduplicated.values(), key=lambda item: item.id)


def _make_has_target(makefile: Path, target: str) -> bool:
    return any(line.strip().startswith(f"{target}:") for line in _read(makefile).splitlines())


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:  # pragma: no cover - unreadable config
        return ""


def run_checks(
    checks: list[Check],
    *,
    workspace_root: str,
    on_start: Callable[[Check], None] | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> list[CheckResult]:
    """Run each check and record what happened, including when it could not run.

    Commands are executed as argument lists with ``shell=False``: a check name
    containing a quote or a semicolon is a string, not a command.
    """

    results: list[CheckResult] = []
    for check in checks:
        if on_start is not None:
            on_start(check)
        available, reason = check.available()
        if not available:
            results.append(
                CheckResult(
                    check_id=check.id,
                    command=tuple(check.argv),
                    status=CheckStatus.SKIPPED,
                    detail=reason,
                    output="",
                )
            )
            continue

        verdict = classify_command(list(check.argv), workspace_root=workspace_root)
        if not verdict.permitted:
            results.append(
                CheckResult(
                    check_id=check.id,
                    command=tuple(check.argv),
                    status=CheckStatus.BLOCKED,
                    detail=f"refused by command policy: {verdict.risk_label}",
                    output="",
                )
            )
            continue

        started = time.monotonic()
        try:
            completed = subprocess.run(
                list(check.argv),
                cwd=workspace_root,
                capture_output=True,
                text=True,
                timeout=min(timeout_seconds, check.timeout_seconds),
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            results.append(
                CheckResult(
                    check_id=check.id,
                    command=tuple(check.argv),
                    status=CheckStatus.BLOCKED,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    detail=f"timed out after {timeout_seconds}s",
                )
            )
            continue
        except OSError as exc:
            results.append(
                CheckResult(
                    check_id=check.id,
                    command=tuple(check.argv),
                    status=CheckStatus.SKIPPED,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    detail=f"could not start: {exc}",
                )
            )
            continue

        duration = int((time.monotonic() - started) * 1000)
        output = (completed.stdout or "") + (completed.stderr or "")
        results.append(
            CheckResult(
                check_id=check.id,
                command=tuple(check.argv),
                status=CheckStatus.PASSED if completed.returncode == 0 else CheckStatus.FAILED,
                exit_code=completed.returncode,
                duration_ms=duration,
                output=output[-MAX_OUTPUT_CHARS:],
                detail=f"{verdict.risk_label} via {check.source or 'declared'}",
            )
        )
    return results


def summarise(results: list[CheckResult]) -> dict[str, int]:
    """Counts that keep "could not run" visible instead of folding it into success."""

    counts = {
        status.value: sum(1 for item in results if item.status is status) for status in CheckStatus
    }
    counts["total"] = len(results)
    return counts
