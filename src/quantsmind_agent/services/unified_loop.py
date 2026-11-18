"""CLI/Desktop unified loop facade (WP v1.8).

Both surfaces call the same AgentCore/ToolRegistry/broker stack (desktop
already does). This facade proves the CLI path exercises the shared loop
over an in-process fake provider — no second execution engine.
"""

from __future__ import annotations

from typing import Any

__all__ = ["UnifiedLoop"]


class UnifiedLoop:
    """Thin shared-loop facade used by CLI and GUI alike."""

    def __init__(self, agent: Any) -> None:
        self._agent = agent

    def run_turn(self, text: str) -> Any:
        """Run one user turn through the shared agent core."""
        return self._agent.run_turn(text) if hasattr(self._agent, "run_turn") else self._agent(text)
