"""Structured planning: a model answer becomes a plan, or a list of reasons.

The point of this file is the second outcome. It is easy to write a planner that
returns a plan; it is much harder to write one that refuses to return a plan when
the model produced something unusable, and that refusal is the property that
matters.
"""

from __future__ import annotations

import json
from typing import Any, cast

import httpx
import pytest

from quantsmind_agent.llm import (
    ChatRequest,
    ModelGateway,
    ProviderCapabilities,
)
from quantsmind_agent.planner import (
    PLAN_SCHEMA,
    PLAN_SCHEMA_VERSION,
    TaskPlanner,
    plan_from_payload,
)
from quantsmind_agent.providers.openai_compat import OpenAICompatProvider
from quantsmind_agent.tasks import PlanRejected
from tests.fakes import FakeModelProvider, text_response

pytestmark = pytest.mark.integration

TOOLS = frozenset({"read_file", "write_file", "search_content"})


def gateway_returning(
    content: str, *, capabilities: ProviderCapabilities | None = None
) -> ModelGateway:
    provider = OpenAICompatProvider(
        "https://stub.invalid/v1",
        api_key="k",
        capabilities=capabilities,
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(
                200,
                json={
                    "model": "stub-model",
                    "choices": [{"message": {"content": content}}],
                },
            )
        ),
    )
    return ModelGateway(provider)


def fake_gateway(responses: list[Any]) -> ModelGateway:
    return ModelGateway(FakeModelProvider(responses))


def good_plan(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "objective": "update the greeting module",
        "steps": [
            {
                "id": "write",
                "description": "write the new greeting",
                "tool": "write_file",
                "arguments": {"path": "a.py", "content": "hello\n"},
                "depends_on": [],
                "risk": "low",
                "expected_paths": ["a.py"],
            }
        ],
        "acceptance_criteria": [{"id": "c1", "description": "greeting updated"}],
    }
    payload.update(overrides)
    return payload


def planner_for(gateway: Any, **kwargs: Any) -> TaskPlanner:
    kwargs.setdefault("model", "stub-model")
    return TaskPlanner(gateway=gateway, known_tools=TOOLS, workspace="/tmp/project", **kwargs)


class TestTheSchemaItself:
    def test_the_schema_is_versioned(self) -> None:
        assert PLAN_SCHEMA_VERSION >= 1

    def test_the_schema_requires_objective_and_steps(self) -> None:
        assert set(PLAN_SCHEMA["required"]) == {"objective", "steps"}

    def test_a_step_requires_an_identity_and_a_tool(self) -> None:
        required = PLAN_SCHEMA["properties"]["steps"]["items"]["required"]
        assert {"id", "description", "tool"} <= set(required)

    def test_risk_is_constrained_to_the_known_levels(self) -> None:
        item = PLAN_SCHEMA["properties"]["steps"]["items"]
        assert item["properties"]["risk"]["enum"] == ["read_only", "low", "medium", "high"]


class TestValidPlans:
    def test_a_well_formed_plan_is_returned(self) -> None:
        proposal = planner_for(gateway_returning(json.dumps(good_plan()))).propose(
            "update the greeting module"
        )

        assert proposal.ok is True
        assert proposal.plan is not None
        assert proposal.plan.steps[0].tool == "write_file"

    def test_a_fenced_plan_is_accepted(self) -> None:
        gateway = gateway_returning("```json\n" + json.dumps(good_plan()) + "\n```")

        assert planner_for(gateway).propose("update the greeting module").ok is True

    def test_a_provider_that_declares_no_support_is_refused_explicitly(self) -> None:
        gateway = gateway_returning(
            json.dumps(good_plan()),
            capabilities=ProviderCapabilities(structured_output=False),
        )

        proposal = planner_for(gateway).propose("update the greeting module")

        assert proposal.ok is False
        assert any("structured" in reason for reason in proposal.problems)


