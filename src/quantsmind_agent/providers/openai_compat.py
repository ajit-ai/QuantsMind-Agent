"""OpenAI-compatible model provider.

Implements the OpenAI ``/chat/completions`` contract over ``httpx``:

* ``POST {base_url}/chat/completions`` for non-streamed completions,
* the same endpoint with ``stream: true`` for SSE streaming,
* ``GET {base_url}/models`` for best-effort model discovery.

Because the contract is OpenAI's, the same class serves OpenAI, OpenRouter,
Groq, Together, Fireworks, DeepSeek, vLLM, Ollama, LM Studio and llama.cpp.

Credential handling: the key is read from a callable and only ever placed in an
``Authorization`` header. It is never logged, never stored on the provider
instance and never included in error messages.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import fields
from typing import Any

import httpx

from quantsmind_agent.diagnostics.logging import get_logger
from quantsmind_agent.errors import (
    LLMConfigurationError,
    LLMProtocolError,
    LLMTimeoutError,
    MissingAPIKeyError,
    ModelUnavailableError,
    NetworkError,
    QuantsMindError,
)
from quantsmind_agent.providers.gateway import (
    ChatRequest,
    ChatResponse,
    ProviderCapabilities,
    StreamChunk,
    parse_structured,
)
from quantsmind_agent.schema import ToolCall

__all__ = ["OpenAICompatProvider"]

logger = get_logger(__name__)

_DONE_SENTINEL = "[DONE]"


class OpenAICompatProvider:
    """A :class:`~quantsmind_agent.providers.gateway.ModelProvider` for OpenAI-style APIs."""

    name = "openai_compatible"

    #: What the *protocol* supports. A particular service behind this protocol
    #: may honour less; ``capabilities`` below narrows that per endpoint, and the
    #: presets in :mod:`quantsmind_agent.app.settings` say by how much.
    base_capabilities = ProviderCapabilities(
        tools=True,
        streaming=True,
        structured_output=True,
        context_window=None,
        cancellation=False,
    )

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | Callable[[], str | None] | None = None,
        organization: str | None = None,
        timeout_seconds: float = 120.0,
        requires_api_key: bool = True,
        capabilities: ProviderCapabilities | Mapping[str, Any] | None = None,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
        follow_redirects: bool = False,
        trust_env: bool = True,
    ) -> None:
        base = str(base_url).strip().rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise LLMConfigurationError(
                f"Base URL must be http(s), got {base_url!r}",
                user_message="The provider base URL must start with http:// or https://.",
                details={"base_url": base},
            )
        self.base_url = base
        self.organization = organization
        self.timeout_seconds = float(timeout_seconds)
        self._requires_api_key = requires_api_key
        self._api_key_source = api_key
        self._transport = transport
        self._client = client
        #: Redirects are refused by default (M3B-S1.1). A redirect would move
        #: the request -- and the prompt and the ``Authorization`` header with
        #: it -- to a host the security policy never authorised.
        self._follow_redirects = bool(follow_redirects)
        #: Whether environment variables (proxy settings, ``.netrc``) may shape
        #: the connection. The authentication gateway turns this off for a
        #: credential-bearing route so a proxy variable cannot silently become
        #: the destination of a request that carries an API key.
        self._trust_env = bool(trust_env)
        self.capabilities = self._resolve_capabilities(capabilities)

    @classmethod
    def _resolve_capabilities(
        cls, overrides: ProviderCapabilities | Mapping[str, Any] | None
    ) -> ProviderCapabilities:
        """Merge an endpoint's declared capabilities over the protocol's own.

        Only keys the endpoint actually declares are overridden, so a preset that
        is silent about streaming adds no opinion rather than removing streaming.
        An explicit ``None`` in the override means "unknown", which is different
        from silence and is preserved as such.
        """

        if overrides is None:
            return cls.base_capabilities
        if isinstance(overrides, ProviderCapabilities):
            return overrides
        known = {item.name for item in fields(ProviderCapabilities)}
        unknown = set(overrides) - known
        if unknown:
            raise LLMConfigurationError(
                f"Unknown capability names: {', '.join(sorted(unknown))}",
                user_message=(
                    "The provider configuration names capabilities that do not exist: "
                    f"{', '.join(sorted(unknown))}."
                ),
                details={"unknown": sorted(unknown), "known": sorted(known)},
            )
        merged = {
            item.name: getattr(cls.base_capabilities, item.name)
            for item in fields(cls.base_capabilities)
        }
        merged.update(overrides)
        return ProviderCapabilities(**merged)

    # -- credential -------------------------------------------------------
    def _resolve_api_key(self) -> str | None:
        source = self._api_key_source
        if callable(source):
            return source() or None
        return source or None

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "QuantsMind-Agent/0.1",
        }
        key = self._resolve_api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        elif self._requires_api_key:
            raise MissingAPIKeyError(self.name)
        if self.organization:
            headers["OpenAI-Organization"] = self.organization
        return headers

    # -- transport --------------------------------------------------------
    def _http(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        return httpx.Client(
            timeout=httpx.Timeout(self.timeout_seconds, connect=15.0),
            transport=self._transport,
            follow_redirects=self._follow_redirects,
            trust_env=self._trust_env,
            # Certificate validation is never turned off. It is stated here so
            # that the next person to touch this constructor has to delete the
            # line rather than not notice it.
            verify=True,
        )

    def _refuse_redirect(self, response: httpx.Response) -> None:
        """Report a redirect as an error instead of following it (M3B-S1.1)."""

        if response.status_code not in (301, 302, 303, 307, 308):
            return
        location = response.headers.get("location", "")
        raise LLMProtocolError(
            "The provider redirected the request",
            user_message=(
                "The provider redirected the request to another address. The application "
                "does not follow redirects for authenticated requests, so no API key was "
                "sent to the new address. Check the provider address in Settings."
            ),
            details={
                "status_code": response.status_code,
                "location_host": location.split("/")[2] if "://" in location else "",
            },
        )

    def _close_http(self, client: httpx.Client) -> None:
        if self._client is None:
            client.close()

    @staticmethod
    def _check_status(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        status = response.status_code
        try:
            body = response.json()
        except ValueError:
            body = {"error": {"message": response.text[:400]}}

        detail: Any = body
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            detail = body["error"]
        message = ""
        if isinstance(detail, dict):
            message = str(detail.get("message") or detail.get("detail") or detail)
        message = message or f"HTTP {status}"
        details: dict[str, object] = {"status_code": status}

        if status in (401, 403):
            raise ModelUnavailableError(
                f"Authentication rejected by the provider: {message}",
                user_message=(
                    "The provider rejected the API key. Check the key in Settings and retry."
                ),
                details=details,
            )
        if status == 404:
            raise ModelUnavailableError(
                f"Endpoint or model not found: {message}",
                user_message=(
                    "The provider could not find that model or endpoint. "
                    "Check the model name and base URL in Settings."
                ),
                details=details,
            )
        if status == 429:
            raise ModelUnavailableError(
                f"Provider rate limit reached: {message}",
                user_message=(
                    "The provider is rate limiting requests (HTTP 429). "
                    "Wait a moment or check your plan's quota, then retry."
                ),
                details=details,
            )
        raise ModelUnavailableError(
            f"Provider returned HTTP {status}: {message}",
            user_message=f"The provider returned an error (HTTP {status}). Please retry.",
            details=details,
        )

    @staticmethod
    def _wrap_network_error(exc: Exception, base_url: str, timeout: float) -> Exception:
        if isinstance(exc, httpx.TimeoutException):
            return LLMTimeoutError(
                f"The provider did not respond within {timeout:.0f}s",
                user_message=(
                    "The language model took too long to respond. "
                    "Increase the timeout in Settings or retry."
                ),
                details={"timeout_seconds": timeout},
                cause=exc,
            )
        return NetworkError(
            f"Could not reach the provider at {base_url}: {type(exc).__name__}",
            user_message=(
                "The language model provider could not be reached. "
                "Check your internet connection and the base URL in Settings."
            ),
            details={"base_url": base_url},
            cause=exc,
        )

    # -- request building -------------------------------------------------
    @staticmethod
    def build_payload(request: ChatRequest, *, stream: bool) -> dict[str, Any]:
        """Translate a :class:`ChatRequest` into the OpenAI JSON body."""

        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [message.to_wire() for message in request.messages],
            "temperature": request.temperature,
            "stream": stream,
        }
        if request.max_output_tokens:
            payload["max_tokens"] = request.max_output_tokens
        if request.tools:
            payload["tools"] = request.tools
            payload["tool_choice"] = "auto"
        if request.stop:
            payload["stop"] = request.stop
        if request.structured is not None:
            # The native constrained-output form is only requested when the
            # endpoint has declared it. Otherwise the request still goes out and
            # the answer is parsed and validated by the application, which the
            # response records as "parsed" rather than "native".
            payload["response_format"] = (
                {
                    "type": "json_schema",
                    "json_schema": {"name": "response", "schema": request.structured.schema},
                }
                if request.structured.native
                else {"type": "json_object"}
            )
        return payload

    # -- ModelProvider ----------------------------------------------------
    def list_models(self) -> list[str]:
        """Best-effort model discovery; empty list when unsupported."""

        client = self._http()
        try:
            try:
                response = client.get(f"{self.base_url}/models", headers=self._headers())
                self._refuse_redirect(response)
                self._check_status(response)
                body = response.json()
            except QuantsMindError:
                logger.info("Model discovery was rejected by %s", self.base_url)
                return []
            except Exception:
                logger.info("Model discovery is unavailable on %s", self.base_url)
                return []
        finally:
            self._close_http(client)

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            return []
        names: list[str] = []
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                names.append(item["id"])
            elif isinstance(item, str):
                names.append(item)
        return sorted(set(names))

    def complete(self, request: ChatRequest) -> ChatResponse:
        """Perform a blocking completion."""

        payload = self.build_payload(request, stream=False)
        timeout = request.timeout_seconds or self.timeout_seconds
        client = self._http()
        try:
            try:
                response = client.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                    timeout=timeout,
                )
                self._refuse_redirect(response)
                self._check_status(response)
                try:
                    body = response.json()
                except ValueError as exc:
                    raise LLMProtocolError(
                        "The provider returned a non-JSON response",
                        user_message=(
                            "The provider returned an unexpected response. Verify the base URL "
                            "points at an OpenAI-compatible endpoint such as .../v1."
                        ),
                        details={"status_code": response.status_code},
                        cause=exc,
                    ) from exc
            except QuantsMindError:
                raise
            except Exception as exc:
                raise self._wrap_network_error(exc, self.base_url, timeout) from exc
        finally:
            self._close_http(client)

        return self.parse_completion(body, fallback_model=request.model, request=request)

    def stream(self, request: ChatRequest) -> Iterator[StreamChunk]:
        """Yield response increments from an SSE stream."""

        payload = self.build_payload(request, stream=True)
        timeout = request.timeout_seconds or self.timeout_seconds
        client = self._http()
        try:
            try:
                with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                    timeout=timeout,
                ) as response:
                    self._refuse_redirect(response)
                    self._check_status(response)
                    yield from self._iter_sse(response)
            except QuantsMindError:
                raise
            except Exception as exc:
                raise self._wrap_network_error(exc, self.base_url, timeout) from exc
        finally:
            self._close_http(client)

    # -- parsing ----------------------------------------------------------
    @staticmethod
    def parse_completion(
        body: Any, *, fallback_model: str = "", request: ChatRequest | None = None
    ) -> ChatResponse:
        """Convert an OpenAI chat-completion body into a :class:`ChatResponse`."""

        if not isinstance(body, dict):
            raise LLMProtocolError(
                "Provider returned a JSON value that is not an object",
                user_message=(
                    "The provider returned an unexpected response. "
                    "Check that the base URL points at an OpenAI-compatible endpoint."
                ),
            )
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMProtocolError(
                "Provider response contains no choices",
                user_message=(
                    "The model returned an empty response. "
                    "Retry, or verify the model name in Settings."
                ),
                details={"keys": sorted(str(key) for key in body)},
            )
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message")
        if not isinstance(message, dict):
            raise LLMProtocolError(
                "Provider response choice has no message object",
                user_message="The model returned an unexpected response format. Please retry.",
            )

        content = message.get("content")
        tool_calls = OpenAICompatProvider._parse_tool_calls(message.get("tool_calls"))
        raw_usage = body.get("usage")
        usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}

        text = content if isinstance(content, str) else ""
        structured: Any = None
        structured_mode = ""
        wanted = getattr(request, "structured", None)
        if wanted is not None:
            # Parsed and validated here, whatever the endpoint did or did not
            # enforce. A malformed or off-schema answer raises rather than
            # arriving as an empty result, because "no data" and "bad data" must
            # not look the same to the caller.
            structured = parse_structured(text, wanted.schema)
            structured_mode = "native" if wanted.native else "parsed"

        return ChatResponse(
            content=text,
            model=str(body.get("model") or fallback_model),
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason"),
            prompt_tokens=_as_int(usage.get("prompt_tokens")),
            completion_tokens=_as_int(usage.get("completion_tokens")),
            raw=body,
            structured=structured,
            structured_mode=structured_mode,
        )

    @staticmethod
    def _parse_tool_calls(raw: Any) -> list[ToolCall]:
        if not isinstance(raw, list):
            return []
        calls: list[ToolCall] = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            function = item.get("function")
            function = function if isinstance(function, dict) else {}
            name = str(function.get("name") or "")
            if not name:
                continue
            calls.append(
                ToolCall(
                    id=str(item.get("id") or f"call_{index}"),
                    name=name,
                    arguments=_parse_arguments(function.get("arguments")),
                )
            )
        return calls

    @staticmethod
    def _accumulate_tool_calls(
        deltas: Any, accumulators: dict[int, dict[str, Any]]
    ) -> list[ToolCall]:
        """Fold streamed ``tool_calls`` fragments into complete calls.

        OpenAI sends tool-call arguments in fragments keyed by a numeric index;
        a fragment carrying only ``arguments`` is appended to the slot.
        """

        if not isinstance(deltas, list):
            return []
        completed: list[ToolCall] = []
        for fragment in deltas:
            if not isinstance(fragment, dict):
                continue
            index = fragment.get("index")
            if not isinstance(index, int) or isinstance(index, bool):
                index = len(accumulators)
            slot = accumulators.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if fragment.get("id"):
                slot["id"] = str(fragment["id"])
            function = fragment.get("function")
            function = function if isinstance(function, dict) else {}
            if function.get("name"):
                slot["name"] = str(function["name"])
            if function.get("arguments"):
                slot["arguments"] += str(function["arguments"])
            # A call is only executable once its name is known *and* its
            # arguments have arrived as parseable JSON. An empty argument
            # string means "nothing sent yet", not "no arguments".
            if slot["name"] and slot["arguments"] and _is_complete_json(slot["arguments"]):
                completed.append(
                    ToolCall(
                        id=slot["id"] or f"call_{index}",
                        name=slot["name"],
                        arguments=_parse_arguments(slot["arguments"]),
                    )
                )
                accumulators.pop(index, None)
        return completed

    @staticmethod
    def _iter_sse(response: httpx.Response) -> Iterator[StreamChunk]:
        accumulators: dict[int, dict[str, Any]] = {}
        for line in response.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data:
                continue
            if data == _DONE_SENTINEL:
                break
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                logger.debug("Skipping malformed SSE frame")
                continue
            if not isinstance(event, dict):
                continue

            choices = event.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            choice = choices[0] if isinstance(choices[0], dict) else {}
            delta = choice.get("delta")
            delta = delta if isinstance(delta, dict) else {}

            calls = OpenAICompatProvider._accumulate_tool_calls(
                delta.get("tool_calls") or [], accumulators
            )
            text = delta.get("content")
            finish = choice.get("finish_reason")
            chunk = StreamChunk(
                delta=text if isinstance(text, str) else "",
                tool_calls=calls,
                finish_reason=finish if isinstance(finish, str) else None,
            )
            if not chunk.is_empty:
                yield chunk

        # Flush any call the stream never completed: the provider stopped early
        # (or sent no argument fragments at all for a zero-argument tool).
        for index, slot in sorted(accumulators.items()):
            if slot["name"]:
                yield StreamChunk(
                    tool_calls=[
                        ToolCall(
                            id=slot["id"] or f"call_{index}",
                            name=slot["name"],
                            arguments=_parse_arguments(slot["arguments"]),
                        )
                    ],
                    finish_reason="tool_calls",
                )


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """Decode tool-call arguments, tolerating empty or malformed JSON."""

    if isinstance(raw, dict):
        return {str(key): value for key, value in raw.items()}
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Tool arguments were not valid JSON; passing them through verbatim")
        return {"_raw": raw}
    if isinstance(parsed, dict):
        return {str(key): value for key, value in parsed.items()}
    return {"_value": parsed}


def _is_complete_json(text: str) -> bool:
    if not text:
        return True
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return False
    return True
