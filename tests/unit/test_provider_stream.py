"""Unit tests for provider streaming and model discovery (offline)."""

from __future__ import annotations

import json

import httpx
import pytest

from quantsmind_agent.errors import LLMTimeoutError, ModelUnavailableError
from quantsmind_agent.providers.gateway import ChatRequest
from quantsmind_agent.providers.openai_compat import OpenAICompatProvider
from quantsmind_agent.schema import SystemMessage, UserMessage
from tests.fakes import TIMEOUT_ERROR, mock_transport, sse_body

SECRET = "sk-test-0123456789abcdef"


def _provider(handler: httpx.BaseTransport) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        "https://llm.test/v1",
        api_key=SECRET,
        requires_api_key=True,
        transport=handler,
    )


def _request() -> ChatRequest:
    return ChatRequest(
        messages=[SystemMessage(content="s"), UserMessage(content="hi")],
        model="test-model",
    )


def _sse(frames: list[str]) -> httpx.Response:
    return httpx.Response(
        status_code=200,
        text="".join(f"{frame}\n\n" for frame in [*frames, "data: [DONE]"]),
        headers={"Content-Type": "text/event-stream"},
    )


def _tool_call_frames(fragments: list[dict[str, object]]) -> list[str]:
    """Build SSE frames carrying ``tool_calls`` deltas."""

    return [
        "data: " + json.dumps({"choices": [{"index": 0, "delta": {"tool_calls": [fragment]}}]})
        for fragment in fragments
    ]


class TestStream:
    def test_reassembles_text(self) -> None:
        handler = mock_transport({"/chat/completions": sse_body(["Hello", ", ", "world"])})
        chunks = list(_provider(handler).stream(_request()))
        assert "".join(chunk.delta for chunk in chunks) == "Hello, world"
        assert chunks[-1].finish_reason == "stop"

    def test_malformed_frames_are_skipped(self) -> None:
        body = _sse(
            [
                'data: {"choices":[{"delta":{"content":"a"}}]}',
                "data: {not json",
                'data: {"choices":[{"delta":{"content":"b"}}]}',
            ]
        )
        chunks = list(_provider(mock_transport({"/chat/completions": body})).stream(_request()))
        assert "".join(chunk.delta for chunk in chunks) == "ab"

    def test_comment_and_blank_lines_are_ignored(self) -> None:
        body = _sse(
            [
                ": keep-alive",
                "",
                'data: {"choices":[{"delta":{"content":"a"}}]}',
                "data:",
            ]
        )
        chunks = list(_provider(mock_transport({"/chat/completions": body})).stream(_request()))
        assert "".join(chunk.delta for chunk in chunks) == "a"

    def test_tool_call_fragments_are_accumulated(self) -> None:
        body = _sse(
            _tool_call_frames(
                [
                    {"index": 0, "id": "c1", "function": {"name": "read_file", "arguments": ""}},
                    {"index": 0, "function": {"arguments": '{"path":'}},
                    {"index": 0, "function": {"arguments": ' "a.py"}'}},
                ]
            )
        )
        handler = mock_transport({"/chat/completions": body})
        calls = [
            call for chunk in _provider(handler).stream(_request()) for call in chunk.tool_calls
        ]
        assert len(calls) == 1
        assert calls[0].name == "read_file"
        assert calls[0].arguments == {"path": "a.py"}
        assert calls[0].id == "c1"

    def test_two_parallel_tool_calls_are_kept_separate(self) -> None:
        body = _sse(
            _tool_call_frames(
                [
                    {
                        "index": 0,
                        "id": "a",
                        "function": {"name": "list_directory", "arguments": "{}"},
                    },
                    {
                        "index": 1,
                        "id": "b",
                        "function": {"name": "read_file", "arguments": '{"path": "x"}'},
                    },
                ]
            )
        )
        handler = mock_transport({"/chat/completions": body})
        calls = [
            call for chunk in _provider(handler).stream(_request()) for call in chunk.tool_calls
        ]
        assert {call.name for call in calls} == {"list_directory", "read_file"}
        assert {call.id for call in calls} == {"a", "b"}

    def test_error_status_is_mapped(self) -> None:
        body = httpx.Response(status_code=401, json={"error": {"message": "no"}})
        handler = mock_transport({"/chat/completions": body})
        with pytest.raises(ModelUnavailableError):
            list(_provider(handler).stream(_request()))

    def test_timeout_is_mapped(self) -> None:
        handler = mock_transport({"/chat/completions": TIMEOUT_ERROR})
        with pytest.raises(LLMTimeoutError):
            list(_provider(handler).stream(_request()))

    def test_streaming_sends_the_stream_flag(self) -> None:
        handler = mock_transport({"/chat/completions": sse_body(["x"])})
        list(_provider(handler).stream(_request()))
        sent = json.loads(handler.requests[0].content)
        assert sent["stream"] is True


class TestModelDiscovery:
    def test_lists_models(self) -> None:
        body = httpx.Response(status_code=200, json={"data": [{"id": "b"}, {"id": "a"}]})
        assert _provider(mock_transport({"/models": body})).list_models() == ["a", "b"]

    def test_unsupported_endpoint_returns_empty(self) -> None:
        assert (
            _provider(mock_transport({"/models": httpx.Response(status_code=404)})).list_models()
            == []
        )

    def test_unexpected_shape_returns_empty(self) -> None:
        body = httpx.Response(status_code=200, json={"unexpected": True})
        assert _provider(mock_transport({"/models": body})).list_models() == []

    def test_accepts_plain_string_ids(self) -> None:
        body = httpx.Response(status_code=200, json={"data": ["m1", "m1", "m2"]})
        assert _provider(mock_transport({"/models": body})).list_models() == ["m1", "m2"]
