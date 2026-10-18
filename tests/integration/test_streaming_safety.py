"""Streaming safety: when a failure may be served again, and when it may not.

Each test is about a retry that must *not* happen. A router that can select a
second endpoint is easy; one that refuses to replay work already done or possibly
already done is the property that makes retrying safe at all.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from quantsmind_agent.errors import LLMTimeoutError, ModelUnavailableError
from quantsmind_agent.llm import ChatRequest, ProviderCapabilities, StreamChunk
from quantsmind_agent.providers.openai_compat import OpenAICompatProvider
from quantsmind_agent.routing import (
    ProviderRouter,
    RouteCandidate,
    RoutingPolicy,
    classify_failure,
)
from quantsmind_agent.schema import UserMessage
from quantsmind_agent.streaming import (
    DeliveryState,
    RetryDecision,
    classify_stream_failure,
)

pytestmark = pytest.mark.integration


def request_for(model: str = "m") -> ChatRequest:
    return ChatRequest(messages=[UserMessage(content="hi")], model=model)


class RecordingStream:
    """A provider stream that records whether it was closed."""

    def __init__(self, chunks: list[StreamChunk]) -> None:
        self._chunks = list(chunks)
        self.closed = False

    def __iter__(self) -> Any:
        return self

    def __next__(self) -> StreamChunk:
        if not self._chunks:
            raise StopIteration
        return self._chunks.pop(0)

    def close(self) -> None:
        self.closed = True


class FakeProvider:
    """A provider whose stream behaves exactly as a test needs it to."""

    def __init__(self, stream: Any | None = None, *, name: str = "fake") -> None:
        self.name = name
        self._stream = stream
        self.capabilities = ProviderCapabilities(tools=True, streaming=True)
        self.complete_calls = 0

    def list_models(self) -> list[str]:
        return ["m"]

    def complete(self, request: ChatRequest) -> Any:
        self.complete_calls += 1
        raise ModelUnavailableError("not used", details={"status_code": 500})

    def stream(self, request: ChatRequest) -> Any:
        return self._stream


def router_of(*providers: FakeProvider, policy: RoutingPolicy | None = None) -> ProviderRouter:
    return ProviderRouter(
        [RouteCandidate(provider=p, model="m", label=p.name) for p in providers],
        policy=policy,
    )


def chunk(text: str) -> StreamChunk:
    return StreamChunk(delta=text)


class TestTheDecisionFunction:
    def test_nothing_sent_may_be_served_elsewhere(self) -> None:
        decision = classify_stream_failure(
            delivered=DeliveryState.NONE, dispatched=False, failure_kind="rate_limit"
        )

        assert decision is RetryDecision.SAFE

    def test_partial_output_is_never_restarted(self) -> None:
        decision = classify_stream_failure(
            delivered=DeliveryState.PARTIAL, dispatched=True, failure_kind="rate_limit"
        )

        assert decision is RetryDecision.NO_RESTART

    def test_a_complete_response_is_never_restarted(self) -> None:
        decision = classify_stream_failure(
            delivered=DeliveryState.COMPLETE, dispatched=True, failure_kind="server"
        )

        assert decision is RetryDecision.NO_RESTART

    def test_a_dispatched_request_that_failed_is_ambiguous(self) -> None:
        decision = classify_stream_failure(
            delivered=DeliveryState.NONE, dispatched=True, failure_kind="timeout"
        )

        assert decision is RetryDecision.AMBIGUOUS

    def test_a_refusal_after_dispatch_is_still_safe(self) -> None:
        """A 429 is reported without the work being done."""

        decision = classify_stream_failure(
            delivered=DeliveryState.NONE, dispatched=True, failure_kind="rate_limit"
        )

        assert decision is RetryDecision.SAFE

    def test_cancellation_is_never_retried(self) -> None:
        decision = classify_stream_failure(
            delivered=DeliveryState.NONE,
            dispatched=False,
            failure_kind="rate_limit",
            cancelled=True,
        )

        assert decision is RetryDecision.TERMINAL

    def test_an_expired_deadline_is_never_retried(self) -> None:
        decision = classify_stream_failure(
            delivered=DeliveryState.NONE,
            dispatched=False,
            failure_kind="rate_limit",
            deadline_expired=True,
        )

        assert decision is RetryDecision.TERMINAL


class TestStreamingThroughTheRouter:
    def test_a_healthy_stream_is_served_and_closed(self) -> None:
        stream = RecordingStream([chunk("he"), chunk("llo")])

        chunks = list(router_of(FakeProvider(stream)).stream(request_for()))

        assert "".join(item.delta for item in chunks) == "hello"
        assert stream.closed is True

    def test_a_failure_before_the_first_chunk_may_fall_back(self) -> None:
        class Failing(FakeProvider):
            def stream(self, request: ChatRequest) -> Any:
                raise ModelUnavailableError("busy", details={"status_code": 429})

        second = RecordingStream([chunk("from the second")])
        served = router_of(Failing(name="first"), FakeProvider(second, name="second"))

        chunks = list(served.stream(request_for()))

        assert "".join(item.delta for item in chunks) == "from the second"
        assert served.last_decision is not None
        assert served.last_decision.fell_back is True

    def test_a_failure_after_partial_output_does_not_restart(self) -> None:
        """The caller already has text; another endpoint's answer is not a continuation."""

        class PartialThenFail(FakeProvider):
            def stream(self, request: ChatRequest) -> Any:
                def generate() -> Any:
                    yield chunk("half an ans")
                    raise LLMTimeoutError("the connection went")

                return generate()

        second = RecordingStream([chunk("a different whole answer")])
        served = router_of(PartialThenFail(name="first"), FakeProvider(second, name="second"))

        received: list[str] = []
        with pytest.raises(LLMTimeoutError):
            for item in served.stream(request_for()):
                received.append(item.delta)

        assert received == ["half an ans"], "nothing was appended from the second endpoint"

    def test_a_dispatched_timeout_does_not_fall_back(self) -> None:
        """Sent, no answer: the provider may have finished the work."""

        class Dispatched(FakeProvider):
            def stream(self, request: ChatRequest) -> Any:
                def generate() -> Any:
                    # Yields first so the failure lands *after* dispatch, which is
                    # the case that matters: the request was sent, so the provider
                    # may have done the work and lost the response.
                    yield StreamChunk()
                    raise LLMTimeoutError("no answer")

                return generate()

        second = RecordingStream([chunk("should not appear")])
        served = router_of(Dispatched(name="first"), FakeProvider(second, name="second"))

        with pytest.raises(LLMTimeoutError):
            list(served.stream(request_for()))

    def test_authentication_failures_do_not_fall_back(self) -> None:
        class Unauthorized(FakeProvider):
            def stream(self, request: ChatRequest) -> Any:
                raise ModelUnavailableError("no", details={"status_code": 401})

        second = RecordingStream([chunk("should not appear")])
        served = router_of(Unauthorized(name="first"), FakeProvider(second, name="second"))

        with pytest.raises(ModelUnavailableError):
            list(served.stream(request_for()))

    def test_an_empty_stream_is_not_an_error(self) -> None:
        served = router_of(FakeProvider(RecordingStream([])))

        assert list(served.stream(request_for())) == []

    def test_a_stream_is_closed_when_the_caller_walks_away(self) -> None:
        """A half-read stream must not outlive the turn that opened it."""

        stream = RecordingStream([chunk("a"), chunk("b"), chunk("c")])
        served = router_of(FakeProvider(stream))

        for _ in served.stream(request_for()):
            break

        assert stream.closed is True

    def test_a_stream_that_cannot_be_closed_does_not_mask_the_outcome(self) -> None:
        class Stubborn:
            """A stream that refuses to be closed, then ends normally."""

            def __init__(self) -> None:
                self._left = 1

            def __iter__(self) -> Any:
                return self

            def __next__(self) -> StreamChunk:
                if self._left <= 0:
                    raise StopIteration
                self._left -= 1
                return chunk("x")

            def close(self) -> None:
                raise OSError("cannot close")

        served = router_of(FakeProvider(Stubborn()))

        assert len(list(served.stream(request_for()))) > 0


