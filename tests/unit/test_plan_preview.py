"""v1.6 plan preview tests."""

from __future__ import annotations

from quantsmind_agent.services.plan_preview import preview_plan
from quantsmind_agent.services.tasks.models import Plan, PlannedStep, RiskLevel


def _plan(deps=None):
    return Plan(
        objective="objective",
        steps=[
            PlannedStep(
                id="a",
                description="a",
                tool="write_file",
                arguments={"path": "a.txt"},
                risk=RiskLevel.LOW,
                expected_paths=("a.txt",),
            ),
            PlannedStep(
                id="b",
                description="b",
                tool="write_file",
                arguments={"path": "b.txt"},
                depends_on=("a",),
                risk=RiskLevel.LOW,
                expected_paths=("b.txt",),
            ),
        ],
    )


def test_preview_valid_and_cost() -> None:
    preview = preview_plan(_plan())
    assert preview.valid and preview.step_ids == ["a", "b"]
    assert preview.files == ["a.txt", "b.txt"]
    assert preview.cost_usd >= 0
    assert preview.tier


def test_preview_invalid_cycle() -> None:

    bad = Plan(
        objective="objective",
        steps=[
            PlannedStep(id="a", description="a", tool="x", depends_on=("b",)),
            PlannedStep(id="b", description="b", tool="x", depends_on=("a",)),
        ],
    )
    preview = preview_plan(bad)
    assert not preview.valid
    assert preview.errors
