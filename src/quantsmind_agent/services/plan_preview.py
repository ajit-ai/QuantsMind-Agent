"""Async plan preview / what-if dry run (WP v1.6).

Validates a plan and estimates cost/files without executing anything.
Read-only: no registry execution, no file writes, no mutation.
"""

from __future__ import annotations

from dataclasses import dataclass

from quantsmind_agent.providers.cost_router import CostRouter
from quantsmind_agent.services.tasks.models import Plan, PlanRejected, validate_plan

__all__ = ["PlanPreview", "preview_plan"]


@dataclass(frozen=True, slots=True)
class PlanPreview:
    step_ids: list[str]
    files: list[str]
    estimated_tokens: int
    tier: str
    cost_usd: float
    valid: bool
    errors: tuple[str, ...] = ()


def preview_plan(
    plan: Plan, *, task_tokens: int = 2000, known_tools: set[str] | None = None
) -> PlanPreview:
    """Validate + estimate a plan without executing it."""
    try:
        validate_plan(plan, known_tools=known_tools)
    except PlanRejected as exc:
        return PlanPreview([], [], 0, "", 0.0, False, tuple(exc.reasons))
    router = CostRouter()
    tier = router.choose(task_tokens, budget_usd=1.0)
    files = sorted({p for step in plan.steps for p in (getattr(step, "expected_paths", ()) or ())})
    meter = router.meter(task_tokens, tier)
    raw_cost = meter.get("cost_usd", 0.0)
    cost = float(raw_cost) if isinstance(raw_cost, (int, float)) else 0.0
    return PlanPreview(
        step_ids=[s.id for s in plan.steps],
        files=files,
        estimated_tokens=task_tokens,
        tier=tier.name,
        cost_usd=round(cost, 4),
        valid=True,
    )
