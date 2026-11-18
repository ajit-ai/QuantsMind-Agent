"""Performance governance hook (WP v1.4).

Pulls the existing resource governor into tool budgets: callers pass the
current pressure and receive clamped timeouts/output caps. No new threads.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["GovernedBudget", "govern_budget"]


@dataclass(frozen=True, slots=True)
class GovernedBudget:
    timeout_seconds: float
    max_output_chars: int
    pressure: str


_PRESSURE_FACTORS = {
    "low": (1.0, 1.0),
    "moderate": (0.5, 0.75),
    "high": (0.25, 0.5),
    "critical": (0.1, 0.25),
}


def govern_budget(
    *,
    timeout_seconds: float = 120.0,
    max_output_chars: int = 20000,
    pressure: str = "low",
) -> GovernedBudget:
    temp, out = _PRESSURE_FACTORS.get(pressure, (1.0, 1.0))
    return GovernedBudget(
        timeout_seconds=max(1.0, min(600.0, timeout_seconds * temp)),
        max_output_chars=max(2000, int(max_output_chars * out)),
        pressure=pressure,
    )
