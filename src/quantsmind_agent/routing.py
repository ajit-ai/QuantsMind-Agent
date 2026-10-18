"""Provider routing: which endpoint answers, and when it is allowed to change.

Routing here is deliberately small. It is not a marketplace, not model ranking, and
not a retry engine: it is a deterministic ordered list of endpoints, a rule about
which failures may move to the next one, and a record of what happened.

The rule that matters most is which failures *do not* permit a change of endpoint:

* **Authentication and configuration** failures. A second endpoint with the same
  bad key, or a model name the provider does not recognise, produces the same
  failure. Moving would hide a misconfiguration behind a second attempt.
* **Timeouts and connection failures after the request was sent.** The provider
  may have run the request to completion and lost the response. Re-sending it on
  another endpoint is how one turn becomes two, so these never fall back.

Only failures that provably happened *before* the work could have started -- a
rate limit, a server-side error -- permit fallback.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from quantsmind_agent.errors import (
    LLMConfigurationError,
    LLMError,
    LLMProtocolError,
    LLMTimeoutError,
    MissingAPIKeyError,
    ModelUnavailableError,
    NetworkError,
)
from quantsmind_agent.llm import ChatRequest, ChatResponse, ProviderCapabilities, StreamChunk
from quantsmind_agent.streaming import DeliveryState, RetryDecision, classify_stream_failure

__all__ = [
    "FailureKind",
    "ProviderRouter",
    "RouteAttempt",
    "RoutingPolicy",
    "classify_failure",
]

logger = logging.getLogger(__name__)


def _close(chunks: Any) -> None:
    """Close a provider stream if it can be closed, and say so if it cannot."""

    closer = getattr(chunks, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:  # a stream that cannot close must not mask the outcome
            logger.debug("could not close a provider stream", exc_info=True)


class FailureKind(StrEnum):
    """What went wrong, in terms the routing policy can reason about."""

    AUTHENTICATION = "authentication"
    CONFIGURATION = "configuration"
    RATE_LIMIT = "rate_limit"
    SERVER = "server"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    PROTOCOL = "protocol"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


#: Failures that provably happened before the provider did any work. These are the
#: only ones that may move to another endpoint.
FALLBACK_ELIGIBLE = frozenset({FailureKind.RATE_LIMIT, FailureKind.SERVER})


def classify_failure(exc: BaseException) -> FailureKind:
    """Name the kind of failure, using the taxonomy the adapters already produce.

    The provider adapters collapse every HTTP status into
    :class:`ModelUnavailableError`, so the status is read from ``details`` rather
    than guessed from the message.
    """

    if isinstance(exc, MissingAPIKeyError):
        return FailureKind.AUTHENTICATION
    if isinstance(exc, LLMConfigurationError):
        return FailureKind.CONFIGURATION
    if isinstance(exc, LLMTimeoutError):
        return FailureKind.TIMEOUT
    if isinstance(exc, NetworkError):
        return FailureKind.CONNECTION
    if isinstance(exc, LLMProtocolError):
        return FailureKind.PROTOCOL
    if isinstance(exc, ModelUnavailableError):
        details: dict[str, Any] = exc.details if isinstance(exc.details, dict) else {}
        raw = details.get("status_code")
        try:
            code = int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            code = 0
        if code in (401, 403):
            return FailureKind.AUTHENTICATION
        if code == 404:
            return FailureKind.CONFIGURATION
        if code == 429:
            return FailureKind.RATE_LIMIT
        if code >= 500:
            return FailureKind.SERVER
        return FailureKind.CONFIGURATION if 400 <= code < 500 else FailureKind.UNKNOWN
    if isinstance(exc, LLMError):
        return FailureKind.UNKNOWN
    return FailureKind.UNKNOWN


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    """How far the router may go, and on what."""

    max_attempts: int = 2
    #: Failures that permit trying another endpoint. Deliberately small.
    fallback_kinds: frozenset[FailureKind] = FALLBACK_ELIGIBLE

    def permits(self, kind: FailureKind) -> bool:
        return kind in self.fallback_kinds


@dataclass(frozen=True, slots=True)
class RouteAttempt:
    """One endpoint's turn, recorded without any prompt or credential."""

    index: int
    provider: str
    model: str
    outcome: str
    failure: FailureKind | None = None
    detail: str = ""

    def describe(self) -> str:
        if self.failure is None:
            return f"{self.provider}:{self.model} -> {self.outcome}"
        return f"{self.provider}:{self.model} -> {self.outcome} ({self.failure.value})"


