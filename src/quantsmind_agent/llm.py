"""Provider-independent model gateway.

The Agent Core only ever sees :class:`ChatRequest`, :class:`ChatResponse` and
:class:`ModelGateway`. Concrete providers are registered by name, so adding
Anthropic/Bedrock/Vertex later means implementing :class:`ModelProvider` and
registering it -- no Agent Core change.

v0.1 ships one provider,
:class:`~quantsmind_agent.providers.openai_compat.OpenAICompatProvider`, which
speaks the OpenAI ``/chat/completions`` API (streaming and non-streaming) and
therefore also covers OpenRouter, Groq, Together, vLLM, Ollama, LM Studio and
llama.cpp.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from quantsmind_agent.errors import LLMConfigurationError, LLMError, MissingAPIKeyError
from quantsmind_agent.events import AgentEventType, EventBus
from quantsmind_agent.logging_setup import get_logger
from quantsmind_agent.schema import AssistantMessage, Message, ToolCall

__all__ = [
    "ChatRequest",
    "ChatResponse",
    "ModelGateway",
    "ModelInfo",
    "ModelProvider",
    "StreamChunk",
]

logger = get_logger(__name__)


@dataclass(slots=True)
class StreamChunk:
    """One increment of a streamed completion."""

    delta: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.delta and not self.tool_calls and self.finish_reason is None


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """Describes a configured model endpoint (never includes credentials)."""

    provider: str
    model: str
    base_url: str = ""
    supports_tools: bool = True
    supports_streaming: bool = True
    context_window: int | None = None

    def describe(self) -> str:
        return f"{self.provider}:{self.model}"


@dataclass(slots=True)
class ChatRequest:
    """A provider-agnostic completion request."""

    messages: list[Message]
    model: str
    temperature: float = 0.2
    max_output_tokens: int = 4096
    tools: list[dict[str, Any]] = field(default_factory=list)
    timeout_seconds: float = 120.0
    stop: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def supports_tools(self) -> bool:
        return bool(self.tools)

    def describe(self) -> str:
        return f"{self.model} ({len(self.messages)} messages, {len(self.tools)} tools)"


@dataclass(slots=True)
class ChatResponse:
    """A provider-agnostic completion result."""

    content: str = ""
    model: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)

    @property
    def total_tokens(self) -> int | None:
        if self.prompt_tokens is None and self.completion_tokens is None:
            return None
        return (self.prompt_tokens or 0) + (self.completion_tokens or 0)

    def as_assistant_message(self) -> AssistantMessage:
        return AssistantMessage(
            content=self.content,
            tool_calls=list(self.tool_calls),
            model=self.model,
            finish_reason=self.finish_reason,
            total_tokens=self.total_tokens,
        )

    def describe(self) -> str:
        tokens = self.total_tokens
        token_text = f", {tokens} tokens" if tokens else ""
        return f"{self.model}: {len(self.content)} chars{token_text} in {self.latency_ms:.0f} ms"


@runtime_checkable
class ModelProvider(Protocol):
    """The contract every LLM backend implements."""

    name: str

    def list_models(self) -> list[str]:
        """Return the model identifiers the endpoint exposes (best effort)."""

    def complete(self, request: ChatRequest) -> ChatResponse:
        """Return a complete (non-streamed) response."""

    def stream(self, request: ChatRequest) -> Iterator[StreamChunk]:
        """Yield response increments until the model finishes."""


class ModelGateway:
    """Single entry point the Agent Core uses to talk to any provider.

    Responsibilities:

    * hold the active provider and its configuration,
    * require a credential before any request leaves the machine,
    * translate provider/HTTP failures into the ``LLMError`` family,
    * emit :class:`~quantsmind_agent.events.AgentEventType` events, and
    * guarantee that no credential is ever logged.
    """

    def __init__(
        self,
        provider: ModelProvider,
        *,
        api_key: str | None = None,
        event_bus: EventBus | None = None,
        session_id: str = "",
        default_timeout: float = 120.0,
    ) -> None:
        self._provider = provider
        self._api_key = api_key or None
        self._events = event_bus or EventBus()
        self._session_id = session_id
        self._default_timeout = default_timeout

    # -- configuration ---------------------------------------------------
    @property
    def provider(self) -> ModelProvider:
        return self._provider

    @property
    def provider_name(self) -> str:
        return self._provider.name

    @property
    def event_bus(self) -> EventBus:
        """The bus this gateway publishes to (shared with the Agent Core)."""

        return self._events

    @property
    def has_api_key(self) -> bool:
        return bool(self._api_key)

    def set_api_key(self, api_key: str | None) -> None:
        self._api_key = api_key or None

    def set_provider(self, provider: ModelProvider) -> None:
        self._provider = provider

    def list_models(self) -> list[str]:
        return self._provider.list_models()

    def describe(self, model: str) -> ModelInfo:
        """Best-effort description of the active endpoint."""

        base_url = getattr(self._provider, "base_url", "")
        return ModelInfo(provider=self._provider.name, model=model, base_url=base_url)

    def require_api_key(self) -> str:
        """Return the credential or raise :class:`MissingAPIKeyError`."""

        if not self._api_key:
            raise MissingAPIKeyError(self._provider.name)
        return self._api_key

    def _emit(self, event_type: AgentEventType, payload: dict[str, Any]) -> None:
        self._events.emit(event_type, payload, session_id=self._session_id)

    def _translate(self, exc: BaseException) -> LLMError:
        """Map an arbitrary provider failure onto the ``LLMError`` family."""

        if isinstance(exc, LLMError):
            return exc
        message = str(exc) or exc.__class__.__name__
        return LLMError(
            f"The model request failed: {message}",
            user_message=(
                "The request to the language model failed. "
                "Check the provider settings and your network connection, then try again."
            ),
            details={"provider": self._provider.name, "error_type": type(exc).__name__},
            cause=exc,
        )

    # -- inference --------------------------------------------------------
    def complete(self, request: ChatRequest) -> ChatResponse:
        """Perform one non-streamed completion."""

        request.timeout_seconds = request.timeout_seconds or self._default_timeout
        self._emit(
            AgentEventType.MODEL_REQUEST,
            {
                "provider": self._provider.name,
                "model": request.model,
                "message_count": len(request.messages),
                "tool_count": len(request.tools),
            },
        )
        started = time.perf_counter()
        try:
            response = self._provider.complete(request)
        except Exception as exc:
            self._emit(
                AgentEventType.MODEL_RESPONSE,
                {
                    "provider": self._provider.name,
                    "model": request.model,
                    "error": type(exc).__name__,
                },
            )
            raise self._translate(exc) from exc

        response.latency_ms = (time.perf_counter() - started) * 1000
        self._emit(
            AgentEventType.MODEL_RESPONSE,
            {
                "provider": self._provider.name,
                "model": response.model or request.model,
                "content_chars": len(response.content),
                "tool_calls": [call.name for call in response.tool_calls],
                "finish_reason": response.finish_reason,
                "latency_ms": round(response.latency_ms, 1),
                "total_tokens": response.total_tokens,
            },
        )
        return response

    def stream(self, request: ChatRequest) -> Iterator[StreamChunk]:
        """Yield :class:`StreamChunk` objects for a streamed completion."""

        request.timeout_seconds = request.timeout_seconds or self._default_timeout
        self._emit(
            AgentEventType.MODEL_REQUEST,
            {
                "provider": self._provider.name,
                "model": request.model,
                "stream": True,
                "message_count": len(request.messages),
            },
        )
        started = time.perf_counter()
        chunks = 0
        try:
            for chunk in self._provider.stream(request):
                chunks += 1
                if not chunk.is_empty:
                    yield chunk
        except Exception as exc:
            self._emit(
                AgentEventType.MODEL_RESPONSE,
                {
                    "provider": self._provider.name,
                    "model": request.model,
                    "stream": True,
                    "error": type(exc).__name__,
                    "chunks": chunks,
                },
            )
            raise self._translate(exc) from exc

        self._emit(
            AgentEventType.MODEL_RESPONSE,
            {
                "provider": self._provider.name,
                "model": request.model,
                "stream": True,
                "chunks": chunks,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )

    def stream_to_response(self, request: ChatRequest) -> ChatResponse:
        """Consume :meth:`stream` and assemble one complete response."""

        parts: list[str] = []
        calls: list[ToolCall] = []
        finish: str | None = None
        for chunk in self.stream(request):
            parts.append(chunk.delta)
            calls.extend(chunk.tool_calls)
            if chunk.finish_reason:
                finish = chunk.finish_reason
        return ChatResponse(
            content="".join(parts),
            model=request.model,
            tool_calls=calls,
            finish_reason=finish,
        )

    def validate_configuration(self) -> None:
        """Fail fast (before a user sends a message) on bad configuration."""

        if not getattr(self._provider, "name", ""):
            raise LLMConfigurationError(
                "No model provider is selected",
                user_message="Select a provider in Settings before sending a message.",
            )
        base_url = getattr(self._provider, "base_url", "")
        if base_url and not str(base_url).startswith(("http://", "https://")):
            raise LLMConfigurationError(
                f"Provider base URL is not an HTTP(S) URL: {base_url!r}",
                user_message=(
                    "The provider base URL is invalid. It must start with http:// or https://."
                ),
                details={"base_url": str(base_url)},
            )
