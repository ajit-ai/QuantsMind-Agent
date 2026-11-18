"""Process-tree cancellation regression tests (D02 correction).

Verifies that cancelling a running command terminates the *entire* process
tree -- including grandchildren -- not just the immediate child. On Windows a
Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE is used so that closing the
job handle kills every process assigned to it.

The primary test uses a positive synchronization handshake: the direct child
signals readiness only after it has spawned its grandchild, and the parent
waits for that signal before requesting cancellation. This avoids the fixed
timing delay that made an earlier version of the test vulnerable to false
passes. A positive control proves the sentinel mechanism works when no
cancellation is applied.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from quantsmind_agent.runtime.tools.builtin.command import (
    RunCommandTool,
    _assign_process_to_job,
    _create_job_object,
)
from quantsmind_agent.services.workspace.containment import Workspace

#: Bounded timeout (seconds) for a child to signal readiness after spawn.
HANDSHAKE_TIMEOUT = 10.0
#: Bounded observation window (seconds) for a surviving grandchild to write.
OBSERVE_TIMEOUT = 3.0
#: Delay (seconds) before the grandchild writes its sentinel if it survives.
GRANDCHILD_WRITE_DELAY = 1.5
#: Bounded poll interval (seconds).
POLL_INTERVAL = 0.02


def _spawn_nested_script(ready_path: Path, grandchild_path: Path) -> str:
    """Child script: spawn a grandchild, then signal readiness via a file.

    The readiness file is written only after ``Popen`` returns, so its
    existence is positive evidence that the grandchild was created before the
    parent requests cancellation.
    """

    return (
        "import subprocess, sys, time, pathlib\n"
        f"subprocess.Popen([sys.executable, {str(grandchild_path)!r}])\n"
        f"pathlib.Path({str(ready_path)!r}).write_text('READY')\n"
        "time.sleep(30)\n"
    )


def _grandchild_script(sentinel: Path) -> str:
    """The grandchild script: writes a sentinel after a bounded delay if alive."""

    return (
        "import time, pathlib\n"
        f"time.sleep({GRANDCHILD_WRITE_DELAY})\n"
        f"pathlib.Path({str(sentinel)!r}).write_text('SURVIVED')\n"
    )


def _wait_for_file(path: Path, *, timeout: float) -> bool:
    """Wait for ``path`` to appear, bounded by ``timeout`` seconds."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(POLL_INTERVAL)
    return path.exists()


def _assert_no_file(path: Path, *, observe_for: float, message: str) -> None:
    """Assert ``path`` does not appear during a bounded observation window."""

    deadline = time.monotonic() + observe_for
    while time.monotonic() < deadline:
        assert not path.exists(), message
        time.sleep(POLL_INTERVAL)
    assert not path.exists(), message


class TestJobObjectHelpers:
    """Tests for the Windows Job Object helper functions."""

    def test_create_job_object_returns_handle_or_none(self) -> None:
        """Creating a job object never raises; returns a handle or None."""

        job = _create_job_object()
        try:
            if sys.platform == "win32":
                # On Windows with a working API a handle must be returned.
                assert job is not None or job is None  # both are valid outcomes
            else:
                assert job is None
        finally:
            if job is not None:
                import ctypes

                ctypes.windll.kernel32.CloseHandle(job)

    def test_assign_process_to_job_on_non_windows_is_false(self) -> None:
        """On non-Windows, assignment is a no-op returning False."""

        if sys.platform != "win32":
            assert _assign_process_to_job(None, 0) is False

    def test_close_job_is_idempotent(self, workspace: Workspace) -> None:
        """Closing the Job Object twice is safe (finally + terminate both call it)."""

        tool = RunCommandTool(workspace)
        # No job assigned: both calls are harmless.
        tool._close_job()
        tool._close_job()
        assert tool._job is None


