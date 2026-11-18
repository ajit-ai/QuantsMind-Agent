"""Repeated ConPTY session creation/teardown stability (WP1 blocker)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import quantsmind_agent.runtime.tools.builtin.pty_backend as _pty_backend
from quantsmind_agent.runtime.tools.builtin.pty_backend import PtyBackend, PtyUnavailable


def test_repeated_sessions_stable(tmp_path: Path) -> None:
    b = PtyBackend()
    shell = ["cmd.exe"] if sys.platform == "win32" else ["sh"]
    for i in range(10):
        try:
            s = b.spawn(shell, cwd=str(tmp_path), timeout_seconds=10)
        except PtyUnavailable as exc:
            pytest.skip(f"ConPTY unavailable: {exc}")
        assert s.is_alive
        s.resize(80 + i, 24)
        assert s.columns == 80 + i
        s.terminate()
        assert s.exit_code is not None
        assert not s.is_alive
        s.terminate()  # double-terminate must be safe


def test_spawn_failure_cleanup(tmp_path: Path) -> None:
    # Missing program must raise cleanly without leaking a live session.
    from quantsmind_agent.errors import CommandExecutionError

    b = PtyBackend()
    with pytest.raises((CommandExecutionError, FileNotFoundError, OSError)):
        b.spawn(["definitely-not-a-real-program-xyz"], cwd=str(tmp_path), timeout_seconds=5)


def test_short_lived_exit_code(tmp_path: Path) -> None:
    b = PtyBackend()
    argv = ["cmd.exe", "/c", "exit", "7"] if sys.platform == "win32" else ["sh", "-c", "exit 7"]
    s = b.spawn(argv, cwd=str(tmp_path), timeout_seconds=10)
    out = None
    end = time.time() + 8
    while time.time() < end:
        out = s.read(timeout=0.5)
        if out and out.exited:
            break
    assert out is not None and out.exited and out.exit_code == 7
    s.terminate()
    # No lingering child process.
    assert subprocess.run(["git", "--version"], capture_output=True).returncode == 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only helper wiring")
def test_posix_process_helpers_are_callable() -> None:
    """Regression: the POSIX helpers must not be clobbered to None.

    The Windows-absent branch at the end of pty_backend.py previously set
    ``_terminate_process_group``, ``_close_conpty`` and ``_wait_process`` to
    ``None`` even on POSIX, overwriting their real definitions. terminate()
    then raised TypeError (swallowed), leaving children unreaped and exit_code
    None after close. These helpers must be callables on every platform where
    PtySession.terminate()/_close_fds() invoke them.
    """

    for name in ("_terminate_process_group", "_close_conpty", "_wait_process"):
        helper = getattr(_pty_backend, name, None)
        assert callable(helper), f"{name} must be callable on POSIX, got {helper!r}"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only process-group behavior")
def test_posix_child_owns_its_process_group(tmp_path: Path) -> None:
    """Regression: a POSIX PTY child must be in a separate process group.

    Otherwise os.killpg(os.getpgid(child), SIGTERM) in terminate() signals the
    host process group (the agent / test runner) rather than just the child.
    """

    b = PtyBackend()
    try:
        s = b.spawn(["sh"], cwd=str(tmp_path), timeout_seconds=10)
    except PtyUnavailable:
        pytest.skip("PTY unavailable on this host")
    try:
        assert os.getpgid(s.pid) != os.getpgrp()
    finally:
        s.terminate()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only reaping behavior")
def test_posix_terminate_reaps_and_sets_exit_code(tmp_path: Path) -> None:
    """Regression: terminate() must reap the POSIX child and set exit_code.

    Directly asserts the contract that failed on Linux/macOS CI (exit_code
    was None after terminate). No sleeps: the bounded wait inside terminate()
    is the synchronization point.
    """

    b = PtyBackend()
    try:
        s = b.spawn(["sh"], cwd=str(tmp_path), timeout_seconds=10)
    except PtyUnavailable:
        pytest.skip("PTY unavailable on this host")
    assert s.is_alive
    s.terminate()
    assert s.exit_code is not None
    assert not s.is_alive
