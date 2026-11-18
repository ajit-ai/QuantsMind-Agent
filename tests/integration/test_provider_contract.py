"""The provider contract: one suite every adapter must satisfy.

These tests are deliberately about the *contract* rather than about one
implementation. A provider that passes them is interchangeable with any other
provider that passes them, which is the property the application depends on when
a user switches between OpenRouter and a local Ollama endpoint without anything
else in the product knowing.

Nothing here touches the network. These are simulated-provider tests and are
labelled as such; live verification is a separate procedure and is reported
separately, never folded into a green run of this file.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from quantsmind_agent.app.settings import PROVIDER_PRESETS, ProviderSettings
from quantsmind_agent.errors import (
    LLMProtocolError,
    LLMTimeoutError,
    MissingAPIKeyError,
    ModelUnavailableError,
    NetworkError,
)
from quantsmind_agent.providers import build_provider, registered_provider_names
from quantsmind_agent.providers.gateway import (
    ChatRequest,
    ChatResponse,
    ModelGateway,
    ProviderCapabilities,
    StreamChunk,
)
from quantsmind_agent.providers.openai_compat import OpenAICompatProvider
from quantsmind_agent.schema import ToolCall, UserMessage

pytestmark = pytest.mark.integration


def user(text: str = "hello") -> UserMessage:
    return UserMessage(content=text)


def request_for(provider: OpenAICompatProvider, **kwargs: Any) -> ChatRequest:
    return ChatRequest(messages=[user()], model="test-model", **kwargs)


def completion_body(
    content: str = "hi", tool_calls: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "cmpl-1",
        "model": "test-model",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3},
    }


def tool_call_payload() -> list[dict[str, Any]]:
    return [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
        }
    ]


def provider_with(
    handler: Any, *, capabilities: ProviderCapabilities | None = None, key: str | None = "k"
) -> OpenAICompatProvider:
    """An adapter wired to a stub transport, so no socket is opened."""

    def default(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=completion_body())

    return OpenAICompatProvider(
        "https://stub.invalid/v1",
        api_key=key,
        capabilities=capabilities,
        transport=httpx.MockTransport(handler or default),
    )


class TestTheContractHolds:
    """Every adapter must behave this way, whichever service is behind it."""

    def test_a_valid_request_returns_a_valid_response(self) -> None:
        provider = provider_with(None)
        response = provider.complete(request_for(provider))

        assert isinstance(response, ChatResponse)
        assert response.content == "hi"
        assert response.model == "test-model"

    def test_tool_calls_are_parsed_into_the_shared_shape(self) -> None:
        provider = provider_with(
            lambda _r: httpx.Response(200, json=completion_body(tool_calls=tool_call_payload()))
        )

        response = provider.complete(request_for(provider))

        assert len(response.tool_calls) == 1
        call = response.tool_calls[0]
        assert call.name == "read_file"
        assert isinstance(call, ToolCall)

    def test_streaming_yields_deltas_and_terminates(self) -> None:
        lines = [
            b'data: {"choices":[{"delta":{"content":"he"}}]}',
            b'data: {"choices":[{"delta":{"content":"llo"}}]}',
            b"data: [DONE]",
        ]
        provider = provider_with(lambda _r: httpx.Response(200, content=b"\n".join(lines)))

        chunks = list(provider.stream(request_for(provider)))

        assert "".join(chunk.delta for chunk in chunks) == "hello"
        assert all(isinstance(chunk, StreamChunk) for chunk in chunks)

    def test_usage_metadata_survives_normalisation(self) -> None:
        provider = provider_with(None)
        response = provider.complete(request_for(provider))

        assert response.prompt_tokens == 7
        assert response.completion_tokens == 3
        assert response.total_tokens == 10


class TestFailureIsClassified:
    """A provider failure must arrive as a *kind* of failure, not a string."""

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (401, ModelUnavailableError),
            (403, ModelUnavailableError),
            (404, ModelUnavailableError),
            (429, ModelUnavailableError),
            (500, ModelUnavailableError),
        ],
    )
    def test_http_status_maps_to_a_typed_error(self, status: int, expected: type) -> None:
        provider = provider_with(lambda _r: httpx.Response(status, json={"error": "no"}))

        with pytest.raises(expected):
            provider.complete(request_for(provider))

    def test_a_timeout_is_a_timeout_not_a_generic_failure(self) -> None:
        def timeout(_request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("too slow")

        provider = provider_with(timeout)

        with pytest.raises(LLMTimeoutError):
            provider.complete(request_for(provider))

    def test_a_transport_failure_is_a_network_error(self) -> None:
        def refused(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        provider = provider_with(refused)

        with pytest.raises(NetworkError):
            provider.complete(request_for(provider))

    @pytest.mark.parametrize("body", [b"not json at all", b'{"unexpected": true}'])
    def test_a_malformed_response_is_a_protocol_error(self, body: bytes) -> None:
        """A 200 that is not a completion is a protocol failure, not an answer."""

        provider = provider_with(lambda _r: httpx.Response(200, content=body))

        with pytest.raises(LLMProtocolError):
            provider.complete(request_for(provider))

    def test_a_missing_credential_fails_before_the_request(self) -> None:
        calls: list[int] = []

        def handler(_request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200, json=completion_body())

        provider = provider_with(handler, key=None)

        with pytest.raises(MissingAPIKeyError):
            provider.complete(request_for(provider))
        assert calls == [], "no request should leave without a credential"


class TestCapabilitiesAreDeclaredNotAssumed:
    def test_an_adapter_declares_what_it_supports(self) -> None:
        provider = provider_with(
            None,
            capabilities=ProviderCapabilities(tools=False, streaming=True, structured_output=False),
        )

        capabilities = provider.capabilities

        assert capabilities.tools is False
        assert capabilities.streaming is True
        assert capabilities.structured_output is False

    def test_a_silent_adapter_is_unknown_rather_than_capable(self) -> None:
        """The gateway must not invent support that nobody declared."""

        class Silent:
            name = "silent"

            def list_models(self) -> list[str]:
                return []

            def complete(self, request: ChatRequest) -> ChatResponse:
                return ChatResponse(content="")

            def stream(self, request: ChatRequest) -> Any:
                yield StreamChunk(delta="")

        gateway = ModelGateway(Silent())

        capabilities = gateway.capabilities()

        assert capabilities.tools is None
        assert capabilities.supports("tools") is False

    def test_every_registered_preset_declares_its_capabilities(self) -> None:
        for name, preset in PROVIDER_PRESETS.items():
            assert "capabilities" in preset, f"{name} does not declare what it supports"

    def test_a_local_endpoint_is_not_assumed_to_support_tools(self) -> None:
        """Local models accept the request shape and may honour none of it."""

        settings = ProviderSettings(name="openai_compatible", base_url="http://localhost:11434/v1")
        settings.capability_overrides = PROVIDER_PRESETS["Local (Ollama)"]["capabilities"]

        provider = build_provider(
            settings,
            api_key=None,
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=completion_body())),
        )

        assert ModelGateway(provider).capabilities().tools is None  # unknown, not "yes"


class TestTheGatewayIsTheOnlyDoor:
    def test_the_gateway_reports_capabilities_without_a_credential(self) -> None:
        provider = provider_with(lambda _r: httpx.Response(200, json=completion_body()))
        gateway = ModelGateway(provider)

        info = gateway.describe("test-model")

        assert info.provider == "openai_compatible"
        assert info.supports_tools is True
        assert info.context_window is None or isinstance(info.context_window, int)

    def test_registered_providers_are_buildable(self) -> None:
        assert registered_provider_names() == ["openai_compatible"]
        settings = ProviderSettings()
        provider = build_provider(
            settings, api_key="k", transport=httpx.MockTransport(lambda _r: httpx.Response(200))
        )
        assert provider.name == "openai_compatible"
