"""Durable sessions: checkpoints, and whether interrupted work may continue.

A task that was interrupted has an outcome nobody knows. It may have finished, it
may have half-applied a change, or it may have changed nothing. The difference
between those is not knowable from the record alone, so this module refuses to
guess: it distinguishes three answers and only one of them continues the work.

    SAFE_TO_RESUME    the last thing that happened is known, and everything it
                      depended on still holds
    REQUIRES_REVIEW   the last thing that happened is *not* known, so a human has
                      to look before anything runs again
    NOT_RESUMABLE     continuing is impossible: the record, the plan or the world
                      has moved too far

The default is the last of those whenever the evidence is thin. A recovery feature
that assumes the best case is a feature that re-runs a command the user already
paid for, or applies a change twice.

Persistence reuses the existing task store rather than introducing a second one:
a session is a task, plus the checkpoints that say where it got to.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from quantsmind_agent.services.tasks.models import (
    StepStatus,
    Task,
    TaskStatus,
)

__all__ = [
    "CHECKPOINT_SCHEMA_VERSION",
    "Checkpoint",
    "RecoveryDecision",
    "RecoveryVerdict",
    "SessionState",
    "checkpoint_for",
    "latest_checkpoint",
    "session_state",
]

logger = logging.getLogger(__name__)

#: Bumped when the checkpoint shape changes. A record from a newer build is
#: refused rather than reinterpreted, because a half-understood checkpoint is how
#: work gets repeated that should not be.
CHECKPOINT_SCHEMA_VERSION = 1


class SessionState(StrEnum):
    """How a session stands relative to being runnable.

    Derived rather than stored separately from the task status, because two
    sources of truth for "is this running?" is how a completed task comes back to
    life.
    """

    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    RECOVERY_REVIEW = "recovery_review"


class RecoveryDecision(StrEnum):
    """Whether a session may continue, and under what conditions."""

    SAFE_TO_RESUME = "safe_to_resume"
    REQUIRES_REVIEW = "requires_review"
    NOT_RESUMABLE = "not_resumable"


def session_state(task: Task) -> SessionState:
    """The session state of a task, derived from its own record."""

    if task.status in (TaskStatus.COMPLETED, TaskStatus.VERIFYING):
        return (
            SessionState.COMPLETED if task.status is TaskStatus.COMPLETED else SessionState.ACTIVE
        )
    if task.status is TaskStatus.FAILED:
        return SessionState.FAILED
    if task.status is TaskStatus.CANCELLED:
        return SessionState.INTERRUPTED
    if task.status is TaskStatus.PAUSED:
        return SessionState.RECOVERY_REVIEW
    return SessionState.ACTIVE


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """A known boundary in a task, with enough evidence to continue from it.

    Not a crash-consistent snapshot of the world: it records what this
    application knows about itself, and says so. It never claims to describe
    what an external system did.
    """

    session_id: str
    plan_digest: str
    #: Steps whose outcome this application has recorded, by id.
    completed_steps: tuple[str, ...] = ()
    #: The step that was in flight when the session was interrupted, if any.
    in_flight_step: str = ""
    workspace: str = ""
    at: float = 0.0
    schema_version: int = CHECKPOINT_SCHEMA_VERSION
    #: Free-form validation facts, scalars only.
    notes: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        done = len(self.completed_steps)
        if self.in_flight_step:
            return f"checkpoint at step {done}, interrupted during {self.in_flight_step}"
        return f"checkpoint at step {done}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "plan_digest": self.plan_digest,
            "completed_steps": list(self.completed_steps),
            "in_flight_step": self.in_flight_step,
            "workspace": self.workspace,
            "at": self.at,
            "notes": dict(self.notes),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Checkpoint:
        """Read a checkpoint, refusing one written by a newer build."""

        version = int(payload.get("schema_version", 0) or 0)
        if version > CHECKPOINT_SCHEMA_VERSION:
            raise ValueError(f"checkpoint schema {version} is newer than this build")
        notes = payload.get("notes")
        return cls(
            session_id=str(payload.get("session_id", "")),
            plan_digest=str(payload.get("plan_digest", "")),
            completed_steps=tuple(str(item) for item in payload.get("completed_steps") or []),
            in_flight_step=str(payload.get("in_flight_step", "")),
            workspace=str(payload.get("workspace", "")),
            at=float(payload.get("at", 0.0) or 0.0),
            schema_version=version,
            notes=dict(notes) if isinstance(notes, dict) else {},
        )


def checkpoint_for(task: Task, *, at: float = 0.0) -> Checkpoint:
    """The checkpoint describing where ``task`` actually got to.

    Written from the task's own record rather than from a caller's expectation,
    because the difference between those two is exactly the difference between a
    step that completed and a step that was assumed to.
    """

    plan = task.plan
    completed = tuple(
        step_id
        for step_id, status in task.step_status.items()
        if status in (StepStatus.SUCCEEDED, StepStatus.SKIPPED)
    )
    in_flight = tuple(
        step_id for step_id, status in task.step_status.items() if status is StepStatus.RUNNING
    )
    notes: dict[str, Any] = {}
    snapshot = getattr(task, "workspace_snapshot", None)
    if isinstance(snapshot, dict) and snapshot.get("digest"):
        # Scalar validation facts, not a copy of the snapshot: a checkpoint is a
        # boundary, and the full inventory already lives on the task record.
        notes["workspace_digest"] = str(snapshot.get("digest"))[:32]
        notes["workspace_kind"] = str(snapshot.get("project_kind", ""))[:40]
        notes["workspace_files"] = int(snapshot.get("file_count", 0) or 0)
    return Checkpoint(
        session_id=task.id,
        plan_digest=plan.digest if plan is not None else "",
        completed_steps=completed,
        in_flight_step=in_flight[0] if in_flight else "",
        workspace=task.workspace,
        at=at or task.updated_at,
        notes=notes,
    )


def latest_checkpoint(task: Task) -> Checkpoint | None:
    """The most recent readable checkpoint, or ``None``.

    An unreadable entry is skipped rather than fatal: a corrupt checkpoint means
    "no known boundary", which is a safe thing to be, not a reason to refuse the
    whole session from loading.
    """

    for raw in reversed(list(getattr(task, "checkpoints", []) or [])):
        if not isinstance(raw, dict):
            continue
        try:
            return Checkpoint.from_dict(raw)
        except (ValueError, TypeError):
            continue
    return None


@dataclass(frozen=True, slots=True)
class RecoveryVerdict:
    """What may happen to a session, and the reasons behind it."""

    decision: RecoveryDecision
    reasons: tuple[str, ...] = ()
    checkpoint: Checkpoint | None = None

    @property
    def resumable(self) -> bool:
        """Whether anything may be executed again without a human looking first."""

        return self.decision is RecoveryDecision.SAFE_TO_RESUME

    def describe(self) -> str:
        head = {
            RecoveryDecision.SAFE_TO_RESUME: "can continue",
            RecoveryDecision.REQUIRES_REVIEW: "needs review before continuing",
            RecoveryDecision.NOT_RESUMABLE: "cannot continue",
        }[self.decision]
        if self.reasons:
            return f"{head}: {'; '.join(self.reasons[:3])}"
        return head


class RecoveryService:
    """Decides whether an interrupted session may continue.

    Every input is a fact about the world, not about the exception that
    interrupted it. The decision is made from the checkpoint, the plan, the
    workspace and the tools that are available now -- and anything missing, stale
    or unprovable resolves against continuing.
    """

    def __init__(
        self,
        *,
        known_tools: frozenset[str] | set[str] | None = None,
        current_workspace: str = "",
        history_limit: int = 50,
    ) -> None:
        self._tools = frozenset(known_tools or ())
        self._workspace = current_workspace
        self._history_limit = history_limit

    def decide(
        self,
        task: Task,
        *,
        checkpoint: Checkpoint | None = None,
    ) -> RecoveryVerdict:
        """Classify a session, collecting every reason rather than the first."""

        reasons: list[str] = []
        # The *stored* checkpoint is the authority on where execution got to. A
        # checkpoint rebuilt from the task right now would describe the present,
        # which is the thing being questioned.
        mark = (
            checkpoint
            if checkpoint is not None
            else (latest_checkpoint(task) or checkpoint_for(task))
        )

        if task.plan is None:
            return RecoveryVerdict(
                RecoveryDecision.NOT_RESUMABLE, ("the session has no plan to continue",), mark
            )

        # 1. The plan must still be the plan that was checkpointed. A changed plan
        #    is not a resumption; it is a new task that happens to share a name.
        if mark.plan_digest and mark.plan_digest != task.plan.digest:
            reasons.append("the plan changed since the checkpoint")

        # 2. The checkpoint must belong to this session.
        if mark.session_id and mark.session_id != task.id:
            reasons.append("the checkpoint belongs to a different session")

        # 3. Anything that was in flight has an unknown outcome. This is the rule
        #    that matters most: a step that may have already run is not a step
        #    that may be run again on the strength of a saved record.
        if mark.in_flight_step:
            reasons.append(
                f"step {mark.in_flight_step!r} was in flight and may already have changed something"
            )

        # 4. The workspace must still be the one this session was approved for.
        workspace = self._workspace or task.workspace
        if mark.workspace and mark.workspace != workspace:
            reasons.append("a different project is open")
        if task.workspace != workspace:
            reasons.append("this session belongs to another project")

        # 5. Every tool the remaining steps need must still exist here.
        needed = {
            step.tool
            for step in task.plan.steps
            if step.id not in mark.completed_steps and step.tool
        }
        if self._tools:
            missing = sorted(needed - self._tools)
            if missing:
                reasons.append(f"tools no longer available: {', '.join(missing)}")

        # 6. The approval must still be one this plan can run under. A missing
        #    or stale approval is not authority to execute on the strength of an
        #    old decision: it is a reason to ask the user again.
        if task.approval is None or not task.approval.approved:
            reasons.append("there is no standing approval to continue under")
        elif task.approval.plan_digest != task.plan.digest:
            reasons.append("the approval no longer matches the plan")

        # 7. Dependencies of the remaining steps must actually hold.
        unresolved = _unresolved_dependencies(task, mark)
        if unresolved:
            reasons.append(f"steps depend on unfinished work: {', '.join(sorted(unresolved))}")

        # 8. A finished session is not resumed at all. It can be inspected.
        if task.status is TaskStatus.COMPLETED:
            return RecoveryVerdict(
                RecoveryDecision.NOT_RESUMABLE, ("this task is already complete",), mark
            )
        if task.status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
            return RecoveryVerdict(
                RecoveryDecision.NOT_RESUMABLE,
                (f"this task is {task.status.value}",),
                mark,
            )

        # 9. The point the task stopped at has to be one that may be left.
        #    Verification and repair may have run commands whose outcome nobody
        #    recorded; a blocked or unapproved task is waiting for a decision, not
        #    for a resume.
        if task.status in (TaskStatus.VERIFYING, TaskStatus.REPAIRING):
            reasons.append("verification or repair was in progress and its outcome is unknown")
        elif task.status is TaskStatus.BLOCKED:
            reasons.append("this task is blocked and needs a decision")
        elif task.status is TaskStatus.AWAITING_APPROVAL:
            reasons.append("this task is waiting for approval")
        elif task.status is TaskStatus.DRAFT:
            reasons.append("this task never started")

        if not reasons:
            return RecoveryVerdict(RecoveryDecision.SAFE_TO_RESUME, (), mark)

        # Anything that involves an unknown side effect, a changed world or a
        # decision the user has not made needs a human; anything else -- a
        # missing tool, a stale dependency -- simply cannot be continued as
        # recorded.
        review_markers = (
            "may already have changed",
            "plan changed",
            "different project",
            "standing approval",
            "approval no longer matches",
            "verification or repair",
            "blocked",
            "waiting for approval",
        )
        if any(marker in reason for reason in reasons for marker in review_markers):
            return RecoveryVerdict(RecoveryDecision.REQUIRES_REVIEW, tuple(reasons), mark)
        return RecoveryVerdict(RecoveryDecision.NOT_RESUMABLE, tuple(reasons), mark)


def _unresolved_dependencies(task: Task, mark: Checkpoint) -> set[str]:
    """Remaining steps whose dependencies have not been recorded as finished."""

    plan = task.plan
    if plan is None:
        return set()
    done = set(mark.completed_steps)
    unfinished = {
        step.id
        for step in plan.steps
        if step.id not in done
        and task.status_of(step.id) not in (StepStatus.SUCCEEDED, StepStatus.SKIPPED)
    }
    return {
        dependency
        for step in plan.steps
        if step.id in unfinished
        for dependency in step.depends_on
        if dependency in unfinished or dependency not in done
    }