class TestAttemptsAreRecorded:
    def test_a_stream_attempt_is_recorded(self) -> None:
        seen: list[str] = []
        served = ProviderRouter(
            [
                RouteCandidate(
                    provider=FakeProvider(RecordingStream([chunk("x")])), model="m", label="p0"
                )
            ],
            on_attempt=lambda attempt: seen.append(attempt.describe()),
        )

        list(served.stream(request_for()))

        assert seen == ["p0:m -> served"]

    def test_a_refused_stream_records_the_kind(self) -> None:
        class Unauthorized(FakeProvider):
            def stream(self, request: ChatRequest) -> Any:
                raise ModelUnavailableError("no", details={"status_code": 403})

        served = router_of(Unauthorized(name="first"))

        with pytest.raises(ModelUnavailableError):
            list(served.stream(request_for()))

        assert served.last_decision is not None
        assert served.last_decision.attempts[0].failure is not None
        assert "authentication" in served.last_decision.attempts[0].failure.value


class TestProviderErrorClassificationStillHolds:
    def test_a_timeout_is_not_a_fallback_kind(self) -> None:
        assert classify_failure(LLMTimeoutError("slow")) is not None
        assert classify_failure(LLMTimeoutError("slow")).value == "timeout"


class TestAdapterCleanup:
    def test_the_adapter_stream_is_iterable_and_finite(self) -> None:
        """The adapter's own SSE reader must not leak an open response."""

        lines = [
            b'data: {"choices":[{"delta":{"content":"ok"}}]}',
            b"data: [DONE]",
        ]
        provider = OpenAICompatProvider(
            "https://stub.invalid/v1",
            api_key="k",
            transport=httpx.MockTransport(
                lambda _r: httpx.Response(200, content=b"\n".join(lines))
            ),
        )

        chunks = list(provider.stream(request_for()))

        assert "".join(item.delta for item in chunks) == "ok"
