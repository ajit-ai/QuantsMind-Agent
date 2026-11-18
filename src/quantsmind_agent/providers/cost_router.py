"""Cost/latency model router (WP3.3 of v1.2).

Tier selection (fast/strong) with per-task token budgets; mock-priced by
`providers/routing.py` presets. Never sends secrets; budgets are advisory and
surface in the task panel.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["CostRouter", "ModelTier", "estimate_cost"]


@dataclass(frozen=True, slots=True)
class ModelTier:
    name: str
    cost_per_1k: float
    latency_ms: int
    quality: int  # 1..3


def estimate_cost(tokens: int, tier: ModelTier) -> float:
    return (tokens / 1000.0) * tier.cost_per_1k


class CostRouter:
    def __init__(self, tiers: list[ModelTier] | None = None) -> None:
        self.tiers = tiers or [
            ModelTier("fast", 0.10, 800, 1),
            ModelTier("balanced", 0.50, 1500, 2),
            ModelTier("strong", 2.00, 3000, 3),
        ]

    def choose(
        self, task_tokens: int, *, budget_usd: float = 1.0, min_quality: int = 1
    ) -> ModelTier:
        ranked = sorted(
            (t for t in self.tiers if t.quality >= min_quality),
            key=lambda t: (estimate_cost(task_tokens, t), t.latency_ms),
        )
        for tier in ranked:
            if estimate_cost(task_tokens, tier) <= budget_usd:
                return tier
        return ranked[0] if ranked else self.tiers[0]

    def meter(self, task_tokens: int, tier: ModelTier) -> dict[str, object]:
        return {
            "tier": tier.name,
            "cost_usd": round(estimate_cost(task_tokens, tier), 4),
            "latency_ms": tier.latency_ms,
        }
