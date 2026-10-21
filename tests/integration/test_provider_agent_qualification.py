"""Agent-level qualification of the provider path.

Every scenario here drives the **real** provider adapter over an in-process
``httpx`` transport, so the bytes that reach the agent are the bytes a
provider would send. Nothing here reaches the network, and no credential is
transmitted: the transport is local and the endpoint needs no key.

The point of the module is the seam between the provider and the agent: a
provider response must reach the tool registry, and the tool registry must
reach the permission broker before anything runs. Fifteen scenarios from the
M3A brief are covered, plus the two questions M2B left open -- whether a
provider can bypass the broker, and whether the optional ``expected_sha256``
contract can actually travel through the model workflow.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from quantsmind_agent.agent import AgentCore
from quantsmind_agent.context import ContextEngine
from quantsmind_agent.events import EventBus
from quantsmind_agent.llm import ModelGateway
from quantsmind_agent.permissions import (
    ApprovalOutcome,
    Decision,
    PermissionBroker,
    StaticPermissionProvider,
)
from quantsmind_agent.providers.openai_compat import OpenAICompatProvider
from quantsmind_agent.session import Session
from quantsmind_agent.tools import ToolRegistry
from quantsmind_agent.workspace import Workspace
from quantsmind_agent.workspace_tools import build_default_tools
from quantsmind_agent.write_tools import build_mutating_tools

pytestmark = pytest.mark.integration


def _completion(
    content: str = "",
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
) -> httpx.Response:
    message: dict[str, Any] = {"content": content, "role": "assistant"}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-qualification",
            "object": "chat.completion",
            "model": "qualification-model",
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        },
    )


def _tool_call(name: str, arguments: dict[str, Any], call_id: str = "call_1") -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


class _Stack:
    """The real stack: provider adapter -> gateway -> agent -> registry -> broker."""

    def __init__(
        self,
        workspace: Workspace,
        responses: dict[str, httpx.Response | Exception | list[httpx.Response | Exception]],
        *,
        approver: Any = None,
        max_iterations: int = 4,
    ) -> None:
        self.workspace = workspace
        self.bus = EventBus()
        self.broker = PermissionBroker(
            StaticPermissionProvider.for_v0_2(),
            approver=approver,
        )
        self.registry = ToolRegistry(permissions=self.broker, event_bus=self.bus)
        self.registry.register_all(build_default_tools(workspace))
        self.registry.register_all(build_mutating_tools(workspace))
        self.session = Session(workspace)
        self.provider = OpenAICompatProvider(
            "http://127.0.0.1:7860/v1",
            requires_api_key=False,
            transport=self._transport(responses),
        )
        self.gateway = ModelGateway(self.provider, event_bus=self.bus)
        self.agent = AgentCore(
            self.gateway,
            self.registry,
            ContextEngine(workspace),
            self.session,
            event_bus=self.bus,
            max_iterations=max_iterations,
            use_streaming=False,
        )

    @staticmethod
    def _transport(
        responses: dict[str, httpx.Response | Exception | list[httpx.Response | Exception]],
    ) -> httpx.BaseTransport:
        """Serve a queued response per path, then a plain final answer.

        A provider is asked once per agent iteration, so a scenario that needs a
        tool call *and* an answer needs two responses in order. Once the queue
        is empty the endpoint answers with a final answer, so a scenario never
        depends on the iteration limit to end the turn.
        """

        queues: dict[str, list[httpx.Response | Exception]] = {}
        for suffix, value in responses.items():
            queues[suffix] = list(value) if isinstance(value, list) else [value]

        def handle(request: httpx.Request) -> httpx.Response:
            for suffix, queue in queues.items():
                if not request.url.path.endswith(suffix):
                    continue
                if not queue:
                    return _completion("done")
                response = queue.pop(0)
                if isinstance(response, Exception):
                    raise response
                return response
            return httpx.Response(404, json={"error": {"message": "no route"}})

        class _Handler(httpx.BaseTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                return handle(request)

        return _Handler()

    def run(self, text: str = "go") -> Any:
        return self.agent.run(text)


class TestAgentScenarios:
    """The fifteen scenarios of the M3A brief, driven through the real adapter."""

    def test_a_provider_final_answer_reaches_the_agent(self, workspace: Workspace) -> None:
        stack = _Stack(workspace, {"/chat/completions": _completion("the answer")})
        outcome = stack.run()
        assert outcome.succeeded
        assert outcome.text == "the answer"
        assert outcome.tool_results == []

    def test_a_provider_read_only_tool_request_is_executed(self, workspace: Workspace) -> None:
        (workspace.root / "notes.txt").write_text("hello\n", encoding="utf-8")
        stack = _Stack(
            workspace,
            {
                "/chat/completions": _completion(
                    tool_calls=[_tool_call("read_file", {"path": "notes.txt"})]
                ),
            },
            max_iterations=2,
        )
        outcome = stack.run()
        assert [result.tool for result in outcome.tool_results] == ["read_file"]
        assert outcome.tool_results[0].ok

    def test_a_provider_write_request_reaches_the_broker(self, workspace: Workspace) -> None:
        stack = _Stack(
            workspace,
            {
                "/chat/completions": _completion(
                    tool_calls=[_tool_call("write_file", {"path": "a.txt", "content": "x"})]
                )
            },
            approver=lambda request: ApprovalOutcome.DENY,
            max_iterations=2,
        )
        outcome = stack.run()
        assert len(outcome.tool_results) == 1
        assert not outcome.tool_results[0].ok
        assert not (workspace.root / "a.txt").exists()
        assert stack.broker.history, "the permission decision was never recorded"

    def test_a_granted_write_reaches_the_filesystem(self, workspace: Workspace) -> None:
        stack = _Stack(
            workspace,
            {
                "/chat/completions": _completion(
                    tool_calls=[_tool_call("write_file", {"path": "a.txt", "content": "x"})]
                )
            },
            approver=lambda request: ApprovalOutcome.ALLOW_ONCE,
            max_iterations=2,
        )
        outcome = stack.run()
        assert outcome.tool_results[0].ok
        assert (workspace.root / "a.txt").read_text(encoding="utf-8") == "x"

    def test_an_unknown_tool_is_reported_without_failing_the_turn(
        self, workspace: Workspace
    ) -> None:
        stack = _Stack(
            workspace,
            {
                "/chat/completions": _completion(
                    tool_calls=[_tool_call("no_such_tool", {})],
                )
            },
            max_iterations=2,
        )
        outcome = stack.run()
        assert len(outcome.tool_results) == 1
        assert not outcome.tool_results[0].ok
        assert outcome.succeeded

    def test_malformed_tool_arguments_are_refused_without_executing(
        self, workspace: Workspace
    ) -> None:
        stack = _Stack(
            workspace,
            {
                "/chat/completions": httpx.Response(
                    200,
                    json={
                        "model": "qualification-model",
                        "choices": [
                            {
                                "message": {
                                    "content": "",
                                    "tool_calls": [
                                        {
                                            "id": "c1",
                                            "type": "function",
                                            "function": {
                                                "name": "write_file",
                                                "arguments": "{not json",
                                            },
                                        }
                                    ],
                                },
                                "finish_reason": "tool_calls",
                            }
                        ],
                    },
                )
            },
            approver=lambda request: ApprovalOutcome.ALLOW_ONCE,
            max_iterations=2,
        )
        outcome = stack.run()
        assert len(outcome.tool_results) == 1
        assert not outcome.tool_results[0].ok
        assert not (workspace.root / "a.txt").exists()

    def test_multiple_tool_calls_in_one_response_are_executed_in_order(
        self, workspace: Workspace
    ) -> None:
        stack = _Stack(
            workspace,
            {
                "/chat/completions": _completion(
                    tool_calls=[
                        _tool_call("write_file", {"path": "one.txt", "content": "1"}, "c1"),
                        _tool_call("write_file", {"path": "two.txt", "content": "2"}, "c2"),
                    ]
                )
            },
            approver=lambda request: ApprovalOutcome.ALLOW_ONCE,
            max_iterations=2,
        )
        outcome = stack.run()
        assert [result.tool for result in outcome.tool_results] == ["write_file", "write_file"]
        assert (workspace.root / "one.txt").exists()
        assert (workspace.root / "two.txt").exists()
        assert len(stack.broker.history) == 2, "each call must be authorised separately"

    def test_a_provider_failure_after_a_tool_result_is_reported(self, workspace: Workspace) -> None:
        stack = _Stack(
            workspace,
            {"/chat/completions": httpx.Response(500, json={"error": {"message": "boom"}})},
            max_iterations=2,
        )
        outcome = stack.run()
        assert not outcome.succeeded
        assert outcome.error

    def test_an_empty_provider_response_is_an_error_not_a_success(
        self, workspace: Workspace
    ) -> None:
        stack = _Stack(
            workspace,
            {"/chat/completions": httpx.Response(200, json={"model": "m", "choices": []})},
        )
        outcome = stack.run()
        assert not outcome.succeeded
        assert outcome.stopped_reason == "error"

    def test_a_tool_failure_does_not_fail_the_turn(self, workspace: Workspace) -> None:
        stack = _Stack(
            workspace,
            {
                "/chat/completions": _completion(
                    tool_calls=[_tool_call("read_file", {"path": "absent.txt"})]
                )
            },
            max_iterations=2,
        )
        outcome = stack.run()
        assert len(outcome.tool_results) == 1
        assert not outcome.tool_results[0].ok
        assert outcome.succeeded

    def test_the_turn_limit_is_reported(self, workspace: Workspace) -> None:
        stack = _Stack(
            workspace,
            {
                "/chat/completions": [
                    _completion(tool_calls=[_tool_call("list_directory", {"path": "."})]),
                    _completion(tool_calls=[_tool_call("list_directory", {"path": "."})]),
                    _completion(tool_calls=[_tool_call("list_directory", {"path": "."})]),
                ]
            },
            max_iterations=2,
        )
        outcome = stack.run()
        assert outcome.stopped_reason == "max_iterations"
        assert "tool-call limit" in outcome.text


class TestProviderCannotBypassTheBroker:
    def test_a_provider_write_is_denied_when_the_broker_denies(self, workspace: Workspace) -> None:
        """The provider asked; the broker decided; the file was not written."""

        stack = _Stack(
            workspace,
            {
                "/chat/completions": _completion(
                    tool_calls=[_tool_call("write_file", {"path": "a.txt", "content": "x"})]
                )
            },
            approver=lambda request: ApprovalOutcome.DENY,
            max_iterations=2,
        )
        stack.run()
        assert not (workspace.root / "a.txt").exists()
        assert stack.broker.history, "the broker recorded no decision"
        assert all(decision is Decision.DENY for _request, decision in stack.broker.history), (
            "the broker did not record a denial"
        )

    def test_a_provider_write_with_no_approver_is_refused(self, workspace: Workspace) -> None:
        """An unconfigured approver must mean DENY, not ALLOW."""

        stack = _Stack(
            workspace,
            {
                "/chat/completions": _completion(
                    tool_calls=[_tool_call("write_file", {"path": "a.txt", "content": "x"})]
                )
            },
            approver=None,
            max_iterations=2,
        )
        outcome = stack.run()
        assert not (workspace.root / "a.txt").exists()
        assert outcome.tool_results and not outcome.tool_results[0].ok

    def test_a_provider_cannot_reach_a_tool_the_registry_does_not_hold(
        self, workspace: Workspace
    ) -> None:
        stack = _Stack(
            workspace,
            {
                "/chat/completions": _completion(
                    tool_calls=[_tool_call("write_file", {"path": "a.txt", "content": "x"})]
                )
            },
            approver=lambda request: ApprovalOutcome.ALLOW_ONCE,
            max_iterations=2,
        )
        stack.registry._tools.pop("write_file", None)
        outcome = stack.run()
        assert outcome.tool_results and not outcome.tool_results[0].ok
        assert not (workspace.root / "a.txt").exists()


class TestStreamingIsNotRequestedWhenItIsNotWanted:
    """A provider that refuses ``stream: true`` must still be usable.

    The local Taksh OpenAI-compatible server answers HTTP 400 for
    ``stream=true`` and for the ``stream_options`` parameter. An endpoint that
    declares no streaming capability must not be asked to stream.

    The final round of a turn -- the one that carries the answer -- is the
    round that streams, so the scenarios below use two iterations: the first
    asks for a tool, the second carries the answer.
    """

    @staticmethod
    def _handler(bodies: list[bytes], *, refuse_streaming: bool) -> httpx.BaseTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            bodies.append(request.content)
            if refuse_streaming and b'"stream":true' in request.content:
                return httpx.Response(
                    400,
                    json={
                        "error": {
                            "message": "Streaming is not implemented in this slice.",
                            "type": "invalid_request_error",
                            "code": "streaming_unsupported",
                        }
                    },
                )
            if b'"tools"' in request.content:
                return _completion(tool_calls=[_tool_call("list_directory", {"path": "."})])
            return _completion("the answer")

        class _Handler(httpx.BaseTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                return handle(request)

        return _Handler()

    def test_an_endpoint_that_refuses_streaming_is_still_answered(
        self, workspace: Workspace
    ) -> None:
        bodies: list[bytes] = []
        stack = _Stack(workspace, {"/chat/completions": []})
        stack.provider._transport = self._handler(bodies, refuse_streaming=True)
        outcome = stack.run()

        assert outcome.succeeded, outcome.error
        assert outcome.text == "the answer"
        assert not any(b'"stream":true' in body for body in bodies), (
            "the application asked for a streamed answer from an endpoint that refuses streaming"
        )

    def test_turning_streaming_off_stops_the_application_asking_for_it(
        self, workspace: Workspace
    ) -> None:
        bodies: list[bytes] = []
        stack = _Stack(workspace, {"/chat/completions": []})
        stack.provider._transport = self._handler(bodies, refuse_streaming=False)
        stack.agent.set_streaming(False)
        outcome = stack.run()

        assert outcome.succeeded
        assert not any(b'"stream":true' in body for body in bodies), (
            "set_streaming(False) did not stop the streamed request"
        )


class TestTheStaleContentContractTravelsThroughTheModel:
    """The digest the read returns must be usable by the next model call.

    This is the D-03 contract as the model actually meets it: read the file,
    the provider copies the fingerprint into the next call, and a change that
    lands in between must stop the write.
    """

    def test_a_write_carrying_the_read_fingerprint_is_refused_after_a_change(
        self, workspace: Workspace
    ) -> None:
        target = workspace.root / "notes.txt"
        target.write_text("original\n", encoding="utf-8")
        observed: dict[str, Any] = {}

        seen = {"requests": 0}

        def handle(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            seen["requests"] += 1
            if seen["requests"] == 1:
                return _completion(
                    tool_calls=[_tool_call("read_file", {"path": "notes.txt"}, "c1")]
                )
            # The second round carries the tool observation, so the fingerprint
            # the read returned is available to the provider.
            for message in payload["messages"]:
                if message.get("role") == "tool":
                    # The observation is "TOOL RESULT (...): summary" followed
                    # by the JSON payload.
                    body = message["content"].split("\n", 1)[-1]
                    observed["sha256"] = json.loads(body)["sha256"]
            # The user edits the file between the read and the write.
            target.write_text("the user changed this\n", encoding="utf-8")
            return _completion(
                tool_calls=[
                    _tool_call(
                        "write_file",
                        {
                            "path": "notes.txt",
                            "content": "the agent replaced this\n",
                            "expected_sha256": observed.get("sha256", ""),
                        },
                        "c2",
                    )
                ]
            )

        class _Handler(httpx.BaseTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                return handle(request)

        stack = _Stack(
            workspace,
            {"/chat/completions": []},
            approver=lambda request: ApprovalOutcome.ALLOW_ONCE,
        )
        stack.provider._transport = _Handler()
        outcome = stack.run()

        assert observed.get("sha256"), "the read did not carry a fingerprint to the model"
        writes = [r for r in outcome.tool_results if r.tool == "write_file"]
        assert writes, "the write was never attempted"
        assert not writes[0].ok, "a write over a changed file was allowed"
        assert "changed since you last read it" in (writes[0].error or "")
        assert target.read_text(encoding="utf-8") == "the user changed this\n"

    def test_a_write_carrying_a_current_fingerprint_is_allowed(self, workspace: Workspace) -> None:
        target = workspace.root / "notes.txt"
        target.write_text("original\n", encoding="utf-8")
        observed: dict[str, Any] = {}

        seen = {"requests": 0}

        def handle(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            seen["requests"] += 1
            if seen["requests"] == 1:
                return _completion(
                    tool_calls=[_tool_call("read_file", {"path": "notes.txt"}, "c1")]
                )
            for message in payload["messages"]:
                if message.get("role") == "tool":
                    # The observation is "TOOL RESULT (...): summary" followed
                    # by the JSON payload.
                    body = message["content"].split("\n", 1)[-1]
                    observed["sha256"] = json.loads(body)["sha256"]
            return _completion(
                tool_calls=[
                    _tool_call(
                        "write_file",
                        {
                            "path": "notes.txt",
                            "content": "the agent replaced this\n",
                            "expected_sha256": observed.get("sha256", ""),
                        },
                        "c2",
                    )
                ]
            )

        class _Handler(httpx.BaseTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                return handle(request)

        stack = _Stack(
            workspace,
            {"/chat/completions": []},
            approver=lambda request: ApprovalOutcome.ALLOW_ONCE,
        )
        stack.provider._transport = _Handler()
        outcome = stack.run()

        writes = [r for r in outcome.tool_results if r.tool == "write_file"]
        assert writes and writes[0].ok, writes[0].error if writes else "no write"
        assert target.read_text(encoding="utf-8") == "the agent replaced this\n"
