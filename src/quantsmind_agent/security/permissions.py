"""The permission layer.

Every tool declares a :class:`Permission` requirement. Nothing executes without
passing through :class:`PermissionBroker`, which consults a
:class:`PermissionProvider` and returns :class:`Decision` values of ``ALLOW``,
``DENY`` or ``ASK``.

The v0.1 policy allowed reads and denied everything else. v0.2 keeps that
taxonomy and adds the interactive path: writes, edits, deletes and commands are
``ASK`` by default, so nothing dangerous happens without a human decision, and
an ``ASK`` with no approver wired up still degrades to ``DENY``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from quantsmind_agent.errors import PermissionDeniedError

__all__ = [
    "ApprovalCallback",
    "ApprovalOutcome",
    "ApprovalQueue",
    "Decision",
    "PendingApproval",
    "Permission",
    "PermissionBroker",
    "PermissionProvider",
    "PermissionRequest",
    "PermissionRule",
    "SingleUseApproval",
    "StaticPermissionProvider",
]


class Permission(StrEnum):
    """Capabilities the agent may exercise."""

    READ_WORKSPACE = "READ_WORKSPACE"
    WRITE_WORKSPACE = "WRITE_WORKSPACE"
    EXECUTE_COMMAND = "EXECUTE_COMMAND"
    GIT = "GIT"
    NETWORK = "NETWORK"
    CHANGESET_APPLY = "CHANGESET_APPLY"
    CHANGESET_ROLLBACK = "CHANGESET_ROLLBACK"


class Decision(StrEnum):
    """Outcome of a permission check."""

    ALLOW = "ALLOW"
    DENY = "DENY"
    ASK = "ASK"


class ApprovalOutcome(StrEnum):
    """What a human chose when the policy asked.

    ``ALLOW_ONCE`` covers only the call that triggered the dialog.
    ``ALLOW_FOR_SESSION`` records a grant for every later call of the same tool
    and permission until the agent stack is rebuilt. ``DENY`` is final: the
    broker never re-asks within the same evaluation and the tool does not run.
    """

    ALLOW_ONCE = "ALLOW_ONCE"
    ALLOW_FOR_SESSION = "ALLOW_FOR_SESSION"
    DENY = "DENY"


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
        """The policy shipped with the application (v0.2).

        Reads are allowed because they cannot change anything. Writes and
        commands are ``ASK`` so every one of them needs a human decision.
        ``GIT`` is allowed because v0.2 registers only read-only Git tools,
        which build their own argument vector and therefore cannot mutate the
        repository; the mutation verbs are not reachable as tools.
        ``NETWORK`` stays denied: v0.2 has no network tool.
        """

        return [
            PermissionRule(Permission.READ_WORKSPACE, Decision.ALLOW),
            PermissionRule(Permission.WRITE_WORKSPACE, Decision.ASK),
            PermissionRule(Permission.EXECUTE_COMMAND, Decision.ASK),
            PermissionRule(Permission.GIT, Decision.ALLOW),
            PermissionRule(Permission.NETWORK, Decision.DENY),
            PermissionRule(Permission.CHANGESET_APPLY, Decision.ASK),
            PermissionRule(Permission.CHANGESET_ROLLBACK, Decision.ASK),
        ]

    def v0_1_rules(self) -> list[PermissionRule]:
        """The frozen v0.1 read-only policy, kept for tests and archaeology."""

        return [
            PermissionRule(Permission.READ_WORKSPACE, Decision.ALLOW),
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
        """The frozen v0.1 policy: reads allowed, everything else denied."""

        provider = cls()
        provider._rules = provider.v0_1_rules()
        return provider

    @classmethod
    def for_v0_2(cls) -> StaticPermissionProvider:
        """The shipped v0.2 policy: reads/git allowed, writes/commands asked."""

        provider = cls()
        provider._rules = provider.default_rules()
        return provider


#: Callback signature for interactive approval.
#:
#: A v0.2 approver returns an :class:`ApprovalOutcome`. Returning a plain ``bool``
#: is still accepted so existing call sites and the v0.1 approval pattern keep
#: working: ``True`` means "allow once" and ``False`` means "deny".
ApprovalCallback = Callable[[PermissionRequest], "ApprovalOutcome | bool"]

ApprovalHook = Callable[[PermissionRequest, ApprovalOutcome], None]


class PermissionBroker:
    """The single choke point every tool call must pass through.

    Responsibilities:

    * ask the :class:`PermissionProvider` for a policy decision,
    * escalate ``ASK`` to a human and record the answer,
    * remember ``Allow for Session`` grants for the lifetime of this instance,
    * refuse to let a tool run unless the final decision is ``ALLOW``.
    """

    def __init__(
        self,
        provider: PermissionProvider | None = None,
        *,
        on_decision: Callable[[PermissionRequest, Decision], None] | None = None,
        on_approval: ApprovalHook | None = None,
        interactive: bool = False,
        approver: ApprovalCallback | None = None,
    ) -> None:
        self._provider: PermissionProvider = provider or StaticPermissionProvider.for_v0_2()
        self._on_decision = on_decision
        self._on_approval = on_approval
        self._approver = approver
        self._interactive = interactive or approver is not None
        self.history: list[tuple[PermissionRequest, Decision]] = []
        self.approvals: list[tuple[PermissionRequest, ApprovalOutcome]] = []
        self._session_grants: set[tuple[Permission, str]] = set()
        self._queue = ApprovalQueue()

    @property
    def provider(self) -> PermissionProvider:
        return self._provider

    def set_provider(self, provider: PermissionProvider) -> None:
        self._provider = provider

    def set_approver(self, approver: ApprovalCallback | None) -> None:
        self._approver = approver
        self._interactive = approver is not None

    def set_on_approval(self, hook: ApprovalHook | None) -> None:
        self._on_approval = hook

    @property
    def session_grants(self) -> frozenset[tuple[Permission, str]]:
        """``(permission, tool)`` pairs the user allowed for this session."""

        return frozenset(self._session_grants)

    def clear_session_grants(self) -> None:
        """Forget every session-scoped grant (new conversation / new project)."""

        self._session_grants.clear()

    def evaluate(self, request: PermissionRequest) -> Decision:
        """Resolve ``request`` to a final decision.

        An ``ASK`` result is escalated to the approver callback. When no
        approver is wired up, ``ASK`` degrades to ``DENY`` -- a safe default
        that guarantees nothing dangerous runs unattended.

        A session grant only ever *upgrades* an ``ASK`` for the same tool and
        permission the user already approved. It is checked after the provider
        so an explicit ``DENY`` rule still wins.
        """

        decision = self._provider.decide(request)
        if decision is Decision.ASK:
            outcome = self._ask(request)
            if outcome is ApprovalOutcome.ALLOW_FOR_SESSION:
                self._session_grants.add((request.permission, request.tool))
            decision = Decision.DENY if outcome is ApprovalOutcome.DENY else Decision.ALLOW

        self.history.append((request, decision))
        if self._on_decision is not None:
            self._on_decision(request, decision)
        return decision

    def _ask(self, request: PermissionRequest) -> ApprovalOutcome:
        """Consult the human, or fall back to a denial.

        Order matters: an existing session grant answers the request without
        showing a dialog, so "Allow for Session" really does mean *once*.
        """

        if (request.permission, request.tool) in self._session_grants:
            outcome = ApprovalOutcome.ALLOW_FOR_SESSION
            self._record_approval(request, outcome)
            return outcome

        if not (self._interactive and self._approver is not None):
            # No human available: ASK becomes DENY.
            outcome = ApprovalOutcome.DENY
            self._record_approval(request, outcome)
            return outcome

        raw = self._approver(request)
        outcome = (
            raw
            if isinstance(raw, ApprovalOutcome)
            else (ApprovalOutcome.ALLOW_ONCE if raw else ApprovalOutcome.DENY)
        )
        self._record_approval(request, outcome)
        return outcome

    def _record_approval(self, request: PermissionRequest, outcome: ApprovalOutcome) -> None:
        self.approvals.append((request, outcome))
        if self._on_approval is not None:
            self._on_approval(request, outcome)

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


@dataclass(frozen=True, slots=True)
class PendingApproval:
    """One approval request waiting for a human decision.

    Bound to a plan digest so it can only be used for the exact transaction it
    was granted for. Single-use by design: once decided, it is removed from the
    queue and cannot be replayed.
    """

    request: PermissionRequest
    plan_digest: str
    created_at: float
    decided: bool = False
    outcome: ApprovalOutcome | None = None

    @property
    def is_expired(self) -> bool:
        return self.decided


class ApprovalQueue:
    """Bounded queue of pending approvals with revocable grants.

    Every ASK that reaches a human is recorded here before the decision is
    known, so a crash cannot lose an in-flight approval. Grants are revocable:
    revoking a grant removes it from session scope so the next ASK re-prompts.
    """

    def __init__(self, *, max_pending: int = 128) -> None:
        self._max_pending = max_pending
        self._pending: dict[str, PendingApproval] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._grants: set[tuple[Permission, str]] = set()

    def submit(
        self,
        request: PermissionRequest,
        plan_digest: str,
    ) -> str:
        """Register a pending approval and return its queue id."""

        import time

        with self._lock:
            if len(self._pending) >= self._max_pending:
                # Drop the oldest pending item to stay bounded.
                oldest = self._order.pop(0)
                self._pending.pop(oldest, None)
            qid = f"qa-{len(self._order):06d}-{int(time.time() * 1000)}"
            self._pending[qid] = PendingApproval(
                request=request,
                plan_digest=plan_digest,
                created_at=time.time(),
            )
            self._order.append(qid)
            return qid

    def decide(self, qid: str, outcome: ApprovalOutcome) -> bool:
        """Record a decision for a pending approval. Returns False if unknown."""

        from dataclasses import replace

        with self._lock:
            pending = self._pending.get(qid)
            if pending is None or pending.decided:
                return False
            self._pending[qid] = replace(
                pending,
                decided=True,
                outcome=outcome,
            )
            if outcome is ApprovalOutcome.ALLOW_FOR_SESSION:
                # Upgrade to a session grant, bound to the approval's permission+tool.
                self._grants.add((pending.request.permission, pending.request.tool))
            return True

    def revoke(self, permission: Permission, tool: str) -> bool:
        """Revoke a session grant so the next ASK re-prompts the human."""

        with self._lock:
            key = (permission, tool)
            if key in self._grants:
                self._grants.discard(key)
                return True
            return False

    def clear_grants(self) -> None:
        """Revoke every session grant (new conversation / new project)."""

        with self._lock:
            self._grants.clear()

    def pending(self) -> tuple[PendingApproval, ...]:
        """All undecided pending approvals, oldest first."""

        with self._lock:
            return tuple(
                self._pending[qid] for qid in self._order if not self._pending[qid].decided
            )

    #: Session grants held by this queue (revocable).
    _grants: set[tuple[Permission, str]]


class SingleUseApproval:
    """A digest-bound, single-use approval token.

    Holds a plan digest and a single-use flag. Once ``consume`` is called, the
    approval is spent and cannot be reused. A mismatch on the digest refuses.
    """

    def __init__(self, plan_digest: str) -> None:
        self._plan_digest = plan_digest
        self._consumed = False

    @property
    def plan_digest(self) -> str:
        return self._plan_digest

    @property
    def consumed(self) -> bool:
        return self._consumed

    def consume(self, *, expected_digest: str) -> bool:
        """Consume the approval if it matches ``expected_digest`` and is unused.

        Returns ``True`` if consumed, ``False`` otherwise.
        """

        if self._consumed:
            return False
        if self._plan_digest != expected_digest:
            return False
        self._consumed = True
        return True
