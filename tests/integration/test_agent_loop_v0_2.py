"""The realistic coding-agent sequence required by v0.2.

inspect -> read -> edit -> run test -> inspect result -> report

Every mutating step passes through approval, so a denial must stop that step
while leaving the loop able to continue and report. The loop stays bounded and
unknown tools stay recoverable.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from quantsmind_agent.errors import ToolNotFoundError
from quantsmind_agent.permissions import ApprovalOutcome, PermissionBroker
from quantsmind_agent.tools import ToolRegistry
from quantsmind_agent.workspace import Workspace
from tests.fakes import FakeModelProvider, text_response, tool_response
from tests.harness import AgentHarness

PYTHON = sys.executable


def _harness(
    workspace: Workspace,
    responses: list[Any] | None = None,
    *,
    outcome: ApprovalOutcome = ApprovalOutcome.ALLOW_ONCE,
    max_iterations: int = 10,
    include_git: bool = False,
) -> AgentHarness:
    provider = FakeModelProvider(responses or [])
    return AgentHarness(
        workspace,
        provider,
        tools="all",
        max_iterations=max_iterations,
        approver=lambda request: outcome,
        include_git=include_git,
    )


class TestFullCodingWorkflow:
    def test_inspect_edit_test_report(self, sample_project: Path) -> None:
        """The canonical sequence from the v0.2 brief, end to end."""

        workspace = Workspace(sample_project)
        target = sample_project / "src" / "app" / "settings.py"
        original = target.read_text()

        harness = _harness(
            workspace,
            [
                tool_response([("project_structure", {"path": "src"})]),
                tool_response([("read_file", {"path": "src/app/settings.py"})]),
                tool_response(
                    [
                        (
                            "edit_file",
                            {
                                "path": "src/app/settings.py",
                                "operation": "replace",
                                "old_text": "DEFAULT_PORT = 8080",
                                "new_text": "DEFAULT_PORT = 9090",
                            },
                        )
                    ]
                ),
                tool_response(
                    [("run_command", {"command": PYTHON, "arguments": ["-c", "print('tests ok')"]})]
                ),
                tool_response([("read_file", {"path": "src/app/settings.py", "max_lines": 3})]),
                text_response("Changed the default port from 8080 to 9090 and verified it."),
            ],
        )

        outcome = harness.run("Change the default port in settings.py and verify the change.")

        assert outcome.error is None
        assert not outcome.cancelled
        assert "9090" in target.read_text()
        assert target.read_text() != original

        assert [result.tool for result in harness.recorder.finished] == [
            "project_structure",
            "read_file",
            "edit_file",
            "run_command",
            "read_file",
        ]
        # Only the two mutating steps asked for approval.
        assert [tool for tool, _ in harness.approvals] == ["edit_file", "run_command"]
        assert all(result.ok for result in harness.recorder.finished)

    def test_the_loop_is_bounded(self, sample_project: Path) -> None:
        """A model that keeps calling tools is stopped, not followed forever."""

        workspace = Workspace(sample_project)
        provider = FakeModelProvider(
            [tool_response([("write_file", {"path": "x.txt", "content": "1"})])] * 20
        )
        harness = AgentHarness(
            workspace,
            provider,
            tools="all",
            max_iterations=3,
            approver=lambda request: ApprovalOutcome.ALLOW_ONCE,
        )
        outcome = harness.run("Keep writing files forever")

        assert outcome.iterations <= 3
        assert outcome.stopped_reason in {"max_iterations", "final_answer"}

    def test_unknown_tool_is_recoverable(self, sample_project: Path) -> None:
        workspace = Workspace(sample_project)
        harness = _harness(
            workspace,
            [
                tool_response([("git_commit", {"message": "x"})]),
                tool_response([("write_file", {"path": "ok.txt", "content": "y"})]),
                text_response("There is no such tool; I wrote a file instead."),
            ],
        )
        outcome = harness.run("Commit and write a file")

        assert outcome.error is None
        assert (sample_project / "ok.txt").exists()
        first = harness.recorder.finished[0]
        assert first.ok is False
        assert "unknown tool" in (first.error or "").lower()

    def test_unknown_tool_raises_when_executed_directly(self) -> None:
        with pytest.raises(ToolNotFoundError):
            ToolRegistry(permissions=PermissionBroker()).execute("nope", {})


class TestDenialStopsOnlyThatStep:
    def test_denied_write_leaves_the_file_untouched(self, sample_project: Path) -> None:
        workspace = Workspace(sample_project)
        before = (sample_project / "README.md").read_text()
        harness = _harness(
            workspace,
            [
                tool_response([("write_file", {"path": "README.md", "content": "pwned\n"})]),
                tool_response([("read_file", {"path": "README.md", "max_lines": 2})]),
                text_response("I was not allowed to change the README."),
            ],
            outcome=ApprovalOutcome.DENY,
        )
        outcome = harness.run("Overwrite the README")

        assert outcome.error is None
        assert (sample_project / "README.md").read_text() == before
        assert harness.recorder.finished[0].ok is False
        # The loop kept going: a denial is a failed observation, not a crash.
        assert harness.recorder.finished[1].ok is True

    def test_denied_command_never_runs(self, sample_project: Path) -> None:
        workspace = Workspace(sample_project)
        marker = sample_project / "command_ran.txt"
        harness = _harness(
            workspace,
            [
                tool_response(
                    [
                        (
                            "run_command",
                            {
                                "command": PYTHON,
                                "arguments": ["-c", f"open({str(marker)!r}, 'w').write('x')"],
                            },
                        )
                    ]
                ),
                text_response("The command was not approved."),
            ],
            outcome=ApprovalOutcome.DENY,
        )
        harness.run("Write a marker file using a command")
        assert not marker.exists()

    def test_blocked_command_never_reaches_the_dialog(self, sample_project: Path) -> None:
        """A policy denial happens before the user is even asked."""

        workspace = Workspace(sample_project)
        harness = _harness(
            workspace,
            [
                tool_response([("run_command", {"command": "format", "arguments": ["C:"]})]),
                text_response("That command is not permitted."),
            ],
        )
        harness.run("Format the C drive")

        result = next(r for r in harness.recorder.finished if r.tool == "run_command")
        assert result.ok is False
        assert "safety policy" in (result.error or "")
        assert harness.approvals == []

    def test_a_stale_edit_is_recoverable(self, sample_project: Path) -> None:
        workspace = Workspace(sample_project)
        harness = _harness(
            workspace,
            [
                tool_response(
                    [
                        (
                            "edit_file",
                            {
                                "path": "src/app/settings.py",
                                "operation": "replace",
                                "old_text": "text that is not present",
                                "new_text": "x",
                            },
                        )
                    ]
                ),
                tool_response(
                    [("edit_file", {"path": "README.md", "operation": "append", "new_text": "!\n"})]
                ),
                text_response("The first edit did not match; I appended instead."),
            ],
        )
        outcome = harness.run("Edit the settings file")

        assert outcome.error is None
        assert (sample_project / "README.md").read_text().endswith("!\n")
        assert harness.recorder.finished[0].ok is False


class TestSessionApproval:
    def test_allow_once_prompts_every_time(self, sample_project: Path) -> None:
        workspace = Workspace(sample_project)
        harness = _harness(
            workspace,
            [
                tool_response(
                    [
                        ("write_file", {"path": "a.txt", "content": "1"}),
                        ("write_file", {"path": "b.txt", "content": "2"}),
                    ]
                ),
                text_response("Done."),
            ],
            outcome=ApprovalOutcome.ALLOW_ONCE,
        )
        harness.run("Write two files")
        assert [tool for tool, _ in harness.approvals] == ["write_file", "write_file"]

    def test_allow_for_session_prompts_once_and_is_clearable(self, sample_project: Path) -> None:
        workspace = Workspace(sample_project)
        harness = _harness(
            workspace,
            [
                tool_response(
                    [
                        ("write_file", {"path": "a.txt", "content": "1"}),
                        ("write_file", {"path": "b.txt", "content": "2"}),
                    ]
                ),
                text_response("Done."),
            ],
            outcome=ApprovalOutcome.ALLOW_FOR_SESSION,
        )
        harness.run("Write two files")
        assert (sample_project / "a.txt").exists()
        assert (sample_project / "b.txt").exists()
        assert len(harness.approvals) == 1
        assert harness.permissions.session_grants

        harness.permissions.clear_session_grants()
        assert harness.permissions.session_grants == frozenset()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A small real repository, used by the Git-awareness test."""

    import subprocess

    root = tmp_path / "repo"
    root.mkdir()
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n")

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    git("init", "-b", "main")
    for key, value in (
        ("user.email", "t@example.invalid"),
        ("user.name", "T"),
        ("commit.gpgsign", "false"),
    ):
        git("config", key, value)
    git("add", ".")
    git("commit", "-m", "initial")
    return root


