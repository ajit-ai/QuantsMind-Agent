"""Tests for ``run_command``: execution, bounds, containment and cancellation."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from quantsmind_agent.command_tools import (
    MAX_OUTPUT_CHARS,
    RunCommandTool,
    build_command_tools,
    tokenize_command,
)
from quantsmind_agent.config import AgentSettings
from quantsmind_agent.errors import CommandTimeoutError
from quantsmind_agent.permissions import ApprovalOutcome, PermissionBroker, StaticPermissionProvider
from quantsmind_agent.tools import ToolRegistry
from quantsmind_agent.workspace import Workspace

#: An interpreter that exists on every supported platform.
PYTHON = sys.executable


def _registry(
    project: Path,
    *,
    outcome: ApprovalOutcome | None = ApprovalOutcome.ALLOW_ONCE,
    cancel_event: threading.Event | None = None,
) -> ToolRegistry:
    broker = PermissionBroker(
        StaticPermissionProvider.for_v0_2(),
        approver=(lambda request: outcome) if outcome is not None else None,
    )
    registry = ToolRegistry(permissions=broker)
    registry.register_all(
        build_command_tools(Workspace(project), cancel_event=cancel_event, default_timeout=30.0)
    )
    return registry


@pytest.fixture
def commands(sample_project: Path) -> ToolRegistry:
    return _registry(sample_project)


class TestTokenizer:
    def test_simple_command(self) -> None:
        assert tokenize_command("python -m pytest -q") == ["python", "-m", "pytest", "-q"]

    def test_quoted_argument_stays_one_token(self) -> None:
        assert tokenize_command('ruff check "src/a b.py"') == ["ruff", "check", "src/a b.py"]

    def test_surrounding_whitespace_is_dropped(self) -> None:
        assert tokenize_command("  git   status ") == ["git", "status"]

    def test_malformed_quotes_raise(self) -> None:
        from quantsmind_agent.errors import CommandExecutionError

        with pytest.raises(CommandExecutionError):
            tokenize_command('python "unclosed')


class TestExecution:
    def test_runs_and_captures_stdout(self, commands: ToolRegistry) -> None:
        result = commands.execute("run_command", {"command": PYTHON, "arguments": ["--version"]})
        assert result.ok
        assert result.data["exit_code"] == 0
        assert "Python" in result.data["stdout"]

    def test_captures_stderr_and_a_nonzero_exit_code(self, commands: ToolRegistry) -> None:
        script = "import sys; sys.stderr.write('boom'); sys.exit(3)"
        result = commands.execute("run_command", {"command": PYTHON, "arguments": ["-c", script]})
        assert result.ok
        assert result.data["exit_code"] == 3
        assert result.data["stderr"].strip() == "boom"
        assert result.data["timed_out"] is False

    def test_a_failed_command_is_still_a_successful_tool_call(self, commands: ToolRegistry) -> None:
        # The command ran; the *model* decides what a non-zero exit means. The
        # tool reports it rather than raising, so the agent can react.
        result = commands.execute(
            "run_command", {"command": PYTHON, "arguments": ["-c", "exit(1)"]}
        )
        assert result.ok
        assert result.data["exit_code"] == 1

    def test_arguments_list_is_used_verbatim(self, commands: ToolRegistry) -> None:
        result = commands.execute(
            "run_command", {"command": PYTHON, "arguments": ["-c", "print('a b c')"]}
        )
        assert result.data["stdout"].strip() == "a b c"

    def test_reports_risk_metadata(self, commands: ToolRegistry) -> None:
        result = commands.execute("run_command", {"command": PYTHON, "arguments": ["--version"]})
        assert result.data["risk"]
        assert result.data["risk_label"]
        assert result.data["policy_rule"]

    def test_shell_metacharacters_are_inert(self, commands: ToolRegistry) -> None:
        """No shell means no metacharacter expansion, so no second program."""

        result = commands.execute(
            "run_command",
            {"command": PYTHON, "arguments": ["-c", "print('; && | literal')"]},
        )
        assert "literal" in result.data["stdout"]


class TestBounds:
    def test_output_is_capped_and_flagged(self, commands: ToolRegistry) -> None:
        result = commands.execute(
            "run_command", {"command": PYTHON, "arguments": ["-c", "print('x' * 50000)"]}
        )
        assert len(result.data["stdout"]) <= 20_000
        assert result.data["stdout_truncated"] is True

    def test_timeout_stops_the_command(self, sample_project: Path) -> None:
        tool = RunCommandTool(Workspace(sample_project))
        with pytest.raises(CommandTimeoutError):
            tool.run(
                {
                    "command": PYTHON,
                    "arguments": ["-c", "import time; time.sleep(30)"],
                    "timeout_seconds": 1,
                }
            )

    def test_timeout_value_is_clamped(self, commands: ToolRegistry) -> None:
        result = commands.execute(
            "run_command",
            {"command": PYTHON, "arguments": ["-c", "print(1)"], "timeout_seconds": 99_999},
        )
        assert result.ok

    def test_cancellation_stops_the_command(self, sample_project: Path) -> None:
        cancel = threading.Event()
        registry = _registry(sample_project, cancel_event=cancel)
        threading.Timer(0.5, cancel.set).start()
        started = time.monotonic()
        result = registry.execute(
            "run_command", {"command": PYTHON, "arguments": ["-c", "import time; time.sleep(30)"]}
        )
        elapsed = time.monotonic() - started
        assert not result.ok
        assert "cancel" in (result.error or "").lower()
        assert elapsed < 20, "cancellation did not take effect promptly"

    def test_argument_count_is_capped(self, commands: ToolRegistry) -> None:
        result = commands.execute(
            "run_command",
            {"command": PYTHON, "arguments": ["-c", "print(1)"] + ["x"] * 200},
        )
        assert not result.ok
        assert "too many arguments" in (result.error or "")

    def test_null_byte_in_an_argument_is_refused(self, commands: ToolRegistry) -> None:
        assert not commands.execute("run_command", {"command": PYTHON, "arguments": ["-c\x00"]}).ok

    def test_missing_program_is_reported_clearly(self, commands: ToolRegistry) -> None:
        result = commands.execute("run_command", {"command": "definitely-not-a-real-program-xyz"})
        assert not result.ok
        assert "not found" in (result.error or "")

    def test_empty_command_is_refused(self, commands: ToolRegistry) -> None:
        assert not commands.execute("run_command", {"command": "   "}).ok


class TestWorkingDirectoryContainment:
    def test_runs_in_the_workspace_root(self, commands: ToolRegistry, sample_project: Path) -> None:
        result = commands.execute(
            "run_command",
            {"command": PYTHON, "arguments": ["-c", "import os; print(os.getcwd())"]},
        )
        assert Path(result.data["stdout"].strip()).resolve() == sample_project.resolve()

    def test_accepts_a_workspace_relative_subdirectory(
        self, commands: ToolRegistry, sample_project: Path
    ) -> None:
        result = commands.execute(
            "run_command",
            {"command": PYTHON, "arguments": ["-c", "import os; print(os.getcwd())"], "cwd": "src"},
        )
        assert Path(result.data["stdout"].strip()).resolve() == (sample_project / "src").resolve()

    @pytest.mark.parametrize("bad_cwd", ["../..", "..", "C:/Windows", "/etc", "nope"])
    def test_rejects_a_cwd_outside_the_workspace(
        self, commands: ToolRegistry, bad_cwd: str
    ) -> None:
        result = commands.execute(
            "run_command", {"command": PYTHON, "arguments": ["-c", "print(1)"], "cwd": bad_cwd}
        )
        assert not result.ok

    def test_rejects_a_cwd_that_is_a_file(self, commands: ToolRegistry) -> None:
        result = commands.execute(
            "run_command",
            {"command": PYTHON, "arguments": ["-c", "print(1)"], "cwd": "README.md"},
        )
        assert not result.ok


class TestCommandPolicyGate:
    """The policy runs before the process is created, so a deny is final."""

    @pytest.mark.parametrize(
        "argv",
        [
            ["format", "C:"],
            ["shutdown", "/s"],
            ["curl", "https://example.invalid"],
            ["git", "push"],
        ],
    )
    def test_blocked_commands_never_execute(self, commands: ToolRegistry, argv: list[str]) -> None:
        result = commands.execute("run_command", {"command": argv[0], "arguments": argv[1:]})
        assert not result.ok
        assert "safety policy" in (result.error or "")

    def test_approval_cannot_override_the_policy(self, sample_project: Path) -> None:
        """Even with an always-allow approver, a blocked command stays blocked."""

        registry = _registry(sample_project, outcome=ApprovalOutcome.ALLOW_FOR_SESSION)
        result = registry.execute("run_command", {"command": "format", "arguments": ["C:"]})
        assert not result.ok
        assert "safety policy" in (result.error or "")

    def test_a_hostile_cwd_cannot_bypass_the_policy(self, sample_project: Path) -> None:
        registry = _registry(sample_project, outcome=ApprovalOutcome.ALLOW_ONCE)
        result = registry.execute(
            "run_command",
            {
                "command": "format",
                "arguments": ["C:"],
                "cwd": ".",
                "permission": "EXECUTE_COMMAND",
                "decision": "ALLOW",
            },
        )
        assert not result.ok
        assert "safety policy" in (result.error or "")


class TestCommandApproval:
    def test_user_denial_prevents_execution(self, sample_project: Path) -> None:
        registry = _registry(sample_project, outcome=ApprovalOutcome.DENY)
        marker = sample_project / "should_not_exist.txt"
        result = registry.execute(
            "run_command",
            {"command": PYTHON, "arguments": ["-c", f"open({str(marker)!r}, 'w').write('x')"]},
        )
        assert not result.ok
        assert "not permitted" in (result.error or "")
        assert not marker.exists()

    def test_no_approver_means_denied(self, sample_project: Path) -> None:
        registry = _registry(sample_project, outcome=None)
        result = registry.execute("run_command", {"command": PYTHON, "arguments": ["--version"]})
        assert not result.ok
        assert "not permitted" in (result.error or "")

    def test_allow_for_session_skips_later_prompts(self, sample_project: Path) -> None:
        calls: list[str] = []

        def approver(request: object) -> ApprovalOutcome:
            calls.append(getattr(request, "tool", "?"))
            return ApprovalOutcome.ALLOW_FOR_SESSION

        broker = PermissionBroker(StaticPermissionProvider.for_v0_2(), approver=approver)
        registry = ToolRegistry(permissions=broker)
        registry.register_all(build_command_tools(Workspace(sample_project), default_timeout=30.0))
        for _ in range(3):
            result = registry.execute(
                "run_command", {"command": PYTHON, "arguments": ["--version"]}
            )
            assert result.ok
        assert len(calls) == 1, "Allow for Session must prompt exactly once"

    def test_allow_once_prompts_every_time(self, sample_project: Path) -> None:
        calls: list[str] = []

        def approver(request: object) -> ApprovalOutcome:
            calls.append(getattr(request, "tool", "?"))
            return ApprovalOutcome.ALLOW_ONCE

        broker = PermissionBroker(StaticPermissionProvider.for_v0_2(), approver=approver)
        registry = ToolRegistry(permissions=broker)
        registry.register_all(build_command_tools(Workspace(sample_project), default_timeout=30.0))
        for _ in range(3):
            assert registry.execute(
                "run_command", {"command": PYTHON, "arguments": ["--version"]}
            ).ok
        assert len(calls) == 3


class TestCredentialHygiene:
    def test_model_credentials_are_not_inherited(self, commands: ToolRegistry) -> None:
        # The child prints the length of the key it was handed; an empty result
        # proves the application key was not passed down.
        script = "import os; print(len(os.environ.get('QM_API_KEY', '')))"
        result = commands.execute("run_command", {"command": PYTHON, "arguments": ["-c", script]})
        assert result.data["stdout"].strip() == "0"

    def test_extra_redacted_names_are_also_removed(self, sample_project: Path) -> None:
        broker = PermissionBroker(
            StaticPermissionProvider.for_v0_2(),
            approver=lambda request: ApprovalOutcome.ALLOW_ONCE,
        )
        registry = ToolRegistry(permissions=broker)
        registry.register_all(
            build_command_tools(
                Workspace(sample_project),
                default_timeout=30.0,
                redact_env=("MY_SECRET_TOKEN",),
            )
        )
        script = "import os; print(len(os.environ.get('MY_SECRET_TOKEN', '')))"
        result = registry.execute("run_command", {"command": PYTHON, "arguments": ["-c", script]})
        assert result.data["stdout"].strip() == "0"


def _capped_registry(project: Path, cap: int | None) -> ToolRegistry:
    registry = ToolRegistry(
        permissions=PermissionBroker(
            StaticPermissionProvider.for_v0_2(),
            approver=lambda request: ApprovalOutcome.ALLOW_ONCE,
        )
    )
    if cap is None:
        tools = build_command_tools(Workspace(project), default_timeout=30.0)
    else:
        tools = build_command_tools(Workspace(project), default_timeout=30.0, max_output_chars=cap)
    registry.register_all(tools)
    return registry


class TestConfiguredOutputCap:
    """``max_command_output_chars`` has to reach the reader that does the capping."""

    def test_a_custom_cap_is_applied(self, sample_project: Path) -> None:
        registry = _capped_registry(sample_project, 100)

        result = registry.execute(
            "run_command", {"command": PYTHON, "arguments": ["-c", "print('x' * 50000)"]}
        )

        assert len(result.data["stdout"]) == 100
        assert result.data["stdout_truncated"] is True

    def test_a_custom_cap_never_shrinks_the_output(self, sample_project: Path) -> None:
        registry = _capped_registry(sample_project, 100_000)

        result = registry.execute(
            "run_command", {"command": PYTHON, "arguments": ["-c", "print('x' * 50000)"]}
        )

        assert len(result.data["stdout"]) == 50_001  # 50000 characters plus the newline
        assert result.data["stdout_truncated"] is False

    def test_output_exactly_at_the_cap_is_not_truncated(self, sample_project: Path) -> None:
        # 99 characters plus the newline the child adds: exactly at the boundary.
        registry = _capped_registry(sample_project, 100)

        result = registry.execute(
            "run_command", {"command": PYTHON, "arguments": ["-c", "print('x' * 99)"]}
        )

        assert len(result.data["stdout"]) == 100
        assert result.data["stdout_truncated"] is False

    def test_stderr_is_capped_by_the_same_setting(self, sample_project: Path) -> None:
        # Both streams are drained and capped independently; neither may bypass
        # the limit the user configured.
        registry = _capped_registry(sample_project, 100)
        script = "import sys; sys.stdout.write('o' * 5000); sys.stderr.write('e' * 5000)"

        result = registry.execute("run_command", {"command": PYTHON, "arguments": ["-c", script]})

        assert len(result.data["stdout"]) == 100
        assert len(result.data["stderr"]) == 100
        assert result.data["stdout_truncated"] is True
        assert result.data["stderr_truncated"] is True

    def test_the_default_is_used_when_no_cap_is_configured(self, sample_project: Path) -> None:
        registry = _capped_registry(sample_project, None)

        result = registry.execute(
            "run_command", {"command": PYTHON, "arguments": ["-c", "print('x' * 50000)"]}
        )

        assert len(result.data["stdout"]) == MAX_OUTPUT_CHARS
        assert result.data["stdout_truncated"] is True

    def test_the_default_cap_matches_the_configuration_default(self) -> None:
        # A fresh install sets neither value; the two must not drift apart.
        assert AgentSettings().max_command_output_chars == MAX_OUTPUT_CHARS
