"""Adversarial tests for the Windows sandbox boundary (M3B-S1.4).

Every test is offline and deterministic:

* no test contacts a live provider;
* the sandbox's own probe payload is synthetic: a canary file the test
  creates, a canary credential file, a loopback listener on an ephemeral
  port, and canary environment variables;
* no test reads a real credential, and no test uses the real Windows
  Credential Manager.

The Windows enforcement tests are marked, so a reader can tell an
**OS-enforced** result from a **mocked** one.

The pinned-transport tests live in ``test_m3b_s14_pinned_transport.py`` so
that they are collected on every supported OS instead of being skipped with
the Windows-only boundary.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

import pytest

# ``quantsmind_agent.sandbox`` imports ``ctypes.wintypes`` at module scope,
# which raises on non-Windows. Decide the platform before importing it so
# collection succeeds everywhere and this module is reported as skipped.
if sys.platform != "win32":
    pytest.skip("the sandbox boundary is Windows-only", allow_module_level=True)

from quantsmind_agent.sandbox import IsolationLevel, SandboxError, SandboxPolicy
from quantsmind_agent.sandbox.runner import SandboxRunner
from quantsmind_agent.sandbox.windows import (
    CREDENTIAL_ENV_NAMES,
    build_child_environment,
    sandbox_supported,
)
from quantsmind_agent.security.audit import AuditEventType, SecurityAuditLog

pytestmark = pytest.mark.skipif(
    not sandbox_supported(), reason="the sandbox boundary is Windows-only"
)


def _policy(root: Path, command: tuple[str, ...], **kwargs: Any) -> SandboxPolicy:
    options: dict[str, Any] = {
        "workspace_root": root,
        "allowed_command": command,
        "timeout_seconds": 30.0,
        "max_output_bytes": 100_000,
        "max_child_processes": 4,
        "max_memory_bytes": 512 * 1024 * 1024,
    }
    options.update(kwargs)
    return SandboxPolicy(**options)


class TestSandboxExecution:
    """S14-G8, S14-G9: the boundary runs the command and contains it."""

    def test_a_command_runs_and_its_output_is_captured(self, tmp_path: Path) -> None:
        policy = _policy(tmp_path, (sys.executable, "-c", "print('inside the sandbox')"))
        result = SandboxRunner().run(policy)
        assert result.exit_code == 0
        assert "inside the sandbox" in result.stdout
        assert result.isolation_verified is True

    def test_the_working_directory_is_the_workspace(self, tmp_path: Path) -> None:
        policy = _policy(
            tmp_path,
            (sys.executable, "-c", "import os; print(os.getcwd())"),
        )
        result = SandboxRunner().run(policy)
        assert Path(result.stdout.strip()).resolve() == tmp_path.resolve()

    def test_the_environment_is_allowlisted(self, tmp_path: Path) -> None:
        env = build_child_environment(
            _policy(tmp_path, (sys.executable, "-c", "pass")), tmp_path / "home"
        )
        for name in CREDENTIAL_ENV_NAMES:
            assert name not in env
        assert env["PYTHONNOUSERSITE"] == "1"
        assert env["USERPROFILE"] == str(tmp_path / "home")

    def test_a_credential_variable_does_not_reach_the_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("QM_API_KEY", "sk-canary-sandbox-0000000000")
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-canary-sandbox-1111111111")
        policy = _policy(
            tmp_path,
            (
                sys.executable,
                "-c",
                "import os; print('|'.join(sorted(n for n in os.environ "
                "if 'KEY' in n or 'TOKEN' in n)))",
            ),
        )
        result = SandboxRunner().run(policy)
        assert "QM_API_KEY" not in result.stdout
        assert "OPENROUTER_API_KEY" not in result.stdout
        assert result.stdout.strip() == ""

    def test_the_output_is_capped(self, tmp_path: Path) -> None:
        policy = _policy(
            tmp_path,
            (sys.executable, "-c", "print('x' * 200000)"),
            max_output_bytes=5000,
        )
        result = SandboxRunner().run(policy)
        assert len(result.stdout) <= 5000
        assert result.truncated is True

    def test_a_timeout_terminates_the_child(self, tmp_path: Path) -> None:
        policy = _policy(
            tmp_path,
            (sys.executable, "-c", "import time; time.sleep(60)"),
            timeout_seconds=2.0,
        )
        result = SandboxRunner().run(policy)
        assert result.timed_out is True
        assert result.exit_code != 0

    def test_cancellation_terminates_the_child(self, tmp_path: Path) -> None:
        cancel = threading.Event()
        threading.Timer(1.0, cancel.set).start()
        policy = _policy(
            tmp_path,
            (sys.executable, "-c", "import time; time.sleep(60)"),
            timeout_seconds=30.0,
        )
        result = SandboxRunner().run(policy, cancel_event=cancel)
        assert result.cancelled is True

    def test_the_run_is_audited_without_secrets(self, tmp_path: Path) -> None:
        audit = SecurityAuditLog()
        policy = _policy(tmp_path, (sys.executable, "-c", "print('ok')"))
        SandboxRunner(audit=audit).run(policy)
        events = audit.events_of_type(AuditEventType.SANDBOX_EXECUTION)
        assert events
        assert "sk-" not in audit.to_json_lines()


class TestFailClosed:
    """S14-G9: without the required isolation, nothing runs."""

    def test_untrusted_code_is_refused_on_this_host(self, tmp_path: Path) -> None:
        policy = _policy(
            tmp_path,
            (sys.executable, "-c", "print('should not run')"),
            required_isolation=IsolationLevel.UNTRUSTED_CODE,
        )
        result = SandboxRunner().run(policy)
        assert result.isolation_verified is False
        assert result.failure_reason == "isolation_insufficient"
        assert "should not run" not in result.stdout

    def test_the_refusal_is_audited(self, tmp_path: Path) -> None:
        audit = SecurityAuditLog()
        policy = _policy(
            tmp_path,
            (sys.executable, "-c", "print('should not run')"),
            required_isolation=IsolationLevel.UNTRUSTED_CODE,
        )
        SandboxRunner(audit=audit).run(policy)
        assert audit.events_of_type(AuditEventType.SANDBOX_DENIED)

    def test_the_measurement_reports_what_is_not_isolated(self, tmp_path: Path) -> None:
        """The probe must *measure*, and it must not claim more than it found."""

        policy = _policy(tmp_path, (sys.executable, "-c", "pass"))
        report = SandboxRunner().measure(policy)
        checks = report.checks
        # These are the properties a job object cannot provide.
        assert checks["read_outside_file"] is True
        assert checks["read_credential_store"] is True
        assert checks["connect_local_listener"] is True
        # The environment *is* controlled, and that one is enforced.
        assert report.credential_env_leaked is False
        assert report.level is IsolationLevel.LIFECYCLE

    def test_an_invalid_policy_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(SandboxError):
            _policy(tmp_path, ())
        with pytest.raises(SandboxError):
            _policy(tmp_path, (sys.executable,), timeout_seconds=0)
        with pytest.raises(SandboxError):
            _policy(tmp_path, (sys.executable,), max_output_bytes=0)
