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

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from quantsmind_agent.diagnostics.logging import get_logger
from quantsmind_agent.errors import LLMConfigurationError, LLMError, MissingAPIKeyError
from quantsmind_agent.events import AgentEventType, EventBus
from quantsmind_agent.schema import AssistantMessage, Message, ToolCall

__all__ = [
    "ChatRequest",
    "ChatResponse",
    "ModelGateway",
    "ModelInfo",
    "ModelProvider",
    "ProviderCapabilities",
    "StreamChunk",
    "StructuredOutput",
    "StructuredOutputError",
    "parse_structured",
    "validate_structured",
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
class ProviderCapabilities:
    """What a provider actually supports, declared rather than assumed.

    These fields existed before as booleans on :class:`ModelInfo` and nothing
    ever read them, which is worse than not having them: they said "tools are
    supported" to any code that cared to ask, and no code asked, so an endpoint
    that silently ignored tool schemas looked identical to one that honoured them.

    Every field is optional in the sense that ``None`` means "not declared" --
    which is reported as unknown rather than as yes. Callers decide what to do
    about an unknown capability; the application never silently assumes the
    optimistic answer.
    """

    tools: bool | None = True
    streaming: bool | None = True
    structured_output: bool | None = None
    context_window: int | None = None
    cancellation: bool | None = False

    def supports(self, name: str) -> bool:
        """Whether ``name`` is declared *and* supported.

        An undeclared capability answers ``False``: the application then uses a
        supported alternative or explains the limitation, which is the behaviour
        the specification asks for. Answering ``True`` for an unknown capability
        would be the silent weakening it is trying to prevent.
        """

        return bool(getattr(self, name, None))

    def describe(self) -> str:
        """A one-line, human-readable capability summary for diagnostics."""

        parts = [
            f"{name}={'yes' if value is True else 'no' if value is False else 'unknown'}"
            for name, value in (
                ("tools", self.tools),
                ("streaming", self.streaming),
                ("structured_output", self.structured_output),
                ("cancellation", self.cancellation),
            )
        ]
        window = f", context_window={self.context_window}" if self.context_window else ""
        return "; ".join(parts) + window


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """Describes a configured model endpoint (never includes credentials)."""

    provider: str
    model: str
    base_url: str = ""
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)

    @property
    def supports_tools(self) -> bool:
        return self.capabilities.supports("tools")

    @property
    def supports_streaming(self) -> bool:
        return self.capabilities.supports("streaming")

    @property
    def context_window(self) -> int | None:
        return self.capabilities.context_window

    def describe(self) -> str:
        return f"{self.provider}:{self.model} ({self.capabilities.describe()})"


class StructuredOutput:
    """A request for JSON-shaped output, and the schema it should satisfy.

    Two things are requested and must be kept apart, because they carry
    different guarantees:

    ``schema``   what the data should look like. The application validates
                 against this itself, every time, whichever path produced it.
    ``native``   whether the provider's own constrained-output mode should be
                 used. When it is unavailable the request still succeeds, but the
                 answer is *parsed*, not constrained -- so the response records
                 which guarantee was obtained. Presenting a parsed answer as a
                 constrained one would overstate what was enforced.
    """

    __slots__ = ("native", "schema")

    def __init__(self, schema: dict[str, Any], *, native: bool = True) -> None:
        self.schema = schema
        self.native = native

    def describe(self) -> str:
        return f"structured output ({'native' if self.native else 'parsed'})"


class StructuredOutputError(LLMError):
    """Structured output that arrived malformed, truncated, or schema-invalid.

    A dedicated type because it is never a transport problem: the provider
    answered, and the answer cannot be used. Callers must not fall back to
    treating the same content as prose.
    """

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message,
            user_message=(
                "The model returned a structured response that could not be read. "
                "Try again, or use a model with structured-output support."
            ),
            details=details or {},
        )


#: The JSON Schema subset this application enforces itself.
#:
#: Deliberately small. A dependency such as ``jsonschema`` would validate more,
#: but the guarantee that matters here is not breadth -- it is that the
#: application can always re-check what it received without trusting the provider
#: to have constrained it.
_SUPPORTED_SCHEMA_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}


