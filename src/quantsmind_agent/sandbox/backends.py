"""Capability-based isolation backends (M3B-S1.4A).

The runner does not *know* how a boundary is established; it asks the
available backends which level each can reach, and it uses a backend only
when that backend's level satisfies the policy. There is deliberately no
"best effort" backend: a policy that demands ``untrusted_code`` is either
served by a backend that can prove that level, or it is refused.

The backends, in the order the milestone prefers them:

``AppContainerBackend``
    The full boundary: a different security context, no capabilities (and
    therefore no network), and a filesystem limited to what the ACLs grant.
    Requires an activated profile and a token that may create tokens.

``JobObjectBackend``
    The lifecycle boundary: process-tree containment and resource limits.
    It is always available, and it is **not** a confidentiality boundary, so
    it can never satisfy an ``untrusted_code`` policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from quantsmind_agent.sandbox.appcontainer import (
    AppContainerCapability,
    detect_appcontainer_capability,
)
from quantsmind_agent.sandbox.policy import (
    ISOLATION_LEVELS,
    IsolationLevel,
    SandboxPolicy,
    SandboxResult,
)
from quantsmind_agent.sandbox.windows import launch, sandbox_supported

__all__ = [
    "AppContainerBackend",
    "BackendAvailability",
    "IsolationBackend",
    "IsolationEvidence",
    "JobObjectBackend",
    "select_backend",
]


def _level_rank(level: IsolationLevel) -> int:
    return ISOLATION_LEVELS.index(level)


@dataclass(frozen=True, slots=True)
class BackendAvailability:
    """What one backend can provide on this host, right now."""

    name: str
    available: bool
    level: IsolationLevel
    reason: str = ""
    missing_requirements: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "available": self.available,
            "level": str(self.level),
            "reason": self.reason,
            "missing_requirements": list(self.missing_requirements),
        }


@dataclass(frozen=True, slots=True)
class IsolationEvidence:
    """What a backend's own verification established."""

    level: IsolationLevel
    checks: dict[str, bool] = field(default_factory=dict)
    detail: str = ""

    def satisfies(self, required: IsolationLevel) -> bool:
        if _level_rank(self.level) < _level_rank(required):
            return False
        return all(self.checks.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": str(self.level),
            "checks": dict(self.checks),
            "detail": self.detail,
        }


@runtime_checkable
class IsolationBackend(Protocol):
    """What every backend must offer."""

    name: str

    def availability(self, policy: SandboxPolicy) -> BackendAvailability:
        """What this backend can provide for this policy, on this host."""

    def evidence(self) -> IsolationEvidence:
        """What this backend has established."""

    def launch(
        self,
        policy: SandboxPolicy,
        *,
        cancel_event: Any = None,
    ) -> SandboxResult:
        """Run the policy's command inside the boundary."""


class JobObjectBackend:
    """The lifecycle boundary: the job object, with the user's own token.

    It contains the process tree and caps the resources. It does **not**
    provide confidentiality, and it says so in its availability, so it can
    never be selected for an ``untrusted_code`` policy.
    """

    name = "job_object"

    def availability(self, policy: SandboxPolicy) -> BackendAvailability:
        if not sandbox_supported():
            return BackendAvailability(
                name=self.name,
                available=False,
                level=IsolationLevel.LIFECYCLE,
                reason="the job object boundary is only available on Windows",
            )
        return BackendAvailability(
            name=self.name,
            available=True,
            level=IsolationLevel.LIFECYCLE,
            reason=(
                "the process tree is contained and capped, but the child keeps the "
                "desktop user's token, so the credential store, the user's files and "
                "the network remain reachable"
            ),
        )

    def evidence(self) -> IsolationEvidence:
        return IsolationEvidence(
            level=IsolationLevel.LIFECYCLE,
            checks={},
            detail="lifecycle controls only; no confidentiality property is claimed",
        )

    def launch(
        self,
        policy: SandboxPolicy,
        *,
        cancel_event: Any = None,
    ) -> SandboxResult:
        result = launch(policy, cancel_event=cancel_event)
        return SandboxResult(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=result.timed_out,
            cancelled=result.cancelled,
            isolation_verified=result.isolation_verified,
            established_level=str(IsolationLevel.LIFECYCLE),
            failure_reason=result.failure_reason,
            duration_seconds=result.duration_seconds,
            truncated=result.truncated,
        )


class AppContainerBackend:
    """The full boundary: an AppContainer token with no capabilities."""

    name = "appcontainer"

    def __init__(self, capability: AppContainerCapability | None = None) -> None:
        self._capability = capability or detect_appcontainer_capability()

    @property
    def capability(self) -> AppContainerCapability:
        return self._capability

    def availability(self, policy: SandboxPolicy) -> BackendAvailability:
        if not self._capability.available:
            return BackendAvailability(
                name=self.name,
                available=False,
                level=IsolationLevel.LIFECYCLE,
                reason=self._capability.reason,
                missing_requirements=self._capability.missing_requirements,
            )
        return BackendAvailability(
            name=self.name,
            available=True,
            level=IsolationLevel.UNTRUSTED_CODE,
            reason=(
                "the AppContainer profile is registered and this token may create "
                "an AppContainer token"
            ),
        )

    def evidence(self) -> IsolationEvidence:
        return IsolationEvidence(
            level=IsolationLevel.UNTRUSTED_CODE,
            checks={},
            detail=(
                "the child runs in an AppContainer with no capabilities; the "
                "confidentiality properties are established by the in-child probe, "
                "not by the parent's configuration"
            ),
        )

    def launch(
        self,
        policy: SandboxPolicy,
        *,
        cancel_event: Any = None,
    ) -> SandboxResult:
        # The launch path needs the AppContainer attribute list, which the
        # M3B-S1.4A implementation provides. Until the profile is activated
        # the capability detection refuses, so this is never reached with an
        # unactivated host.
        availability = self.availability(policy)
        if not availability.available:
            return SandboxResult(
                exit_code=None,
                stdout="",
                stderr=availability.reason,
                isolation_verified=False,
                established_level=str(IsolationLevel.LIFECYCLE),
                failure_reason="backend_unavailable",
            )
        return launch(policy, cancel_event=cancel_event, use_appcontainer=True)


def select_backend(
    policy: SandboxPolicy,
    backends: list[Any] | None = None,
) -> tuple[Any | None, list[BackendAvailability]]:
    """The backend that can satisfy the policy, and every backend's answer.

    A backend is selected only when its availability says it can reach the
    policy's required level. The job object backend can never satisfy an
    ``untrusted_code`` policy, so there is no insecure fall back.
    """

    candidates: list[Any] = (
        list(backends) if backends is not None else [AppContainerBackend(), JobObjectBackend()]
    )
    report: list[BackendAvailability] = []
    chosen = None
    for backend in candidates:
        availability = backend.availability(policy)
        report.append(availability)
        if (
            chosen is None
            and availability.available
            and _level_rank(availability.level) >= _level_rank(policy.required_isolation)
        ):
            chosen = backend
    return chosen, report
