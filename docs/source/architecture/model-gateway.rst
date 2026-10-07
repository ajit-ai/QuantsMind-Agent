.. _architecture-model-gateway:


=============
Model Gateway
=============


The gateway is the boundary between "the agent needs a completion" and "some
vendor serves completions". The Agent Core only sees the DTOs in
:mod:`quantsmind_agent.llm`.

Interface
=========

.. code-block:: python

    class ModelProvider(Protocol):
        name: str

        def list_models(self) -> list[str]: ...
        def complete(self, request: ChatRequest) -> ChatResponse: ...
        def stream(self, request: ChatRequest) -> Iterator[StreamChunk]: ...

.. code-block:: python

    class ModelGateway:
        def complete(self, request: ChatRequest) -> ChatResponse: ...
        def stream(self, request: ChatRequest) -> Iterator[StreamChunk]: ...
        def stream_to_response(self, request: ChatRequest) -> ChatResponse: ...
        def list_models(self) -> list[str]: ...
        def require_api_key(self) -> str: ...
        def validate_configuration(self) -> None: ...

DTOs
====

.. code-block:: python

    @dataclass
    class ChatRequest:
        messages: list[Message]
        model: str
        temperature: float = 0.2
        max_output_tokens: int = 4096
        tools: list[dict] = []
        timeout_seconds: float = 120.0
        stop: list[str] = []

    @dataclass
    class ChatResponse:
        content: str
        model: str
        tool_calls: list[ToolCall] = []
        finish_reason: str | None = None
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        latency_ms: float = 0.0

Adding a provider therefore means implementing three methods and registering a
factory -- no Agent Core change. See :doc:`../developer/extension-points`.

Error normalisation
===================

Every provider failure is mapped onto the ``LLMError`` family so the GUI can
show one actionable message per situation:

==========================  ===============================================
Condition                   Exception / user message
==========================  ===============================================
No credential               ``MissingAPIKeyError`` -- "check Settings"
HTTP 401/403                ``ModelUnavailableError`` -- "rejected the key"
HTTP 404                    ``ModelUnavailableError`` -- "model/endpoint"
HTTP 429                    ``ModelUnavailableError`` -- "rate limited"
Other HTTP error            ``ModelUnavailableError`` -- include status
``httpx.TimeoutException``  ``LLMTimeoutError`` -- "took too long"
Other transport failure     ``NetworkError`` -- "could not be reached"
Non-JSON or empty response  ``LLMProtocolError`` -- "unexpected response"
==========================  ===============================================

Messages are written for a user, not a log reader: they say what to do next.
Detail goes to the log, never to the transcript. No error message ever contains
a credential.

Events
======

Each call emits ``model_request`` before and ``model_response`` after, carrying
provider, model, whether the call streamed, token counts and latency. The
response event on failure carries the exception *type name* only, so an event
consumer cannot leak a secret through a payload.

The v0.1 provider
=================

:class:`~quantsmind_agent.providers.openai_compat.OpenAICompatProvider`
implements the OpenAI ``/chat/completions`` contract:

.. code-block:: text

    POST {base_url}/chat/completions      non-streamed
    POST {base_url}/chat/completions      streamed (stream: true, SSE)
    GET  {base_url}/models                 best-effort discovery

Because the contract is OpenAI's, one class serves OpenAI, OpenRouter, Groq,
Together, Fireworks, DeepSeek, vLLM, Ollama, LM Studio and llama.cpp.

Credentials
-----------

The key is passed as a *callable* and read at request time, so it is never
cached on the provider instance:

.. code-block:: python

    provider = build_provider(settings, api_key=config_manager.resolve_api_key)

It is placed only in the ``Authorization: Bearer`` header. ``list_models()`` and
error paths never include it.

Tool-call streaming
-------------------

OpenAI streams tool calls as fragments keyed by a numeric index:

.. code-block:: json

    {"choices":[{"delta":{"tool_calls":[
       {"index":0,"id":"c1","function":{"name":"read_file","arguments":""}}]}}]}

The provider accumulates fragments per index and emits a complete ``ToolCall``
once the accumulated arguments parse as JSON. An empty argument string means
"nothing sent yet", not "no arguments" -- conflating the two would execute a tool
with empty arguments.

Anything the stream leaves open is flushed at the end, so a truncated call is
still surfaced rather than silently dropped.

Best-effort discovery
---------------------

``list_models()`` returns ``[]`` instead of raising when the endpoint does not
support ``/models`` or rejects the request. Discovery is a convenience, not a
requirement.

Related
=======

* :doc:`adr/0002-openai-compatible-provider`
* :doc:`../developer/extension-points`
* :doc:`../user/settings`
