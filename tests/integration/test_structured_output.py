"""Structured output: parsed, validated, and honest about which guarantee applied.

Three things are being kept apart here, because conflating any two of them is how
a caller ends up trusting data that was never constrained:

* whether the endpoint can do structured output at all,
* whether the answer was *constrained* by the provider or merely *parsed* here,
* and whether what came back matches the schema.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from quantsmind_agent.llm import (
    ChatRequest,
    ModelGateway,
    ProviderCapabilities,
    StructuredOutput,
    StructuredOutputError,
    parse_structured,
    validate_structured,
)
from quantsmind_agent.providers.openai_compat import OpenAICompatProvider
from quantsmind_agent.schema import UserMessage

pytestmark = pytest.mark.integration

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["objective", "steps"],
    "properties": {
        "objective": {"type": "string"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "tool"],
                "properties": {"id": {"type": "string"}, "tool": {"type": "string"}},
            },
        },
    },
}


def completion(content: str) -> dict[str, Any]:
    return {"model": "m", "choices": [{"message": {"role": "assistant", "content": content}}]}


def stub(content: str, *, capabilities: ProviderCapabilities | None = None) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        "https://stub.invalid/v1",
        api_key="k",
        capabilities=capabilities,
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=completion(content))),
    )


def ask(provider: OpenAICompatProvider, *, native: bool = True) -> ChatRequest:
    return ChatRequest(
        messages=[UserMessage(content="Return JSON matching the schema.")],
        model="m",
        structured=StructuredOutput(PLAN_SCHEMA, native=native),
    )


class TestSchemaValidation:
    def test_a_matching_object_validates(self) -> None:
        value = {"objective": "do it", "steps": [{"id": "s1", "tool": "write_file"}]}

        assert validate_structured(value, PLAN_SCHEMA) == []

    def test_a_missing_required_field_is_reported_by_name(self) -> None:
        problems = validate_structured({"objective": "do it"}, PLAN_SCHEMA)

        assert any("steps" in problem for problem in problems)

    def test_a_wrong_type_is_reported_with_the_path(self) -> None:
        problems = validate_structured({"objective": 7, "steps": []}, PLAN_SCHEMA)

        assert any("$.objective" in problem for problem in problems)

    def test_a_nested_array_item_is_checked(self) -> None:
        problems = validate_structured({"objective": "do it", "steps": [{"id": "s1"}]}, PLAN_SCHEMA)

        assert any("$.steps[0]" in problem for problem in problems)

    def test_a_boolean_is_not_an_integer(self) -> None:
        schema = {"type": "object", "properties": {"count": {"type": "integer"}}}

        assert validate_structured({"count": True}, schema) != []

    def test_an_enum_is_enforced(self) -> None:
        schema = {"type": "string", "enum": ["a", "b"]}

        assert validate_structured("c", schema) != []
        assert validate_structured("a", schema) == []


class TestParsing:
    def test_plain_json_parses(self) -> None:
        value = parse_structured(json.dumps({"objective": "x", "steps": []}), PLAN_SCHEMA)

        assert value["objective"] == "x"

    def test_a_fenced_block_parses(self) -> None:
        """Models wrap JSON in fences; that is a formatting habit, not a failure."""

        text = '```json\n{"objective": "x", "steps": []}\n```'

        assert parse_structured(text, PLAN_SCHEMA)["objective"] == "x"

    def test_prose_around_json_is_refused(self) -> None:
        """Guessing where the JSON starts is how a wrong answer gets accepted."""

        with pytest.raises(StructuredOutputError):
            parse_structured('Sure! Here you go: {"objective": "x"}', PLAN_SCHEMA)

    def test_a_truncated_answer_is_refused(self) -> None:
        with pytest.raises(StructuredOutputError):
            parse_structured('{"objective": "x", "ste', PLAN_SCHEMA)

    def test_schema_mismatch_is_refused(self) -> None:
        with pytest.raises(StructuredOutputError) as caught:
            parse_structured(json.dumps({"wrong": True}), PLAN_SCHEMA)

        assert caught.value.details.get("problems")

    def test_an_empty_answer_is_not_a_valid_empty_result(self) -> None:
        with pytest.raises(StructuredOutputError):
            parse_structured("", PLAN_SCHEMA)


class TestThroughTheAdapter:
    def test_a_valid_response_yields_the_parsed_payload(self) -> None:
        provider = stub(json.dumps({"objective": "do it", "steps": []}))

        response = provider.complete(ask(provider))

        assert response.structured == {"objective": "do it", "steps": []}
        assert response.structured_mode == "native"

    def test_the_native_path_sends_a_schema_to_the_provider(self) -> None:
        seen: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(200, json=completion(json.dumps({"objective": "x", "steps": []})))

        provider = OpenAICompatProvider(
            "https://stub.invalid/v1", api_key="k", transport=httpx.MockTransport(handler)
        )

        provider.complete(ask(provider, native=True))

        assert seen[0]["response_format"]["type"] == "json_schema"

    def test_the_compatibility_path_is_recorded_as_parsed(self) -> None:
        """A parsed answer is not a constrained one, and says so."""

        provider = stub(json.dumps({"objective": "do it", "steps": []}))

        response = provider.complete(ask(provider, native=False))

        assert response.structured_mode == "parsed"

    def test_malformed_output_raises_rather_than_returning_empty(self) -> None:
        provider = stub("I could not produce JSON.")

        with pytest.raises(StructuredOutputError):
            provider.complete(ask(provider))

    def test_plain_text_requests_are_unchanged(self) -> None:
        provider = stub("just prose")

        response = provider.complete(ChatRequest(messages=[UserMessage(content="hi")], model="m"))

        assert response.content == "just prose"
        assert response.structured is None
        assert response.structured_mode == ""


class TestCapabilityGate:
    def test_an_endpoint_that_declares_no_support_refuses_the_request(self) -> None:
        provider = stub(
            json.dumps({"objective": "x", "steps": []}),
            capabilities=ProviderCapabilities(structured_output=False),
        )
        gateway = ModelGateway(provider)

        with pytest.raises(StructuredOutputError) as caught:
            gateway.complete(ask(provider))

        assert caught.value.details.get("capability") == "structured_output"

    def test_the_gate_uses_the_declared_capability_not_the_protocol_default(self) -> None:
        provider = stub(
            json.dumps({"objective": "x", "steps": []}),
            capabilities=ProviderCapabilities(structured_output=True),
        )
        gateway = ModelGateway(provider)

        assert gateway.complete(ask(provider)).structured_mode == "native"


class TestSecretsStayOut:
    def test_a_malformed_error_does_not_echo_the_prompt(self) -> None:
        provider = stub("nope")
        request = ask(provider)
        request.messages[0].content = "my private question"

        with pytest.raises(StructuredOutputError) as caught:
            provider.complete(request)

        assert "private question" not in str(caught.value)
