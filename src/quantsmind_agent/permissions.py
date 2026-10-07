"""The permission layer.

Every tool declares a :class:`Permission` requirement. Nothing executes without
passing through :class:`PermissionBroker`, which consults a
:class:`PermissionProvider` and returns :class:`Decision` values of ``ALLOW``,
``DENY`` or ``ASK``.

In v0.1 the only *active* permission is ``READ_WORKSPACE``; the remaining
permissions exist in the taxonomy so v0.2 can add writes, commands, git and
network access without touching call sites.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from quantsmind_agent.errors import PermissionDeniedError

__all__ = [
    "Decision",
    "Permission",
    "PermissionBroker",
    "PermissionProvider",
    "PermissionRequest",
    "PermissionRule",
    "StaticPermissionProvider",
]


class Permission(StrEnum):
    """Capabilities the agent may exercise."""

    READ_WORKSPACE = "READ_WORKSPACE"
    WRITE_WORKSPACE = "WRITE_WORKSPACE"
    EXECUTE_COMMAND = "EXECUTE_COMMAND"
    GIT = "GIT"
    NETWORK = "NETWORK"


class Decision(StrEnum):
    """Outcome of a permission check."""

    ALLOW = "ALLOW"
    DENY = "DENY"
    ASK = "ASK"


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    """A concrete capability use the agent wants to perform."""

    permission: Permission
    tool: str
    summary: str
    target: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PermissionRule:
    """A policy entry: allow/ask/deny a permission for a tool or target glob."""

    permission: Permission
    decision: Decision
    tool: str = "*"
    target_pattern: str = "*"


@runtime_checkable
class PermissionProvider(Protocol):
    """Strategy that answers permission questions."""

    def decide(self, request: PermissionRequest) -> Decision:
        """Return the decision for ``request``."""


class StaticPermissionProvider:
    """Rule-list provider evaluated in first-match order.

    Rules are matched by exact ``tool`` first, then by ``"*"``. Within a tool,
    the ``target_pattern`` is matched with :func:`fnmatch.fnmatch` so callers can
    allow (for example) all reads under ``src/`` while asking for anything else.
    """

    def __init__(self, rules: list[PermissionRule] | None = None) -> None:
        self._rules: list[PermissionRule] = list(rules or [])

    def add_rule(self, rule: PermissionRule) -> None:
        self._rules.append(rule)

    @property
    def rules(self) -> tuple[PermissionRule, ...]:
        return tuple(self._rules)

    def default_rules(self) -> list[PermissionRule]:
        """The v0.1 policy shipped with the application."""

        return [
            PermissionRule(Permission.READ_WORKSPACE, Decision.ALLOW),
            # Everything else is interactive/denied until v0.2 ships the tools.
            PermissionRule(Permission.WRITE_WORKSPACE, Decision.DENY),
            PermissionRule(Permission.EXECUTE_COMMAND, Decision.DENY),
            PermissionRule(Permission.GIT, Decision.DENY),
            PermissionRule(Permission.NETWORK, Decision.DENY),
        ]

    def decide(self, request: PermissionRequest) -> Decision:
        import fnmatch

        ordered = sorted(
            self._rules,
            key=lambda rule: (rule.tool != request.tool, rule.target_pattern == "*"),
        )
        for rule in ordered:
            if rule.permission is not request.permission:
                continue
            if rule.tool not in ("*", request.tool):
                continue
            if rule.target_pattern not in ("*", request.target) and not fnmatch.fnmatch(
                request.target or "", rule.target_pattern
            ):
                continue
            return rule.decision
        return Decision.DENY

    @classmethod
    def for_v0_1(cls) -> StaticPermissionProvider:
        provider = cls()
        provider._rules = provider.default_rules()
        return provider


#: Callback signature for interactive approval: takes a request, returns a bool.
ApprovalCallback = Callable[[PermissionRequest], bool]


class PermissionBroker:
    """The single choke point every tool call must pass through."""

    def __init__(
        self,
        provider: PermissionProvider | None = None,
        *,
        on_decision: Callable[[PermissionRequest, Decision], None] | None = None,
        interactive: bool = False,
        approver: ApprovalCallback | None = None,
    ) -> None:
        self._provider: PermissionProvider = provider or StaticPermissionProvider.for_v0_1()
        self._on_decision = on_decision
        self._approver = approver
        self._interactive = interactive or approver is not None
        self.history: list[tuple[PermissionRequest, Decision]] = []

    @property
    def provider(self) -> PermissionProvider:
        return self._provider

    def set_provider(self, provider: PermissionProvider) -> None:
        self._provider = provider

    def set_approver(self, approver: ApprovalCallback | None) -> None:
        self._approver = approver
        self._interactive = approver is not None

    def evaluate(self, request: PermissionRequest) -> Decision:
        """Resolve ``request`` to a final decision.

        An ``ASK`` result is escalated to the approver callback. When no
        approver is wired up, ``ASK`` degrades to ``DENY`` -- a safe default
        that guarantees nothing dangerous runs unattended.
        """

        decision = self._provider.decide(request)
        if decision is Decision.ASK:
            if self._interactive and self._approver is not None:
                decision = Decision.ALLOW if self._approver(request) else Decision.DENY
            else:
                decision = Decision.DENY

        self.history.append((request, decision))
        if self._on_decision is not None:
            self._on_decision(request, decision)
        return decision

    def require(self, request: PermissionRequest) -> None:
        """Evaluate ``request`` and raise when it is not allowed."""

        decision = self.evaluate(request)
        if decision is not Decision.ALLOW:
            reason = (
                "explicitly denied by policy"
                if decision is Decision.DENY
                else "requires interactive approval"
            )
            raise PermissionDeniedError(
                str(request.permission),
                reason,
                details={
                    "tool": request.tool,
                    "target": request.target,
                    "summary": request.summary,
                },
            )
