"""Integration tests for the agent loop, tool round-trip and error recovery.

Chain exercised here::

    user request -> ContextEngine -> ModelGateway -> tool call
                 -> ToolRegistry (permission check) -> observation
                 -> ModelGateway -> final answer

The model is a scripted ``FakeModelProvider``, so these tests are deterministic
and offline. Real-LLM behaviour is covered by the manual Windows 11 acceptance
procedure in ``docs/source/testing/acceptance-v0.1.rst``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quantsmind_agent.errors import LLMError
from quantsmind_agent.events import AgentEventType
from quantsmind_agent.llm import ChatRequest, ChatResponse
from quantsmind_agent.workspace import Workspace
from tests.fakes import FakeModelProvider, text_response, tool_response
from tests.harness import AgentHarness

pytestmark = pytest.mark.integration


@pytest.fixture
def harness(workspace: Workspace) -> AgentHarness:
    return AgentHarness(workspace, FakeModelProvider())


class TestSingleRound:
    def test_plain_answer_needs_no_tools(self, harness: AgentHarness) -> None:
        harness.provider.script(text_response("This project is a sample app."))
        outcome = harness.run("Explain the structure of this project.")
        assert outcome.succeeded
        assert outcome.text == "This project is a sample app."
        assert outcome.iterations == 1
        assert not outcome.used_tools
        assert harness.provider.requests[0].tools  # tools were offered

    def test_events_cover_the_whole_turn(self, harness: AgentHarness) -> None:
        harness.provider.script(text_response("done"))
        harness.run("hello")
        assert harness.seen[0] is AgentEventType.USER_MESSAGE
        assert AgentEventType.AGENT_STARTED in harness.seen
        assert AgentEventType.MODEL_REQUEST in harness.seen
        assert AgentEventType.MODEL_RESPONSE in harness.seen
        assert harness.seen[-1] is AgentEventType.AGENT_COMPLETED

    def test_turn_is_recorded_in_the_session(self, harness: AgentHarness) -> None:
        harness.provider.script(text_response("answer"))
        harness.run("question")
        assert len(harness.session.turns) == 1
        turn = harness.session.turns[0]
        assert turn.user_text == "question"
        assert turn.final_text == "answer"
        assert turn.succeeded

    def test_empty_request_is_rejected(self, harness: AgentHarness) -> None:
        outcome = harness.run("   ")
        assert outcome.error == "The request is empty."
        assert outcome.stopped_reason == "empty_request"

    def test_context_carries_the_workspace(self, harness: AgentHarness) -> None:
        harness.provider.script(text_response("ok"))
        harness.run("what is this?")
        system = harness.provider.requests[0].messages[0].content
        assert "Current workspace" in system
        active = harness.session.workspace
        assert active is not None
        assert str(active.root) in system


class TestToolRoundTrip:
    def test_read_file_observation_feeds_the_next_round(self, harness: AgentHarness) -> None:
        harness.provider.script(
            tool_response([("read_file", {"path": "README.md"})]),
            text_response("The README describes a small sample project."),
        )
        outcome = harness.run("Read the README and summarize the project.")

        assert outcome.succeeded
        assert outcome.used_tools
        assert outcome.iterations == 2
        assert outcome.tool_results[0].tool == "read_file"
        assert outcome.tool_results[0].ok
        assert "Sample Project" in outcome.tool_results[0].data["content"]

        second = harness.provider.requests[1]
        assert [m.role.value for m in second.messages[-2:]] == ["assistant", "tool"]
        assert "TOOL RESULT (read_file)" in second.messages[-1].content

    def test_several_tools_in_one_round(self, harness: AgentHarness) -> None:
        harness.provider.script(
            tool_response(
                [
                    ("project_structure", {}),
                    ("list_directory", {"path": "src"}),
                ]
            ),
            text_response("Structure captured."),
        )
        outcome = harness.run("Show me the project layout.")
        assert [result.tool for result in outcome.tool_results] == [
            "project_structure",
            "list_directory",
        ]
        assert all(result.ok for result in outcome.tool_results)

    def test_search_then_read(self, harness: AgentHarness) -> None:
        harness.provider.script(
            tool_response([("search_content", {"pattern": "DEFAULT_PORT"})]),
            tool_response([("read_file", {"path": "src/app/settings.py"})]),
            text_response("The default port is 8080."),
        )
        outcome = harness.run("Where is the port configured?")
        assert [r.tool for r in outcome.tool_results] == ["search_content", "read_file"]
        assert outcome.iterations == 3
        assert outcome.text == "The default port is 8080."

    def test_tool_activity_is_reported_to_the_view(self, harness: AgentHarness) -> None:
        harness.provider.script(
            tool_response([("list_directory", {"path": "."})]),
            text_response("done"),
        )
        harness.run("List the project.")
        assert harness.recorder.started[0][0] == "list_directory"
        assert harness.recorder.finished[0].tool == "list_directory"
        assert harness.recorder.iterations == [1, 2]

    def test_tool_events_are_emitted(self, harness: AgentHarness) -> None:
        harness.provider.script(
            tool_response([("list_directory", {"path": "."})]),
            text_response("done"),
        )
        harness.run("List the project.")
        assert AgentEventType.TOOL_REQUESTED in harness.seen
        assert AgentEventType.TOOL_STARTED in harness.seen
        assert AgentEventType.TOOL_COMPLETED in harness.seen
        assert AgentEventType.PERMISSION_DECISION in harness.seen

    def test_escaping_path_is_reported_back_to_the_model(
        self, harness: AgentHarness, escape_target: Path
    ) -> None:
        harness.provider.script(
            tool_response([("read_file", {"path": "../../../etc/passwd"})]),
            text_response("I cannot read that path; it is outside the workspace."),
        )
        outcome = harness.run("Read the passwd file.")
        result = outcome.tool_results[0]
        assert not result.ok
        assert "outside the current workspace" in (result.error or "")
        assert harness.provider.requests[1].messages[-1].content.startswith("TOOL ERROR")
        assert escape_target.exists()  # nothing outside was touched

    def test_unknown_tool_is_reported_not_fatal(self, harness: AgentHarness) -> None:
        harness.provider.script(
            tool_response([("delete_everything", {})]),
            text_response("That tool does not exist, so nothing happened."),
        )
        outcome = harness.run("Delete everything.")
        assert outcome.succeeded
        assert not outcome.tool_results[0].ok

    def test_escape_via_list_directory_is_blocked(self, harness: AgentHarness) -> None:
        harness.provider.script(
            tool_response([("list_directory", {"path": "../.."})]),
            text_response("Outside the workspace is off limits."),
        )
        outcome = harness.run("List my home directory.")
        assert outcome.succeeded
        assert "outside the current workspace" in (outcome.tool_results[0].error or "")


class TestLimitsAndFailures:
    def test_iteration_limit_produces_a_fallback(self, harness: AgentHarness) -> None:
        harness.agent.set_max_iterations(2)
        harness.provider.script(
            tool_response([("list_directory", {"path": "."})]),
            tool_response([("list_directory", {"path": "src"})]),
        )
        outcome = harness.run("Keep exploring.")
        assert outcome.stopped_reason == "max_iterations"
        assert "tool-call limit" in outcome.text
        assert outcome.iterations == 2

    def test_last_round_forbids_tool_calls(self, harness: AgentHarness) -> None:
        harness.agent.set_max_iterations(2)
        harness.provider.script(
            tool_response([("list_directory", {"path": "."})]),
            text_response("Done, no more tools."),
        )
        harness.run("Explore.")
        assert harness.provider.requests[-1].tools == []

    def test_provider_failure_is_returned_not_raised(self, workspace: Workspace) -> None:
        class BrokenProvider(FakeModelProvider):
            def complete(self, request: ChatRequest) -> ChatResponse:
                raise LLMError("provider exploded")

        harness = AgentHarness(workspace, BrokenProvider())
        outcome = harness.run("hello")
        assert outcome.error
        assert not outcome.succeeded
        assert harness.seen[-1] is AgentEventType.AGENT_FAILED
        assert not harness.agent.is_running

    def test_agent_is_not_running_afterwards(self, harness: AgentHarness) -> None:
        harness.provider.script(text_response("ok"))
        harness.run("hi")
        assert harness.agent.is_running is False

    def test_a_second_turn_continues_the_conversation(self, harness: AgentHarness) -> None:
        harness.provider.script(text_response("First answer."), text_response("Second."))
        harness.run("First question.")
        harness.run("Follow-up question.")
        second = harness.provider.requests[1]
        contents = [m.content for m in second.messages]
        assert any("First question." in c for c in contents)
        assert contents[-1] == "Follow-up question."
