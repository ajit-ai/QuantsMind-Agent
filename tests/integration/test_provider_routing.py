"""Provider routing: what may move to another endpoint, and what may not.

The policy being tested here is mostly a list of refusals. A router that retries
too eagerly is worse than no router: it can turn one turn into two, and it hides a
misconfiguration behind a second attempt.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from quantsmind_agent.errors import (
    LLMProtocolError,
    LLMTimeoutError,
    MissingAPIKeyError,
    ModelUnavailableError,
    NetworkError,
)
from quantsmind_agent.providers.gateway import ChatRequest, ProviderCapabilities
from quantsmind_agent.providers.openai_compat import OpenAICompatProvider
from quantsmind_agent.providers.routing import (
    FailureKind,
    ProviderRouter,
    RouteCandidate,
    RoutingPolicy,
    classify_failure,
)
from quantsmind_agent.schema import UserMessage

pytestmark = pytest.mark.integration


def request_for(model: str = "test-model") -> ChatRequest:
    return ChatRequest(messages=[UserMessage(content="hello")], model=model)


def body(content: str = "hi") -> dict[str, Any]:
    return {"model": "test-model", "choices": [{"message": {"content": content}}]}


def stub(
    handler: Any,
    *,
    label: str = "stub",
    capabilities: ProviderCapabilities | None = None,
) -> OpenAICompatProvider:
    def default(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body())

    return OpenAICompatProvider(
        "https://stub.invalid/v1",
        api_key="k",
        capabilities=capabilities,
        transport=httpx.MockTransport(handler or default),
    )


def attempts(served: ProviderRouter) -> list[Any]:
    """The attempts of the last decision, for assertions after a completed call."""

    decision = served.last_decision
    assert decision is not None
    return decision.attempts


def failing(status: int) -> OpenAICompatProvider:
    return stub(lambda _r: httpx.Response(status, json={"error": {"message": "no"}}))


def router(
    *providers: OpenAICompatProvider,
    policy: RoutingPolicy | None = None,
    on_attempt: Any = None,
) -> ProviderRouter:
    candidates = [
        RouteCandidate(provider=provider, model=f"model-{index}", label=f"p{index}")
        for index, provider in enumerate(providers)
    ]
    return ProviderRouter(candidates, policy=policy, on_attempt=on_attempt)


class TestFailureClassification:
    @pytest.mark.parametrize(
        ("status", "kind"),
        [
            (401, FailureKind.AUTHENTICATION),
            (403, FailureKind.AUTHENTICATION),
            (404, FailureKind.CONFIGURATION),
            (429, FailureKind.RATE_LIMIT),
            (500, FailureKind.SERVER),
            (503, FailureKind.SERVER),
        ],
    )
    def test_http_status_is_named(self, status: int, kind: FailureKind) -> None:
        provider = failing(status)

        with pytest.raises(ModelUnavailableError) as caught:
            provider.complete(request_for())

        assert classify_failure(caught.value) is kind

    def test_a_missing_key_is_authentication(self) -> None:
        assert classify_failure(MissingAPIKeyError("p")) is FailureKind.AUTHENTICATION

    def test_a_timeout_is_not_fallback_eligible(self) -> None:
        assert classify_failure(LLMTimeoutError("slow")) is FailureKind.TIMEOUT

    def test_a_connection_failure_is_not_fallback_eligible(self) -> None:
        assert classify_failure(NetworkError("unreachable")) is FailureKind.CONNECTION

    def test_a_protocol_error_is_not_fallback_eligible(self) -> None:
        assert classify_failure(LLMProtocolError("garbled")) is FailureKind.PROTOCOL


class TestDeterministicSelection:
    def test_the_first_endpoint_answers_by_default(self) -> None:
        served = router(stub(None), stub(None))

        response = served.complete(request_for())

        assert response.content == "hi"
        assert served.last_decision is not None
        assert served.last_decision.provider == "p0"
        assert served.last_decision.fell_back is False

    def test_the_order_written_is_the_order_used(self) -> None:
        calls: list[str] = []

        def tracking(name: str) -> Any:
            def handler(_request: httpx.Request) -> httpx.Response:
                calls.append(name)
                return httpx.Response(200, json=body())

            return handler

        served = router(stub(tracking("first")), stub(tracking("second")))

        served.complete(request_for())

        assert calls == ["first"]


class TestWhatPermitsFallback:
    def test_a_rate_limit_moves_to_the_next_endpoint(self) -> None:
        served = router(failing(429), stub(None))

        response = served.complete(request_for())

        assert response.content == "hi"
        assert served.last_decision is not None
        assert served.last_decision.fell_back is True
        assert served.last_decision.provider == "p1"

    def test_a_server_error_moves_to_the_next_endpoint(self) -> None:
        served = router(failing(503), stub(None))

        served.complete(request_for())

        assert served.last_decision is not None
        assert served.last_decision.fell_back is True

    @pytest.mark.parametrize("status", [401, 403, 404])
    def test_auth_and_configuration_failures_stop_immediately(self, status: int) -> None:
        """The same bad key or model on the next endpoint fails the same way."""

        calls: list[str] = []

        def counting(_request: httpx.Request) -> httpx.Response:
            calls.append("first")
            return httpx.Response(status, json={"error": {"message": "no"}})

        second_calls: list[str] = []

        def counted(_request: httpx.Request) -> httpx.Response:
            second_calls.append("second")
            return httpx.Response(200, json=body())

        served = router(stub(counting), stub(counted))

        with pytest.raises(ModelUnavailableError):
            served.complete(request_for())

        assert calls == ["first"]
        assert second_calls == [], "the second endpoint was not tried"

    def test_a_timeout_does_not_retry_somewhere_else(self) -> None:
        """The provider may have finished the work and lost the response."""

        def slow(_request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("slow")

        second_calls: list[str] = []

        def counted(_request: httpx.Request) -> httpx.Response:
            second_calls.append("second")
            return httpx.Response(200, json=body())

        served = router(stub(slow), stub(counted))

        with pytest.raises(LLMTimeoutError):
            served.complete(request_for())

        assert second_calls == []


class TestBoundedAttempts:
    def test_attempts_are_capped(self) -> None:
        served = router(
            failing(429),
            failing(429),
            stub(None),
            policy=RoutingPolicy(max_attempts=2),
        )

        with pytest.raises(ModelUnavailableError):
            served.complete(request_for())

        assert len(attempts(served)) == 2

    def test_a_single_endpoint_router_makes_one_attempt(self) -> None:
        served = router(failing(429))

        with pytest.raises(ModelUnavailableError):
            served.complete(request_for())

        assert len(attempts(served)) == 1

    def test_the_original_failure_is_raised_when_nothing_serves(self) -> None:
        served = router(failing(429), failing(500))

        with pytest.raises(ModelUnavailableError) as caught:
            served.complete(request_for())

        assert caught.value.details.get("status_code") == 500

    def test_an_empty_router_is_a_configuration_error(self) -> None:
        from quantsmind_agent.errors import LLMConfigurationError

        with pytest.raises(LLMConfigurationError):
            ProviderRouter([])


class TestCapabilitiesAreTheCommonDenominator:
    def test_capabilities_intersect_across_endpoints(self) -> None:
        served = router(
            stub(None, capabilities=ProviderCapabilities(tools=True, streaming=True)),
            stub(None, capabilities=ProviderCapabilities(tools=True, streaming=False)),
        )

        capabilities = served.capabilities

        assert capabilities.tools is True
        assert capabilities.streaming is False

    def test_an_unknown_capability_is_not_promoted_to_supported(self) -> None:
        served = router(
            stub(None, capabilities=ProviderCapabilities(tools=True)),
            stub(None, capabilities=ProviderCapabilities(tools=None)),
        )

        assert served.capabilities.tools is None


class TestDiagnosticsCarryNoSecrets:
    def test_an_attempt_records_the_endpoint_and_the_kind_only(self) -> None:
        seen: list[str] = []
        served = router(failing(429), stub(None), on_attempt=lambda a: seen.append(a.describe()))

        served.complete(request_for())

        assert seen == ["p0:model-0 -> failed (rate_limit)", "p1:model-1 -> served"]
        for line in seen:
            assert "Bearer" not in line
            assert "sk-" not in line

    def test_the_attempt_detail_does_not_carry_the_prompt(self) -> None:
        served = router(failing(429), stub(None))
        request = ChatRequest(messages=[UserMessage(content="a very private question")], model="m")

        served.complete(request)

        detail = attempts(served)[0].detail
        assert "private question" not in detail
