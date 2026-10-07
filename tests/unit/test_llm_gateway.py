"""Unit tests for the provider-independent Model Gateway."""

from __future__ import annotations

import pytest

from quantsmind_agent.errors import LLMConfigurationError, LLMError, MissingAPIKeyError
from quantsmind_agent.events import AgentEventType, EventBus
from quantsmind_agent.llm import ChatRequest, ChatResponse, ModelGateway
from quantsmind_agent.schema import UserMessage
from tests.fakes import FakeModelProvider, text_response, tool_response


def _request(**kwargs: object) -> ChatRequest:
    defaults: dict[str, object] = {
        "messages": [UserMessage(content="hi")],
        "model": "fake-model",
    }
    defaults.update(kwargs)
    return ChatRequest(**defaults)  # type: ignore[arg-type]


class ExplodingProvider(FakeModelProvider):
    """Provider that always fails, to exercise error normalisation."""

    def __init__(self, exc: BaseException) -> None:
        super().__init__()
        self._exc = exc

    def complete(self, request: ChatRequest) -> ChatResponse:
        raise self._exc


class TestConfiguration:
    def test_missing_api_key_raises(self) -> None:
        gateway = ModelGateway(FakeModelProvider())
        assert gateway.has_api_key is False
        with pytest.raises(MissingAPIKeyError):
            gateway.require_api_key()

    def test_api_key_can_be_set(self) -> None:
        gateway = ModelGateway(FakeModelProvider())
        gateway.set_api_key("abc123456")
        assert gateway.has_api_key
        assert gateway.require_api_key() == "abc123456"
        gateway.set_api_key(None)
        assert not gateway.has_api_key

    def test_provider_can_be_swapped(self) -> None:
        gateway = ModelGateway(FakeModelProvider())
        replacement = FakeModelProvider()
        gateway.set_provider(replacement)
        assert gateway.provider is replacement
        assert gateway.provider_name == "fake"

    def test_list_models(self) -> None:
        assert ModelGateway(FakeModelProvider()).list_models() == [
            "fake-model",
            "fake-model-mini",
        ]

    def test_describe_has_no_credentials(self) -> None:
        gateway = ModelGateway(FakeModelProvider(), api_key="super-secret-value")
        info = gateway.describe("fake-model")
        assert info.provider == "fake"
        assert "secret" not in repr(info)

    def test_validate_configuration_ok(self) -> None:
        ModelGateway(FakeModelProvider()).validate_configuration()

    def test_validate_configuration_rejects_non_http_url(self) -> None:
        provider = FakeModelProvider()
        provider.base_url = "ftp://bad"
        with pytest.raises(LLMConfigurationError):
            ModelGateway(provider).validate_configuration()

    def test_validate_configuration_rejects_unnamed_provider(self) -> None:
        provider = FakeModelProvider()
        provider.name = ""
        with pytest.raises(LLMConfigurationError):
            ModelGateway(provider).validate_configuration()


class TestComplete:
    def test_returns_provider_response(self) -> None:
        gateway = ModelGateway(FakeModelProvider([text_response("done")]))
        assert gateway.complete(_request()).content == "done"

    def test_emits_request_and_response_events(self) -> None:
        bus = EventBus()
        seen: list[AgentEventType] = []
        bus.subscribe_all(lambda event: seen.append(event.type))
        gateway = ModelGateway(FakeModelProvider([text_response("done")]), event_bus=bus)
        gateway.complete(_request())
        assert seen == [AgentEventType.MODEL_REQUEST, AgentEventType.MODEL_RESPONSE]

    def test_records_latency(self) -> None:
        gateway = ModelGateway(FakeModelProvider([text_response("done")]))
        assert gateway.complete(_request()).latency_ms >= 0

    def test_errors_are_normalised(self) -> None:
        gateway = ModelGateway(ExplodingProvider(RuntimeError("kaboom")))
        with pytest.raises(LLMError) as info:
            gateway.complete(_request())
        assert "kaboom" in str(info.value)
        assert "unexpected internal" not in info.value.user_message

    def test_existing_llm_errors_pass_through(self) -> None:
        original = LLMError("already classified")
        gateway = ModelGateway(ExplodingProvider(original))
        with pytest.raises(LLMError) as info:
            gateway.complete(_request())
        assert info.value is original

    def test_error_event_is_emitted(self) -> None:
        bus = EventBus()
        payloads: list[dict[str, object]] = []
        bus.subscribe(AgentEventType.MODEL_RESPONSE, lambda event: payloads.append(event.payload))
        gateway = ModelGateway(ExplodingProvider(RuntimeError("x")), event_bus=bus)
        with pytest.raises(LLMError):
            gateway.complete(_request())
        assert payloads and payloads[0]["error"] == "RuntimeError"


class TestStream:
    def test_yields_chunks(self) -> None:
        gateway = ModelGateway(FakeModelProvider(stream_chunks=[["a", "b", "c"]]))
        deltas = [chunk.delta for chunk in gateway.stream(_request())]
        assert "".join(deltas) == "abc"

    def test_stream_to_response_assembles(self) -> None:
        gateway = ModelGateway(FakeModelProvider(stream_chunks=[["Hello", " world"]]))
        response = gateway.stream_to_response(_request())
        assert response.content == "Hello world"
        assert response.finish_reason == "stop"

    def test_stream_to_response_collects_tool_calls(self) -> None:
        provider = FakeModelProvider([tool_response([("read_file", {"path": "a.py"})])])
        gateway = ModelGateway(provider)
        response = gateway.stream_to_response(_request())
        assert response.wants_tools
        assert response.tool_calls[0].name == "read_file"

    def test_stream_errors_are_normalised(self) -> None:
        gateway = ModelGateway(ExplodingProvider(RuntimeError("stream boom")))
        with pytest.raises(LLMError):
            list(gateway.stream(_request()))

    def test_stream_marks_the_request_as_streaming(self) -> None:
        bus = EventBus()
        payloads: list[dict[str, object]] = []
        bus.subscribe(AgentEventType.MODEL_REQUEST, lambda event: payloads.append(event.payload))
        gateway = ModelGateway(FakeModelProvider(stream_chunks=[["x"]]), event_bus=bus)
        list(gateway.stream(_request()))
        assert payloads[0]["stream"] is True

    def test_empty_chunks_are_not_emitted(self) -> None:
        from quantsmind_agent.llm import StreamChunk

        assert StreamChunk().is_empty


class TestAssistantConversion:
    def test_response_becomes_an_assistant_message(self) -> None:
        response = tool_response([("read_file", {"path": "a.py"})], content="checking")
        message = response.as_assistant_message()
        assert message.tool_calls[0].name == "read_file"
        assert message.content == "checking"
        assert message.metadata == {}

    def test_wire_format_round_trip(self) -> None:
        response = tool_response([("read_file", {"path": "a.py"})])
        wire = response.as_assistant_message().to_wire()
        assert wire["role"] == "assistant"
        assert wire["tool_calls"][0]["function"]["name"] == "read_file"
        assert '"path": "a.py"' in wire["tool_calls"][0]["function"]["arguments"]
