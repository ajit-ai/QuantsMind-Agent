"""Test doubles for the model gateway.

``FakeModelProvider`` implements the full
:class:`~quantsmind_agent.llm.ModelProvider` contract from a scripted list of
responses, so the Agent Core, tool system and GUI can be exercised without any
network access or credentials.

These fakes are for tests only. They are deliberately *not* reachable from the
application code: ``providers.build_provider`` never returns one.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx

from quantsmind_agent.errors import LLMTimeoutError, ModelUnavailableError, NetworkError
from quantsmind_agent.llm import ChatRequest, ChatResponse, StreamChunk
from quantsmind_agent.schema import ToolCall

__all__ = [
    "FakeModelProvider",
    "canned_body",
    "sse_body",
    "text_response",
    "tool_response",
]


def text_response(content: str, model: str = "fake-model") -> ChatResponse:
    """A plain final-answer response."""

    return ChatResponse(
        content=content,
        model=model,
        finish_reason="stop",
        prompt_tokens=11,
        completion_tokens=7,
    )


def tool_response(
    calls: list[tuple[str, dict[str, Any]]],
    *,
    model: str = "fake-model",
    content: str = "",
) -> ChatResponse:
    """A response that requests tool calls."""

    return ChatResponse(
        content=content,
        model=model,
        tool_calls=[
            ToolCall(id=f"call_{index}", name=name, arguments=dict(arguments))
            for index, (name, arguments) in enumerate(calls)
        ],
        finish_reason="tool_calls",
        prompt_tokens=5,
        completion_tokens=3,
    )


class FakeModelProvider:
    """Scripted provider: yields one response per :meth:`complete` call.

    Implements :class:`~quantsmind_agent.llm.ModelProvider` structurally. It
    deliberately does not subclass the Protocol: the gateway relies on duck
    typing, and inheriting would couple the fake to the production interface.
    """

    name = "fake"

    def __init__(
        self,
        responses: list[ChatResponse] | None = None,
        *,
        stream_chunks: list[list[str]] | None = None,
        base_url: str = "https://fake.invalid/v1",
    ) -> None:
        self._responses = list(responses or [text_response("ok")])
        self._stream_chunks = stream_chunks
        self.base_url = base_url
        self.requests: list[ChatRequest] = []
        self.stream_requests: list[ChatRequest] = []

    # -- scripting helpers -------------------------------------------------
    def script(self, *responses: ChatResponse) -> FakeModelProvider:
        """Replace the script and return ``self`` for chaining."""

        self._responses = list(responses)
        return self

    def queue(self, response: ChatResponse) -> FakeModelProvider:
        self._responses.append(response)
        return self

    # -- ModelProvider -----------------------------------------------------
    def list_models(self) -> list[str]:
        return ["fake-model", "fake-model-mini"]

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if not self._responses:
            return text_response("(no scripted response)")
        response = self._responses.pop(0)
        if response.model == "":
            response.model = request.model
        return response

    def stream(self, request: ChatRequest) -> Iterator[StreamChunk]:
        self.stream_requests.append(request)
        if self._stream_chunks is not None:
            index = min(len(self.stream_requests) - 1, len(self._stream_chunks) - 1)
            for piece in self._stream_chunks[index]:
                yield StreamChunk(delta=piece)
            yield StreamChunk(finish_reason="stop")
            return
        response = self.complete(request)
        if response.wants_tools:
            yield StreamChunk(tool_calls=list(response.tool_calls), finish_reason="tool_calls")
            return
        words = response.content.split(" ")
        for index, word in enumerate(words):
            piece = word if index == 0 else f" {word}"
            yield StreamChunk(delta=piece)
        yield StreamChunk(finish_reason="stop")


# --------------------------------------------------------------------------
# httpx transport doubles for provider-level tests
# --------------------------------------------------------------------------
def canned_body(
    *,
    content: str = "hello",
    tool_calls: list[dict[str, Any]] | None = None,
    model: str = "test-model",
    finish_reason: str = "stop",
    status_code: int = 200,
) -> httpx.Response:
    """Build a realistic OpenAI chat-completion response."""

    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
        message["content"] = content or None
    body = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
    }
    return httpx.Response(status_code=status_code, json=body)


def sse_body(chunks: list[str], *, finish_reason: str = "stop") -> httpx.Response:
    """Build an SSE response that streams ``chunks`` then ``[DONE]``."""

    frames = [
        f"data: {json.dumps({'choices': [{'index': 0, 'delta': {'content': piece}}]})}"
        for piece in chunks
    ]
    frames.append(
        "data: "
        + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]})
    )
    frames.append("data: [DONE]")
    return httpx.Response(
        status_code=200,
        text="".join(f"{frame}\n\n" for frame in frames),
        headers={"Content-Type": "text/event-stream"},
    )


class _Handler(httpx.BaseTransport):
    """Routes requests to canned responses keyed by URL suffix."""

    def __init__(self, responses: dict[str, httpx.Response | Exception]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for suffix, response in self.responses.items():
            if request.url.path.endswith(suffix):
                if isinstance(response, Exception):
                    raise response
                return response
        return httpx.Response(status_code=404, json={"error": {"message": "no route"}})


def mock_transport(responses: dict[str, httpx.Response | Exception]) -> _Handler:
    """Build a transport that never touches the network."""

    return _Handler(responses)


#: Errors used by provider error-path tests.
TIMEOUT_ERROR = httpx.ReadTimeout("timed out")
CONNECT_ERROR = httpx.ConnectError("connection refused")


__all__ += [
    "CONNECT_ERROR",
    "TIMEOUT_ERROR",
    "mock_transport",
]


def assert_no_secret_in(captured: str, secret: str) -> None:
    """Test helper: fail if ``secret`` appears anywhere in ``captured``."""

    assert secret not in captured, "credential leaked into observable output"


def timeout_error() -> LLMTimeoutError:
    return LLMTimeoutError("timed out")


def unavailable_error() -> ModelUnavailableError:
    return ModelUnavailableError("unavailable")


def network_error() -> NetworkError:
    return NetworkError("offline")
