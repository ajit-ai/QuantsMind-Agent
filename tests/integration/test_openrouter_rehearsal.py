"""Offline rehearsal of the three proposed OpenRouter live-test scenarios.

Nothing here reaches OpenRouter. Every response is served by an in-process
``httpx`` transport, so these are **offline rehearsals of the request shapes
the live test will use**, not results from the live service. A pass here says
the application would send the right request and would handle the answer; it
says nothing about how OpenRouter or any model behaves.

The three scenarios are the ones the live-test plan will run:

* **L1** a basic non-streaming completion;
* **L2** the model requests a registered read-only tool;
* **L3** the model requests a write and the permission broker denies it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from quantsmind_agent.app.settings import PROVIDER_PRESETS, ProviderSettings
from quantsmind_agent.errors import ModelUnavailableError
from quantsmind_agent.providers import OPENAI_COMPAT, build_provider
from quantsmind_agent.providers.gateway import ChatRequest, ModelGateway, ProviderCapabilities
from quantsmind_agent.runtime.tools import ToolRegistry
from quantsmind_agent.schema import UserMessage
from quantsmind_agent.security.permissions import (
    ApprovalOutcome,
    Decision,
    PermissionBroker,
    StaticPermissionProvider,
)
from quantsmind_agent.services.workspace.containment import Workspace
from quantsmind_agent.workspace_tools import build_default_tools
from quantsmind_agent.write_tools import build_mutating_tools

pytestmark = pytest.mark.integration

TEST_KEY = "sk-or-v1-rehearsal-not-a-real-key"
OPENROUTER_BASE = "https://openrouter.ai/api/v1"


def _settings(model: str = "minimax/minimax-m3.1-flash:free") -> ProviderSettings:
    """The OpenRouter endpoint with a chosen model, as the settings dialog builds it."""

    return ProviderSettings(
        name=OPENAI_COMPAT,
        base_url=OPENROUTER_BASE,
        model=model,
    )


class _Endpoint(httpx.BaseTransport):
    def __init__(self, responder: Callable[[httpx.Request], httpx.Response]) -> None:
        self.responder = responder
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responder(request)


def _completion(
    content: str = "", tool_calls: list[dict[str, Any]] | None = None
) -> httpx.Response:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    response: httpx.Response = httpx.Response(
        200,
        json={
            "id": "gen-rehearsal",
            "model": "minimax/minimax-m3.1-flash:free",
            "choices": [{"message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13},
        },
    )
    return response


def _tool_call(name: str, arguments: dict[str, Any], call_id: str = "call_1") -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


class TestL1BasicCompletion:
    """L1 -- a basic, non-streaming completion."""

    def test_the_request_shape_is_the_one_the_live_test_will_use(self) -> None:
        endpoint = _Endpoint(lambda request: _completion("hello from the model"))
        settings = _settings()
        provider = build_provider(
            settings,
            api_key=lambda: TEST_KEY,
            transport=endpoint,
        )
        gateway = ModelGateway(provider)
        response = gateway.complete(
            ChatRequest(
                messages=[UserMessage(content="say hello")],
                model=settings.model,
                max_output_tokens=256,
                timeout_seconds=60.0,
            )
        )

        sent = endpoint.requests[0]
        assert str(sent.url) == f"{OPENROUTER_BASE}/chat/completions"
        assert sent.headers["authorization"] == f"Bearer {TEST_KEY}"
        body = json.loads(sent.content)
        assert body["model"] == "minimax/minimax-m3.1-flash:free"
        assert body["stream"] is False
        assert body["max_tokens"] == 256
        assert response.content == "hello from the model"
        assert response.total_tokens == 13

    def test_a_provider_error_does_not_carry_the_key(self) -> None:
        endpoint = _Endpoint(lambda request: httpx.Response(401, json={"error": {"message": "no"}}))
        provider = build_provider(
            _settings(),
            api_key=lambda: TEST_KEY,
            transport=endpoint,
        )
        with pytest.raises(ModelUnavailableError) as caught:
            provider.complete(
                ChatRequest(
                    messages=[UserMessage(content="hi")],
                    model="minimax/minimax-m3.1-flash:free",
                )
            )
        assert TEST_KEY not in str(caught.value)
        assert TEST_KEY not in caught.value.user_message
        assert "API key" in caught.value.user_message


class TestL2ReadOnlyTool:
    """L2 -- the model requests a registered read-only tool."""

    def test_a_read_only_tool_call_is_executed_after_authorisation(self, tmp_path: Path) -> None:
        workspace = Workspace(tmp_path)
        (workspace.root / "notes.txt").write_text("rehearsal content\n", encoding="utf-8")

        def responder(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            if payload.get("tools"):
                return httpx.Response(
                    200,
                    json={
                        "model": "minimax/minimax-m3.1-flash:free",
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": "",
                                    "tool_calls": [
                                        _tool_call("read_file", {"path": "notes.txt"}, "call_1")
                                    ],
                                },
                                "finish_reason": "tool_calls",
                            }
                        ],
                    },
                )
            return _completion("I read the file.")

        endpoint = _Endpoint(responder)
        settings = _settings()
        provider = build_provider(
            settings,
            api_key=lambda: TEST_KEY,
            transport=endpoint,
        )
        broker = PermissionBroker(
            StaticPermissionProvider.for_v0_2(),
            approver=lambda request: ApprovalOutcome.ALLOW_ONCE,
        )
        registry = ToolRegistry(permissions=broker)
        registry.register_all(build_default_tools(workspace))
        gateway = ModelGateway(provider)
        core = _agent(gateway, registry, workspace)
        outcome = core.run("read notes.txt")

        assert [result.tool for result in outcome.tool_results] == ["read_file"]
        assert outcome.tool_results[0].ok
        assert len(broker.history) == 1
        assert broker.history[0][1] is Decision.ALLOW
        assert outcome.succeeded


class TestL3WriteDenied:
    """L3 -- a model-generated write request is denied by the permission broker."""

    def test_a_write_request_is_refused_and_changes_nothing(self, tmp_path: Path) -> None:
        workspace = Workspace(tmp_path)
        (workspace.root / "notes.txt").write_text("original\n", encoding="utf-8")

        def responder(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            if payload.get("tools"):
                return httpx.Response(
                    200,
                    json={
                        "model": "minimax/minimax-m3.1-flash:free",
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": "",
                                    "tool_calls": [
                                        _tool_call(
                                            "write_file",
                                            {"path": "notes.txt", "content": "replaced"},
                                            "call_1",
                                        )
                                    ],
                                },
                                "finish_reason": "tool_calls",
                            }
                        ],
                    },
                )
            return _completion("I could not write the file.")

        endpoint = _Endpoint(responder)
        provider = build_provider(
            _settings(),
            api_key=lambda: TEST_KEY,
            transport=endpoint,
        )
        broker = PermissionBroker(StaticPermissionProvider.for_v0_2())
        registry = ToolRegistry(permissions=broker)
        registry.register_all(build_default_tools(workspace))
        registry.register_all(build_mutating_tools(workspace))
        gateway = ModelGateway(provider)
        core = _agent(gateway, registry, workspace)
        outcome = core.run("overwrite notes.txt")

        assert outcome.tool_results, "the write was never attempted"
        assert not outcome.tool_results[0].ok
        assert (workspace.root / "notes.txt").read_text(encoding="utf-8") == "original\n"
        assert broker.history, "the broker recorded no decision"
        assert broker.history[0][1] is Decision.DENY


def _agent(gateway: ModelGateway, registry: ToolRegistry, workspace: Workspace) -> Any:
    from quantsmind_agent.context import ContextEngine
    from quantsmind_agent.runtime.agent.core import AgentCore
    from quantsmind_agent.services.conversations.session import Session

    return AgentCore(
        gateway,
        registry,
        ContextEngine(workspace),
        Session(workspace),
        max_iterations=2,
        use_streaming=False,
    )


class TestEndpointAndCapability:
    def test_the_openrouter_preset_endpoint_is_the_api_root(self) -> None:
        assert PROVIDER_PRESETS["OpenRouter"]["base_url"] == OPENROUTER_BASE

    def test_openrouter_is_told_not_to_assume_structured_output(self) -> None:
        """The preset declares structured output unknown, so it must not be assumed."""

        settings = ProviderSettings(
            name=OPENAI_COMPAT,
            base_url=PROVIDER_PRESETS["OpenRouter"]["base_url"],
            model="minimax/minimax-m3.1-flash:free",
        )
        provider = build_provider(settings, api_key=lambda: TEST_KEY)
        capabilities = getattr(provider, "capabilities", None)
        assert isinstance(capabilities, ProviderCapabilities)
        assert capabilities.structured_output is None
        assert capabilities.supports("structured_output") is False

    def test_openrouter_is_told_it_can_call_tools(self) -> None:
        settings = ProviderSettings(
            name=OPENAI_COMPAT,
            base_url=PROVIDER_PRESETS["OpenRouter"]["base_url"],
            model="minimax/minimax-m3.1-flash:free",
        )
        provider = build_provider(settings, api_key=lambda: TEST_KEY)
        capabilities = getattr(provider, "capabilities", None)
        assert isinstance(capabilities, ProviderCapabilities)
        assert capabilities.supports("tools") is True

    def test_the_endpoint_requires_a_credential(self) -> None:
        assert ProviderSettings(base_url=OPENROUTER_BASE).requires_api_key() is True