def validate_structured(value: Any, schema: dict[str, Any], *, path: str = "$") -> list[str]:
    """Return the list of ways ``value`` fails ``schema``; empty means it fits.

    Returns problems rather than raising, so the caller can report all of them at
    once instead of making the model guess which field was wrong.
    """

    problems: list[str] = []
    if not schema:
        return problems
    expected = schema.get("type")
    if isinstance(expected, str) and expected in _SUPPORTED_SCHEMA_TYPES:
        allowed_types: tuple[type, ...] = {
            "object": (dict,),
            "array": (list,),
            "string": (str,),
            "number": (int, float),
            "integer": (int,),
            "boolean": (bool,),
            "null": (type(None),),
        }[expected]
        if expected != "boolean" and isinstance(value, bool) and expected in ("number", "integer"):
            return [f"{path}: expected {expected}, found boolean"]
        if not isinstance(value, allowed_types):
            return [f"{path}: expected {expected}, found {type(value).__name__}"]
    allowed = schema.get("enum")
    if isinstance(allowed, list) and value not in allowed:
        problems.append(f"{path}: {value!r} is not one of the permitted values")
    if isinstance(value, dict):
        for name in schema.get("required") or []:
            if name not in value:
                problems.append(f"{path}.{name}: required but missing")
        properties = schema.get("properties") or {}
        for name, subschema in properties.items():
            if name in value and isinstance(subschema, dict):
                problems.extend(validate_structured(value[name], subschema, path=f"{path}.{name}"))
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            problems.extend(validate_structured(item, schema["items"], path=f"{path}[{index}]"))
    return problems


def parse_structured(content: str, schema: dict[str, Any]) -> Any:
    """Parse and validate a JSON answer, or raise :class:`StructuredOutputError`.

    Handles the fenced ```json blocks models emit in practice; anything else that
    is not JSON is a failure, because guessing where the JSON starts inside a
    paragraph is how a plausible answer gets accepted from a wrong one.
    """

    text = (content or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StructuredOutputError(
            "The model did not return JSON",
            details={"reason": str(exc), "length": len(text)},
        ) from exc
    problems = validate_structured(value, schema)
    if problems:
        raise StructuredOutputError(
            "The model's JSON did not match the requested shape",
            details={"problems": problems[:10]},
        )
    return value


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
    #: Request JSON-shaped output, or ``None`` for ordinary text.
    structured: StructuredOutput | None = None

    @property
    def supports_tools(self) -> bool:
        return bool(self.tools)

    def describe(self) -> str:
        extra = f", {self.structured.describe()}" if self.structured is not None else ""
        return f"{self.model} ({len(self.messages)} messages, {len(self.tools)} tools{extra})"


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
    #: The parsed structured payload, when the request asked for one.
    structured: Any = None
    #: ``"native"`` when the provider constrained the output itself, ``"parsed"``
    #: when the application extracted it from prose. The two carry different
    #: guarantees and must never be reported as the same thing.
    structured_mode: str = ""

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
    """The contract every LLM backend implements.

    ``capabilities`` is optional on the wire but not in practice: an adapter
    that does not declare one is treated as declaring nothing, so the gateway
    reports unknown rather than assuming support.
    """

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
        """Describe the active endpoint, including what it can actually do.

        The capabilities come from the adapter rather than from this class's
        defaults, because the whole point is to stop guessing: an endpoint that
        cannot stream should be found out here, not by watching a stream fail.
        """

        declared = getattr(self._provider, "capabilities", None)
        if declared is None:
            capabilities = ProviderCapabilities(
                tools=None, streaming=None, structured_output=None, cancellation=None
            )
        elif isinstance(declared, ProviderCapabilities):
            capabilities = declared
        else:  # a provider that reports the old boolean trio
            capabilities = ProviderCapabilities(
                tools=bool(getattr(declared, "supports_tools", None)),
                streaming=bool(getattr(declared, "supports_streaming", None)),
                context_window=getattr(declared, "context_window", None),
            )
        return ModelInfo(
            provider=self._provider.name,
            model=model,
            base_url=getattr(self._provider, "base_url", ""),
            capabilities=capabilities,
        )

    def capabilities(self, model: str = "") -> ProviderCapabilities:
        """What the active endpoint supports, for callers that must branch."""

        return self.describe(model).capabilities

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
        """Perform one non-streamed completion.

        Structured output is gated here rather than in the adapter, so an endpoint
        that cannot do it is refused with an explanation instead of quietly
        returning prose that the caller would have to guess the shape of.
        """

        request.timeout_seconds = request.timeout_seconds or self._default_timeout
        if request.structured is not None and not self.capabilities(request.model).supports(
            "structured_output"
        ):
            raise StructuredOutputError(
                f"{self._provider.name} does not support structured output",
                details={"provider": self._provider.name, "capability": "structured_output"},
            )
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
