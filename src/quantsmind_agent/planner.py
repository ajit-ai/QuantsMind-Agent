"""Turning a model's answer into a plan the application is willing to run.

Two gaps made this a real piece of work rather than a wiring exercise:

1. Nothing connected a model response to a ``Plan``. The panel asked the model
   for a fenced JSON block in prose and hoped for the best.
2. There was no schema the model could be held to. A schema the *provider* is
   asked to enforce is helpful; one this application checks itself is what makes
   the result trustworthy when the endpoint does not support constrained output
   at all.

So: a versioned schema, a request that uses the v0.6 structured-output path, and
validation that happens here regardless of what the provider did.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from quantsmind_agent.llm import (
    ChatRequest,
    ModelGateway,
    StructuredOutput,
    StructuredOutputError,
    parse_structured,
)
from quantsmind_agent.schema import UserMessage
from quantsmind_agent.tasks import (
    AcceptanceCriterion,
    Plan,
    PlannedStep,
    PlanRejected,
    validate_plan,
)

__all__ = [
    "PLAN_SCHEMA",
    "PLAN_SCHEMA_VERSION",
    "PlanProposal",
    "TaskPlanner",
    "plan_from_payload",
]

logger = logging.getLogger(__name__)

#: Bumped when the shape below changes. A model asked for one version and judged
#: against another is the classic way a "validated" plan stops meaning anything.
PLAN_SCHEMA_VERSION = 1

_STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["id", "description", "tool"],
    "properties": {
        "id": {"type": "string"},
        "description": {"type": "string"},
        "tool": {"type": "string"},
        "arguments": {"type": "object"},
        "depends_on": {"type": "array", "items": {"type": "string"}},
        "risk": {"type": "string", "enum": ["read_only", "low", "medium", "high"]},
        "expected_paths": {"type": "array", "items": {"type": "string"}},
        "expected_command": {"type": "string"},
        "optional": {"type": "boolean"},
    },
}

_CRITERION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["id", "description"],
    "properties": {
        "id": {"type": "string"},
        "description": {"type": "string"},
        "verified_by": {"type": "string"},
    },
}

#: What a proposed plan must look like. Enforced by the application on every
#: response, whichever path produced it.
PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["objective", "steps"],
    "properties": {
        "schema_version": {"type": "integer"},
        "objective": {"type": "string"},
        "steps": {"type": "array", "items": _STEP_SCHEMA},
        "acceptance_criteria": {"type": "array", "items": _CRITERION_SCHEMA},
        "scope_paths": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
}

_INSTRUCTIONS = (
    "Propose a plan for this change using only the tools you have.\n"
    "Return JSON in exactly this shape:\n"
    '{"objective": "...", "steps": [{"id": "s1", "description": "...", '
    '"tool": "write_file", "arguments": {"path": "a.py", "content": "..."}, '
    '"depends_on": [], "risk": "low", "expected_paths": ["a.py"]}], '
    '"acceptance_criteria": [{"id": "c1", "description": "..."}]}\n'
    "Step ids must be unique, depends_on may only name other steps in this plan, "
    "and every tool must be one you have."
)


def plan_from_payload(payload: dict[str, Any]) -> Plan:
    """Build a :class:`Plan` from structured JSON, validating it on the way.

    Raises :class:`PlanRejected` rather than returning a partially-built plan: a
    plan the application cannot vouch for must not reach the approval screen,
    where it would look exactly like one it can.
    """

    if not isinstance(payload, dict):
        raise PlanRejected(["a plan must be a JSON object"])
    try:
        candidate = Plan(
            objective=str(payload.get("objective", "")),
            steps=tuple(PlannedStep.from_dict(item) for item in payload.get("steps") or []),
            acceptance_criteria=tuple(
                AcceptanceCriterion.from_dict(item)
                for item in payload.get("acceptance_criteria") or []
            ),
            scope_paths=tuple(str(item) for item in payload.get("scope_paths") or ()),
            notes=str(payload.get("notes", "")),
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise PlanRejected([f"the plan could not be read: {exc}"]) from exc
    return candidate


@dataclass(frozen=True, slots=True)
class PlanProposal:
    """The result of asking for a plan: a plan, or the reasons there is none.

    There is deliberately no "empty plan" outcome. A model that failed, refused,
    or returned something unreadable produces *problems*, and the interface shows
    those. An empty plan would look like a plan nobody can do anything with, which
    is a different failure wearing the same clothes.
    """

    plan: Plan | None = None
    problems: tuple[str, ...] = ()
    provider: str = ""
    model: str = ""
    native: bool = False

    @property
    def ok(self) -> bool:
        return self.plan is not None

    def describe(self) -> str:
        if self.plan is not None:
            mode = "native" if self.native else "parsed"
            return f"proposed {len(self.plan.steps)} step(s) ({mode})"
        return "no plan: " + "; ".join(self.problems[:3])


@dataclass
class TaskPlanner:
    """Asks the configured model for a plan and validates whatever comes back."""

    gateway: ModelGateway
    known_tools: frozenset[str] = field(default_factory=frozenset)
    workspace: str = ""
    #: A bounded description of the project this plan is for. Supplied by
    #: :class:`~quantsmind_agent.workspace_intel.WorkspaceIntelligence`; kept as
    #: text so the planner does not own a second discovery cache.
    project_context: str = ""
    #: The configured model to ask. Required: a request without one is refused by
    #: the endpoint, and a planner that hides that behind a generic failure is
    #: making the cause harder to find.
    model: str = ""
    max_steps: int = 100
    max_output_tokens: int = 2048
    timeout_seconds: float = 120.0
    use_native_schema: bool = True

    def propose(self, objective: str, *, conversation: str = "") -> PlanProposal:
        """Request a plan for ``objective`` and validate it before returning.

        Order matters: the JSON shape is checked first because a plan of the wrong
        shape cannot be judged on its steps, and the semantic checks -- unique ids,
        known tools, resolvable dependencies, no cycles, nothing outside the
        workspace -- run afterwards against the real model.
        """

        text = objective.strip()
        if len(text) < 4:
            return PlanProposal(problems=("the objective is too short to plan for",))
        if not self.model.strip():
            return PlanProposal(problems=("no model is configured to ask",))

        # The available tools go in the prompt, not just in the schema. A live
        # check caught a model proposing a step with a tool that did not exist;
        # refusing it was correct, but the better fix is to tell it what it has.
        tools = ", ".join(sorted(self.known_tools)) or "(none)"
        instructions = f"{_INSTRUCTIONS}\nTools you may use: {tools}"
        sections = [instructions]
        # Project facts change where a plan should look first: a src layout, a
        # declared test root, a Node package. They are bounded and contain no
        # absolute paths; the snapshot is responsible for that guarantee.
        project_context = self.project_context.strip()
        if project_context:
            sections.append("Project context:\n" + project_context[:4_000])
        if conversation.strip():
            sections.append("Context:\n" + conversation)
        sections.append(f"Objective: {text}")
        prompt = UserMessage(content="\n\n".join(sections))
        request = ChatRequest(
            messages=[prompt],
            model=self.model,
            max_output_tokens=self.max_output_tokens,
            timeout_seconds=self.timeout_seconds,
            structured=StructuredOutput(PLAN_SCHEMA, native=self.use_native_schema),
        )

        try:
            response = self.gateway.complete(request)
        except StructuredOutputError as exc:
            # The endpoint cannot do structured output, or the answer was not
            # readable. Both are explicit: the user is told which.
            return PlanProposal(problems=(str(exc),), provider=self.gateway.provider_name)
        except Exception as exc:
            logger.info("plan proposal failed: %s", type(exc).__name__)
            return PlanProposal(
                problems=(f"the model could not be asked for a plan: {exc}",),
                provider=self.gateway.provider_name,
            )

        try:
            payload = parse_structured(response.content, PLAN_SCHEMA)
        except StructuredOutputError as exc:
            return PlanProposal(
                problems=(f"the model did not return a usable plan: {exc}",),
                provider=response.model or self.gateway.provider_name,
                model=response.model,
                native=response.structured_mode == "native",
            )

        try:
            candidate = plan_from_payload(payload)
            checked = validate_plan(
                candidate,
                known_tools=self.known_tools or None,
                workspace=self.workspace or None,
                max_steps=self.max_steps,
            )
        except PlanRejected as exc:
            return PlanProposal(
                problems=tuple(exc.reasons),
                provider=response.model or self.gateway.provider_name,
                model=response.model,
                native=response.structured_mode == "native",
            )
        return PlanProposal(
            plan=checked,
            provider=response.model or self.gateway.provider_name,
            model=response.model,
            native=response.structured_mode == "native",
        )