class TestRefusalsAreExplicit:
    def test_malformed_json_is_refused(self) -> None:
        proposal = planner_for(
            gateway_returning("I would rather describe the change in prose.")
        ).propose("update the greeting module")

        assert proposal.ok is False
        assert proposal.plan is None
        assert proposal.problems

    def test_a_truncated_plan_is_refused(self) -> None:
        gateway = gateway_returning('{"objective": "update", "steps": [{"id":')

        assert planner_for(gateway).propose("update the greeting module").ok is False

    def test_a_missing_field_is_refused(self) -> None:
        gateway = gateway_returning(json.dumps({"steps": []}))

        assert planner_for(gateway).propose("update the greeting module").ok is False

    def test_a_wrong_type_is_refused(self) -> None:
        payload = good_plan()
        payload["steps"] = "write the file"

        gateway = gateway_returning(json.dumps(payload))

        assert planner_for(gateway).propose("update the greeting module").ok is False

    def test_an_unknown_tool_is_refused(self) -> None:
        payload = good_plan()
        payload["steps"][0]["tool"] = "run_arbitrary_shell"

        proposal = planner_for(gateway_returning(json.dumps(payload))).propose(
            "update the greeting module"
        )

        assert proposal.ok is False
        assert any("unknown tool" in reason for reason in proposal.problems)

    def test_a_duplicate_step_id_is_refused(self) -> None:
        payload = good_plan()
        payload["steps"].append(dict(payload["steps"][0]))

        proposal = planner_for(gateway_returning(json.dumps(payload))).propose(
            "update the greeting module"
        )

        assert proposal.ok is False
        assert any("more than once" in reason for reason in proposal.problems)

    def test_an_unknown_dependency_is_refused(self) -> None:
        payload = good_plan()
        payload["steps"][0]["depends_on"] = ["nowhere"]

        proposal = planner_for(gateway_returning(json.dumps(payload))).propose(
            "update the greeting module"
        )

        assert proposal.ok is False
        assert any("not a step" in reason for reason in proposal.problems)

    def test_a_dependency_cycle_is_refused(self) -> None:
        payload = good_plan()
        payload["steps"] = [
            {**payload["steps"][0], "id": "a", "depends_on": ["b"]},
            {**payload["steps"][0], "id": "b", "depends_on": ["a"]},
        ]

        proposal = planner_for(gateway_returning(json.dumps(payload))).propose(
            "update the greeting module"
        )

        assert proposal.ok is False
        assert any("cycle" in reason for reason in proposal.problems)

    def test_a_path_outside_the_workspace_is_refused(self) -> None:
        payload = good_plan()
        payload["steps"][0]["arguments"]["path"] = "../outside.py"

        proposal = planner_for(gateway_returning(json.dumps(payload))).propose(
            "update the greeting module"
        )

        assert proposal.ok is False

    def test_an_empty_plan_is_never_substituted(self) -> None:
        """No plan at all is the honest answer when the model produced none."""

        proposal = planner_for(gateway_returning("not json at all")).propose(
            "update the greeting module"
        )

        assert proposal.plan is None
        assert "no plan" in proposal.describe()

    def test_a_provider_failure_is_reported_not_swallowed(self) -> None:
        def failing(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": {"message": "bad key"}})

        provider = OpenAICompatProvider(
            "https://stub.invalid/v1", api_key="k", transport=httpx.MockTransport(failing)
        )

        proposal = planner_for(ModelGateway(provider)).propose("update the greeting module")

        assert proposal.ok is False
        assert proposal.problems

    def test_a_too_short_objective_is_refused_without_calling_the_model(self) -> None:
        proposal = planner_for(fake_gateway([text_response("never used")])).propose("hi")

        assert proposal.ok is False


class TestBuildingFromPayload:
    def test_a_valid_payload_becomes_a_plan(self) -> None:
        plan = plan_from_payload(good_plan())

        assert plan.objective == "update the greeting module"
        assert plan.steps[0].id == "write"

    def test_a_non_object_payload_is_refused(self) -> None:
        with pytest.raises(PlanRejected):
            plan_from_payload(cast(Any, [1, 2, 3]))

    def test_a_step_that_is_not_an_object_is_refused(self) -> None:
        with pytest.raises(PlanRejected):
            plan_from_payload({"objective": "do it", "steps": ["write the file"]})


class TestProjectContext:
    def test_the_project_context_is_put_in_the_prompt(self) -> None:
        seen: list[ChatRequest] = []

        class Recording(FakeModelProvider):
            def complete(self, request: ChatRequest) -> Any:
                seen.append(request)
                return text_response(json.dumps(good_plan()))

        planner = planner_for(
            ModelGateway(Recording()),
            project_context="Workspace: acme\nProject kind: python-package",
        )

        planner.propose("update the greeting module")

        prompt = str(seen[0].messages[-1].content)
        assert "Project context:" in prompt
        assert "Project kind: python-package" in prompt
        assert "Objective: update the greeting module" in prompt

    def test_the_project_context_is_capped_before_it_reaches_the_prompt(self) -> None:
        seen: list[ChatRequest] = []

        class Recording(FakeModelProvider):
            def complete(self, request: ChatRequest) -> Any:
                seen.append(request)
                return text_response(json.dumps(good_plan()))

        planner = planner_for(
            ModelGateway(Recording()),
            project_context="A" * 5_000 + "END-OF-CONTEXT",
        )

        planner.propose("update the greeting module")

        prompt = str(seen[0].messages[-1].content)
        assert "END-OF-CONTEXT" not in prompt
        assert "A" * 100 in prompt

    def test_no_context_means_no_empty_section(self) -> None:
        seen: list[ChatRequest] = []

        class Recording(FakeModelProvider):
            def complete(self, request: ChatRequest) -> Any:
                seen.append(request)
                return text_response(json.dumps(good_plan()))

        planner_for(ModelGateway(Recording())).propose("update the greeting module")

        assert "Project context:" not in str(seen[0].messages[-1].content)


class TestCompatibilityAndRequest:
    def test_a_plan_written_before_structured_planning_still_loads(self) -> None:
        legacy = {
            "objective": "update the greeting module",
            "steps": [
                {
                    "id": "write",
                    "description": "write it",
                    "tool": "write_file",
                    "arguments": {"path": "a.py", "content": "hi\n"},
                }
            ],
            "acceptance_criteria": [],
            "scope_paths": [],
            "notes": "",
            "version": 1,
        }

        assert plan_from_payload(legacy).objective == "update the greeting module"

    def test_the_request_carries_the_schema(self) -> None:
        seen: list[ChatRequest] = []

        class Recording(FakeModelProvider):
            def complete(self, request: ChatRequest) -> Any:
                seen.append(request)
                return text_response(json.dumps(good_plan()))

        planner_for(ModelGateway(Recording())).propose("update the greeting module")

        assert seen[0].structured is not None
        assert seen[0].structured.schema == PLAN_SCHEMA

    def test_a_proposal_carries_no_approval(self) -> None:
        proposal = planner_for(gateway_returning(json.dumps(good_plan()))).propose(
            "update the greeting module"
        )

        assert proposal.ok is True
        assert proposal.plan is not None
