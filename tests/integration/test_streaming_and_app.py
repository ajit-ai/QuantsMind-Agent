"""Integration tests for streaming, cancellation and the composition root."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from quantsmind_agent.app import ApplicationContext
from quantsmind_agent.config import ConfigManager
from quantsmind_agent.events import AgentEventType
from quantsmind_agent.llm import ChatRequest, ChatResponse, StreamChunk
from quantsmind_agent.permissions import ApprovalOutcome
from quantsmind_agent.workspace import Workspace
from tests.fakes import FakeModelProvider, text_response, tool_response
from tests.harness import AgentHarness

pytestmark = pytest.mark.integration


class TestStreaming:
    def test_final_answer_is_streamed_incrementally(self, workspace: Workspace) -> None:
        provider = FakeModelProvider(stream_chunks=[["Hello", ", ", "world"]])
        harness = AgentHarness(workspace, provider, use_streaming=True, max_iterations=1)
        outcome = harness.run("Say hello.")
        assert outcome.text == "Hello, world"
        assert harness.recorder.deltas == ["Hello", ", ", "world"]
        assert harness.recorder.text == "Hello, world"

    def test_tool_rounds_are_not_streamed(self, workspace: Workspace) -> None:
        provider = FakeModelProvider()
        harness = AgentHarness(workspace, provider, use_streaming=True, max_iterations=3)
        provider.script(
            tool_response([("list_directory", {"path": "."})]),
            text_response("Listed."),
        )
        harness.run("List the project.")
        assert provider.stream_requests == []  # only non-streamed requests sent
        assert provider.requests  # the tool round used complete()

    def test_streaming_failure_falls_back_to_one_shot(self, workspace: Workspace) -> None:
        class BrokenStream(FakeModelProvider):
            def stream(self, request: ChatRequest) -> Iterator[StreamChunk]:
                # Yield nothing, then fail: the agent must fall back to a
                # single non-streamed request rather than surfacing an error.
                for _ in ():
                    yield StreamChunk()
                raise RuntimeError("streaming unsupported")

        provider = BrokenStream(stream_chunks=[["never"]])
        provider.script(text_response("Recovered answer."))
        harness = AgentHarness(workspace, provider, use_streaming=True, max_iterations=1)
        outcome = harness.run("Say something.")
        assert outcome.text == "Recovered answer."
        assert outcome.succeeded


class TestCancellation:
    def test_cancel_during_the_loop_stops_it(self, workspace: Workspace) -> None:
        provider = FakeModelProvider()
        harness = AgentHarness(workspace, provider, max_iterations=5)
        original = provider.complete
        calls = {"n": 0}

        def cancelling(request: ChatRequest) -> ChatResponse:
            result = original(request)
            calls["n"] += 1
            harness.agent.cancel()
            return result

        provider.complete = cancelling  # type: ignore[method-assign]
        provider.script(
            tool_response([("list_directory", {"path": "."})]),
            tool_response([("list_directory", {"path": "src"})]),
            text_response("unreachable"),
        )
        outcome = harness.run("Explore a lot.")
        assert outcome.cancelled
        assert outcome.stopped_reason == "cancelled"
        assert outcome.error is None
        assert calls["n"] == 1  # the loop stopped after the cancelled round
        assert AgentEventType.AGENT_CANCELLED in harness.seen

    def test_cancel_before_the_turn_starts_cancels_that_turn(self, workspace: Workspace) -> None:
        """A Stop pressed immediately after Send must not be lost.

        ``cancel()`` called while no loop is running marks the *next* turn, so
        the request the user just sent is cancelled rather than executed.
        """

        provider = FakeModelProvider([text_response("should not appear")])
        harness = AgentHarness(workspace, provider, max_iterations=3)
        harness.agent.cancel()
        outcome = harness.run("go")
        assert outcome.cancelled
        assert provider.requests == []  # the model was never called
        assert "should not appear" not in outcome.text

    def test_reset_cancel_discards_a_pending_cancellation(self, workspace: Workspace) -> None:
        provider = FakeModelProvider([text_response("ran")])
        harness = AgentHarness(workspace, provider, max_iterations=3)
        harness.agent.cancel()
        harness.agent.reset_cancel()
        outcome = harness.run("go")
        assert not outcome.cancelled
        assert outcome.text == "ran"

    def test_cancellation_does_not_leak_into_the_next_turn(self, workspace: Workspace) -> None:
        provider = FakeModelProvider()
        harness = AgentHarness(workspace, provider, max_iterations=3)
        provider.script(tool_response([("list_directory", {"path": "."})]))
        original = provider.complete

        def cancelling(request: ChatRequest) -> ChatResponse:
            result = original(request)
            harness.agent.cancel()
            return result

        provider.complete = cancelling  # type: ignore[method-assign]
        first = harness.run("explore")
        assert first.cancelled

        provider.script(text_response("clean run"))
        provider.complete = original  # type: ignore[method-assign]
        second = harness.run("now answer")
        assert not second.cancelled
        assert second.text == "clean run"


def _print_x(context: ApplicationContext, count: int) -> Any:
    """Run the workspace interpreter, printing ``count`` characters."""

    context.tools.permissions.set_approver(lambda request: ApprovalOutcome.ALLOW_ONCE)
    return context.tools.execute(
        "run_command",
        {"command": sys.executable, "arguments": ["-c", f"print('x' * {count})"]},
    )


class TestApplicationContext:
    @pytest.fixture
    def context(self, config_manager: ConfigManager, sample_project: Path) -> ApplicationContext:
        return ApplicationContext.create(paths=config_manager.paths, config_manager=config_manager)

    def test_the_configured_output_cap_reaches_the_command_tool(
        self, context: ApplicationContext, sample_project: Path
    ) -> None:
        # The setting was inert until it was wired through: a user who lowered
        # it saw no change in what the model was told.
        context.config_manager.config.agent.max_command_output_chars = 64
        context.open_workspace(str(sample_project))

        result = _print_x(context, 5000)

        assert result.ok
        assert len(result.data["stdout"]) == 64
        assert result.data["stdout_truncated"] is True

    def test_saving_the_cap_rebuilds_the_command_tool(
        self, context: ApplicationContext, sample_project: Path
    ) -> None:
        context.open_workspace(str(sample_project))
        context.config_manager.config.agent.max_command_output_chars = 128

        context.apply_configuration()

        result = _print_x(context, 5000)

        assert len(result.data["stdout"]) == 128
        assert result.data["stdout_truncated"] is True

    def test_boots_without_a_workspace(self, context: ApplicationContext) -> None:
        assert context.workspace is None
        assert context.tools is not None
        assert context.tools.names == []
        assert context.readiness()[0] is False

    def test_opening_a_workspace_registers_tools(
        self, context: ApplicationContext, sample_project: Path
    ) -> None:
        workspace = context.open_workspace(str(sample_project))
        assert workspace.root == sample_project.resolve()
        assert context.tools is not None
        # Exact equality, not a subset: a tool appearing or disappearing without
        # a decision is a security-relevant change, so it must fail here. v0.2
        # adds the write/edit/delete tools, undo_file, run_command and the
        # read-only Git tools to the four v0.1 readers; v0.3 adds the
        # read-only code-navigation and project-context tools.
        assert context.tools.names == [
            "code_outline",
            "delete_file",
            "edit_file",
            "find_references",
            "git_diff",
            "git_log",
            "git_status",
            "list_directory",
            "project_context",
            "project_structure",
            "project_symbols",
            "read_file",
            "run_command",
            "search_content",
            "undo_file",
            "write_file",
        ]
        assert "No API key configured" in context.readiness()[1]

    def test_git_tools_can_be_disabled(
        self, context: ApplicationContext, sample_project: Path
    ) -> None:
        config = context.config_manager.config
        config.agent.git_enabled = False
        context.apply_configuration()
        context.open_workspace(str(sample_project))
        assert context.tools is not None
        assert [name for name in context.tools.names if name.startswith("git_")] == []

    def test_closing_a_workspace_clears_tools(
        self, context: ApplicationContext, sample_project: Path
    ) -> None:
        context.open_workspace(str(sample_project))
        context.close_workspace()
        assert context.workspace is None
        assert context.tools is not None
        assert context.tools.names == []

    def test_invalid_workspace_raises_a_friendly_error(
        self, context: ApplicationContext, tmp_path: Path
    ) -> None:
        from quantsmind_agent.errors import InvalidWorkspaceError

        with pytest.raises(InvalidWorkspaceError) as info:
            context.open_workspace(tmp_path / "does-not-exist")
        assert "could not be opened" in info.value.user_message

    def test_last_workspace_is_persisted_and_restored(
        self, context: ApplicationContext, sample_project: Path, config_manager: ConfigManager
    ) -> None:
        context.open_workspace(str(sample_project))
        assert context.config_manager.config.last_workspace == str(sample_project.resolve())
        restored = ApplicationContext.create(
            paths=config_manager.paths, config_manager=config_manager
        )
        assert restored.workspace is not None
        assert restored.workspace.root == sample_project.resolve()

    def test_missing_last_workspace_is_ignored(
        self, context: ApplicationContext, tmp_path: Path
    ) -> None:
        context.config_manager.config.last_workspace = str(tmp_path / "gone")
        context._save_config()
        restored = ApplicationContext.create(
            paths=context.paths, config_manager=context.config_manager
        )
        assert restored.workspace is None

    def test_apply_configuration_rebuilds_the_stack(self, context: ApplicationContext) -> None:
        before = context.agent
        context.config_manager.config.provider.model = "another-model"
        context.apply_configuration()
        assert context.agent is not before
        assert AgentEventType.CONFIG_CHANGED in [event.type for event in context.events.history]

    def test_reset_conversation_keeps_the_workspace(
        self, context: ApplicationContext, sample_project: Path
    ) -> None:
        from quantsmind_agent.schema import UserMessage

        context.open_workspace(str(sample_project))
        context.session.add_message(UserMessage(content="hi"))
        context.reset_conversation()
        assert context.session.messages == []
        assert context.workspace is not None
        assert context.tools is not None
        assert context.tools.names  # tools re-registered for the open project

    def test_status_line_mentions_the_project(
        self, context: ApplicationContext, sample_project: Path
    ) -> None:
        context.open_workspace(str(sample_project))
        assert sample_project.name in context.status_line()

    def test_shutdown_without_a_bridge_is_safe(self, context: ApplicationContext) -> None:
        context.shutdown()
