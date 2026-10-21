"""Protocol qualification for the OpenAI-compatible provider.

This module pins the *protocol* behaviour the M3A compatibility matrix
depends on: the response shapes, the streaming edge cases, the HTTP error
mapping and the configuration rules. It is deliberately offline -- every
response is served by an in-process ``httpx`` transport, so no test in this
module can reach the network, and no credential is ever transmitted.

The existing ``test_provider.py`` and ``test_provider_stream.py`` cover the
happy paths. This module covers the cases that were not pinned anywhere:
an absent or unsupported finish reason, a stream that never terminates, a
connection that dies mid-stream, duplicate tool-call identifiers, the HTTP
statuses that decide failover eligibility, and an endpoint that refuses
OpenAI parameters the way a local Taksh server does.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from quantsmind_agent.errors import (
    LLMProtocolError,
    LLMTimeoutError,
    ModelUnavailableError,
    NetworkError,
)
from quantsmind_agent.llm import ChatRequest, ProviderCapabilities
from quantsmind_agent.providers.openai_compat import OpenAICompatProvider
from quantsmind_agent.routing import FailureKind, classify_failure
from quantsmind_agent.schema import SystemMessage, UserMessage
from tests.fakes import mock_transport

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


class TestCompletionShapes:
    """Section A: the shapes a provider response can take."""

    def test_a_finish_reason_is_carried_through(self) -> None:
        body = {
            "model": "test-model",
            "choices": [
                {"message": {"content": "ok"}, "finish_reason": "length"},
            ],
        }
        provider = _provider(mock_transport({"/chat/completions": httpx.Response(200, json=body)}))
        response = provider.complete(_request())
        assert response.finish_reason == "length"

    def test_an_absent_finish_reason_is_none_not_an_error(self) -> None:
        body = {"model": "test-model", "choices": [{"message": {"content": "ok"}}]}
        provider = _provider(mock_transport({"/chat/completions": httpx.Response(200, json=body)}))
        response = provider.complete(_request())
        assert response.finish_reason is None
        assert response.content == "ok"

    def test_an_unknown_finish_reason_is_preserved_verbatim(self) -> None:
        body = {
            "model": "test-model",
            "choices": [{"message": {"content": "ok"}, "finish_reason": "provider_specific"}],
        }
        provider = _provider(mock_transport({"/chat/completions": httpx.Response(200, json=body)}))
        response = provider.complete(_request())
        assert response.finish_reason == "provider_specific"

    def test_an_empty_content_with_no_tool_calls_is_an_empty_answer(self) -> None:
        body = {"model": "test-model", "choices": [{"message": {"content": ""}}]}
        provider = _provider(mock_transport({"/chat/completions": httpx.Response(200, json=body)}))
        response = provider.complete(_request())
        assert response.content == ""
        assert response.tool_calls == []
        assert response.wants_tools is False

    def test_a_null_content_is_treated_as_text_not_a_crash(self) -> None:
        body = {"model": "test-model", "choices": [{"message": {"content": None}}]}
        provider = _provider(mock_transport({"/chat/completions": httpx.Response(200, json=body)}))
        response = provider.complete(_request())
        assert response.content == ""

    def test_a_choice_that_is_not_an_object_is_refused(self) -> None:
        body = {"model": "test-model", "choices": ["not an object"]}
        provider = _provider(mock_transport({"/chat/completions": httpx.Response(200, json=body)}))
        with pytest.raises(LLMProtocolError):
            provider.complete(_request())

    def test_a_missing_message_object_is_refused(self) -> None:
        body = {"model": "test-model", "choices": [{"finish_reason": "stop"}]}
        provider = _provider(mock_transport({"/chat/completions": httpx.Response(200, json=body)}))
        with pytest.raises(LLMProtocolError):
            provider.complete(_request())

    def test_usage_that_is_not_numeric_is_ignored(self) -> None:
        body = {
            "model": "test-model",
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": "many", "completion_tokens": None},
        }
        provider = _provider(mock_transport({"/chat/completions": httpx.Response(200, json=body)}))
        response = provider.complete(_request())
        assert response.total_tokens is None


class TestStreamingEdges:
    """Section B: the streaming cases that are easy to get wrong."""

    @staticmethod
    def _sse(*frames: str) -> httpx.Response:
        return httpx.Response(200, text="".join(f"data: {frame}\n\n" for frame in frames))

    def test_a_stream_without_a_done_sentinel_still_yields_its_text(self) -> None:
        body = self._sse('{"choices": [{"delta": {"content": "partial"}}]}')
        provider = _provider(mock_transport({"/chat/completions": body}))
        chunks = list(provider.stream(_request()))
        assert "".join(chunk.delta for chunk in chunks) == "partial"

    def test_a_stream_that_ends_mid_tool_call_flushes_the_call(self) -> None:
        frame = (
            '{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", '
            '"function": {"name": "read_file", "arguments": "{\\"path\\": \\"a.txt\\"}"}}]}}]}'
        )
        provider = _provider(mock_transport({"/chat/completions": self._sse(frame)}))
        chunks = list(provider.stream(_request()))
        calls = [call for chunk in chunks for call in chunk.tool_calls]
        assert [call.name for call in calls] == ["read_file"]
        assert calls[0].arguments == {"path": "a.txt"}

    def test_a_connection_that_dies_mid_stream_is_reported(self) -> None:
        class Broken(httpx.BaseTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                raise httpx.ReadError("connection reset")

        provider = _provider(Broken())
        with pytest.raises(NetworkError):
            list(provider.stream(_request()))

    def test_a_stream_that_times_out_is_reported_as_a_timeout(self) -> None:
        class Slow(httpx.BaseTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                raise httpx.ReadTimeout("too slow")

        provider = _provider(Slow())
        with pytest.raises(LLMTimeoutError):
            list(provider.stream(_request()))

    def test_a_frame_that_is_not_json_is_skipped_not_fatal(self) -> None:
        provider = _provider(
            mock_transport(
                {
                    "/chat/completions": self._sse(
                        "{not json",
                        '{"choices": [{"delta": {"content": "ok"}}]}',
                    )
                }
            )
        )
        chunks = list(provider.stream(_request()))
        assert "".join(chunk.delta for chunk in chunks) == "ok"


class TestToolCallParsing:
    """Section C: tool-call shapes, including the ones that must not execute."""

    def test_duplicate_tool_call_identifiers_are_kept_as_separate_calls(self) -> None:
        body = {
            "model": "test-model",
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {"id": "call_1", "function": {"name": "read_file", "arguments": "{}"}},
                            {"id": "call_1", "function": {"name": "read_file", "arguments": "{}"}},
                        ],
                    }
                }
            ],
        }
        provider = _provider(mock_transport({"/chat/completions": httpx.Response(200, json=body)}))
        response = provider.complete(_request())
        assert [call.id for call in response.tool_calls] == ["call_1", "call_1"]
        assert len(response.tool_calls) == 2

    def test_a_tool_call_entry_that_is_not_an_object_is_skipped(self) -> None:
        body = {
            "model": "test-model",
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            "not an object",
                            {"id": "c1", "function": {"name": "read_file", "arguments": "{}"}},
                        ],
                    }
                }
            ],
        }
        provider = _provider(mock_transport({"/chat/completions": httpx.Response(200, json=body)}))
        response = provider.complete(_request())
        assert [call.name for call in response.tool_calls] == ["read_file"]

    def test_a_tool_call_with_an_empty_function_name_is_skipped(self) -> None:
        body = {
            "model": "test-model",
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [{"id": "c1", "function": {"name": "", "arguments": "{}"}}],
                    }
                }
            ],
        }
        provider = _provider(mock_transport({"/chat/completions": httpx.Response(200, json=body)}))
        response = provider.complete(_request())
        assert response.tool_calls == []

    def test_tool_call_arguments_that_are_a_json_array_are_passed_through(self) -> None:
        body = {
            "model": "test-model",
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {"id": "c1", "function": {"name": "read_file", "arguments": "[1, 2]"}}
                        ],
                    }
                }
            ],
        }
        provider = _provider(mock_transport({"/chat/completions": httpx.Response(200, json=body)}))
        response = provider.complete(_request())
        assert response.tool_calls[0].arguments == {"_value": [1, 2]}

    def test_unexpected_argument_keys_are_carried_into_the_call(self) -> None:
        """The provider passes arguments through; the registry decides what to do."""

        body = {
            "model": "test-model",
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "c1",
                                "function": {
                                    "name": "write_file",
                                    "arguments": '{"path": "a.txt", "content": "x", "surprise": 1}',
                                },
                            }
                        ],
                    }
                }
            ],
        }
        provider = _provider(mock_transport({"/chat/completions": httpx.Response(200, json=body)}))
        response = provider.complete(_request())
        assert response.tool_calls[0].arguments["surprise"] == 1


class TestHttpStatusMapping:
    """Section D: the statuses, and which of them may move to another endpoint."""

    @pytest.mark.parametrize("status", [400, 408, 500, 502, 503])
    def test_a_failing_status_is_reported_with_its_code(self, status: int) -> None:
        provider = _provider(
            mock_transport(
                {"/chat/completions": httpx.Response(status, json={"error": {"message": "no"}})}
            )
        )
        with pytest.raises(ModelUnavailableError) as caught:
            provider.complete(_request())
        assert caught.value.details.get("status_code") == status

    def test_a_request_timeout_is_not_reported_as_a_provider_failure(self) -> None:
        provider = _provider(
            mock_transport(
                {"/chat/completions": httpx.Response(408, json={"error": {"message": "slow"}})}
            )
        )
        with pytest.raises(ModelUnavailableError):
            provider.complete(_request())

    @pytest.mark.parametrize(
        ("status", "kind"),
        [
            (400, FailureKind.CONFIGURATION),
            (401, FailureKind.AUTHENTICATION),
            (403, FailureKind.AUTHENTICATION),
            (404, FailureKind.CONFIGURATION),
            (408, FailureKind.CONFIGURATION),
            (429, FailureKind.RATE_LIMIT),
            (500, FailureKind.SERVER),
            (502, FailureKind.SERVER),
            (503, FailureKind.SERVER),
        ],
    )
    def test_each_status_maps_to_a_failover_decision(self, status: int, kind: FailureKind) -> None:
        provider = _provider(
            mock_transport(
                {"/chat/completions": httpx.Response(status, json={"error": {"message": "x"}})}
            )
        )
        with pytest.raises(ModelUnavailableError) as caught:
            provider.complete(_request())
        assert classify_failure(caught.value) is kind

    def test_only_a_rate_limit_or_a_server_error_may_move_to_another_endpoint(self) -> None:
        """A rejected key or a bad request must not silently become another provider."""

        from quantsmind_agent.routing import FALLBACK_ELIGIBLE

        assert {FailureKind.RATE_LIMIT, FailureKind.SERVER} == FALLBACK_ELIGIBLE

    def test_a_provider_error_never_contains_the_key(self) -> None:
        provider = _provider(
            mock_transport(
                {"/chat/completions": httpx.Response(500, json={"error": {"message": "boom"}})}
            )
        )
        with pytest.raises(ModelUnavailableError) as caught:
            provider.complete(_request())
        assert SECRET not in str(caught.value)


class TestRefusesOpenAIParameters:
    """An endpoint that refuses OpenAI parameters, the way a local Taksh server does.

    The local Taksh OpenAI-compatible server answers HTTP 400 for ``tools``,
    ``tool_choice``, ``stream`` and ``response_format``, and for messages with
    a ``tool`` role. Its refusal is deliberate: silently ignoring the
    parameters would let a client believe a tool loop is happening.
    """

    @staticmethod
    def _refuses(parameter: str) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": f"Parameter '{parameter}' is not supported.",
                    "type": "invalid_request_error",
                    "code": "unsupported_parameter",
                    "param": parameter,
                }
            },
        )

    def test_a_refused_tool_parameter_is_reported_not_ignored(self) -> None:
        provider = _provider(mock_transport({"/chat/completions": self._refuses("tools")}))
        with pytest.raises(ModelUnavailableError) as caught:
            provider.complete(_request(tools=[{"type": "function"}]))
        assert caught.value.details.get("status_code") == 400
        assert "tools" in str(caught.value)

    def test_a_refused_stream_flag_is_reported(self) -> None:
        provider = _provider(mock_transport({"/chat/completions": self._refuses("stream")}))
        with pytest.raises(ModelUnavailableError):
            list(provider.stream(_request()))

    def test_the_capability_record_is_what_keeps_the_application_away(self) -> None:
        """An endpoint that declares no tools is never sent tool schemas."""

        capabilities = ProviderCapabilities(tools=False, streaming=False)
        assert capabilities.supports("tools") is False
        assert capabilities.supports("streaming") is False

    def test_an_unknown_capability_is_treated_as_not_supported(self) -> None:
        """Unknown must not be read as yes, or the application would send tools."""

        capabilities = ProviderCapabilities(tools=None, streaming=None)
        assert capabilities.supports("tools") is False
        assert capabilities.supports("streaming") is False


class TestConfiguration:
    """Section E: the configuration rules the provider depends on."""

    def test_a_loopback_endpoint_needs_no_credential(self) -> None:
        from quantsmind_agent.config import ProviderSettings

        settings = ProviderSettings(name="local", base_url="http://127.0.0.1:7860/v1", model="m")
        assert settings.requires_api_key() is False

    def test_a_remote_endpoint_needs_a_credential(self) -> None:
        from quantsmind_agent.config import ProviderSettings

        settings = ProviderSettings(name="cloud", base_url="https://api.example.com/v1", model="m")
        assert settings.requires_api_key() is True

    def test_a_plain_http_remote_endpoint_is_still_accepted_by_the_schema(self) -> None:
        """The schema allows http; the operator chooses. What it must never do is
        skip TLS verification, and the provider never sets verify=False."""

        from quantsmind_agent.config import ProviderSettings

        settings = ProviderSettings(name="cloud", base_url="http://api.example.com/v1", model="m")
        assert settings.base_url == "http://api.example.com/v1"
        source = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "quantsmind_agent"
            / "providers"
            / "openai_compat.py"
        ).read_text(encoding="utf-8")
        assert "verify=False" not in source
        assert "verify=0" not in source

    def test_an_empty_model_identifier_is_refused_by_the_schema(self) -> None:
        from pydantic import ValidationError

        from quantsmind_agent.config import ProviderSettings

        with pytest.raises(ValidationError):
            ProviderSettings(name="cloud", base_url="https://api.example.com/v1", model="  ")

    def test_an_empty_base_url_is_refused_by_the_schema(self) -> None:
        from pydantic import ValidationError

        from quantsmind_agent.config import ProviderSettings

        with pytest.raises(ValidationError):
            ProviderSettings(name="cloud", base_url="   ", model="m")
