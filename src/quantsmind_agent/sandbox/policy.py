"""Execution policy, result and error types for the sandbox (M3B-S1.4).

The sandbox is *fail-closed by construction*. A :class:`SandboxPolicy`
states the isolation the caller requires; the runner establishes what it
can, measures what was actually enforced, and refuses to run the command if
the measurement falls short. There is no "best effort" path that silently
falls back to an ordinary subprocess.

The isolation levels are ordered, and each one demands strictly more than
the one before it:

``lifecycle``
    Process-tree containment and resource limits: the child cannot outlive
    the sandbox, cannot spawn an unbounded number of processes, and cannot
    exceed the time, memory or output budget. **No confidentiality
    guarantee.** The child still runs as the desktop user and can read that
    user's files.

``untrusted_code``
    The level required to execute code the model wrote. It additionally
    demands confidentiality: the credential store, the user's files and the
    network must be unreachable. A host that cannot establish this must
    refuse the execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

__all__ = [
    "ISOLATION_LEVELS",
    "IsolationLevel",
    "SandboxError",
    "SandboxPolicy",
    "SandboxResult",
]


class IsolationLevel(StrEnum):
    """How much isolation a policy demands."""

    LIFECYCLE = "lifecycle"
    """Process-tree containment, resource limits, controlled environment."""

    UNTRUSTED_CODE = "untrusted_code"
    """The level required to execute model-written code: confidentiality too."""


#: The ordered levels, weakest first.
ISOLATION_LEVELS = (IsolationLevel.LIFECYCLE, IsolationLevel.UNTRUSTED_CODE)


class SandboxError(RuntimeError):
    """A sandbox operation could not be completed safely.

    Carries a machine-readable ``reason`` so the failure can be audited and
    reported without leaking anything about the host.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        detail: str = "",
        required_level: str = "",
        established_level: str = "",
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.detail = detail
        self.required_level = required_level
        self.established_level = established_level

    def to_dict(self) -> dict[str, str]:
        return {
            "reason": self.reason,
            "detail": self.detail,
            "required_level": self.required_level,
            "established_level": self.established_level,
        }


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    """The complete execution policy.

    The command is a tuple, so it cannot be assembled from a string, and it
    is fixed before the approval is granted: the policy is what the broker
    approves, and nothing can change it afterwards.
    """

    workspace_root: Path
    allowed_command: tuple[str, ...]
    timeout_seconds: float = 120.0
    max_output_bytes: int = 200_000
    max_child_processes: int = 8
    max_memory_bytes: int = 1_073_741_824
    network_allowed: bool = False
    required_isolation: IsolationLevel = IsolationLevel.LIFECYCLE
    #: Environment variables the child must not inherit.
    stripped_environment: tuple[str, ...] = ()
    #: Environment variables the child must have, with their values.
    injected_environment: dict[str, str] = field(default_factory=dict)
    #: A human-readable reason, recorded in the audit trail.
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.allowed_command:
            raise SandboxError(
                "a sandbox policy needs a command",
                reason="empty_command",
            )
        if self.timeout_seconds <= 0:
            raise SandboxError(
                "a sandbox policy needs a positive timeout",
                reason="invalid_timeout",
            )
        if self.max_output_bytes <= 0:
            raise SandboxError(
                "a sandbox policy needs a positive output limit",
                reason="invalid_output_limit",
            )
        if self.max_child_processes < 1:
            raise SandboxError(
                "a sandbox policy needs a positive child-process limit",
                reason="invalid_child_limit",
            )

    @property
    def command(self) -> tuple[str, ...]:
        return self.allowed_command

    def to_dict(self) -> dict[str, Any]:
        """An audit-safe description. The command is included; the reason is not a secret."""

        return {
            "workspace_root": str(self.workspace_root),
            "allowed_command": list(self.allowed_command),
            "timeout_seconds": self.timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
            "max_child_processes": self.max_child_processes,
            "max_memory_bytes": self.max_memory_bytes,
            "network_allowed": self.network_allowed,
            "required_isolation": str(self.required_isolation),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class SandboxResult:
    """The outcome of one sandboxed execution."""

    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False
    isolation_verified: bool = False
    established_level: str = ""
    failure_reason: str = ""
    duration_seconds: float = 0.0
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return (
            self.exit_code == 0
            and not self.timed_out
            and not self.cancelled
            and self.isolation_verified
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "isolation_verified": self.isolation_verified,
            "established_level": self.established_level,
            "failure_reason": self.failure_reason,
            "duration_seconds": round(self.duration_seconds, 2),
            "truncated": self.truncated,
        }
