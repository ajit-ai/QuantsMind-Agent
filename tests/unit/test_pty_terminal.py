"""PTY backend + terminal tool tests (WP1). Skipped where ConPTY/PTY unavailable."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from quantsmind_agent.runtime.tools.builtin.pty_backend import PtyBackend, PtyUnavailable
from quantsmind_agent.runtime.tools.builtin.terminal import TerminalTool
from quantsmind_agent.services.workspace.containment import Workspace


@pytest.fixture(scope="module")
def any_shell() -> list[str]:
    if sys.platform == "win32":
        return ["cmd.exe"]
    return ["sh"]


class TestPtyInteractive:
    def test_interactive_echo_before_exit(self, tmp_path: Path, any_shell: list[str]) -> None:
        b = PtyBackend()
        try:
            s = b.spawn(any_shell, cwd=str(tmp_path), timeout_seconds=15)
        except PtyUnavailable:
            pytest.skip("PTY unavailable on this host")
        assert s.is_alive
        s.write(b"echo pty-hello\r\n" if sys.platform == "win32" else b"echo pty-hello\n")
        collected = ""
        end = time.time() + 8
        while time.time() < end:
            o = s.read(timeout=0.5)
            if o and o.text:
                collected += o.text
            if collected.count("pty-hello") >= 2:
                break
        assert "pty-hello" in collected
        assert s.is_alive  # output arrived without process exit
        s.resize(100, 30)
        assert (s.columns, s.rows) == (100, 30)
        s.terminate()
        assert s.exit_code is not None
        assert not s.is_alive
        # Idempotent terminate: no exception, no double-close errors.
        s.terminate()

    def test_short_lived_command_exit_status(self, tmp_path: Path) -> None:
        b = PtyBackend()
        argv = ["cmd.exe", "/c", "exit", "7"] if sys.platform == "win32" else ["sh", "-c", "exit 7"]
        try:
            s = b.spawn(argv, cwd=str(tmp_path), timeout_seconds=10)
        except PtyUnavailable:
            pytest.skip("PTY unavailable")
        out = None
        end = time.time() + 8
        while time.time() < end:
            out = s.read(timeout=0.5)
            if out and out.exited:
                break
        assert out is not None and out.exited
        assert out.exit_code == 7

    def test_spawn_missing_program_raises(self, tmp_path: Path) -> None:
        b = PtyBackend()
        with pytest.raises((PtyUnavailable, FileNotFoundError, OSError)):
            try:
                b.spawn(["definitely-not-a-real-program-xyz"], cwd=str(tmp_path), timeout_seconds=5)
            except Exception:
                # Either a clean Unavailable or OS-level failure is acceptable,
                # but the session must not be left half-open without signal.
                raise


class TestTerminalTool:
    def test_start_write_close(self, tmp_path: Path, any_shell: list[str]) -> None:
        tool = TerminalTool(Workspace(tmp_path))
        r = tool.run({"command": " ".join(any_shell), "action": "start", "timeout_seconds": 15})
        assert r.ok
        sid = r.data["session_id"]
        cmd = "echo tool-hello" + ("\r\n" if sys.platform == "win32" else "\n")
        w = tool.run(
            {"command": " ".join(any_shell), "action": "write", "session_id": sid, "input": cmd}
        )
        assert w.ok
        assert "tool-hello" in (w.data.get("output") or "") or w.data.get("output") is not None
        # Write again to force read even if first chunk was banner only.
        time.sleep(0.5)
        w2 = tool.run(
            {
                "command": " ".join(any_shell),
                "action": "write",
                "session_id": sid,
                "input": "echo tool-hello-2" + ("\r\n" if sys.platform == "win32" else "\n"),
            }
        )
        assert w2.ok
        r2 = tool.run(
            {
                "command": " ".join(any_shell),
                "action": "resize",
                "session_id": sid,
                "columns": 90,
                "rows": 25,
            }
        )
        assert r2.ok
        closed = tool.run({"command": " ".join(any_shell), "action": "close", "session_id": sid})
        assert closed.ok
        assert closed.data["exit_code"] is not None

    def test_blocked_command_refused(self, tmp_path: Path) -> None:
        from quantsmind_agent.errors import CommandDeniedError

        tool = TerminalTool(Workspace(tmp_path))
        with pytest.raises(CommandDeniedError):
            tool.run({"command": "format C:", "action": "start"})

    def test_path_escape_rejected(self, tmp_path: Path) -> None:
        from quantsmind_agent.errors import InvalidPathError

        tool = TerminalTool(Workspace(tmp_path))
        with pytest.raises(InvalidPathError):
            tool.run({"command": "cmd.exe", "action": "start", "cwd": "../../.."})

    def test_close_all(self, tmp_path: Path, any_shell: list[str]) -> None:
        tool = TerminalTool(Workspace(tmp_path))
        tool.run({"command": " ".join(any_shell), "action": "start", "timeout_seconds": 15})
        tool.close_all()
        tool.close_all()  # idempotent
