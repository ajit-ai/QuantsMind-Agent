"""The ChangeSet lifecycle: states, the legal moves between them, and who may make them.

The specification (QM-P09-CS-001 §4) is the authority for what follows. Two of its
rules shaped the shape of this module:

- **The table is the law.** A transition that is not listed is refused, and the
  service never coerces a state to make an operation succeed. A ChangeSet that
  cannot go where the caller wants stays exactly where it was.
- **The state is not a field.** Anything that can write `status` directly can
  produce a lifecycle the table forbids, so the status lives behind
  :class:`ChangeSetLifecycle` and changes only through its ``transition`` method,
  which is also where every move gets its audit and recovery metadata.

ChangeSet lifecycle is deliberately independent of task lifecycle: a task may be
blocked while its ChangeSet is APPLIED, and a standalone ChangeSet has no task at
all. Nothing here imports :mod:`quantsmind_agent.services.tasks`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

__all__ = [
    "AMEND_TRIGGERS",
    "CANCELLED_STATES",
    "FILE_EVIDENCE_STATES",
    "TERMINAL_STATES",
    "TRANSITION_TABLE",
    "UNSTABLE_STATES",
    "ChangeSetLifecycle",
    "ChangeSetStatus",
    "InvalidTransition",
    "TransitionFacts",
    "TransitionGuardFailed",
    "TransitionRecord",
    "Trigger",
    "can_transition",
    "expected_trigger",
]


class ChangeSetStatus(StrEnum):
    """The seventeen lifecycle states of a ChangeSet (§4.1), by their specified names.

    The names matter as much as the set: state strings are written into manifests,
    journals and the audit trail, and a renamed state breaks every record that
    predates the rename. ``INTERRUPTED`` is not one of them -- an interrupted apply
    is an ``APPLYING`` record that startup recovery has to explain, not a state the
    engine gets to hide behind.
    """

    DRAFT = "draft"
    PROPOSED = "proposed"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    APPLYING = "applying"
    APPLIED = "applied"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    PARTIALLY_APPLIED = "partially_applied"
    FAILED = "failed"
    CONFLICTED = "conflicted"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_FAILED = "rollback_failed"


#: States a crashed process can leave behind. Recovery must reconcile every one of
#: these before the UI offers a normal action (§4.3).
UNSTABLE_STATES: frozenset[ChangeSetStatus] = frozenset(
    {ChangeSetStatus.APPLYING, ChangeSetStatus.ROLLING_BACK, ChangeSetStatus.PARTIALLY_APPLIED}
)

#: The only states in which file evidence may exist (§4.3). Read as a promise:
#: outside this set there is nothing to show, and a page that renders a diff from
#: ``DRAFT`` is inventing it.
FILE_EVIDENCE_STATES: frozenset[ChangeSetStatus] = frozenset(
    {
        ChangeSetStatus.APPLIED,
        ChangeSetStatus.VERIFIED,
        ChangeSetStatus.FAILED,
        ChangeSetStatus.ROLLED_BACK,
        ChangeSetStatus.ROLLBACK_FAILED,
        ChangeSetStatus.PARTIALLY_APPLIED,
    }
)

#: Moves that can never be undone by another transition. ``FAILED`` is absent on
#: purpose: with accepted file evidence it still owes the user a rollback (§4.3).
TERMINAL_STATES: frozenset[ChangeSetStatus] = frozenset(
    {
        ChangeSetStatus.REJECTED,
        ChangeSetStatus.CANCELLED,
        ChangeSetStatus.SUPERSEDED,
        ChangeSetStatus.ROLLED_BACK,
    }
)

#: States that guarantee zero operations committed (§4.3). A transaction abandoned
#: from here needs no workspace reconciliation, which is why recovery can treat it
#: as settled.
CANCELLED_STATES: frozenset[ChangeSetStatus] = frozenset(
    {
        ChangeSetStatus.CONFLICTED,
        ChangeSetStatus.CANCELLED,
        ChangeSetStatus.SUPERSEDED,
    }
)


class Trigger(StrEnum):
    """Why a transition is being requested.

    Each legal move in §4.2 is named by exactly one trigger, and the engine checks
    the name. That is not bookkeeping for its own sake: "amend" carries three
    consequences of its own (revision++, recompute, clear approval), so accepting
    an arbitrary label on a move would let a caller dodge exactly those, or invoke
    them on a move that must not touch the revision.
    """

    AMEND = "amend"
    SUBMIT = "submit"
    REQUEST_APPROVAL = "request_approval"
    APPROVE = "approve"
    REJECT = "reject"
    CANCEL = "cancel"
    REPLACE = "replace"
    PREFLIGHT_PASS = "preflight_pass"
    PREFLIGHT_CONFLICT = "preflight_conflict"
    APPROVAL_REVOKED = "approval_revoked"
    ALL_OPS_COMMITTED = "all_ops_committed"
    PARTIAL_FAILURE = "partial_failure"
    NO_OP_COMMITTED = "no_op_committed"
    AUTOMATIC_ROLLBACK = "automatic_rollback"
    USER_ROLLBACK = "user_rollback"
    ACCEPT_PARTIAL = "accept_partial"
    VERIFICATION_REQUESTED = "verification_requested"
    VERIFICATION_PASS = "verification_pass"
    VERIFICATION_INCONCLUSIVE = "verification_inconclusive"
    VERIFICATION_FAIL = "verification_fail"
    RESTORE_VERIFIED = "restore_verified"
    RESTORE_UNSAFE = "restore_unsafe"
    ABORT_ROLLBACK = "abort_rollback"
    RETRY_ROLLBACK = "retry_rollback"


#: Triggers that amend the ChangeSet, and therefore must bump the revision and
#: invalidate any approval held against the old content (§4.3).
AMEND_TRIGGERS: frozenset[Trigger] = frozenset({Trigger.AMEND})

#: Every legal move, and the one trigger that causes it. Anything absent here is
#: illegal: the mapping is closed, not a blocklist.
#:
#: This table is the specification's §4.2 restated as data. It is the only place
#: where lifecycle is decided, so a question like "may this go back for
#: re-approval?" has one answer instead of one per caller.
TRANSITION_TABLE: Mapping[ChangeSetStatus, Mapping[ChangeSetStatus, Trigger]] = {
    ChangeSetStatus.DRAFT: {
        ChangeSetStatus.DRAFT: Trigger.AMEND,
        ChangeSetStatus.PROPOSED: Trigger.SUBMIT,
        ChangeSetStatus.CANCELLED: Trigger.CANCEL,
        ChangeSetStatus.SUPERSEDED: Trigger.REPLACE,
    },
    ChangeSetStatus.PROPOSED: {
        ChangeSetStatus.PROPOSED: Trigger.AMEND,
        ChangeSetStatus.AWAITING_APPROVAL: Trigger.REQUEST_APPROVAL,
        ChangeSetStatus.APPROVED: Trigger.APPROVE,
        ChangeSetStatus.REJECTED: Trigger.REJECT,
        ChangeSetStatus.CANCELLED: Trigger.CANCEL,
        ChangeSetStatus.SUPERSEDED: Trigger.REPLACE,
    },
    ChangeSetStatus.AWAITING_APPROVAL: {
        ChangeSetStatus.PROPOSED: Trigger.AMEND,
        ChangeSetStatus.APPROVED: Trigger.APPROVE,
        ChangeSetStatus.REJECTED: Trigger.REJECT,
        ChangeSetStatus.CANCELLED: Trigger.CANCEL,
        ChangeSetStatus.SUPERSEDED: Trigger.REPLACE,
    },
    ChangeSetStatus.APPROVED: {
        ChangeSetStatus.PROPOSED: Trigger.AMEND,
        ChangeSetStatus.APPLYING: Trigger.PREFLIGHT_PASS,
        ChangeSetStatus.CONFLICTED: Trigger.PREFLIGHT_CONFLICT,
        ChangeSetStatus.AWAITING_APPROVAL: Trigger.APPROVAL_REVOKED,
        ChangeSetStatus.CANCELLED: Trigger.CANCEL,
        ChangeSetStatus.SUPERSEDED: Trigger.REPLACE,
    },
    ChangeSetStatus.APPLYING: {
        ChangeSetStatus.APPLIED: Trigger.ALL_OPS_COMMITTED,
        ChangeSetStatus.PARTIALLY_APPLIED: Trigger.PARTIAL_FAILURE,
        ChangeSetStatus.FAILED: Trigger.NO_OP_COMMITTED,
        ChangeSetStatus.ROLLING_BACK: Trigger.AUTOMATIC_ROLLBACK,
    },
    ChangeSetStatus.PARTIALLY_APPLIED: {
        ChangeSetStatus.ROLLING_BACK: Trigger.USER_ROLLBACK,
        ChangeSetStatus.FAILED: Trigger.ACCEPT_PARTIAL,
    },
    ChangeSetStatus.APPLIED: {
        ChangeSetStatus.VERIFYING: Trigger.VERIFICATION_REQUESTED,
        ChangeSetStatus.ROLLING_BACK: Trigger.USER_ROLLBACK,
    },
    ChangeSetStatus.VERIFYING: {
        ChangeSetStatus.VERIFIED: Trigger.VERIFICATION_PASS,
        ChangeSetStatus.APPLIED: Trigger.VERIFICATION_INCONCLUSIVE,
        ChangeSetStatus.FAILED: Trigger.VERIFICATION_FAIL,
        ChangeSetStatus.ROLLING_BACK: Trigger.USER_ROLLBACK,
    },
    ChangeSetStatus.VERIFIED: {
        ChangeSetStatus.ROLLING_BACK: Trigger.USER_ROLLBACK,
    },
    ChangeSetStatus.FAILED: {
        ChangeSetStatus.ROLLING_BACK: Trigger.USER_ROLLBACK,
    },
    ChangeSetStatus.CONFLICTED: {
        ChangeSetStatus.PROPOSED: Trigger.AMEND,
        ChangeSetStatus.REJECTED: Trigger.REJECT,
        ChangeSetStatus.CANCELLED: Trigger.CANCEL,
        ChangeSetStatus.SUPERSEDED: Trigger.REPLACE,
    },
    ChangeSetStatus.ROLLING_BACK: {
        ChangeSetStatus.ROLLED_BACK: Trigger.RESTORE_VERIFIED,
        ChangeSetStatus.ROLLBACK_FAILED: Trigger.RESTORE_UNSAFE,
        ChangeSetStatus.FAILED: Trigger.ABORT_ROLLBACK,
    },
    ChangeSetStatus.ROLLBACK_FAILED: {
        ChangeSetStatus.ROLLING_BACK: Trigger.RETRY_ROLLBACK,
    },
    ChangeSetStatus.REJECTED: {},
    ChangeSetStatus.CANCELLED: {},
    ChangeSetStatus.SUPERSEDED: {},
    ChangeSetStatus.ROLLED_BACK: {},
}


class InvalidTransition(ValueError):
    """A move the lifecycle does not allow.

    Raised, never logged and accepted. An illegal transition means some caller
    believes it may do something the transaction forbids -- applying twice,
    approving a change it has not seen, treating a settled record as editable --
    and letting that pass would corrupt the very state recovery depends upon.
    """

    def __init__(
        self,
        current: ChangeSetStatus,
        target: ChangeSetStatus,
        *,
        trigger: Trigger | None = None,
        detail: str = "",
    ) -> None:
        self.current = current
        self.target = target
        self.trigger = trigger
        message = f"changeSet cannot go from {current.value} to {target.value}"
        if trigger is not None:
            message = f"{message} via {trigger.value}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


class TransitionGuardFailed(InvalidTransition):
    """A listed move whose precondition (§4.3) is not satisfied.

    Separated from :class:`InvalidTransition` because the caller's recovery differs:
    an unlisted pair is a programming error, an unmet guard is a missing approval or
    a missing piece of evidence that a human or a later step can supply.
    """


@dataclass(frozen=True, slots=True)
class TransitionFacts:
    """What the engine claims to be true at the moment of the move.

    Guards read these instead of reaching into the workspace or the approval store:
    the lifecycle stays testable and free of I/O, and a caller cannot satisfy a guard
    by asserting something it has not verified, because the fields only mean
    "verified" when the caller has done the verification.
    """

    approval_recorded: bool = False
    approval_digest_matches: bool = False
    file_evidence: bool = False
    ops_committed: int = 0

    @property
    def approval_verified(self) -> bool:
        """Both halves of §4.3's approval requirement, or neither.

        A persisted record whose digest no longer matches is not an approval, it is
        a stale signature over different content -- which is the forged-approval
        case, not a rounding error.
        """

        return self.approval_recorded and self.approval_digest_matches


#: Moves that may not happen without a verified approval behind them.
_APPROVAL_REQUIRED: frozenset[ChangeSetStatus] = frozenset({ChangeSetStatus.APPROVED})

#: Moves whose legality depends on file evidence existing (§4.3: ``FAILED`` is
#: terminal only when there is nothing to roll back).
_EVIDENCE_REQUIRED: frozenset[tuple[ChangeSetStatus, ChangeSetStatus]] = frozenset(
    {(ChangeSetStatus.FAILED, ChangeSetStatus.ROLLING_BACK)}
)

#: Leaving ``APPROVED`` for these states keeps the approval usable for a later
#: apply. Every other exit invalidates it (§4.3).
_APPROVAL_RETAINING: frozenset[ChangeSetStatus] = frozenset(
    {ChangeSetStatus.APPLYING, ChangeSetStatus.CONFLICTED}
)


@dataclass(frozen=True, slots=True)
class TransitionRecord:
    """One accepted move, kept for the journal, the audit trail and recovery.

    Recovery needs more than the destination: it needs to know whether the record
    reached this state by amend, by automatic rollback, or by a user accepting a
    partial apply, because those leave the workspace in different shapes with the
    same final status.
    """

    sequence: int
    from_status: str
    to_status: str
    trigger: str
    revision: int
    previous_revision: int
    at: str
    actor: str
    approval_invalidated: bool = False
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "trigger": self.trigger,
            "revision": self.revision,
            "previous_revision": self.previous_revision,
            "at": self.at,
            "actor": self.actor,
            "approval_invalidated": self.approval_invalidated,
            "detail": self.detail,
        }


def expected_trigger(current: ChangeSetStatus, target: ChangeSetStatus) -> Trigger | None:
    """The one trigger §4.2 names for this move, or ``None`` when it is not listed."""

    return TRANSITION_TABLE.get(current, {}).get(target)


def can_transition(
    current: ChangeSetStatus,
    target: ChangeSetStatus,
    *,
    facts: TransitionFacts | None = None,
) -> bool:
    """Whether ``current -> target`` is legal, guards included.

    Unlike the task lifecycle, staying put is *not* assumed legal: §4.3 allows only
    two self-transitions, both caused by ``amend``. A helper that waved a
    self-transition through would make ``APPLIED -> APPLIED`` look harmless while
    silently permitting a second apply of the same transaction.
    """

    if expected_trigger(current, target) is None:
        return False
    return _guard_ok(current, target, facts or TransitionFacts())


def _guard_ok(
    current: ChangeSetStatus,
    target: ChangeSetStatus,
    facts: TransitionFacts,
) -> bool:
    approved = target not in _APPROVAL_REQUIRED or facts.approval_verified
    evidenced = (current, target) not in _EVIDENCE_REQUIRED or facts.file_evidence
    return approved and evidenced


class ChangeSetLifecycle:
    """The only thing that may change a ChangeSet's status.

    Held by the ChangeSet record and driven by the service. Direct assignment is not
    offered -- there is no public setter -- because the guarantees in §4.3 (an
    amendment invalidates approval, a revision never goes backwards) are only real if
    every path that changes status passes through ``transition``.
    """

    def __init__(
        self,
        status: ChangeSetStatus = ChangeSetStatus.DRAFT,
        *,
        revision: int = 1,
        approved: bool = False,
        history: tuple[TransitionRecord, ...] = (),
    ) -> None:
        if revision < 1:
            raise ValueError(f"revision must be at least 1, got {revision}")
        self._status = ChangeSetStatus(status)
        self._revision = int(revision)
        self._approved = bool(approved)
        self._history: list[TransitionRecord] = list(history)

    @property
    def status(self) -> ChangeSetStatus:
        return self._status

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def approved(self) -> bool:
        """Whether a verified approval currently covers this revision.

        Cleared by an amendment and by leaving ``APPROVED`` for any state that does
        not lead straight to an apply, per §4.3.
        """

        return self._approved

    @property
    def history(self) -> tuple[TransitionRecord, ...]:
        return tuple(self._history)

    @property
    def is_unstable(self) -> bool:
        return self._status in UNSTABLE_STATES

    @property
    def may_have_file_evidence(self) -> bool:
        return self._status in FILE_EVIDENCE_STATES

    def transition(
        self,
        target: ChangeSetStatus,
        *,
        trigger: Trigger,
        facts: TransitionFacts | None = None,
        actor: str = "system",
        detail: str = "",
        at: str | None = None,
    ) -> TransitionRecord:
        """Move to ``target`` through the named ``trigger``, or raise and change nothing.

        Every guard is evaluated before anything is mutated, so a refused transition
        leaves status, revision and approval exactly as they were -- the caller may
        retry, report, or go elsewhere, but it never finds the record half-moved.
        """

        target = ChangeSetStatus(target)
        named = expected_trigger(self._status, target)
        if named is None:
            raise InvalidTransition(self._status, target, trigger=trigger)
        if trigger is not named:
            raise InvalidTransition(
                self._status,
                target,
                trigger=trigger,
                detail=f"this move is caused by {named.value}",
            )

        evidence = facts or TransitionFacts()
        if target in _APPROVAL_REQUIRED and not evidence.approval_verified:
            raise TransitionGuardFailed(
                self._status,
                target,
                trigger=trigger,
                detail="approval is not recorded against the current revision digest",
            )
        if (self._status, target) in _EVIDENCE_REQUIRED and not evidence.file_evidence:
            raise TransitionGuardFailed(
                self._status,
                target,
                trigger=trigger,
                detail="no file evidence exists to roll back",
            )

        amended = trigger in AMEND_TRIGGERS
        from_status = self._status
        # Counted before the move: the record has to say which revision left the
        # state as well as which one arrived, or an amendment reads as a revision
        # jump with no starting point.
        from_revision = self._revision
        invalidated = from_status is ChangeSetStatus.APPROVED and target not in (
            _APPROVAL_RETAINING
        )

        self._status = target
        if amended:
            self._revision += 1
        if amended or invalidated:
            self._approved = False
        if target is ChangeSetStatus.APPROVED:
            self._approved = True

        record = TransitionRecord(
            sequence=len(self._history) + 1,
            from_status=from_status.value,
            to_status=target.value,
            trigger=trigger.value,
            revision=self._revision,
            at=at or datetime.now(UTC).isoformat(timespec="seconds"),
            actor=actor,
            # An amendment clears the approval too, but it is the same event the
            # caller already reads from ``amended``; this flag is the §4.3 wording,
            # "approval invalidated for future applies", kept where an auditor looks.
            approval_invalidated=invalidated,
            detail=detail,
            previous_revision=from_revision,
        )
        self._history.append(record)
        return record
