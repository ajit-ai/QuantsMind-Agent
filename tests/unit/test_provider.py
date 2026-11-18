"""Unit tests for the OpenAI-compatible provider (non-streaming paths).

No test in this module performs network I/O: an ``httpx`` mock transport serves
canned responses, so the suite is deterministic and offline.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from quantsmind_agent.errors import (
    LLMConfigurationError,
    LLMProtocolError,
    LLMTimeoutError,
    MissingAPIKeyError,
    ModelUnavailableError,
    NetworkError,
)
from quantsmind_agent.providers.gateway import ChatRequest
from quantsmind_agent.providers.openai_compat import OpenAICompatProvider
from quantsmind_agent.schema import SystemMessage, UserMessage
from tests.fakes import CONNECT_ERROR, TIMEOUT_ERROR, canned_body, mock_transport

SECRET = "sk-test-0123456789abcdef"


def _provider(handler: httpx.BaseTransport, **kwargs: Any) -> OpenAICompatProvider:
    defaults: dict[str, Any] = {
        "api_key": SECRET,
        "requires_api_key": True,
        "transport": handler,
    }
    defaults.update(kwargs)
    return OpenAICompatProvider("https://llm.test/v1", **defaults)


def _request(**kwargs: Any) -> ChatRequest:
    defaults: dict[str, Any] = {
        "messages": [SystemMessage(content="s"), UserMessage(content="hi")],
        "model": "test-model",
    }
    defaults.update(kwargs)
    return ChatRequest(**defaults)


class TestConfiguration:
    def test_base_url_is_normalised(self) -> None:
        provider = OpenAICompatProvider("https://llm.test/v1/", api_key="k")
        assert provider.base_url == "https://llm.test/v1"

    def test_non_http_base_url_rejected(self) -> None:
        with pytest.raises(LLMConfigurationError):
            OpenAICompatProvider("ftp://llm.test", api_key="k")

    def test_missing_api_key_raises_on_request(self) -> None:
        provider = OpenAICompatProvider(
            "https://llm.test/v1",
            api_key=None,
            requires_api_key=True,
            transport=mock_transport({"/chat/completions": canned_body()}),
        )
        with pytest.raises(MissingAPIKeyError) as info:
            provider.complete(_request())
        assert "API key" in info.value.user_message

    def test_no_api_key_needed_for_local_servers(self) -> None:
        provider = OpenAICompatProvider(
            "http://localhost:11434/v1",
            api_key=None,
            requires_api_key=False,
            transport=mock_transport({"/chat/completions": canned_body()}),
        )
        assert provider.complete(_request()).content == "hello"

    def test_api_key_can_be_a_callable(self) -> None:
        provider = OpenAICompatProvider(
            "https://llm.test/v1",
            api_key=lambda: "dynamic-key",
            transport=mock_transport({"/chat/completions": canned_body()}),
        )
        provider.complete(_request())
        assert provider._resolve_api_key() == "dynamic-key"

    def test_organization_header(self) -> None:
        handler = mock_transport({"/chat/completions": canned_body()})
        _provider(handler, organization="org-123").complete(_request())
        assert handler.requests[0].headers["OpenAI-Organization"] == "org-123"


class TestRequestBuilding:
    def test_payload_shape(self) -> None:
        payload = OpenAICompatProvider.build_payload(
            _request(tools=[{"type": "function"}], stop=["END"]), stream=False
        )
        assert payload["model"] == "test-model"
        assert payload["stream"] is False
        assert payload["messages"][0]["role"] == "system"
        assert payload["messages"][1]["role"] == "user"
        assert payload["tools"] == [{"type": "function"}]
        assert payload["tool_choice"] == "auto"
        assert payload["stop"] == ["END"]

    def test_stream_flag(self) -> None:
        assert OpenAICompatProvider.build_payload(_request(), stream=True)["stream"] is True

    def test_authorization_header_is_sent(self) -> None:
        handler = mock_transport({"/chat/completions": canned_body()})
        _provider(handler).complete(_request())
        assert handler.requests[0].headers["Authorization"] == f"Bearer {SECRET}"

    def test_user_agent(self) -> None:
        handler = mock_transport({"/chat/completions": canned_body()})
        _provider(handler).complete(_request())
        assert "QuantsMind-Agent" in handler.requests[0].headers["User-Agent"]


class TestComplete:
    def test_parses_content_and_usage(self) -> None:
        handler = mock_transport({"/chat/completions": canned_body()})
        response = _provider(handler).complete(_request())
        assert response.content == "hello"
        assert response.finish_reason == "stop"
        assert response.total_tokens == 17
        assert response.model == "test-model"

    def test_parses_tool_calls(self) -> None:
        body = canned_body(
            content="",
            finish_reason="tool_calls",
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                }
            ],
        )
        response = _provider(mock_transport({"/chat/completions": body})).complete(_request())
        assert response.wants_tools
        assert response.tool_calls[0].name == "read_file"
        assert response.tool_calls[0].arguments == {"path": "a.py"}
        assert response.tool_calls[0].id == "call_1"

    def test_malformed_tool_arguments_do_not_crash(self) -> None:
        body = canned_body(
            tool_calls=[{"id": "c", "function": {"name": "t", "arguments": "not json"}}]
        )
        response = _provider(mock_transport({"/chat/completions": body})).complete(_request())
        assert response.tool_calls[0].arguments == {"_raw": "not json"}

    def test_tool_call_without_a_name_is_skipped(self) -> None:
        body = canned_body(tool_calls=[{"id": "c", "function": {"arguments": "{}"}}])
        response = _provider(mock_transport({"/chat/completions": body})).complete(_request())
        assert not response.wants_tools

    def test_missing_choices_is_a_protocol_error(self) -> None:
        body = httpx.Response(status_code=200, json={"id": "x", "choices": []})
        with pytest.raises(LLMProtocolError) as info:
            _provider(mock_transport({"/chat/completions": body})).complete(_request())
        assert "empty response" in info.value.user_message

    def test_non_json_body_is_a_protocol_error(self) -> None:
        body = httpx.Response(status_code=200, text="<html>not json</html>")
        with pytest.raises(LLMProtocolError):
            _provider(mock_transport({"/chat/completions": body})).complete(_request())

    @pytest.mark.parametrize(
        ("status", "fragment"),
        [
            (401, "rejected the API key"),
            (403, "rejected the API key"),
            (404, "could not find that model"),
            (429, "rate limiting"),
            (500, "returned an error"),
        ],
    )
    def test_http_errors_are_actionable(self, status: int, fragment: str) -> None:
        body = httpx.Response(status_code=status, json={"error": {"message": "nope"}})
        with pytest.raises(ModelUnavailableError) as info:
            _provider(mock_transport({"/chat/completions": body})).complete(_request())
        assert fragment in info.value.user_message

    def test_timeout_is_reported_as_timeout(self) -> None:
        handler = mock_transport({"/chat/completions": TIMEOUT_ERROR})
        with pytest.raises(LLMTimeoutError) as info:
            _provider(handler).complete(_request(timeout_seconds=12))
        assert "too long" in info.value.user_message

    def test_connection_error_is_reported_as_network(self) -> None:
        handler = mock_transport({"/chat/completions": CONNECT_ERROR})
        with pytest.raises(NetworkError) as info:
            _provider(handler).complete(_request())
        assert "could not be reached" in info.value.user_message

    def test_error_text_never_contains_the_key(self) -> None:
        body = httpx.Response(status_code=401, json={"error": {"message": "bad key"}})
        with pytest.raises(ModelUnavailableError) as info:
            _provider(mock_transport({"/chat/completions": body})).complete(_request())
        assert SECRET not in str(info.value)
        assert SECRET not in info.value.user_message
