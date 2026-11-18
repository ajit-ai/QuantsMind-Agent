"""Contract tests for cancellation inside a running agent turn.

The agent loop is interruptible, so a Stop must be honoured everywhere the loop
could still start new work. Three points were not covered by the existing
cancellation tests:

* the round that produces the **final answer** -- the cancel arrives while the
  model request is in flight, so the answer is already on the wire when the
  user presses Stop;
* a response that queues **several tool calls** -- the cancel arrives after
  the first one ran, and the rest of the batch must not start;
* the **persisted record** of a cancelled turn, which must not be stored as a
  clean one.

The contract these tests pin is the one ``AgentCore.cancel`` already states:
an in-flight request cannot be aborted, but a late answer is discarded rather
than shown.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from quantsmind_agent.app import ApplicationContext
from quantsmind_agent.app.settings import ConfigManager
from quantsmind_agent.providers.gateway import ChatRequest, ChatResponse, StreamChunk
from quantsmind_agent.runtime.tools import ToolResult, ToolSpec
from quantsmind_agent.security.permissions import ApprovalOutcome, Permission
from quantsmind_agent.security.secrets import FileSecretStore
from quantsmind_agent.services.workspace.containment import Workspace
from tests.fakes import FakeModelProvider, text_response, tool_response
from tests.harness import AgentHarness

pytestmark = pytest.mark.integration


class CancellingOnAnswer(FakeModelProvider):
    """Cancels the agent while the final-answer request is in flight."""

    def __init__(self, agent_ref: dict[str, Any], content: str = "late answer") -> None:
        super().__init__([text_response(content)])
        self._agent_ref = agent_ref

    def complete(self, request: ChatRequest) -> ChatResponse:
        response = super().complete(request)
        self._agent_ref["agent"].cancel()
        return response


class CancellingOnStream(FakeModelProvider):
    """Cancels the agent while the final answer is being streamed."""

    def __init__(self, agent_ref: dict[str, Any]) -> None:
        super().__init__(
            [
                tool_response([("list_directory", {"path": "."})]),
                text_response("late answer"),
            ]
        )
        self._agent_ref = agent_ref

    def stream(self, request: ChatRequest) -> Iterator[StreamChunk]:
        self.stream_requests.append(request)
        yield StreamChunk(delta="partial ")
        self._agent_ref["agent"].cancel()
        yield StreamChunk(delta="answer")
        yield StreamChunk(finish_reason="stop")


class CancellingTool:
    """A tool that cancels the agent while it runs."""

    def __init__(self, agent_ref: dict[str, Any]) -> None:
        self._agent_ref = agent_ref
        self.spec = ToolSpec(
            name="cancelling_probe",
            description="Cancels the agent, for the cancellation contract tests.",
            input_schema={"type": "object", "properties": {}, "required": []},
            permission=Permission.READ_WORKSPACE,
        )

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        self._agent_ref["agent"].cancel()
        return ToolResult.success("cancelling_probe", "cancelled the agent")


class CancellingAfterFirstTool(FakeModelProvider):
    """Queues a probe tool and a write; the probe cancels the agent mid-batch."""

    def __init__(self, agent_ref: dict[str, Any]) -> None:
        super().__init__(
            [
                tool_response(
                    [
                        ("cancelling_probe", {}),
                        ("write_file", {"path": "second.txt", "content": "2"}),
                    ]
                ),
                text_response("done"),
            ]
        )
        self._agent_ref = agent_ref


def test_a_cancel_during_the_final_answer_round_is_reported_as_cancelled(
    workspace: Workspace,
) -> None:
    """The answer is already on the wire when Stop is pressed."""

    agent_ref: dict[str, Any] = {}
    provider = CancellingOnAnswer(agent_ref)
    harness = AgentHarness(workspace, provider)
    agent_ref["agent"] = harness.agent

    outcome = harness.run("hello")

    assert outcome.cancelled is True
    assert outcome.stopped_reason == "cancelled"
    assert outcome.succeeded is False
    assert outcome.text == ""


def test_a_cancelled_answer_is_not_kept_in_the_session(workspace: Workspace) -> None:
    """The discarded answer must not reappear in the next turn's context."""

    agent_ref: dict[str, Any] = {}
    provider = CancellingOnAnswer(agent_ref)
    harness = AgentHarness(workspace, provider)
    agent_ref["agent"] = harness.agent

    harness.run("hello")

    assistant_messages = [m for m in harness.session.messages if m.role == "assistant"]
    assert assistant_messages == [], "the discarded answer was stored in the session"


def test_a_cancelled_stream_is_not_kept_as_a_partial_answer(workspace: Workspace) -> None:
    """A partial stream is not a complete answer."""

    agent_ref: dict[str, Any] = {}
    provider = CancellingOnStream(agent_ref)
    harness = AgentHarness(workspace, provider, max_iterations=2, use_streaming=True)
    agent_ref["agent"] = harness.agent

    outcome = harness.run("hello")

    assert provider.stream_requests, "the final answer was not streamed"
    assert outcome.cancelled is True
    assert outcome.stopped_reason == "cancelled"
    assert outcome.text == ""


def test_a_cancel_stops_the_remaining_queued_tool_calls(workspace: Workspace) -> None:
    """One response may queue several tools; a cancel must stop the batch."""

    agent_ref: dict[str, Any] = {}
    provider = CancellingAfterFirstTool(agent_ref)
    harness = AgentHarness(
        workspace,
        provider,
        tools="all",
        approver=lambda request: ApprovalOutcome.ALLOW_ONCE,
    )
    harness.registry.register(CancellingTool(agent_ref))
    agent_ref["agent"] = harness.agent

    outcome = harness.run("write two files")

    assert (workspace.root / "second.txt").exists() is False, "the queued second call still ran"
    assert outcome.cancelled is True
    assert outcome.stopped_reason == "cancelled"
    assert [result.tool for result in outcome.tool_results] == ["cancelling_probe"]


def test_a_cancelled_turn_is_persisted_as_unfinished(app_paths: Any, sample_project: Path) -> None:
    """A cancelled turn must not be stored as a clean one."""

    manager = ConfigManager(app_paths, secret_store=FileSecretStore(app_paths.secrets_file))
    context = ApplicationContext.create(paths=app_paths, config_manager=manager)
    context.open_workspace(sample_project, trusted=True)

    agent_ref: dict[str, Any] = {"agent": context.agent}
    context.agent._gateway.set_provider(CancellingOnAnswer(agent_ref))

    outcome = context.agent.run("hello")

    assert outcome.cancelled is True
    context.save_conversation()
    assert context.list_conversations()[0].finished is False