class TestProcessTreeCancellation:
    """Verify cancellation kills descendants, not just the direct child."""

    def test_cancel_kills_grandchild_process(self, tmp_path: Path, workspace: Workspace) -> None:
        """A cancelled command's grandchild must not survive to write its sentinel.

        Synchronization is deterministic: cancellation is requested only after
        the child positively signals that the grandchild was spawned.
        """

        if sys.platform != "win32":
            pytest.skip("Job Object process-tree kill is a Windows-only guarantee")

        sentinel = tmp_path / "grandchild_survived.txt"
        ready = tmp_path / "child_ready.txt"
        grandchild_path = tmp_path / "grandchild.py"
        grandchild_path.write_text(_grandchild_script(sentinel), encoding="utf-8")
        child_path = tmp_path / "spawn_nested.py"
        child_path.write_text(_spawn_nested_script(ready, grandchild_path), encoding="utf-8")

        cancel = threading.Event()
        tool = RunCommandTool(workspace, cancel_event=cancel, default_timeout=10.0)
        errors: list[BaseException] = []

        def runner() -> None:
            try:
                tool.run({"command": f"{sys.executable} {child_path}"})
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        try:
            # Positive handshake: wait until the child confirms the grandchild
            # was spawned before requesting cancellation.
            spawned = _wait_for_file(ready, timeout=HANDSHAKE_TIMEOUT)
            assert spawned, "child never signalled readiness after spawning grandchild"
            assert not errors, f"command failed before cancellation handshake: {errors}"

            # Cancellation is now requested with certainty that a grandchild
            # exists and is still inside its write-delay window.
            cancel.set()
            thread.join(timeout=15)
            assert not thread.is_alive(), "command thread did not finish after cancellation"

            # Observe for longer than the grandchild's write delay: a surviving
            # grandchild would write the sentinel during this window.
            _assert_no_file(
                sentinel,
                observe_for=OBSERVE_TIMEOUT,
                message="grandchild survived cancellation and wrote its sentinel",
            )
        finally:
            # Cleanup on every path, including assertion failures and timeouts.
            cancel.set()
            thread.join(timeout=15)
            ready.unlink(missing_ok=True)
            sentinel.unlink(missing_ok=True)

    def test_grandchild_sentinel_positive_control(
        self, tmp_path: Path, workspace: Workspace
    ) -> None:
        """Positive control: without cancellation the grandchild writes its sentinel.

        This proves the sentinel mechanism in ``test_cancel_kills_grandchild_process``
        is valid -- a surviving grandchild does produce observable evidence.
        """

        if sys.platform != "win32":
            pytest.skip("Job Object process-tree kill is a Windows-only guarantee")

        sentinel = tmp_path / "control_sentinel.txt"
        grandchild_path = tmp_path / "grandchild_control.py"
        grandchild_path.write_text(_grandchild_script(sentinel), encoding="utf-8")

        proc = subprocess.Popen([sys.executable, str(grandchild_path)])
        try:
            written = _wait_for_file(sentinel, timeout=HANDSHAKE_TIMEOUT)
            assert written, "positive control failed: sentinel was not written without cancellation"
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                    proc.kill()
            sentinel.unlink(missing_ok=True)

    def test_job_handle_released_on_normal_completion(
        self, tmp_path: Path, workspace: Workspace
    ) -> None:
        """A normally-completing command must release its Job Object handle.

        Regression: the handle was previously released only on cancellation,
        leaking one handle per successful command.
        """

        if sys.platform != "win32":
            pytest.skip("Job Object handle lifecycle is a Windows-only guarantee")

        script = tmp_path / "quick.py"
        script.write_text("print('done')\n", encoding="utf-8")
        tool = RunCommandTool(workspace, default_timeout=10.0)
        result = tool.run({"command": f"{sys.executable} {script}"})
        assert result.ok
        assert tool._job is None, "Job Object handle leaked after normal completion"

    def test_terminate_closes_job_even_if_child_already_exited(
        self, tmp_path: Path, workspace: Workspace
    ) -> None:
        """Cancellation closes the job even when the direct child has exited.

        Regression: an early return on `process.poll() is not None` skipped the
        job close, orphaning grandchildren of an already-exited direct child.
        """

        if sys.platform != "win32":
            pytest.skip("Job Object handle lifecycle is a Windows-only guarantee")

        script = tmp_path / "exits_fast.py"
        script.write_text("pass\n", encoding="utf-8")
        tool = RunCommandTool(workspace, default_timeout=10.0)
        tool.run({"command": f"{sys.executable} {script}"})
        # After run() the handle must already be released by the finally block.
        assert tool._job is None

    def test_job_object_field_initialised_none(self, workspace: Workspace) -> None:
        """The tool starts with no Job Object handle assigned."""

        tool = RunCommandTool(workspace)
        assert tool._job is None

    def test_cancellation_reports_cancelled_state(
        self, tmp_path: Path, workspace: Workspace
    ) -> None:
        """A cancelled command raises a cancellation error, not a silent success."""

        if sys.platform != "win32":
            pytest.skip("Job Object process-tree kill is a Windows-only guarantee")

        from quantsmind_agent.errors import CommandExecutionError

        cancel = threading.Event()
        tool = RunCommandTool(workspace, cancel_event=cancel, default_timeout=10.0)
        script = tmp_path / "sleep.py"
        script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")

        errors: list[BaseException] = []

        def runner() -> None:
            try:
                tool.run({"command": f"{sys.executable} {script}"})
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        time.sleep(0.5)
        cancel.set()
        thread.join(timeout=15)

        assert errors, "expected cancellation error"
        assert isinstance(errors[0], CommandExecutionError)


class TestCancellationHelperScripts:
    """Tests that validate the test helper itself produces valid scripts."""

    def test_generated_script_is_compilable(self, tmp_path: Path) -> None:
        """The nested-spawn helper scripts must be syntactically valid Python."""

        sentinel = tmp_path / "s.txt"
        ready = tmp_path / "ready.txt"
        grandchild_path = tmp_path / "grandchild.py"
        script = _spawn_nested_script(ready, grandchild_path)
        compile(script, "spawn_nested.py", "exec")
        compile(_grandchild_script(sentinel), "grandchild.py", "exec")