@dataclass(frozen=True, slots=True)
class RouteCandidate:
    """One endpoint the router may use."""

    provider: Any
    model: str
    label: str = ""

    @property
    def name(self) -> str:
        return self.label or getattr(self.provider, "name", "provider")


@dataclass
class RoutingDecision:
    """The outcome of a routed call, including how it got there."""

    attempts: list[RouteAttempt] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    fell_back: bool = False

    def describe(self) -> str:
        served = f"{self.provider}:{self.model}"
        if self.fell_back:
            served += f" (after {len(self.attempts)} attempts)"
        return served


class ProviderRouter:
    """A provider that tries an ordered list of endpoints.

    Implements the same :class:`~quantsmind_agent.llm.ModelProvider` protocol as a
    single adapter, so nothing above the gateway has to know that routing exists.
    The order is the configuration order, and it is never reordered at runtime --
    a route that changes from call to call is impossible to reason about and
    impossible to report.
    """

    name = "routed"

    def __init__(
        self,
        candidates: list[RouteCandidate],
        *,
        policy: RoutingPolicy | None = None,
        on_attempt: Any = None,
    ) -> None:
        if not candidates:
            raise LLMConfigurationError(
                "A router needs at least one endpoint",
                user_message="No model provider is configured.",
            )
        self._candidates = list(candidates)
        self._policy = policy or RoutingPolicy()
        self._on_attempt = on_attempt
        #: The last decision, for diagnostics and for the task trace.
        self.last_decision: RoutingDecision | None = None

    @property
    def candidates(self) -> tuple[RouteCandidate, ...]:
        return tuple(self._candidates)

    @property
    def capabilities(self) -> ProviderCapabilities:
        """The capabilities every candidate has in common.

        Intersection, not union: routing to an endpoint that cannot stream would
        break a caller that asked for streaming, and discovering that mid-turn is
        worse than not offering the feature at all.
        """

        declared = [
            getattr(candidate.provider, "capabilities", None) for candidate in self._candidates
        ]
        known = [item for item in declared if item is not None]
        if not known:
            return ProviderCapabilities(
                tools=None, streaming=None, structured_output=None, cancellation=None
            )

        def all_support(name: str) -> bool | None:
            values = [getattr(item, name, None) for item in known]
            if any(value is False for value in values):
                return False
            if any(value is None for value in values):
                return None
            return True

        windows = [item.context_window for item in known if item.context_window]
        return ProviderCapabilities(
            tools=all_support("tools"),
            streaming=all_support("streaming"),
            structured_output=all_support("structured_output"),
            context_window=min(windows) if windows else None,
            cancellation=all_support("cancellation"),
        )

    def list_models(self) -> list[str]:
        """Models the first candidate exposes; routing does not merge catalogues."""

        try:
            models: list[str] = self._candidates[0].provider.list_models()
            return models
        except Exception as exc:  # a provider may simply be down
            logger.warning("could not list models from the primary endpoint: %s", exc)
            return []

    def complete(self, request: ChatRequest) -> ChatResponse:
        """Serve from the first endpoint that can, within the attempt budget."""

        decision = RoutingDecision()
        attempts_allowed = max(1, self._policy.max_attempts)
        last: BaseException | None = None

        for index, candidate in enumerate(self._candidates):
            if index >= attempts_allowed:
                break
            try:
                response = candidate.provider.complete(request)
            except BaseException as exc:
                kind = classify_failure(exc)
                attempt = RouteAttempt(
                    index=index,
                    provider=candidate.name,
                    model=candidate.model,
                    outcome="failed",
                    failure=kind,
                    detail=str(exc)[:200],
                )
                decision.attempts.append(attempt)
                self._announce(attempt)
                last = exc
                if not self._policy.permits(kind):
                    decision.provider = candidate.name
                    decision.model = candidate.model
                    self.last_decision = decision
                    raise
                continue
            attempt = RouteAttempt(
                index=index,
                provider=candidate.name,
                model=candidate.model,
                outcome="served",
            )
            decision.attempts.append(attempt)
            decision.provider = candidate.name
            decision.model = candidate.model
            decision.fell_back = index > 0
            self._announce(attempt)
            self.last_decision = decision
            served: ChatResponse = response
            return served

        self.last_decision = decision
        if last is not None:
            raise last
        raise LLMError(
            "No model endpoint could serve this request",
            user_message="None of the configured model providers could answer.",
        )

    def stream(self, request: ChatRequest) -> Iterator[StreamChunk]:
        """Stream from the first endpoint that can, and never restart one.

        v0.6 fell back between streams when an endpoint failed before producing
        anything. That is still allowed, and only that case is: once a chunk has
        reached the caller, the conversation already shows part of an answer, and
        another endpoint's answer is not a continuation of it.

        The generator is closed on every exit -- success, failure and cancellation
        -- so a provider's response stream is not left open behind us.
        """

        attempts_allowed = max(1, self._policy.max_attempts)
        last: BaseException | None = None
        for index, candidate in enumerate(self._candidates):
            if index >= attempts_allowed:
                break
            chunks: Iterator[StreamChunk] | None = None
            dispatched = False
            try:
                chunks = candidate.provider.stream(request)
                dispatched = True
                iterator = iter(chunks)
                first: StreamChunk | None = None
                while True:
                    first = next(iterator)
                    break
            except StopIteration:
                if chunks is not None:
                    _close(chunks)
                self._announce(
                    RouteAttempt(
                        index=index, provider=candidate.name, model=candidate.model, outcome="empty"
                    )
                )
                self.last_decision = RoutingDecision(
                    attempts=self.last_decision.attempts if self.last_decision else [],
                    provider=candidate.name,
                    model=candidate.model,
                    fell_back=index > 0,
                )
                return
            except BaseException as exc:
                if chunks is not None:
                    _close(chunks)
                kind = classify_failure(exc)
                attempt = RouteAttempt(
                    index=index,
                    provider=candidate.name,
                    model=candidate.model,
                    outcome="failed",
                    failure=kind,
                    detail=str(exc)[:200],
                )
                self._announce(attempt)
                if self.last_decision is None:
                    self.last_decision = RoutingDecision()
                self.last_decision.attempts.append(attempt)
                last = exc
                decision = classify_stream_failure(
                    delivered=DeliveryState.NONE,
                    dispatched=dispatched,
                    failure_kind=kind.value,
                )
                if decision is not RetryDecision.SAFE:
                    self.last_decision.provider = candidate.name
                    self.last_decision.model = candidate.model
                    raise
                continue

            self._announce(
                RouteAttempt(
                    index=index, provider=candidate.name, model=candidate.model, outcome="served"
                )
            )
            self.last_decision = RoutingDecision(
                attempts=(self.last_decision.attempts if self.last_decision is not None else [])
                + [
                    RouteAttempt(
                        index=index,
                        provider=candidate.name,
                        model=candidate.model,
                        outcome="served",
                    )
                ],
                provider=candidate.name,
                model=candidate.model,
                fell_back=index > 0,
            )
            delivered = DeliveryState.NONE
            try:
                if first is not None:
                    if first.delta or first.tool_calls or first.finish_reason:
                        delivered = DeliveryState.PARTIAL
                    yield first
                for item in iterator:
                    if item.delta or item.tool_calls or item.finish_reason:
                        delivered = DeliveryState.PARTIAL
                    yield item
                delivered = DeliveryState.COMPLETE
            except BaseException as exc:
                # The caller already has content, so this is a NO_RESTART no matter
                # how friendly the failure looks. Recorded rather than swallowed:
                # "the answer stopped half way" is the diagnosis.
                kind = classify_failure(exc)
                attempt = RouteAttempt(
                    index=index,
                    provider=candidate.name,
                    model=candidate.model,
                    outcome="interrupted" if delivered is DeliveryState.PARTIAL else "failed",
                    failure=kind,
                    detail=str(exc)[:200],
                )
                self._announce(attempt)
                if self.last_decision is not None:
                    self.last_decision.attempts.append(attempt)
                raise
            finally:
                # Closing on every exit -- including a caller that walks away --
                # is what stops a half-read stream from outliving the turn.
                _close(chunks)
            # Served. Returning here rather than falling out of the loop matters:
            # an earlier endpoint's failure is still in `last`, and falling through
            # would re-raise it after having already delivered this answer.
            return

        if last is not None:
            raise last

    def _announce(self, attempt: RouteAttempt) -> None:
        """Record the attempt without leaking the prompt or any credential."""

        logger.info("route %s", attempt.describe())
        if self._on_attempt is not None:
            self._on_attempt(attempt)
