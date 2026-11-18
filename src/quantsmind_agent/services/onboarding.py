"""First-run onboarding (WP2.4 of v1.1)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

__all__ = ["OnboardingState", "OnboardingStep", "OnboardingStore"]


class Step(str):
    PROVIDER = "provider"
    TRUST = "trust"
    PROFILE = "profile"


OnboardingStep = Step


@dataclass
class OnboardingState:
    provider_done: bool = False
    trust_done: bool = False
    profile_done: bool = False

    @property
    def current(self) -> str:
        if not self.provider_done:
            return Step.PROVIDER
        if not self.trust_done:
            return Step.TRUST
        if not self.profile_done:
            return Step.PROFILE
        return "done"

    @property
    def complete(self) -> bool:
        return self.current == "done"


class OnboardingStore:
    def __init__(self, root: str | Path, *, root_id: str = "primary") -> None:
        self._path = Path(root) / ".quantsmind" / "onboarding.json"

    def load(self) -> OnboardingState:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return OnboardingState(
                **{
                    k: bool(v)
                    for k, v in data.items()
                    if k in ("provider_done", "trust_done", "profile_done")
                }
            )
        except (OSError, json.JSONDecodeError, TypeError):
            return OnboardingState()

    def save(self, state: OnboardingState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(
                {
                    "provider_done": state.provider_done,
                    "trust_done": state.trust_done,
                    "profile_done": state.profile_done,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