class TestGitAwareness:
    def test_agent_reads_repository_state(self, git_repo: Path) -> None:
        workspace = Workspace(git_repo)
        harness = _harness(
            workspace,
            [
                tool_response([("git_status", {})]),
                tool_response([("git_diff", {})]),
                tool_response([("git_log", {"limit": 3})]),
                text_response("The tree is clean and has one commit."),
            ],
            include_git=True,
        )
        outcome = harness.run("What is the state of the repository?")

        assert outcome.error is None
        assert [result.tool for result in harness.recorder.finished] == [
            "git_status",
            "git_diff",
            "git_log",
        ]
        assert all(result.ok for result in harness.recorder.finished)

    def test_a_mutation_verb_is_not_a_registered_tool(self, git_repo: Path) -> None:
        """The model can ask for a commit; it will simply not exist."""

        workspace = Workspace(git_repo)
        harness = _harness(
            workspace,
            [
                tool_response([("git_commit", {"message": "x"})]),
                text_response("There is no such tool."),
            ],
        )
        harness.run("Commit the changes")
        assert harness.recorder.finished[0].ok is False
        assert "unknown tool" in (harness.recorder.finished[0].error or "").lower()

    def test_push_through_run_command_is_blocked(self, git_repo: Path) -> None:
        workspace = Workspace(git_repo)
        harness = _harness(
            workspace,
            [
                tool_response([("run_command", {"command": "git", "arguments": ["push"]})]),
                text_response("Pushing is not permitted."),
            ],
        )
        harness.run("Push to origin")
        result = next(r for r in harness.recorder.finished if r.tool == "run_command")
        assert result.ok is False
        assert "safety policy" in (result.error or "")
