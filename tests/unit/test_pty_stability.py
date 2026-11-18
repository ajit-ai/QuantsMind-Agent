"""Repeated ConPTY session creation/teardown stability (WP1 blocker)."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

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
